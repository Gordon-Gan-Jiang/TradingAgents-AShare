"""运维 CLI（`scripts/probe_data_sources.py`）的测试。

## 为什么补这个

审计发现这个脚本**零覆盖**——而它恰恰是整轮里唯一真正跑挂过的代码：
漏了 `init_db()`，白跑一整轮 4 分钟真实网络请求，最后死在
`no such table: data_source_probe_results`。

所以这里既测参数/输出/退出码，也**直接钉住那两个坑**：
1. `--list` 必须完全离线（不发请求、不建库）——CI 里才能跑；
2. 真正探测前必须先 `init_db()`——否则换台机器就是"白跑一圈再报错"。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, inspect as sa_inspect

from scripts import probe_data_sources as cli

_REQUIRED_TABLES = {"data_source_probe_runs", "data_source_probe_results"}


# --------------------------------------------------- 表真的会被 create_all 建出来


def test_create_all_builds_the_probe_tables_on_a_fresh_database():
    """`init_db()` 里的 `create_all` 必须在**全新库**上建出这两张表。

    这就是那个坑的本质：表只在 `init_db()` 里创建，而超出应用 lifespan 的调用方
    （脚本、CI）如果忘了调它，就会探完一圈才发现表不存在。
    """
    from api.database import Base

    engine = create_engine("sqlite://")  # 全新、空
    Base.metadata.create_all(bind=engine)
    names = set(sa_inspect(engine).get_table_names())
    missing = _REQUIRED_TABLES - names
    assert not missing, f"create_all 没建出这些表：{missing}"


def test_tables_are_registered_on_the_metadata():
    """两张表必须注册在 `Base.metadata` 上，否则 create_all 根本不知道它们。"""
    from api.database import Base

    assert _REQUIRED_TABLES <= set(Base.metadata.tables)


# ---------------------------------------------------------------- --list 离线


@pytest.fixture
def no_side_effects(monkeypatch):
    """任何网络请求或建表调用都会让测试失败——用来证明 `--list` 是纯离线的。"""

    def _explode(*a, **kw):
        raise AssertionError("--list 不应该发请求/建库")

    monkeypatch.setattr(cli.probe, "run_probe", _explode)
    monkeypatch.setattr(cli, "init_db", _explode)
    return None


def test_list_mode_is_fully_offline(capsys, no_side_effects):
    """`--list` 退出码 0，且**不触发** run_probe / init_db。"""
    code = cli.main(["--list"])

    assert code == 0
    out = capsys.readouterr().out
    assert "未发请求" in out
    assert "cn_baostock" in out


def test_list_mode_includes_the_direct_call_index_target(capsys, no_side_effects):
    """直连能力（基准指数）也必须出现在清单里。

    指数日线不走路由器，曾经是"最关键的基准没人盯着"的那条路；
    如果它从探测清单里掉出去，页面和 CI 都不会发现。
    """
    cli.main(["--list", "--source", "cn_baostock"])
    out = capsys.readouterr().out
    assert "get_index_daily_bars_df" in out


def test_list_mode_filters_by_source(capsys, no_side_effects):
    cli.main(["--list", "--source", "cn_baostock"])
    out = capsys.readouterr().out
    assert "cn_baostock" in out
    assert "cn_akshare" not in out, "--source 过滤没生效"


def test_list_mode_filters_by_tool(capsys, no_side_effects):
    cli.main(["--list", "--tool", "get_daily_bars_df"])
    out = capsys.readouterr().out
    assert "get_daily_bars_df" in out
    assert "get_news" not in out


def test_list_mode_json_is_valid_and_machine_readable(capsys, no_side_effects):
    cli.main(["--list", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list) and payload
    assert set(payload[0]) == {"source", "tool"}
    assert any(item["tool"] == "get_index_daily_bars_df" for item in payload)


def test_no_matching_target_exits_2_with_stderr(capsys, no_side_effects):
    """没有匹配目标是**用法错误**，要能和"探测失败"区分开（退出码 2 vs 1）。"""
    code = cli.main(["--list", "--source", "cn_nonexistent"])

    assert code == 2
    assert "没有匹配的探测目标" in capsys.readouterr().err


# ------------------------------------------------- 真正探测：必须先建表


def test_probe_mode_calls_init_db_before_running(monkeypatch):
    """真正探测前必须调 `init_db()`——这正是当初白跑一圈的那个 bug。"""
    order: list[str] = []

    monkeypatch.setattr(cli, "init_db", lambda: order.append("init_db"))

    class _FakeSession:
        def close(self):
            order.append("close")

    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())

    class _Ctx:
        day = "2026-09-23"

    monkeypatch.setattr(cli.probe.ProbeContext, "build", staticmethod(lambda: _Ctx()))
    monkeypatch.setattr(
        cli.probe, "run_probe", lambda *a, **kw: order.append("run_probe") or _Summary()
    )

    cli.main(["--source", "cn_baostock"])

    assert order[:2] == ["init_db", "run_probe"], f"建表顺序不对：{order}"


def test_session_is_closed_even_when_probe_raises(monkeypatch):
    """探测炸了也要关会话（脚本里同样是连接池泄漏点）。"""
    closed = {"value": False}

    class _FakeSession:
        def close(self):
            closed["value"] = True

    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())

    def _boom(*a, **kw):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(cli.probe, "run_probe", _boom)

    with pytest.raises(RuntimeError):
        cli.main(["--source", "cn_baostock"])

    assert closed["value"], "脚本异常路径没关会话"


# ------------------------------------------------------------ 输出与退出码


class _Outcome:
    def __init__(self, source, tool, status, error_kind=None, error_message=None, row_unit=None):
        self.source = source
        self.tool = tool
        self.status = status
        self.latency_ms = 12
        self.row_count = 5
        self.row_unit = row_unit
        self.version = "00.9.40"
        self.error_kind = error_kind
        self.error_message = error_message


class _Summary:
    def __init__(self, failed=0):
        self.id = 7
        self.status = "failed" if failed else "ok"
        self.trading_day = True
        self.duration_ms = 100
        self.total_count = 2
        self.ok_count = 1
        self.degraded_count = 0
        self.timeout_count = 0
        self.failed_count = failed
        self.unsupported_count = 0
        self.outcomes = [
            _Outcome("cn_baostock", "get_daily_bars_df", "ok"),
            _Outcome(
                "cn_akshare",
                "get_daily_bars_df",
                "failed" if failed else "ok",
                "unavailable",
                "all backends failed",
            ),
        ]


@pytest.fixture
def patched_probe(monkeypatch):
    monkeypatch.setattr(cli, "init_db", lambda: None)

    class _FakeSession:
        def close(self):
            pass

    monkeypatch.setattr(cli, "SessionLocal", lambda: _FakeSession())
    return monkeypatch


def test_exit_code_0_when_no_failures(capsys, patched_probe):
    patched_probe.setattr(cli.probe, "run_probe", lambda *a, **kw: _Summary(failed=0))
    assert cli.main(["--source", "cn_baostock"]) == 0
    assert "可用 1" in capsys.readouterr().out


def test_exit_code_1_when_any_source_fails(capsys, patched_probe):
    """有失败就退 1——CI/告警直接依赖这个约定。"""
    patched_probe.setattr(cli.probe, "run_probe", lambda *a, **kw: _Summary(failed=1))
    assert cli.main(["--source", "cn_baostock"]) == 1


def test_json_output_carries_the_diagnostic_fields(capsys, patched_probe):
    """JSON 里必须带 error_kind / version —— 没有它们就无法区分"没实现"和"连不上"。"""
    patched_probe.setattr(cli.probe, "run_probe", lambda *a, **kw: _Summary(failed=1))
    cli.main(["--source", "cn_baostock", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["failed_count"] == 1
    row = next(r for r in payload["results"] if r["status"] == "failed")
    assert row["error_kind"] == "unavailable"
    assert row["version"] == "00.9.40"


def test_no_persist_is_forwarded_to_the_service(patched_probe):
    """`--no-persist` 必须真的传到服务层，否则 CI 冒烟会污染探测历史。"""
    seen: dict = {}

    def _run(db, **kw):
        seen.update(kw)
        return _Summary()

    patched_probe.setattr(cli.probe, "run_probe", _run)

    cli.main(["--source", "cn_baostock", "--no-persist", "--deadline", "7.5"])

    assert seen["persist"] is False
    assert seen["deadline_seconds"] == 7.5
    assert seen["trigger"] == cli.probe.TRIGGER_MANUAL

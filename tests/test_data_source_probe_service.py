"""数据源探测服务测试。

这里用**假 provider** 注入，绝不联网——真实探测由 `scripts/probe_data_sources.py`
与线上定时任务负责。测试要钉住的是判定逻辑本身，因为"探测报告的判定"就是
这套机制的全部价值所在：

* 成功但 0 行 → degraded（"永不为空的成功"是最难发现的故障形态）
* `SourceUnavailable`（连不上）→ failed，且 `error_kind=unavailable`
* `SourceUnsupported`（没这能力）→ unsupported，**不拉低整轮健康度**
* 挂死的调用 → failed / timeout，且不能拖住整轮（硬截止）
* 版本指纹必须落库（本次事故的核心变量）
"""

from __future__ import annotations

import time

import pandas as pd
import pytest

from api.database import DataSourceProbeResultDB, DataSourceProbeRunDB, Base
from api.services import data_source_probe_service as probe
from tradingagents.dataflows.providers import capability_matrix as cm
from tradingagents.dataflows.providers.errors import (
    SourceEmpty,
    SourceUnavailable,
    SourceUnsupported,
)


class _PureStubProvider:
    """方法体只有一句 `raise NotImplementedError` —— 典型的占位实现。"""

    def get_daily_bars_df(self, *args, **kwargs):
        raise NotImplementedError("not implemented yet")


def _make_provider_class(*, behave: dict):
    """动态造一个 provider 类：behave = {tool: callable 或 Exception 实例}。"""

    def _method(tool):
        def _call(self, *args, **kwargs):
            value = behave[tool]
            if isinstance(value, Exception):
                raise value
            return value

        _call.__name__ = tool
        return _call

    namespace = {tool: _method(tool) for tool in behave}
    return type("FakeProvider", (), namespace)


class _FakeRegistry:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, name):
        return self._mapping.get(name)

    def list_names(self):
        return list(self._mapping)


def _ctx():
    return probe.ProbeContext(
        symbol="600519.SH", board="银行", day="2026-09-23", start="2026-09-11", end="2026-09-23"
    )


def _probe(monkeypatch, tool, behave, *, deadline=5.0):
    provider = _make_provider_class(behave={tool: behave})()
    registry = _FakeRegistry({"fake_src": provider})
    return probe.probe_target("fake_src", tool, _ctx(), deadline_seconds=deadline, registry=registry)


# ------------------------------------------------------------------ 判定


def test_success_with_rows_is_ok(monkeypatch):
    frame = pd.DataFrame({"date": ["2026-09-23"], "close": [1.0]})
    outcome = _probe(monkeypatch, "get_daily_bars_df", frame)
    assert outcome.status == probe.STATUS_OK
    assert outcome.row_count == 1


def test_success_with_zero_rows_is_degraded(monkeypatch):
    """空数据不是"成功"——否则一个永远返回空的源会被报告成全绿。"""
    outcome = _probe(monkeypatch, "get_daily_bars_df", pd.DataFrame())
    assert outcome.status == probe.STATUS_DEGRADED
    assert outcome.error_kind == "empty"
    assert outcome.row_count == 0


def test_friendly_empty_text_is_recognised_as_empty(monkeypatch):
    """provider 用"友好文案"表达没数据时，不能被当成成功。"""
    outcome = _probe(monkeypatch, "get_stock_data", "No data found for symbol '600519.SH'")
    assert outcome.status == probe.STATUS_DEGRADED
    assert outcome.row_count == 0


def test_source_unavailable_is_failed_not_unsupported(monkeypatch):
    """事故回归：连不上必须是 failed/unavailable，不能与"没实现"混淆。"""
    outcome = _probe(monkeypatch, "get_daily_bars_df", SourceUnavailable("网络接收错误"))
    assert outcome.status == probe.STATUS_FAILED
    assert outcome.error_kind == "unavailable"
    assert outcome.error_type == "SourceUnavailable"


def test_source_unsupported_is_reported_without_failing_the_round(monkeypatch):
    outcome = _probe(monkeypatch, "get_daily_bars_df", SourceUnsupported("没有这个能力"))
    assert outcome.status == probe.STATUS_UNSUPPORTED
    assert outcome.error_kind == "unsupported"


def test_source_empty_exception_is_degraded(monkeypatch):
    outcome = _probe(monkeypatch, "get_daily_bars_df", SourceEmpty("今天没数据"))
    assert outcome.status == probe.STATUS_DEGRADED
    assert outcome.error_kind == "empty"


def test_unimplemented_method_is_unsupported_and_never_called(monkeypatch):
    """矩阵若声称支持、方法却是占位，探测必须如实报"不支持"（而不是伪造成功）。

    "没被调用"是通过**失败签名**反推的：如果探测真的调了这个方法，会抛出一个
    **裸的** `NotImplementedError`，落到兜底分支变成 `failed/NotImplementedError`。
    这里断言的是 `unsupported/SourceUnsupported`（探测自己给的合成标签），
    也就证明了调用没有发生。这个区别正是本次事故的核心：
    "没实现"与"连不上"必须在报告里长得不一样。
    """
    registry = _FakeRegistry({"fake_src": _PureStubProvider()})

    outcome = probe.probe_target(
        "fake_src", "get_daily_bars_df", _ctx(), registry=registry
    )

    assert outcome.status == probe.STATUS_UNSUPPORTED
    assert outcome.error_type == "SourceUnsupported"
    assert outcome.error_type != "NotImplementedError", "占位方法被真的调用了"


def test_generic_exception_is_failed(monkeypatch):
    outcome = _probe(monkeypatch, "get_daily_bars_df", KeyError("boom"))
    assert outcome.status == probe.STATUS_FAILED
    assert outcome.error_kind == "error"
    assert outcome.error_type == "KeyError"


def test_unknown_provider_is_failed():
    registry = _FakeRegistry({})
    outcome = probe.probe_target("nope", "get_daily_bars_df", _ctx(), registry=registry)
    assert outcome.status == probe.STATUS_FAILED
    assert outcome.error_type == "NotRegistered"


def test_hanging_call_hits_hard_deadline(monkeypatch):
    """挂死的源不能拖住整轮探测（Baostock 旧版实测挂死 75 秒）。"""

    def _hang(self, *a, **kw):
        time.sleep(30)

    provider = type("HangProvider", (), {"get_daily_bars_df": _hang})()
    registry = _FakeRegistry({"fake_src": provider})

    started = time.monotonic()
    outcome = probe.probe_target(
        "fake_src", "get_daily_bars_df", _ctx(), deadline_seconds=0.5, registry=registry
    )
    elapsed = time.monotonic() - started

    assert outcome.status == probe.STATUS_TIMEOUT, (
        "超时必须自成一类：『源很慢』和『源挂了』的处置方式不同，"
        "都记成 failed 会让告警失去意义"
    )
    assert outcome.error_kind == "timeout"
    assert elapsed < 5, f"硬截止没生效，耗时 {elapsed:.1f}s"


def test_slow_but_successful_call_is_degraded(monkeypatch):
    def _slow(self, *a, **kw):
        time.sleep(0.3)
        return pd.DataFrame({"date": ["2026-09-23"]})

    provider = type("SlowProvider", (), {"get_daily_bars_df": _slow})()
    registry = _FakeRegistry({"fake_src": provider})
    monkeypatch.setattr(probe, "SLOW_LATENCY_MS", 50)

    outcome = probe.probe_target(
        "fake_src", "get_daily_bars_df", _ctx(), deadline_seconds=5, registry=registry
    )
    assert outcome.status == probe.STATUS_DEGRADED
    assert outcome.error_kind == "slow"


# ------------------------------------------------------------- 版本指纹


def test_version_fingerprint_is_recorded_for_third_party_sources():
    """事故核心变量：必须能看出"当时用的哪个版本"。"""
    version = probe.source_version("cn_baostock")
    assert version, "baostock 版本读不到——正是这次事故无法复盘的原因"
    assert version.startswith("0")


def test_version_fingerprint_for_builtin_providers():
    assert probe.source_version("cn_sina_moneyflow") == "builtin"


def test_version_fingerprint_never_raises_for_missing_module():
    assert probe.source_version("not_a_source") is None


# ------------------------------------------------------------------ 整轮


def test_run_probe_persists_run_and_results(db_session):
    provider = _make_provider_class(
        behave={
            "get_daily_bars_df": pd.DataFrame({"date": ["2026-09-23"]}),
            "get_stock_data": SourceUnavailable("boom"),
        }
    )()
    registry = _FakeRegistry({"fake_src": provider})

    summary = probe.run_probe(
        db_session,
        trigger=probe.TRIGGER_MANUAL,
        ctx=_ctx(),
        targets=[("fake_src", "get_daily_bars_df"), ("fake_src", "get_stock_data")],
        registry=registry,
    )

    assert summary.total_count == 2
    assert summary.ok_count == 1
    assert summary.failed_count == 1
    assert summary.status == probe.STATUS_FAILED

    runs = db_session.query(DataSourceProbeRunDB).all()
    results = db_session.query(DataSourceProbeResultDB).all()
    assert len(runs) == 1 and len(results) == 2
    assert runs[0].status == probe.STATUS_FAILED
    assert runs[0].trigger == probe.TRIGGER_MANUAL

    failed = [r for r in results if r.status == probe.STATUS_FAILED][0]
    assert failed.error_kind == "unavailable"
    assert failed.run_id == summary.id


def test_overall_status_ignores_unsupported():
    """unsupported 是配置事实，不是故障——不能把整轮判成失败。"""
    counts = {
        probe.STATUS_OK: 3,
        probe.STATUS_DEGRADED: 0,
        probe.STATUS_FAILED: 0,
        probe.STATUS_UNSUPPORTED: 5,
    }
    assert probe._overall_status(counts) == probe.STATUS_OK


def test_overall_status_degrades_then_fails():
    assert probe._overall_status({probe.STATUS_OK: 1, probe.STATUS_DEGRADED: 1}) == (
        probe.STATUS_DEGRADED
    )
    assert (
        probe._overall_status({probe.STATUS_DEGRADED: 1, probe.STATUS_FAILED: 1})
        == probe.STATUS_FAILED
    )


def test_status_payload_is_empty_but_valid_before_first_probe(db_session):
    """页面必须能处理"从没探测过"。"""
    payload = probe.status_payload(db_session)
    assert payload["latest_run"] is None
    assert payload["results"] == []
    assert payload["runs"] == []


def test_status_payload_returns_latest_run_and_results(db_session):
    provider = _make_provider_class(
        behave={"get_daily_bars_df": pd.DataFrame({"date": ["2026-09-23"]})}
    )()
    registry = _FakeRegistry({"fake_src": provider})
    probe.run_probe(
        db_session,
        ctx=_ctx(),
        targets=[("fake_src", "get_daily_bars_df")],
        registry=registry,
    )

    payload = probe.status_payload(db_session)
    assert payload["latest_run"]["ok_count"] == 1
    assert len(payload["results"]) == 1
    row = payload["results"][0]
    assert row["tool"] == "get_daily_bars_df"
    assert row["status"] == probe.STATUS_OK
    assert row["checked_at"] is not None
    assert len(payload["runs"]) == 1


def test_matrix_payload_is_serializable():
    import json

    payload = probe.matrix_payload()
    assert payload["items"]
    json.dumps(payload)


def test_probe_targets_are_exactly_the_matrix_expansion():
    """**单一事实源**：探测清单必须**恰好**等于能力矩阵的展开，不多不少。

    回归点：早先直连能力是用另一个常量 `EXTRA_PROBE_TARGETS` 硬编码补进来的，
    等于"矩阵一份、探测清单又一份"——矩阵改了探测不会跟着改，迟早对不上。
    现在直连能力用 `routed=False` 登记进矩阵，`probe_targets()` 只遍历矩阵。
    """
    expected = {
        (src.source, cap.tool)
        for cap in cm.CAPABILITIES
        for src in cap.sources
        if cap.tool in probe.PROBE_CALLS
    }
    assert set(probe.probe_targets()) == expected
    # 且不该再存在第二份清单
    assert not hasattr(probe, "EXTRA_PROBE_TARGETS"), (
        "第二个事实源又回来了：直连能力应通过矩阵的 routed=False 登记"
    )


def test_probe_targets_all_have_calls_and_come_from_the_matrix():
    """矩阵里的每个 (源, 维度) 都必须有探测调用——否则就是"假探"。"""
    targets = probe.probe_targets()
    assert targets, "没有探测目标"
    for source, tool in targets:
        assert tool in probe.PROBE_CALLS, f"{tool} 缺少探测调用"


def test_probe_targets_include_direct_call_capabilities():
    """直连能力（不经路由器）也必须被探测。

    基准指数由 `mainline_scoring` 直接调用，**不走 route_to_vendor**；它恰恰是
    "以前只有 AKShare 一家、而东财整片不通"的那条路——漏探等于没人盯着它。
    """
    targets = probe.probe_targets()
    assert ("cn_baostock", "get_index_daily_bars_df") in targets
    assert "get_index_daily_bars_df" in probe.PROBE_CALLS
    # 它必须是通过矩阵的 routed=False 进来的，而不是靠某处硬编码
    cap = cm.BY_TOOL["get_index_daily_bars_df"]
    assert cap.routed is False


def test_index_probe_uses_the_benchmark_code(monkeypatch):
    """指数探测必须打沪深300（主线评分用的就是它）。"""
    seen: list[str] = []

    class _P:
        def get_index_daily_bars_df(self, code, start, end):
            seen.append(code)
            return pd.DataFrame({"date": ["2026-09-23"], "close": [4517.28]})

    registry = _FakeRegistry({"cn_baostock": _P()})
    outcome = probe.probe_target(
        "cn_baostock", "get_index_daily_bars_df", _ctx(), registry=registry
    )

    assert outcome.status == probe.STATUS_OK
    assert outcome.row_count == 1
    assert seen == [probe.PROBE_BENCHMARK]


def test_probe_context_uses_previous_completed_trading_day():
    """09:00 探测时今日还没有数据，用今天会造出"源不可用"的假故障。"""
    ctx = probe.ProbeContext.build(today="2026-09-23")
    assert ctx.day <= "2026-09-23"
    assert ctx.start < ctx.day and ctx.end == ctx.day


@pytest.fixture
def db_session():
    """独立内存库（不影响 conftest 的临时库）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


# ------------------------------------------- provider 吞掉异常 → 必须记成失败

def test_swallowed_failure_text_is_reported_as_failed_not_data(monkeypatch):
    """**回归**：provider 把异常写成一句失败文案时，不能算"有数据"。

    `cn_akshare` 有 20+ 处 `return f"...获取失败：{exc}"`——这类返回值不是异常，
    探测若按"非空文本 = 有数据"处理，就会把**失败**记成"降级/慢"甚至"可用"。
    实测 `get_board_cons` 报的"降级、18611ms、1 行"，那 1 行正是失败文案。
    """
    provider = type(
        "Swallower",
        (),
        {
            "get_board_hist": lambda self, *a, **kw: (
                "板块历史行情获取失败（无替代源）：TimeoutError: 30s"
            )
        },
    )()
    registry = _FakeRegistry({"cn_akshare": provider})

    outcome = probe.probe_target(
        "cn_akshare", "get_board_hist", _ctx(), deadline_seconds=5, registry=registry
    )

    assert outcome.status == probe.STATUS_FAILED, (
        "把失败文案当成了数据——这正是『永不为空的成功』"
    )
    assert outcome.error_kind == "text-error"
    assert "获取失败" in (outcome.error_message or "")
    assert outcome.row_count == 0, "失败文案的行数不该被当成载荷规模"


def test_real_payload_containing_failure_words_is_not_misclassified(monkeypatch):
    """反面：正常数据里出现"失败"二字**不能**被误判（避免守卫逼人写错东西）。"""
    payload = "代码,名称,涨跌幅\n" + "\n".join(
        f"{i:06d},测试股票{i},1.23" for i in range(50)
    )
    provider = type(
        "Normal", (), {"get_board_spot": lambda self, *a, **kw: payload}
    )()
    registry = _FakeRegistry({"cn_akshare": provider})

    outcome = probe.probe_target(
        "cn_akshare", "get_board_spot", _ctx(), deadline_seconds=5, registry=registry
    )

    assert outcome.status == probe.STATUS_OK
    assert outcome.row_count == 51


def test_long_text_is_not_treated_as_a_failure_message(monkeypatch):
    """"失败"出现在长文本里时不判失败（错误文案都是短句，这条是长度护栏）。"""
    long_text = "获取失败\n" + "\n".join(f"第{i}行数据,1.0" for i in range(200))
    provider = type(
        "LongText", (), {"get_board_spot": lambda self, *a, **kw: long_text}
    )()
    registry = _FakeRegistry({"cn_akshare": provider})

    outcome = probe.probe_target(
        "cn_akshare", "get_board_spot", _ctx(), deadline_seconds=5, registry=registry
    )

    assert outcome.status != probe.STATUS_FAILED, "长报表被误判成失败文案"
    assert outcome.row_count == 201


def test_failure_marker_detection_units():
    assert probe._looks_failed_text("板块成分股获取失败：ConnectionError: boom")
    assert probe._looks_failed_text("No available vendor for method 'x'")
    assert not probe._looks_failed_text("")
    assert not probe._looks_failed_text("数据暂不可用。")  # 那是 empty，不是 failure
    assert not probe._looks_failed_text("x" * 600 + "获取失败")

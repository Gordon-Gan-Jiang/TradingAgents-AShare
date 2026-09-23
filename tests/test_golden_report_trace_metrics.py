"""W4-T1：黄金集报告里的两项**真实 trace 度量**（零证据率 / 参数丢弃率）。

## 为什么这两项要进报告

`golden_report.py` 量的是**规划层**（意图/标的/工具），它完全看不到
"计划跑完之后到底产出了几张证据"。于是出现了一个盲区：规划准确率很高、
工具也调了，但**每一条 step 的 `evidence_count` 都是 0**——用户看到的仍然是
"本轮无可用证据"，而报告上一个字都不会说。`W4` 把这两个比率补上。

参数丢弃率的数据源**只有一个**：`loop.py::_dropped_arguments` 写进 step 的
`dropped_arguments` 字段（`registry.last_dropped_arguments` 的消费点，W1 新增）。
所以这条指标一旦不见，说明那个消费点被绕过了——它不是"另一个可以算的口径"。

## 为什么必须读真实库，且必须只读

这两个比率的全部价值在于"它是真的"。若允许写，就不该让一个度量脚本去改
被度量的库（`clean_test_polluted_reports.py` 已经吃过测试污染生产库的亏）。
因此测试里把库文件 `chmod 444` 后仍要求跑通——只读是**可验证**的性质，
不是注释里的一句承诺。

## 为什么缺失要报"无法度量"而不是 0%

这是本仓库最贵的失败形态：把"没测到"渲染成"没问题"。库不在、窗口内没有回合，
都必须显式说明，且比率给 `None`——`0.0` 会被读成"零丢弃、零空证据"。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REAL_TRACE_DB = ROOT / "tradingagents.db"
sys.path.insert(0, str(ROOT / "scripts"))

import golden_report  # type: ignore  # noqa: E402


# ── 夹具：一个最小的 trace 库（结构与 `ChatTurnTraceDB` 的读路径一致）────────


def _make_trace_db(path: Path, rows) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE chat_turn_traces (id TEXT PRIMARY KEY, created_at TEXT, steps TEXT)"
    )
    for rid, created_at, steps in rows:
        con.execute(
            "INSERT INTO chat_turn_traces VALUES (?, ?, ?)",
            (rid, created_at, json.dumps(steps, ensure_ascii=False)),
        )
    con.commit()
    con.close()
    return path


def _step(**kw) -> dict:
    """一个 step，默认**有证据**；要造零证据就显式传 `evidence_count=0`。"""
    base = {"tool": "get_realtime_quotes", "ok": True, "elapsed_ms": 12, "evidence_count": 3}
    base.update(kw)
    return base


# ── 两个比率的定义 ────────────────────────────────────────────────────────


def test_zero_evidence_rate_counts_turns_whose_steps_sum_to_zero(tmp_path):
    """零证据率 = 所有 step 的 `evidence_count` 之和为 0 的回合占比。

    口径必须是"**求和**"而不是"任意一步为空"：一个回合里 3 步有证据、1 步空，
    对用户来说仍然答得出来，不该计入分子。
    """
    db = _make_trace_db(
        tmp_path / "t.db",
        [
            ("a", "2026-09-22 01:00:00.000000", [_step(), _step(evidence_count=0)]),
            ("b", "2026-09-22 02:00:00.000000", [_step(evidence_count=0)]),
            ("c", "2026-09-22 03:00:00.000000", []),
            ("d", "2026-09-22 04:00:00.000000", [_step(evidence_count=5)]),
        ],
    )
    m = golden_report.trace_metrics(db_path=db)
    assert m["available"] is True
    assert m["turns"] == 4
    assert m["zero_evidence_turns"] == 2, "b（单步零证据）与 c（无 step）都该计"
    assert m["zero_evidence_rate"] == pytest.approx(0.5)


def test_zero_evidence_rate_exposes_how_much_of_it_is_no_step_at_all(tmp_path):
    """零证据率必须**拆开**汇报"压根没调工具"与"调了却没证据"。

    实测（2026-09-23，本仓 753 条 trace）零证据率 35.1%，其中 18 条是**无 step**
    的回合（追问 / 校验重试轨迹），246 条才是"调了工具却一张可引用证据都没拿到"。
    不拆开，读者会把 35.1% 整体理解成后者的规模——那是 32.7%，差 2.4pt。
    """
    db = _make_trace_db(
        tmp_path / "t.db",
        [
            ("a", "2026-09-22 01:00:00.000000", []),
            ("b", "2026-09-22 02:00:00.000000", [_step(evidence_count=0)]),
            ("c", "2026-09-22 03:00:00.000000", [_step()]),
        ],
    )
    m = golden_report.trace_metrics(db_path=db)
    assert m["no_steps_turns"] == 1
    assert m["steps_but_no_evidence_turns"] == 1
    assert m["zero_evidence_turns"] == 2, "两个子项之和必须等于分子"


def test_dropped_arguments_rate_is_read_only_from_that_step_field(tmp_path):
    """参数丢弃率的数据源**唯一**：step 里的 `dropped_arguments` 键。

    刻意**不**去读别的东西（日志、`last_dropped_arguments`、step 的 detail 文本）：
    那样得到的"丢弃率"与 W1 那个消费点脱钩，字段失效时指标照样好看。
    """
    db = _make_trace_db(
        tmp_path / "t.db",
        [
            ("a", "2026-09-22 01:00:00.000000", [_step(dropped_arguments=["board_name"])]),
            ("b", "2026-09-22 02:00:00.000000", [_step()]),
            ("c", "2026-09-22 03:00:00.000000", [_step(detail="unknown arguments: ['x']")]),
            ("d", "2026-09-22 04:00:00.000000", [_step(), _step(dropped_arguments=["symbol"])]),
        ],
    )
    m = golden_report.trace_metrics(db_path=db)
    assert m["dropped_arguments_turns"] == 2, "只有带该键的回合计入（c 的 detail 不算）"
    assert m["dropped_arguments_rate"] == pytest.approx(0.5)


def test_empty_dropped_arguments_key_still_counts_as_a_drop(tmp_path):
    """写的是空列表也算发生过丢弃——键存在就是"这次调用丢过参数"。

    `loop.py` 只在**非空**时写这个键（"键不存在" == "没有丢弃"），所以键存在
    本身携带了全部信息。若改成按非空值统计，就会与写入端的约定不一致。
    """
    db = _make_trace_db(
        tmp_path / "t.db",
        [("a", "2026-09-22 01:00:00.000000", [_step(dropped_arguments=[])])],
    )
    m = golden_report.trace_metrics(db_path=db)
    assert m["dropped_arguments_turns"] == 1


# ── 时间窗：改前 / 改后靠它对比 ────────────────────────────────────────────


def test_window_bounds_filter_turns(tmp_path):
    """`--since` 含、`--until` **不含**：两个相邻窗口必须覆盖全集且不重叠。

    `until` 排他 + `since` 含，才能让 `[不限, 09-23)` 与 `[09-23, 不限)` 拼成全集。
    边界上的那条（09-23 00:00:00 整点）必须落在**后**一个窗口里——差一个整点
    的错窗，人眼在报告里是看不出来的。
    """
    rows = [
        ("a", "2026-09-22 23:59:59.000000", [_step()]),
        ("b", "2026-09-23 00:00:00.000000", [_step(evidence_count=0)]),
        ("c", "2026-09-23 12:00:00.000000", [_step(evidence_count=0)]),
        ("d", "2026-09-24 00:00:00.000000", [_step()]),
    ]
    db = _make_trace_db(tmp_path / "t.db", rows)

    before = golden_report.trace_metrics(db_path=db, until="2026-09-23")
    after = golden_report.trace_metrics(db_path=db, since="2026-09-23")
    whole = golden_report.trace_metrics(db_path=db)

    assert before["turns"] == 1, "`--until 2026-09-23`（裸日期）排除当天整天"
    assert after["turns"] == 3, "边界上那条（00:00:00）算在后窗"
    assert before["turns"] + after["turns"] == whole["turns"], "两个窗口必须无重叠且完整"

    # 再切一个**有界**窗口：两个上界各管一边。
    inner = golden_report.trace_metrics(
        db_path=db, since="2026-09-23", until="2026-09-24"
    )
    assert inner["turns"] == 2
    assert inner["zero_evidence_rate"] == pytest.approx(1.0)


def test_window_accepts_an_hour_resolution_boundary(tmp_path):
    """`T` 分隔的时间必须与库里的空格分隔格式可比。

    库里 `created_at` 存的是 `YYYY-MM-DD HH:MM:SS.ffffff`（UTC）。若不归一化，
    `'2026-09-23T14:32'` 与 `'2026-09-23 14:02:52'` 逐字符比较时 `T`(0x54) >
    `' '`(0x20)，比较结果与人的直觉相反——一个静默错窗。
    """
    db = _make_trace_db(
        tmp_path / "t.db",
        [
            ("a", "2026-09-23 14:02:52.000000", [_step()]),
            ("b", "2026-09-23 15:00:00.000000", [_step()]),
        ],
    )
    m = golden_report.trace_metrics(db_path=db, since="2026-09-23T14:32")
    assert m["turns"] == 1, "14:02 的那条在 14:32 之前，必须被排除"
    assert m["since"] == "2026-09-23 14:32", "报告里要落盘归一化后的实际边界"


# ── "没测到" ≠ "没问题" ────────────────────────────────────────────────────


def test_missing_database_is_unavailable_not_zero(tmp_path):
    """库不在 → `available=False` + 原因，比率必须是 `None` 而不是 0.0。

    这是本仓库反复记录的失败形态（见 `tests/test_quality_gates.py` 的 M6）：
    把"没测到"渲染成"没问题"。一个 0.0% 的丢弃率会被读成"零丢弃"。
    """
    m = golden_report.trace_metrics(db_path=tmp_path / "not-there.db")
    assert m["available"] is False
    assert m["reason"], "必须说明为什么量不了"
    assert "not-there.db" in m["reason"], "原因里要有实际找过的路径"
    assert m["zero_evidence_rate"] is None
    assert m["dropped_arguments_rate"] is None


def test_window_with_no_turns_reports_no_rate(tmp_path):
    """窗口内一条回合都没有 → 同样不许给 0.0%，并说明是窗口的问题。"""
    db = _make_trace_db(tmp_path / "t.db", [("a", "2026-09-22 01:00:00.000000", [_step()])])
    m = golden_report.trace_metrics(db_path=db, since="2026-09-25")
    assert m["available"] is True
    assert m["turns"] == 0
    assert m["zero_evidence_rate"] is None
    assert m["dropped_arguments_rate"] is None
    assert "窗口" in m["reason"]


def test_dropped_arguments_field_liveness_is_reported(tmp_path):
    """必须报出"窗口里有没有一条**可能**携带该字段的回合"。

    实测（2026-09-23）：全库 753 条 trace 的 `dropped_arguments` 回合数是 0，
    但这不是"没有丢弃"——写该字段的提交 c112775 落在 2026-09-23T14:32:02Z，
    而库里最后一条 trace 是 14:02:52Z。**0% 结构性为零**。脚本必须把这件事
    说清楚，否则这个 0 会被当成"W1 之后没再丢过参数"的证据。
    """
    old = _make_trace_db(
        tmp_path / "old.db", [("a", "2026-09-23 14:02:52.000000", [_step()])]
    )
    new = _make_trace_db(
        tmp_path / "new.db", [("a", "2026-09-23 14:40:00.000000", [_step()])]
    )
    assert golden_report.trace_metrics(db_path=old)["field_live_in_window"] is False
    assert golden_report.trace_metrics(db_path=new)["field_live_in_window"] is True
    assert golden_report.trace_metrics(db_path=old)["field_live_since"] == (
        golden_report.DROPPED_ARGUMENTS_LANDED_AT
    )
    assert golden_report.DROPPED_ARGUMENTS_LANDED_AT == "2026-09-23 14:32:02"


def test_read_only_connection_needs_no_write_permission(tmp_path):
    """把库**设为只读**后仍要跑通，并且内容逐字节不变。

    只读是这条指标的可信度前提：一个会写被度量库的度量脚本，迟早会把
    生产 trace 改掉（`clean_test_polluted_reports.py` 的整个存在就是证据）。
    """
    db = _make_trace_db(tmp_path / "t.db", [("a", "2026-09-22 01:00:00.000000", [_step()])])
    before = db.read_bytes()
    db.chmod(0o444)
    try:
        m = golden_report.trace_metrics(db_path=db)
        assert m["available"] is True and m["turns"] == 1
    finally:
        db.chmod(0o644)
    assert db.read_bytes() == before, "度量动作改动了被度量的库"


# ── 报告必须真的写出来 ────────────────────────────────────────────────────


def test_rendered_report_carries_both_rates_and_the_window():
    """报告正文里必须有两个比率、口径说明与时间窗——否则数字等于没测。"""
    trace = {
        "available": True, "reason": "", "since": "2026-09-23", "until": None,
        "turns": 4, "zero_evidence_turns": 2, "zero_evidence_rate": 0.5,
        "no_steps_turns": 1, "steps_but_no_evidence_turns": 1,
        "dropped_arguments_turns": 1, "dropped_arguments_rate": 0.25,
        "turns_with_steps": 3,
        "first_turn_at": "2026-09-23 01:00:00", "last_turn_at": "2026-09-23 02:00:00",
        "field_live_in_window": True,
        "field_live_since": golden_report.DROPPED_ARGUMENTS_LANDED_AT,
    }
    payload = {
        "metrics": {
            "total": 0, "coverage": 0.0, "planning_accuracy": 0.0, "tool_recall": 0.0,
            "tool_precision": 0.0, "clarify_accuracy": 0.0, "failures": [],
            "unmapped_capabilities": [],
        },
        "by_intent": {}, "with_planner": False, "planner_model": "",
        "name_map_size": 0, "trace": trace,
    }
    text = golden_report.render(payload)
    assert "零证据率" in text
    assert "参数丢弃率" in text
    assert "50.0%" in text and "25.0%" in text
    assert "2026-09-23" in text, "时间窗必须写进报告，否则数字不可回溯"


def test_rendered_report_flags_a_structurally_zero_dropped_rate():
    """结构性为零必须在报告里**喊出来**，不能只留一个 0.0%。"""
    payload = {
        "metrics": {
            "total": 0, "coverage": 0.0, "planning_accuracy": 0.0, "tool_recall": 0.0,
            "tool_precision": 0.0, "clarify_accuracy": 0.0, "failures": [],
            "unmapped_capabilities": [],
        },
        "by_intent": {}, "with_planner": False, "planner_model": "",
        "name_map_size": 0,
        "trace": {
            "available": True, "reason": "", "since": None, "until": None,
            "turns": 753, "zero_evidence_turns": 264, "zero_evidence_rate": 0.3506,
            "no_steps_turns": 18, "steps_but_no_evidence_turns": 246,
            "dropped_arguments_turns": 0, "dropped_arguments_rate": 0.0,
            "turns_with_steps": 735,
            "first_turn_at": "2026-09-21 12:41:33", "last_turn_at": "2026-09-23 14:02:52",
            "field_live_in_window": False,
            "field_live_since": golden_report.DROPPED_ARGUMENTS_LANDED_AT,
        },
    }
    text = golden_report.render(payload)
    assert "结构性" in text, "0% 会被读成'没有丢弃'，必须标注它结构性为零"


def test_unavailable_trace_db_is_rendered_as_unmeasured():
    """库不在时报告里要写"无法度量"，不能写一个 0.0%。"""
    payload = {
        "metrics": {
            "total": 0, "coverage": 0.0, "planning_accuracy": 0.0, "tool_recall": 0.0,
            "tool_precision": 0.0, "clarify_accuracy": 0.0, "failures": [],
            "unmapped_capabilities": [],
        },
        "by_intent": {}, "with_planner": False, "planner_model": "",
        "name_map_size": 0,
        "trace": {
            "available": False, "reason": "找不到 trace 库 /x/tradingagents.db",
            "since": None, "until": None, "turns": 0,
            "zero_evidence_rate": None, "dropped_arguments_rate": None,
        },
    }
    text = golden_report.render(payload)
    assert "无法度量" in text
    assert "找不到 trace 库" in text


def test_payload_carries_trace_metrics_for_before_after_comparison(monkeypatch):
    """两个比率必须进 JSON payload——`--compare` 是"改前 vs 改后"的唯一机制。

    只打印在终端上的数字没法与上一版比对，等于每次都要人肉记。
    """
    payload = golden_report.run(verbose=False, with_planner=False)
    assert "trace" in payload
    trace = payload["trace"]
    assert trace["available"] is True, "本仓有 tradingagents.db，应当度得到"
    assert isinstance(trace["zero_evidence_rate"], float)
    assert isinstance(trace["dropped_arguments_rate"], float)


# ── 真库上的实测（库不在则跳过：CI 容器可能没有这份数据）────────────────────


@pytest.mark.skipif(not REAL_TRACE_DB.exists(), reason="本机没有 trade trace 库")
def test_real_trace_db_is_readable_and_measurable():
    """真库上必须能算出两个比率，且库**未被改动**。"""
    before = (REAL_TRACE_DB.stat().st_mtime_ns, REAL_TRACE_DB.stat().st_size)
    m = golden_report.trace_metrics()
    after = (REAL_TRACE_DB.stat().st_mtime_ns, REAL_TRACE_DB.stat().st_size)
    assert after == before, "只读度量不得触碰库文件"
    assert m["available"] is True
    assert m["turns"] > 0
    assert 0.0 <= m["zero_evidence_rate"] <= 1.0
    assert 0.0 <= m["dropped_arguments_rate"] <= 1.0

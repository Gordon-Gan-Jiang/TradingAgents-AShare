"""P2：闭环评估**不能静默失败**。

## 缺陷

`evaluate_t1_for_report` 把两种语义完全不同的情况压成同一个 `return []`：

1. **报告日之后暂无前瞻数据** —— 合法等待，下次重试即可，不该报警
2. **基准/板块数据不可用（陈旧或抓不到）** —— 故障，必须让人看见

后果（实测）：基准缓存冻结在 2026-08-31，于是每份 9 月报告的
`check_date <= trade_date` 恒真 → 走分支 1 的样子、实际是故障 2
→ `mainline_t1_outcomes` 0 行，而**没有任何地方**说得出为什么。

`refresh_t1_outcomes` 更把"空结果"这个洞放大了：

    outs = evaluate_t1_for_report(...)
    if outs:
        evaluated += 1
    # 空 → 既不记 evaluated，也不进 skipped：这份报告凭空消失

所以本文件同时守住两条：
* 故障必须**抛**（调用方已有 `except` → 记进 `skipped`，天然可见）
* 记账必须**完整**：`evaluated + skipped == 处理的报告数`
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.database import Base, MainlineReportDB
from api.services import mainline_service


def _mk_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _run(trade_date="2026-09-10", report_id="r1", user_id="u1"):
    return {
        "id": report_id,
        "user_id": user_id,
        "trade_date": trade_date,
        "mainlines": [
            {
                "name": "AI算力",
                "type": "concept",
                "representative_boards": [{"board": "CPO概念", "chg_1d": 3.2}],
            },
        ],
    }


def _bench(dates, base=3000.0):
    return pd.Series([base * (1 + 0.001 * i) for i in range(len(dates))], index=dates)


# ------------------------------------------------- 判据一：故障要抛，不要静默

def test_stale_benchmark_raises_instead_of_silent_empty():
    """**核心回归**：基准不覆盖报告日（陈旧）时，必须抛出而不是返回 []。

    修复前：返回 []（与"还没到时间"无法区分）→ 本测试失败。
    """
    db = _mk_db()
    stale = _bench(["2026-08-28", "2026-08-31"])  # 报告日是 9-10，基准停在 8-31

    with patch(
        "tradingagents.dataflows.mainline_backtest.fetch_benchmark_close", return_value=stale
    ):
        with pytest.raises(mainline_service.MainlineDataUnavailable):
            mainline_service.evaluate_t1_for_report(db, _run())


def test_benchmark_fetch_failure_is_declared():
    """基准抓取直接抛异常 → 以 MainlineDataUnavailable 声明，不要退化成空序列。"""
    db = _mk_db()

    with patch(
        "tradingagents.dataflows.mainline_backtest.fetch_benchmark_close",
        side_effect=ConnectionError("东财封禁"),
    ):
        with pytest.raises(mainline_service.MainlineDataUnavailable):
            mainline_service.evaluate_t1_for_report(db, _run())


def test_exception_message_names_the_actual_cause():
    """异常信息必须能直接看出原因（含基准末日），而不是一句"数据不足"。"""
    db = _mk_db()
    stale = _bench(["2026-08-28", "2026-08-31"])

    with patch(
        "tradingagents.dataflows.mainline_backtest.fetch_benchmark_close", return_value=stale
    ):
        with pytest.raises(mainline_service.MainlineDataUnavailable) as ei:
            mainline_service.evaluate_t1_for_report(db, _run())

    msg = str(ei.value)
    assert "2026-08-31" in msg, f"信息里没有基准末日，无法定位：{msg}"


# ------------------------- 判据二：合法等待必须仍然静默返回（不能修过头）

def test_no_forward_data_yet_still_returns_empty():
    """基准**新鲜**、只是还不够报告日的前瞻 → 仍返回 []（合法等待，不是故障）。

    这条是反向护栏：修复不能把"正常的还没到时间"也报成故障，
    否则每天都会刷出一堆假警报。
    """
    db = _mk_db()
    fresh = _bench(["2026-09-10", "2026-09-11", "2026-09-12"])

    with patch(
        "tradingagents.dataflows.mainline_backtest.fetch_benchmark_close", return_value=fresh
    ):
        out = mainline_service.evaluate_t1_for_report(db, _run(trade_date="2026-09-12"))

    assert out == []


def test_normal_evaluation_still_works():
    """正常路径不受影响：基准与板块都有数据时照常产出结果。

    注意 `as_of_date` 必须**晚于**报告日，否则上界就是报告日本身、
    永远算不出前瞻——那是函数的设计（`as_of_date or trade_date`）。
    """
    db = _mk_db()
    bench = _bench(["2026-09-10", "2026-09-11", "2026-09-12"])

    def fake_board_hist(ak, board_name, sector_type, trade_date, check_date):
        return pd.Series({"2026-09-10": 100.0, "2026-09-12": 108.0})

    with patch(
        "tradingagents.dataflows.mainline_backtest.fetch_benchmark_close", return_value=bench
    ), patch.object(mainline_service, "_board_hist_close", fake_board_hist):
        out = mainline_service.evaluate_t1_for_report(
            db, _run(trade_date="2026-09-10"), as_of_date="2026-09-12", ak_module=object()
        )

    assert out, "正常路径不该被判成不可用"
    assert out[0]["outcome"] in ("兑现", "证伪", "走平(跑输基准)", "弱兑现(抗跌)", "数据不足")


# --------------------------------------------- 判据三：refresh 记账必须完整

def _insert_report(db, trade_date="2026-09-10", rid="r1"):
    # `mainlines` 必须给：它是 JSON 列，为空时 evaluate_t1_for_report 会走
    # "脏数据 → return []" 的早期分支，那就测不到我们真正要测的东西了。
    db.add(
        MainlineReportDB(
            id=rid,
            user_id="u1",
            trade_date=trade_date,
            status="completed",
            mainlines=[
                {
                    "name": "AI算力",
                    "type": "concept",
                    "representative_boards": [{"board": "CPO概念", "chg_1d": 3.2}],
                },
            ],
            created_at=datetime(2026, 9, 10, 8, 0, 0),
        )
    )
    db.commit()


def test_refresh_accounts_for_every_report():
    """**核心回归**：每份报告要么计入 evaluated，要么进 skipped——不能凭空消失。

    修复前：基准陈旧时 outs 为空，该报告既不计 evaluated 也不进 skipped，
    `evaluated + skipped != runs`，而且没有任何线索。
    """
    db = _mk_db()
    _insert_report(db, trade_date="2026-09-10", rid="r1")
    stale = _bench(["2026-08-28", "2026-08-31"])

    with patch(
        "tradingagents.dataflows.mainline_backtest.fetch_benchmark_close", return_value=stale
    ):
        result = mainline_service.refresh_t1_outcomes(db, user_id="u1")

    assert result["evaluated_reports"] == 0
    assert len(result["skipped"]) == 1, "报告既没评估也没被跳过：记账出现空洞"
    reason = result["skipped"][0]["reason"]
    assert "MainlineDataUnavailable" in reason or "基准" in reason, f"原因说不清：{reason}"


def test_refresh_records_legit_waiting_as_skip():
    """合法等待也要进 skipped（有原因），而不是静默不计。"""
    db = _mk_db()
    _insert_report(db, trade_date="2026-09-12", rid="r1")
    fresh = _bench(["2026-09-10", "2026-09-11", "2026-09-12"])

    with patch(
        "tradingagents.dataflows.mainline_backtest.fetch_benchmark_close", return_value=fresh
    ):
        result = mainline_service.refresh_t1_outcomes(db, user_id="u1")

    assert result["evaluated_reports"] == 0
    assert len(result["skipped"]) == 1
    assert result["skipped"][0]["reason"], "skipped 必须带非空原因"


def test_refresh_is_quiet_when_there_are_no_reports():
    """没有报告时不该产生噪声 skip 记录。"""
    db = _mk_db()
    result = mainline_service.refresh_t1_outcomes(db, user_id="u1")
    assert result == {"evaluated_reports": 0, "skipped": []}

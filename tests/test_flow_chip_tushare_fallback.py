"""资金流与筹码的 Tushare 兜底（东财被封后唯一可用的补齐路径）。

实测：东财系接口 IP 级封禁 → `stock_individual_fund_flow`、`stock_cyq_em` 全挂 →
判定的 `capital_composition` 与 `chip_distribution` 长期缺失 → 判定停在"证据不足"。

本文件锁住：
1. 东财失败时**必须**尝试 Tushare（而不是直接判为取不到）。
2. Tushare 也没配/也失败时返回 None（既有的诚实路径：声明未覆盖）。
3. 两个源都不可用时**不得**返回空帧冒充"取到了"。
4. Tushare 的字段要按既有内部契约归一（否则维度算错，比缺失更糟）。
"""

from __future__ import annotations

import pandas as pd
import pytest


class _FakeAk:
    """东财路径一律失败的 akshare 替身。"""

    def stock_individual_fund_flow(self, **kwargs):
        raise ConnectionError("Remote end closed connection")

    def stock_cyq_em(self, **kwargs):
        raise ConnectionError("Remote end closed connection")


def _seal_em_direct(monkeypatch):
    """把**东财直连**那条中间通道也打桩成失败。

    为什么必须这样：这个文件测的是"东财失败 → 退 Tushare"。而东财直连是链路的
    第二环（akshare → 东财直连 → Tushare），**它并不总是失败**——实测本机东财
    并非全面封禁（指数快照与公告接口都通），于是链路在我打桩的 Tushare 之前
    就返回了真实数据，断言随之失败。

    这不是被测代码的问题，而是**测试不密闭**：只打桩了第一环与第三环，
    却依赖了第二环必然失败这种网络状态。测试必须不依赖网络才能说明问题。
    """
    from tradingagents.dataflows.providers import cn_eastmoney_direct as em

    monkeypatch.setattr(
        em, "fetch_em_individual_fund_flow_direct", lambda symbol: None
    )


def test_flow_falls_back_to_tushare(monkeypatch):
    _seal_em_direct(monkeypatch)
    from tradingagents.dataflows.providers import cn_research_data as mod
    from tradingagents.dataflows.providers import cn_tushare

    monkeypatch.setattr(mod, "_ak", lambda: _FakeAk())
    monkeypatch.setattr(
        cn_tushare,
        "fetch_moneyflow_row",
        lambda symbol, day: {
            "ts_code": "600519.SH",
            "trade_date": "20260919",
            # Tushare 的净流入额单位是**万元**：-12500 万 = -1.25 亿元
            "net_mf_amount": -12500.0,
            # 故意给一组"自己相减会得到不同答案"的分项，确保实现用的是官方净额
            "buy_lg_amount": 12000.0, "buy_elg_amount": 3000.0,
            "sell_lg_amount": 1000.0, "sell_elg_amount": 500.0,
        },
    )

    frame = mod.fetch_flow_frame("600519.SH", "2026-09-19")

    assert frame is not None, "东财挂了但有 Tushare，不该直接判为取不到"
    assert "main_net_inflow" in frame.columns, f"必须归一成内部契约列，实际 {list(frame.columns)}"
    value = float(frame["main_net_inflow"].iloc[0])
    assert value == pytest.approx(-12500.0 * 10_000), (
        f"必须用官方 net_mf_amount 并换算成元（万元→元），实际 {value}"
    )
    assert value != pytest.approx((12000 + 3000 - 1000 - 500) * 10_000), (
        "官方明确：净流入不能由大小单相减得到"
    )


def test_flow_returns_none_when_tushare_absent(monkeypatch):
    _seal_em_direct(monkeypatch)
    from tradingagents.dataflows.providers import cn_research_data as mod
    from tradingagents.dataflows.providers import cn_tushare

    monkeypatch.setattr(mod, "_ak", lambda: _FakeAk())

    def _unavailable(symbol, day):
        raise cn_tushare.TushareUnavailable("未配置 TUSHARE_TOKEN")

    monkeypatch.setattr(cn_tushare, "fetch_moneyflow_row", _unavailable)
    assert mod.fetch_flow_frame("600519.SH", "2026-09-19") is None, (
        "两源都不可用时必须返回 None，由上层声明未覆盖"
    )


def test_chip_falls_back_to_tushare(monkeypatch):
    from tradingagents.dataflows.providers import cn_research_data as mod
    from tradingagents.dataflows.providers import cn_tushare

    monkeypatch.setattr(mod, "_ak", lambda: _FakeAk())
    monkeypatch.setattr(
        cn_tushare,
        "fetch_chip_row",
        lambda symbol, day: {"ts_code": "600519.SH", "trade_date": "20260919",
                             "winner_rate": 58.3, "cost_5pct": 1200.0, "cost_95pct": 1300.0},
    )

    frame = mod.fetch_chip_frame("600519.SH", "2026-09-19")

    assert frame is not None, "筹码维度应能通过 Tushare 补齐"
    assert "profit_ratio" in frame.columns, f"必须归一成内部契约列，实际 {list(frame.columns)}"
    value = float(frame["profit_ratio"].iloc[0])
    assert 0 <= value <= 1, f"获利比例必须归一为 0-1 比率，实际 {value}"


def test_chip_returns_none_when_tushare_absent(monkeypatch):
    from tradingagents.dataflows.providers import cn_research_data as mod
    from tradingagents.dataflows.providers import cn_tushare

    monkeypatch.setattr(mod, "_ak", lambda: _FakeAk())

    def _unavailable(symbol, day):
        raise cn_tushare.TushareUnavailable("x")

    monkeypatch.setattr(cn_tushare, "fetch_chip_row", _unavailable)
    assert mod.fetch_chip_frame("600519.SH", "2026-09-19") is None


def test_empty_tushare_row_does_not_fake_a_frame(monkeypatch):
    _seal_em_direct(monkeypatch)
    """Tushare 返回"没有这条数据"时不得造一个空帧冒充成功。"""
    from tradingagents.dataflows.providers import cn_research_data as mod
    from tradingagents.dataflows.providers import cn_tushare

    monkeypatch.setattr(mod, "_ak", lambda: _FakeAk())
    monkeypatch.setattr(cn_tushare, "fetch_moneyflow_row", lambda symbol, day: None)
    assert mod.fetch_flow_frame("600519.SH", "2026-09-19") is None

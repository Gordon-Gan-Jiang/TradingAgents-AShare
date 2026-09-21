"""分时数据的东财 → 新浪兜底。

## 实测背景

本机（以及 akshare 用户普遍遇到的）**东财系接口被 IP 级封禁**：
`stock_zh_a_hist_min_em`、`stock_cyq_em`、`stock_individual_fund_flow`、
`stock_board_industry_name_em` 全部 `RemoteDisconnected`；而**新浪系全部可用**
（`stock_zh_a_minute` 实测 1970 行 / 1.0s，`stock_sector_spot` 49 行）。

后果：判定需要的 `intraday_absorption`（下跌中有没有人接）维度**长期缺失**，
而它恰恰是洗盘最直接的证据——判定只能退化为"证据不足"。数据是取得到的，
只是没接上第二个源。

## 本文件锁住什么

1. 东财失败时**必须**退到新浪，而不是直接判为"取不到"。
2. 新浪的列名（`day`）要归一成内部契约（`date`），且**只保留请求当日**——
   新浪返回的是多日区间，混进别的交易日会让"当日承接"算错。
3. 两个源都失败时返回 None（分时缺失是一等状态，不是错误）。
"""

from __future__ import annotations

import pandas as pd
import pytest


def _sina_minute_frame() -> pd.DataFrame:
    """复刻 `ak.stock_zh_a_minute` 的真实形状（`day` 为完整时间戳）。"""
    return pd.DataFrame(
        {
            "day": [
                "2026-09-18 14:55:00", "2026-09-18 15:00:00",
                "2026-09-19 09:35:00", "2026-09-19 14:55:00", "2026-09-19 15:00:00",
            ],
            "open": [10.0, 10.1, 9.9, 9.5, 9.6],
            "high": [10.2, 10.2, 10.0, 9.6, 9.7],
            "low": [9.9, 10.0, 9.8, 9.4, 9.5],
            "close": [10.1, 10.1, 9.9, 9.6, 9.6],
            "volume": [100, 120, 200, 300, 280],
            "amount": [1000.0, 1200.0, 2000.0, 3000.0, 2800.0],
        }
    )


class _FakeAk:
    def __init__(self, *, em_error: Exception | None, sina: pd.DataFrame | None = None):
        self._em_error = em_error
        self._sina = sina
        self.calls: list[str] = []

    def stock_zh_a_hist_min_em(self, **kwargs):
        self.calls.append("em")
        if self._em_error is not None:
            raise self._em_error
        return pd.DataFrame(
            {
                "时间": ["2026-09-19 14:55:00"],
                "开盘": [9.5], "最高": [9.6], "最低": [9.4], "收盘": [9.6], "成交量": [300],
            }
        )

    def stock_zh_a_minute(self, **kwargs):
        self.calls.append("sina")
        return self._sina


def test_em_failure_falls_back_to_sina(monkeypatch):
    """东财被封时必须退到新浪——这是当前环境下的常态路径。"""
    from tradingagents.dataflows.providers import cn_research_data as mod

    fake = _FakeAk(em_error=ConnectionError("Remote end closed connection"), sina=_sina_minute_frame())
    monkeypatch.setattr(mod, "_ak", lambda: fake)

    frame = mod.fetch_intraday_frame("600519.SH", "2026-09-19", "5")

    assert frame is not None, "新浪可取到分时，不该返回 None"
    assert fake.calls == ["em", "sina"], f"应尝试东财后回退新浪，实际：{fake.calls}"


def test_sina_frame_is_normalized_to_the_internal_contract(monkeypatch):
    from tradingagents.dataflows.providers import cn_research_data as mod

    fake = _FakeAk(em_error=ConnectionError("x"), sina=_sina_minute_frame())
    monkeypatch.setattr(mod, "_ak", lambda: fake)

    frame = mod.fetch_intraday_frame("600519.SH", "2026-09-19", "5")

    for column in ("date", "open", "high", "low", "close", "volume"):
        assert column in frame.columns, f"缺少契约列 {column!r}，实际 {list(frame.columns)}"
    assert "day" not in frame.columns, "新浪的 day 列必须归一成 date"


def test_sina_frame_is_limited_to_the_requested_day(monkeypatch):
    """新浪返回的是多日区间；混进别的交易日会把「当日承接」算错。"""
    from tradingagents.dataflows.providers import cn_research_data as mod

    fake = _FakeAk(em_error=ConnectionError("x"), sina=_sina_minute_frame())
    monkeypatch.setattr(mod, "_ak", lambda: fake)

    frame = mod.fetch_intraday_frame("600519.SH", "2026-09-19", "5")

    assert len(frame) == 3, f"应只剩 09-19 的 3 根，实际 {len(frame)}"
    assert all(str(value).startswith("2026-09-19") for value in frame["date"])


def test_sina_symbol_uses_the_market_prefix(monkeypatch):
    """新浪要 `sh600519` 这种前缀格式；传裸代码会静默取不到。"""
    from tradingagents.dataflows.providers import cn_research_data as mod

    seen: dict = {}

    class _Recorder(_FakeAk):
        def stock_zh_a_minute(self, **kwargs):
            seen.update(kwargs)
            return _sina_minute_frame()

    monkeypatch.setattr(
        mod, "_ak", lambda: _Recorder(em_error=ConnectionError("x"), sina=_sina_minute_frame())
    )
    mod.fetch_intraday_frame("600519.SH", "2026-09-19", "5")
    assert seen.get("symbol") == "sh600519"


def test_both_sources_failing_returns_none(monkeypatch):
    """分时缺失是一等状态：返回 None 而不是抛错（调用方据此声明未覆盖）。"""
    from tradingagents.dataflows.providers import cn_research_data as mod

    fake = _FakeAk(em_error=ConnectionError("x"), sina=None)
    monkeypatch.setattr(mod, "_ak", lambda: fake)

    assert mod.fetch_intraday_frame("600519.SH", "2026-09-19", "5") is None


def test_empty_sina_frame_returns_none(monkeypatch):
    from tradingagents.dataflows.providers import cn_research_data as mod

    empty = pd.DataFrame({"day": [], "open": [], "high": [], "low": [], "close": [], "volume": []})
    monkeypatch.setattr(mod, "_ak", lambda: _FakeAk(em_error=ConnectionError("x"), sina=empty))
    assert mod.fetch_intraday_frame("600519.SH", "2026-09-19", "5") is None


def test_em_success_does_not_call_sina(monkeypatch):
    """东财可用时不要多打一次新浪（省配额、省延迟）。"""
    from tradingagents.dataflows.providers import cn_research_data as mod

    fake = _FakeAk(em_error=None, sina=_sina_minute_frame())
    monkeypatch.setattr(mod, "_ak", lambda: fake)

    frame = mod.fetch_intraday_frame("600519.SH", "2026-09-19", "5")
    assert fake.calls == ["em"]
    assert frame is not None and len(frame) == 1

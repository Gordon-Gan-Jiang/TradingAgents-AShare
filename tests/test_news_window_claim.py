"""新闻窗口不得虚假断言（见 docs/report-judgment-accuracy-audit.md §3.2、§3.5）。

## 缺陷

`ak.stock_news_em` 只有 `pageSize: 10` 且**无分页循环**——该源结构上最多返回
**最近 10 条**。而调用方请求 **90 天**窗口（`data_collector.py` 的 `LONG_DAYS`）。
provider 的做法是"取最近 10 条 → 按窗口过滤 → 过滤后为空就说
`No news found for {ticker} between {start} and {end}`"。

这句话把**源的能力不足**说成了**世界的事实**：对任何历史日期，新闻分析师都会读到
「该公司在此期间没有新闻」，进而得出"无催化剂 / 无负面报道"。真相是：**不知道**。

判据：
* 源上限 < 请求窗口时，不得输出「窗口内无新闻」这类**关于世界**的断言；
* 这类"无法判断"文案必须**可被识别为降级**，否则它又会被当成"数据可用"；
* 覆盖受限这件事必须在**成功路径**上说出来，否则读者以为窗口被完整扫描了。

§3.5 是同一类错误的另一个实例：`get_global_news` 的标题写「90 天窗口」，
内容其实只有**1 天**的新闻联播。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

NEWS_COLUMNS = ["发布时间", "新闻标题", "文章来源", "新闻内容", "新闻链接"]


class _FakeAk:
    """只提供 `stock_news_em`，模拟"最多返回最近 10 条"的真实行为。"""

    def __init__(self, df):
        self._df = df

    def stock_news_em(self, symbol=None, **kwargs):
        return self._df


def _news_df(dates):
    return pd.DataFrame(
        {
            "发布时间": dates,
            "新闻标题": [f"标题{i}" for i in range(len(dates))],
            "文章来源": ["测试源"] * len(dates),
            "新闻内容": [f"正文{i}" for i in range(len(dates))],
            "新闻链接": [f"https://example.com/{i}" for i in range(len(dates))],
        },
        columns=NEWS_COLUMNS,
    )


def _provider_with(monkeypatch, df):
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

    provider = CnAkshareProvider()
    monkeypatch.setattr(provider, "_ak", lambda: _FakeAk(df))
    monkeypatch.setattr(provider, "_normalize_symbol", lambda s: "600519")
    return provider


def _looks_like_failure(text: str) -> bool:
    from tradingagents.tools.data import _looks_like_vendor_failure

    return _looks_like_vendor_failure(text)


# ══════════════════════════════════════════════════════════════════════════
# §3.2 窗口断言
# ══════════════════════════════════════════════════════════════════════════


def test_old_window_does_not_claim_there_was_no_news(monkeypatch):
    """**核心**：源只覆盖最近 10 条时，历史窗口不得断言"没有新闻"。

    场景：今天是 2026-03-20，源返回最近 10 条（全部在 2026-03-10 之后），
    而请求的窗口是 **2026-01-01 至 2026-01-31**（远早于源的覆盖范围）。
    过滤后必然为空——但这**只说明源看不到那么早**，不说明那期间没有新闻。
    """
    recent = [
        (datetime(2026, 3, 20) - timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")
        for i in range(10)
    ]
    provider = _provider_with(monkeypatch, _news_df(recent))

    out = provider.get_news("600519.SH", "2026-01-01", "2026-01-31")

    assert "No news found" not in out, (
        f"不得断言窗口内没有新闻（源只覆盖最近 10 条）：{out!r}"
    )
    assert "2026-01-01" in out or "无法" in out or "覆盖" in out, (
        f"必须说清窗口与源覆盖范围的关系：{out!r}"
    )
    assert _looks_like_failure(out) is True, (
        f"「无法判断」必须被识别为降级，否则又会被当成可用数据：{out!r}"
    )


def test_covered_window_may_state_no_news_but_scopes_the_claim(monkeypatch):
    """**反过度修正**：源返回的条目**跨越**了窗口时，"窗口内无新闻"是可信的。

    这里源返回的最早一条早于窗口起点，说明该源的覆盖够到窗口起点；
    此时允许说"无新闻"，但必须限定在"返回的这 10 条里"这一证据范围内。
    """
    dates = ["2026-01-01 09:00:00"] + [
        (datetime(2026, 3, 20) - timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")
        for i in range(9)
    ]
    provider = _provider_with(monkeypatch, _news_df(dates))

    out = provider.get_news("600519.SH", "2026-01-15", "2026-01-20")

    assert "2026-01-15" in out, f"应当说清是哪个窗口：{out!r}"
    assert ("10" in out) or ("条" in out), (
        f"断言必须限定在返回条数这一证据范围内，而不是当世界的事实：{out!r}"
    )


def test_success_path_discloses_the_source_item_limit(monkeypatch):
    """成功路径也必须说出"只看了最近 10 条"，否则读者以为窗口被完整扫描。"""
    dates = [
        (datetime(2026, 3, 20) - timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")
        for i in range(10)
    ]
    provider = _provider_with(monkeypatch, _news_df(dates))

    out = provider.get_news("600519.SH", "2026-03-01", "2026-03-20")

    assert "标题0" in out, "正常新闻仍要返回"
    assert "10" in out, f"必须披露源的条目上限，否则窗口覆盖率被高估：{out!r}"


def test_empty_source_result_is_not_a_window_claim(monkeypatch):
    """源**完全**返回空时，只能说"这个源没给到"，不能说窗口内没有。"""
    provider = _provider_with(monkeypatch, _news_df([]))

    out = provider.get_news("600519.SH", "2026-01-01", "2026-01-31")

    assert "between 2026-01-01 and 2026-01-31" not in out, (
        f"源返回空不得表述成该窗口内没有新闻：{out!r}"
    )
    assert _looks_like_failure(out) is True, f"须被识别为降级：{out!r}"


def test_window_claim_text_is_recognised_as_degraded():
    """把"无法判断"登记进唯一权威判断，避免它被当成有效内容。"""
    from tradingagents.dataflows.vendor_text import (
        looks_like_empty_text,
        looks_like_failure_text,
    )

    for text in (
        "cn_akshare 新闻窗口覆盖不足：源仅返回最近 10 条，无法判断 2026-01-01 至 2026-01-31 是否有新闻",
        "窗口覆盖不足：无法判断该窗口内是否有新闻",
    ):
        assert looks_like_empty_text(text) or looks_like_failure_text(text), text


# ══════════════════════════════════════════════════════════════════════════
# §3.5 全球新闻：标题不得覆盖超过内容
# ══════════════════════════════════════════════════════════════════════════


class _FakeAkCctv:
    def __init__(self, df):
        self._df = df

    def news_cctv(self, date=None, **kwargs):
        return self._df


def test_global_news_header_does_not_overclaim_the_window(monkeypatch):
    """请求 90 天、内容只有 1 天时，标题不得写成 90 天窗口。"""
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

    day = "20260320"
    df = pd.DataFrame(
        {
            "title": ["联播头条"],
            "content": ["内容主体"],
            "date": [day],
        }
    )
    provider = CnAkshareProvider()
    monkeypatch.setattr(provider, "_ak", lambda: _FakeAkCctv(df))

    out = provider.get_global_news("2026-03-20", look_back_days=90)

    assert "联播头条" in out, "当日新闻仍要返回"
    # 请求窗口起点是 90 天前（2025-12-20）；不得让读者以为这 90 天都被扫描了
    start = (datetime(2026, 3, 20) - timedelta(days=90)).strftime("%Y-%m-%d")
    header = out.splitlines()[0]
    assert not (start in header and "2025-12-20" in header and "仅" not in header), (
        f"标题不得把 1 天内容写成 90 天窗口：{header!r}"
    )
    assert ("1" in header) or ("仅" in header) or ("实际" in header), (
        f"必须说明实际内容只覆盖哪一天：{header!r}"
    )

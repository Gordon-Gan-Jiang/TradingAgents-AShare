"""日线跨层契约：provider 必须说**信号层的语言**。

## 这个 bug 的形态

线上实测（真实链路）每次个股判定都是：

    InsufficientHistory: daily bars missing required columns: ['date', 'close']

而同一时刻直接调用取数器却拿到 36 行数据。原因是**跨层契约不一致**：

| 层 | 期望的列名 | 依据 |
|---|---|---|
| 纯信号层 `_normalize_daily` | 小写 `date` / `close` | `tradingagents/signals/context.py` |
| 平台通用 OHLCV 约定 | `["date","open","high","low","close","volume"]` | `tradingagents/graph/data_collector.py:40` |
| **provider `get_daily_bars_df`** | **`Date` / `Open` / … 大写** | `_normalize_hist_df` 的列映射（为提示词表格服务） |

后果：**洗盘判定从来没有真正跑通过真实数据**——它只在测试里"通过"，因为测试桩
`make_daily()` 造的是小写列，正好符合信号层的期望，于是这个断口在 2000+ 用例
里一次都没被撞到。这正是"测试与生产形状不一致"最典型的藏身处。

## 本文件锁住的分界

* `get_daily_bars_df`（结构化日线）**必须**返回小写 OHLCV——它自己文档里就写着
  "确定性信号层需要 DataFrame"，那就得说信号层的语言。
* `_normalize_hist_df` 保持大写不变：它是**给提示词用的中文表格**那条路的中间形态，
  两条路的消费者不同，不能靠改一边来迁就。
"""

from __future__ import annotations

import pandas as pd
import pytest

from tradingagents.graph.data_collector import _OHLCV_COLS
from tradingagents.signals.context import _normalize_daily


def _titlecase_bars() -> pd.DataFrame:
    """复刻 `_normalize_hist_df` 的真实输出形状（大写列）。"""
    return pd.DataFrame(
        {
            # 6 根：信号层要求 >= 6 根才有统计意义（少于此不值得判定）
            "Date": ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19"],
            "Open": [10.1, 10.0, 9.8, 9.6, 9.4, 9.3],
            "High": [10.3, 10.2, 9.9, 9.7, 9.5, 9.6],
            "Low": [9.9, 9.9, 9.7, 9.5, 9.3, 9.2],
            "Close": [10.1, 10.0, 9.8, 9.6, 9.4, 9.6],
            "Volume": [1000, 1000, 700, 600, 500, 700],
        }
    )


def test_titlecase_frame_is_what_breaks_the_signal_layer():
    """把这个断口本身写成测试：大写列进信号层必炸。"""
    from tradingagents.signals.context import InsufficientHistory

    with pytest.raises(InsufficientHistory) as excinfo:
        _normalize_daily(_titlecase_bars(), "2026-09-21", 6)
    assert "missing required columns" in str(excinfo.value)


def test_get_daily_bars_df_speaks_the_signal_contract(monkeypatch):
    """结构化日线必须小写——这是本次修复的核心断言（此前必失败）。"""
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

    provider = CnAkshareProvider.__new__(CnAkshareProvider)
    monkeypatch.setattr(provider, "_fetch_hist_df", lambda *a, **k: _titlecase_bars())

    frame = provider.get_daily_bars_df("600519.SH", "2026-09-01", "2026-09-21")

    for column in _OHLCV_COLS:
        assert column in frame.columns, f"结构化日线缺少契约列 {column!r}，实际列：{list(frame.columns)}"
    assert "Date" not in frame.columns, "结构化日线不该把提示词表格的列名带出来"


def test_provider_output_flows_through_the_signal_layer(monkeypatch):
    """端到端地跨过那个断口：provider 输出 → 信号层归一化不再抛错。"""
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

    provider = CnAkshareProvider.__new__(CnAkshareProvider)
    monkeypatch.setattr(provider, "_fetch_hist_df", lambda *a, **k: _titlecase_bars())

    frame = provider.get_daily_bars_df("600519.SH", "2026-09-01", "2026-09-21")
    normalized = _normalize_daily(frame, "2026-09-21", 6)

    assert len(normalized) == 6
    assert normalized["date"].iloc[0] == "2026-09-14"
    assert normalized["date"].iloc[-1] == "2026-09-19"
    assert normalized["close"].iloc[-1] == pytest.approx(9.6)


def test_conversion_keeps_the_llm_table_path_untouched(monkeypatch):
    """中文表格那条路仍要拿到大写/中文列：两条路的消费者不同。"""
    from tradingagents.dataflows.providers.cn_akshare_provider import CnAkshareProvider

    provider = CnAkshareProvider.__new__(CnAkshareProvider)
    monkeypatch.setattr(provider, "_fetch_hist_df", lambda *a, **k: _titlecase_bars())
    raw = provider._fetch_hist_df("600519.SH", "", "")

    assert "Date" in raw.columns, "_fetch_hist_df 是提示词表格路径，列名不应被改掉"


def test_conversion_tolerates_missing_optional_columns():
    """只有 date/close 的最小帧也要能转换（部分源不返回成交量）。"""
    from tradingagents.dataflows.providers.cn_akshare_provider import to_signal_bars

    minimal = pd.DataFrame(
        {
            "Date": [f"2026-09-{day:02d}" for day in range(14, 20)],
            "Close": [9.9, 9.8, 9.7, 9.6, 9.5, 9.6],
        }
    )
    converted = to_signal_bars(minimal)
    assert set(converted.columns) == {"date", "close"}
    assert _normalize_daily(converted, "2026-09-21", 6)["close"].iloc[-1] == pytest.approx(9.6)

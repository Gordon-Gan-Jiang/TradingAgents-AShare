"""结构化 K 线的**共享列契约**。

## 为什么单独抽一个模块

平台有两条消费路径，对列名的要求不同：

* **提示词表格**：大写 `Date`/`Close`（`_fetch_hist_df` 的输出），直接被格式化成
  中文表格喂给分析师。
* **信号层数值计算**：小写 `date`/`open`/`high`/`low`/`close`/`volume`
  （`signals/context.py::_normalize_daily` 按小写列名取值）。

这两条路曾经混过一次，代价是**洗盘判定从未在真实数据上跑通过**：`get_daily_bars_df`
把大写帧直接交给信号层，于是每次真实取数都变成
`InsufficientHistory: daily bars missing required columns`，而测试因为桩数据恰好是
小写列而全绿。这段历史记在原 `cn_akshare_provider.to_signal_bars` 的 docstring 里。

现在有多个 provider（AKShare、BaoStock）都要产出这种帧。把契约留在一处，是为了避免
"每个 provider 各写一份转换"——那正是下一次漂移的起点。`cn_akshare_provider` 仍然
re-export 这个名字，既有导入路径（含测试）不受影响。
"""

from __future__ import annotations

import pandas as pd

#: 信号层契约列（顺序即输出顺序）
SIGNAL_BAR_COLUMNS: tuple[str, ...] = ("date", "open", "high", "low", "close", "volume")

#: 兼容旧名（历史上叫 `_SIGNAL_BAR_COLUMNS`）
_SIGNAL_BAR_COLUMNS = SIGNAL_BAR_COLUMNS


def empty_signal_bars() -> pd.DataFrame:
    """空帧也必须带契约列——返回零列 DataFrame 会让下游报"缺列"而不是"空数据"。"""
    return pd.DataFrame(columns=list(SIGNAL_BAR_COLUMNS))


def to_signal_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """把内部大写帧转成**信号层契约**的小写 OHLCV。

    只保留契约列，其余（`outstanding_share` / `turnover` 之类）不往下传：
    信号层按列名取值，多带列不会更安全，只会让上游改动无声穿透。
    `date` 统一成 `YYYY-MM-DD` 字符串，OHLCV 统一成数值（不可解析 → NaN）。
    """
    if frame is None or len(frame) == 0:
        return empty_signal_bars()

    out = frame.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]
    keep = [c for c in SIGNAL_BAR_COLUMNS if c in out.columns]
    out = out[keep]

    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    for column in ("open", "high", "low", "close", "volume"):
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    return out.reset_index(drop=True)

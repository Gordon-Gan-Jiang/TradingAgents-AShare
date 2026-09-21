"""
A 股宏观摘要（短文本）：供 DataCollector 注入 pool，供 Macro Analyst 引用。

失败时返回空字符串，不阻塞主拉数流程。
"""
from __future__ import annotations

import os


def fetch_ashare_macro_pool_brief(trade_date: str, config: dict | None = None) -> str:
    """
    拉取若干国内宏观序列的最新一行（若接口可用），压缩为短文本。
    trade_date 目前作审计占位；具体指标以数据源最新披露为准。
    """
    cfg = config or {}
    if cfg.get("methodology_data_enrichment") is False:
        return ""
    if os.getenv("TA_METHODOLOGY_DATA_ENRICH", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return ""
    try:
        import akshare as ak  # type: ignore
    except ImportError:
        return ""

    chunks: list[str] = []
    _ = trade_date  # 预留：未来可按交易日截断

    fetchers: list[tuple[str, object]] = [
        ("PMI", getattr(ak, "macro_china_pmi", None)),
        ("LPR", getattr(ak, "macro_china_lpr", None)),
        ("M2同比", getattr(ak, "macro_china_m2_yearly", None)),
    ]

    for label, fn in fetchers:
        if fn is None or not callable(fn):
            continue
        try:
            df = fn()
            if df is None or getattr(df, "empty", True):
                continue
            last = df.iloc[-1]
            brief = last.to_dict() if hasattr(last, "to_dict") else str(last)
            s = str(brief)
            if len(s) > 800:
                s = s[:800] + "…"
            chunks.append(f"- **{label}**（最新披露样本）: {s}")
        except Exception:
            continue

    if not chunks:
        return ""

    header = (
        "## 国内宏观数据摘要（自动抓取，供交叉验证；以监管与交易所正式披露为准）\n"
    )
    return header + "\n".join(chunks[:6])

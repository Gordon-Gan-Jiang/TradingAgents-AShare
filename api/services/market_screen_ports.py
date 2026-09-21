"""全市场扫描端口：把既有扫描器接给工具层。

工具层（`tradingagents/tools/screen.py`）只认 `MarketScreener` 协议，不认识
`market_scanner_service` —— 分层方向是 `api → agent → tools`，由 AST 边界测试
强制。本模块是那个**唯一的接缝**。

刻意不在这里做任何过滤/排序/改写：扫描器的六因子与档案是既有资产，本轮只是
"接线"，不改判定口径。缺失的字段由工具层如实标为未覆盖，而不是在这里补 0。
"""

from __future__ import annotations

from typing import Any, Mapping


class MarketScannerPort:
    """`MarketScreener` 协议的真实实现。"""

    def __init__(self, *, scan_limit: int | None = None) -> None:
        # 默认不截断：实测全市场 5020 只热启动 10.0s、冷启动 61.5s，
        # 静默把范围砍到几百只会让「全市场」名不副实。
        self._scan_limit = scan_limit

    def screen(
        self,
        *,
        top_k: int,
        profile: str | None = None,
        min_change_pct: float = 0.0,
        scan_limit: int | None = None,
    ) -> Mapping[str, Any]:
        from api.services.market_scanner_service import scan_market_candidates

        return scan_market_candidates(
            top_k=top_k,
            min_change_pct=min_change_pct,
            profile=profile,
            scan_limit=scan_limit if scan_limit is not None else self._scan_limit,
        )


def build_market_screener(*, scan_limit: int | None = None) -> MarketScannerPort:
    return MarketScannerPort(scan_limit=scan_limit)

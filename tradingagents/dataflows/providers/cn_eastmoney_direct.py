"""东财直连数据源（push2delay 域名）：彻底解决 push2 域名被网络重置的问题。

背景（实测）：
- akshare 的 spot_em / hist_em 硬编码 push2.eastmoney.com —— 本网络对其 python requests
  连接被重置（RemoteDisconnected），导致概念板块涨幅榜、全市场宽度长期"不可用"；
- push2delay.eastmoney.com（东财延迟行情域名）对 python requests **可直连**（0.2s），
  clist 接口可返回东财概念/行业板块涨幅榜与全市场快照，与本模块配合作为可靠数据源。

接口：GET https://push2delay.eastmoney.com/api/qt/clist/get
  fs:  m:90+t:3+f:!50 概念板块 | m:90+t:2+f:!50 行业板块
       m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23 全市场 A 股（沪深京）
  fields: f2 最新价 f3 涨跌幅 f8 换手率 f12 代码 f14 名称 f20 总市值
          f104 上涨家数 f105 下跌家数 f128 领涨股 f136 领涨股-涨跌幅
  pz 上限 100 → 自动分页。
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from ._http_utils import HttpRetry, http_get_json

_HOST = "https://push2delay.eastmoney.com"
_PATH = "/api/qt/clist/get"

_FS = {
    "concept": "m:90+t:3+f:!50",
    "industry": "m:90+t:2+f:!50",
    "market": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
}
_FIELDS = "f2,f3,f8,f12,f14,f20,f104,f105,f128,f136"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}


def _clist_paged(
    fs: str,
    *,
    max_pages: int = 60,
    retry: HttpRetry = HttpRetry(attempts=3, base_sleep_s=0.5, timeout_s=10.0),
) -> list[dict[str, Any]]:
    """分页拉取 clist 全部行（pz 上限 100）。"""
    out: list[dict[str, Any]] = []
    for pn in range(1, max_pages + 1):
        params = {
            "pn": pn,
            "pz": 100,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f3",
            "fs": fs,
            "fields": _FIELDS,
        }
        data = http_get_json(_HOST + _PATH, params=params, headers=_HEADERS, retry=retry)
        diff = ((data or {}).get("data") or {}).get("diff") or []
        if not diff:
            break
        out.extend(diff)
        if len(diff) < 100:
            break
    return out


def _row_to_series(d: dict[str, Any]) -> dict[str, Any]:
    """clist 行 → 与 _normalize_board_spot_df 一致的列。"""
    return {
        "name": str(d.get("f14") or ""),
        "code": str(d.get("f12") or ""),
        "latest": d.get("f2"),
        "chg_1d": d.get("f3"),
        "total_mv": d.get("f20"),
        "turnover": d.get("f8"),
        "up_count": d.get("f104"),
        "down_count": d.get("f105"),
        "leader": str(d.get("f128") or ""),
        "leader_chg": d.get("f136"),
    }


def fetch_em_board_spot_direct(sector_type: str = "concept") -> pd.DataFrame:
    """东财板块涨幅榜直连（push2delay）：industry / concept，归一化列。"""
    if sector_type not in _FS or sector_type == "market":
        raise ValueError(f"fetch_em_board_spot_direct: unsupported sector_type={sector_type!r}")
    rows = _clist_paged(_FS[sector_type])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([_row_to_series(r) for r in rows])
    for c in ("latest", "chg_1d", "total_mv", "turnover", "up_count", "down_count", "leader_chg"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["net_inflow"] = None
    return df.reset_index(drop=True)


def fetch_em_market_breadth_direct() -> dict:
    """东财全市场宽度直连（push2delay，分页约 56 页/6s，TTL 缓存后无感）。"""
    rows = _clist_paged(_FS["market"])
    chgs = [r.get("f3") for r in rows if isinstance(r.get("f3"), (int, float))]
    total = len(rows)
    up = sum(1 for c in chgs if c > 0)
    down = sum(1 for c in chgs if c < 0)
    flat = sum(1 for c in chgs if c == 0)
    return {"up": up, "down": down, "flat": flat, "total": total, "as_of": None}


def fetch_em_zt_pool_direct() -> pd.DataFrame:
    """东财涨停池直连（push2delay clist fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23 需按涨幅过滤）。

    用 clist 全市场 + 涨停判定（f3 >= 9.8 且非 ST 无脑近似不可靠），
    因此涨停池仍优先 akshare stock_zt_pool_em（实测稳定），本函数暂不启用。
    """
    raise NotImplementedError("涨停池请使用 akshare stock_zt_pool_em（稳定可用）")

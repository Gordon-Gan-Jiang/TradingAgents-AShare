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

import logging
from typing import Any, Optional

import pandas as pd

from ._http_utils import HttpRetry, http_get_json
from .errors import SourceEmpty, SourceUnavailable, SourceUnsupported

logger = logging.getLogger(__name__)

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


# ── 个股资金流（东财直连 + 代理） ───────────────────────────────────────────
#
# 端点照 akshare 的实现（`stock_individual_fund_flow`），但这里**自己发请求**，
# 目的只有一个：让出口代理生效。akshare 内部用 `requests.get` 且不传 proxies，
# 于是"配了代理也没用"——东财封 IP 时只能干等超时。
_FUND_FLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
_FUND_FLOW_UT = "b2884a393a59ad64002292a3e90d46a5"

# klines 每行字段顺序（与 akshare 的列名一一对应）
# 0 日期 1 主力净额 2 小单 3 中单 4 大单 5 超大单 6..10 各类净占比 11 收盘 12 涨跌幅
_FUND_FLOW_FIELDS = (
    "date", "main_net_inflow", "small_net_inflow", "medium_net_inflow",
    "large_net_inflow", "xlarge_net_inflow",
    "main_net_ratio", "small_net_ratio", "medium_net_ratio",
    "large_net_ratio", "xlarge_net_ratio", "close", "change_pct",
)


def fetch_em_individual_fund_flow_direct(symbol: str) -> Optional[pd.DataFrame]:
    """个股资金流日线（单位：元，与既有内部契约一致）。

    取不到返回 None——由上层声明该维度未覆盖，**不造空帧**。
    """
    code = "".join(ch for ch in str(symbol or "") if ch.isdigit())[-6:]
    if not code:
        return None
    market = 1 if code.startswith(("5", "6", "9")) else 0
    params = {
        "lmt": "0",
        "klt": "101",
        "secid": f"{market}.{code}",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": _FUND_FLOW_UT,
    }
    try:
        # `direct=True`：未配 `TA_EM_PROXY` 时**绕过系统代理**。系统代理会把请求
        # 交给一个我们无法验证的出口（实测对东财是 `ProxyError`），而这条通道的
        # 全部意义就是"我们自己控制出口"。
        from ._http_utils import resolve_proxies

        payload = http_get_json(
            _FUND_FLOW_URL, params=params, headers=_HEADERS,
            direct=resolve_proxies() is None,
        )
    except Exception:  # noqa: BLE001 — 东财不可用时如实返回 None
        return None

    klines = ((payload or {}).get("data") or {}).get("klines") or []
    rows: list[dict[str, Any]] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 13:
            continue
        row: dict[str, Any] = {}
        for index, name in enumerate(_FUND_FLOW_FIELDS):
            raw = parts[index]
            if name == "date":
                row[name] = raw
            else:
                try:
                    row[name] = float(raw)
                except (TypeError, ValueError):
                    row[name] = None
        rows.append(row)
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    frame["date"] = frame["date"].astype(str).str[:10]
    return frame


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


def fetch_em_market_breadth_direct() -> dict | None:
    """东财全市场宽度直连（push2delay，分页约 56 页/6s，TTL 缓存后无感）。

    算不出宽度时返回 **None**（而不是一份全零宽度）。

    旧实现无论有没有涨跌幅字段都返回 `{up, down, flat, total}`：一旦 `f3`
    缺失（东财改字段名/限流返回残缺页），就得到 `up=0 down=0 flat=0` 配
    `total=5598`——把一个约 5600 只股票的市场报成「零涨零跌」。
    `0` 是合法观测值，所以这个伪事实不会触发任何降级，还会作为
    `market_collector._breadth` 的**首选**（且不带 `approximate` 标记）
    一路进入分析师提示词（审计 §4.11）。
    """
    rows = _clist_paged(_FS["market"])
    if not rows:
        return None
    chgs = [r.get("f3") for r in rows if isinstance(r.get("f3"), (int, float))]
    if not chgs:
        # 一行涨跌幅都取不到 → 无法计算宽度。显式降级让上层问下一个源，
        # 而不是凭空造出「全市场零涨零跌」。
        logger.warning(
            "eastmoney breadth: %d rows but no numeric f3; degrading to next source",
            len(rows),
        )
        return None
    total = len(rows)
    up = sum(1 for c in chgs if c > 0)
    down = sum(1 for c in chgs if c < 0)
    flat = sum(1 for c in chgs if c == 0)
    return {"up": up, "down": down, "flat": flat, "total": total, "as_of": None}


_LHB_HOST = "https://datacenter-web.eastmoney.com"
_LHB_PATH = "/api/data/v1/get"
_LHB_REPORT = "RPT_DAILYBILLBOARD_DETAILSNEW"
#: `stock_lhb_detail_em` 原始列名（akshare 会重命名成中文，这里保持同一套名字，
#: 让下游 `_LHB_CODE_COLUMNS` / 展示层的行为与走 akshare 时完全一致）。
_LHB_COLUMNS = (
    "SECURITY_CODE", "SECUCODE", "SECURITY_NAME_ABBR", "TRADE_DATE", "EXPLAIN",
    "CLOSE_PRICE", "CHANGE_RATE", "BILLBOARD_NET_AMT", "BILLBOARD_BUY_AMT",
    "BILLBOARD_SELL_AMT", "BILLBOARD_DEAL_AMT", "ACCUM_AMOUNT", "DEAL_NET_RATIO",
    "DEAL_AMOUNT_RATIO", "TURNOVERRATE", "FREE_MARKET_CAP", "EXPLANATION",
    "D1_CLOSE_ADJCHRATE", "D2_CLOSE_ADJCHRATE", "D5_CLOSE_ADJCHRATE",
    "D10_CLOSE_ADJCHRATE", "SECURITY_TYPE_CODE",
)
#: 东财对「该窗口没有数据」返回的业务码：HTTP **200** + `result: null` +
#: `message="返回数据为空"`。这**不是**故障，只是一段没有行的窗口。
_LHB_EMPTY_BUSINESS_CODE = 9201


def fetch_em_lhb_detail_direct(
    start_date: str,
    end_date: str,
    *,
    retry: HttpRetry = HttpRetry(attempts=3, base_sleep_s=0.5, timeout_s=15.0),
) -> pd.DataFrame:
    """东财龙虎榜明细直连，**空窗口返回空表而不是抛错**。

    为什么不直接用 `akshare.stock_lhb_detail_em`：它的解析体是
    ``data_json["result"]["pages"]``，而东财对没有行的窗口返回
    ``HTTP 200 + result: null + code=9201 "返回数据为空"``，于是
    ``None["pages"]`` 抛出 ``TypeError: 'NoneType' object is not subscriptable``
    ——**非异动日（完全正常的情况）被上游当成了解析错误**，再被本模块的
    ``except Exception`` 兜成「龙虎榜数据获取失败」，用户在报告里看到一个红色的
    「数据异常」。实测：2026-09-26（周六）与 2020-01-01（元旦）都是 `result: null`。

    实测过的三种形态：

    * `result` 是 dict ⇒ 正常取 `data`（单日窗口 `pages` 通常为 1）；
    * `result` 为 null 且业务码为 9201 ⇒ **空窗口**，返回空表；
    * 其余（网络异常、未知业务码） ⇒ 原样抛出，由调用方如实报失败。

    返回的列名与 akshare 一致（中文列名由调用方再重命名，或直接按
    `_LHB_CODE_COLUMNS` 过滤），因此下游无需区分数据来自哪条路径。
    """
    start = str(start_date).replace("-", "").strip()
    end = str(end_date).replace("-", "").strip()
    if len(start) != 8 or not start.isdigit() or len(end) != 8 or not end.isdigit():
        raise ValueError(f"日期须为 YYYYMMDD：{start_date!r}/{end_date!r}")
    iso = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}"  # noqa: E731
    params = {
        "sortColumns": "SECURITY_CODE,TRADE_DATE",
        "sortTypes": "1,-1",
        # 单日窗口只有一个 page；pageSize 与 akshare 保持一致。
        "pageSize": "5000",
        "pageNumber": "1",
        "reportName": _LHB_REPORT,
        "columns": ",".join(_LHB_COLUMNS),
        "source": "WEB",
        "client": "WEB",
        "filter": f"(TRADE_DATE<='{iso(end)}')(TRADE_DATE>='{iso(start)}')",
    }
    payload = http_get_json(
        _LHB_HOST + _LHB_PATH, params=params, headers=_HEADERS, retry=retry
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"龙虎榜端点返回了非对象载荷：{type(payload).__name__}")
    result = payload.get("result")
    if result is None:
        code = payload.get("code")
        if code == _LHB_EMPTY_BUSINESS_CODE:
            # 「返回数据为空」= 这段窗口没有龙虎榜行（非异动日）——空表，不是错误。
            logger.info(
                "eastmoney lhb: %s..%s has no rows (code=%s); treating as empty window",
                iso(start), iso(end), code,
            )
            return pd.DataFrame(columns=list(_LHB_COLUMNS))
        # 未知的业务失败：如实抛错，绝不吞成"没有数据"。
        raise RuntimeError(
            f"龙虎榜端点业务错误：code={code} message={payload.get('message')!r}"
        )
    rows = result.get("data") if isinstance(result, dict) else None
    if not rows:
        return pd.DataFrame(columns=list(_LHB_COLUMNS))
    return pd.DataFrame(rows)


def fetch_em_zt_pool_direct() -> pd.DataFrame:
    """东财涨停池直连（push2delay clist fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23 需按涨幅过滤）。

    用 clist 全市场 + 涨停判定（f3 >= 9.8 且非 ST 无脑近似不可靠），
    因此涨停池仍优先 akshare stock_zt_pool_em（实测稳定），本函数暂不启用。
    """
    raise SourceUnsupported("涨停池请使用 akshare stock_zt_pool_em（稳定可用）")

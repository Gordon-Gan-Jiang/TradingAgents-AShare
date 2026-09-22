"""A 股研究类数据的取数实现（分时 / 筹码 / 资金流 / 板块基准 / 新闻）。

## 这些能力为什么在 provider 层

它们是**取数**，不是编排：与既有 19 个工具一样属于数据层。放在引擎层有三个
好处：

1. **可复用**——引擎可以在没有 API 层的情况下独立使用这些能力
2. **守住分层**——`tradingagents/` 不得 import `api/`（由架构边界测试强制）
3. **参与降级链**——经 `route_to_vendor` 调用即可享受既有三级降级与失败冷却，
   而不是像直连 akshare 那样「一个源抖了整个维度就不可得」

## ⚠️ 未实测

当前开发环境无网络（akshare 返回 ConnectionError），因此本模块的取数**未做
联网实测**。设计约束是「失败即降级」：每个函数在拿不到数据时返回 None 或空，
由上层显式报告为「该维度未覆盖」，绝不编造。联网后需实测清单见 tasks.md 组 1。
"""

from __future__ import annotations

import logging
import pathlib
from typing import Sequence

import pandas as pd

from tradingagents.tools.news import NewsItem

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# akshare 取数适配器
#
# ⚠️ 本段是**薄封装**，当前开发环境无网络，因此未做实测（`stock_cyq_em` 实测
# 返回 ConnectionError）。它们的设计约束是「失败即返回 None」：可选源的缺失
# 由维度显式报告 unavailable，而不是抛异常中断整次评估。所以即使某个接口的
# 参数或列名与预期不符，后果是「该维度不可得 + 置信度受限」，而不是功能崩溃。
#
# 联网后需要逐项实测的清单见 tasks.md 第 1 组（1.1 / 1.2 / 1.8）。
# ---------------------------------------------------------------------------

_MINUTE_COLUMNS = {
    "时间": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
}


def _ak():
    import akshare as ak  # 延迟导入：无网络环境也能加载本模块（只是取数会失败）

    return ak


def _bare_code(symbol: str) -> str:
    """600519.SH → 600519"""
    return str(symbol).split(".")[0]


def _market_of(symbol: str) -> str:
    code = _bare_code(symbol)
    if code.startswith(("6", "5")):
        return "sh"
    if code.startswith(("4", "8")):
        return "bj"
    return "sz"


def _sina_symbol(symbol: str) -> str:
    """新浪要 `sh600519` / `sz000001` 这种前缀格式；传裸代码会静默取不到。"""
    code = _bare_code(symbol)
    return f"sh{code}" if code.startswith(("5", "6", "9")) else f"sz{code}"


def _normalize_sina_minute(frame: pd.DataFrame, day: str) -> pd.DataFrame | None:
    """把新浪分钟线归一成内部契约，并**只保留请求当日**。

    新浪返回的是多日区间（实测 1970 行覆盖约两个月），混进别的交易日会把
    「当日承接」算成跨日的量——那是静默的错误结论，比取不到更糟。
    """
    if frame is None or len(frame) == 0:
        return None
    out = frame.rename(columns={"day": "date"}).copy()
    if "date" not in out.columns:
        return None
    for column in ("open", "high", "low", "close", "volume"):
        if column not in out.columns:
            return None
    out["date"] = out["date"].astype(str)
    out = out[out["date"].str.startswith(str(day))]
    if out.empty:
        return None
    return out.reset_index(drop=True)


def fetch_intraday_frame(symbol: str, day: str, freq: str) -> pd.DataFrame | None:
    """分时 bars。akshare 的 period 支持 1/5/15/30/60 分钟。

    **东财 → 新浪**双源：东财系接口存在 IP 级封禁（实测 `RemoteDisconnected`，
    见 akshare issue #6061），而新浪 `stock_zh_a_minute` 稳定可用。分时是
    「下跌中有没有人接」的唯一证据来源，也是洗盘最直接的一条——只有一个源
    意味着它一被封，判定就只能退化为"证据不足"。
    """
    ak = _ak()
    try:
        frame = ak.stock_zh_a_hist_min_em(
            symbol=_bare_code(symbol),
            start_date=f"{day} 09:30:00",
            end_date=f"{day} 15:00:00",
            period=str(freq),
            adjust="qfq",
        )
        if frame is not None and len(frame) > 0:
            renamed = frame.rename(columns=_MINUTE_COLUMNS)
            if all(c in renamed.columns for c in ("open", "high", "low", "close", "volume")):
                return renamed.reset_index(drop=True)
    except Exception:  # noqa: BLE001 — 换源是常态，不是错误
        pass

    try:
        return _normalize_sina_minute(
            ak.stock_zh_a_minute(symbol=_sina_symbol(symbol), period=str(freq)), day
        )
    except Exception:  # noqa: BLE001
        return None


_CHIP_COLUMNS = {
    "日期": "date",
    "获利比例": "profit_ratio",
    "平均成本": "avg_cost",
    "90集中度": "concentration_90",
    "70集中度": "concentration_70",
}


def fetch_chip_frame(symbol: str, day: str) -> pd.DataFrame | None:
    """筹码分布。取 <= day 的最近一行。

    获利比例等字段随数据源口径可能是百分数（如 58.3 表示 58.3%），统一归一为
    0-1 比率——否则「获利盘 58.3%」会被当成 58.3 倍，判据完全走偏。
    """
    ak = _ak()
    frame = None
    try:
        frame = ak.stock_cyq_em(symbol=_bare_code(symbol), adjust="qfq")
    except Exception:  # noqa: BLE001 — 东财被封是常态，退 Tushare
        frame = None
    if frame is None or len(frame) == 0:
        via_tushare = _chip_frame_via_tushare(symbol, day)
        if via_tushare is not None:
            return via_tushare
        # 最后一道：**用日线自算**（模型估计）。东财被封、Tushare 未配时，
        # 这条路径让筹码维度仍然可用——因为它只需要日线，而日线有免费源。
        return _chip_frame_via_model(symbol, day)
    frame = frame.rename(columns=_CHIP_COLUMNS)
    if "profit_ratio" not in frame.columns:
        return None
    if "date" in frame.columns:
        frame["date"] = frame["date"].astype(str).str[:10]
        frame = frame[frame["date"] <= day]
    if len(frame) == 0:
        return None
    frame = frame.tail(1).copy()
    for column in ("profit_ratio", "concentration_90", "concentration_70"):
        if column in frame.columns:
            values = pd.to_numeric(frame[column], errors="coerce")
            # 百分数口径 → 比率
            frame[column] = values.where(values <= 1.0, values / 100.0)
    return frame.reset_index(drop=True)


_FLOW_COLUMNS = {
    "日期": "date",
    "主力净流入-净额": "main_net_inflow",
    "超大单净流入-净额": "super_large_net_inflow",
    "大单净流入-净额": "large_net_inflow",
}


def fetch_flow_frame(symbol: str, day: str) -> pd.DataFrame | None:
    """个股资金流。**东财 → Tushare** 双源。

    实测东财系被 IP 级封禁后，这个维度长期缺失，判定只能停在"证据不足"。
    Tushare 是官方积分制源，作为第二通道；两源都不可用时返回 None——
    由上层声明未覆盖，**不返回空帧冒充"取到了"**。
    """
    ak = _ak()
    try:
        frame = ak.stock_individual_fund_flow(stock=_bare_code(symbol), market=_market_of(symbol))
        if frame is not None and len(frame) > 0:
            renamed = frame.rename(columns=_FLOW_COLUMNS)
            if "main_net_inflow" in renamed.columns:
                if "date" in renamed.columns:
                    renamed["date"] = renamed["date"].astype(str).str[:10]
                    renamed = renamed[renamed["date"] <= day]
                if len(renamed) > 0:
                    return renamed.tail(1).reset_index(drop=True)
    except Exception:  # noqa: BLE001 — 换源是常态
        pass

    # 第二通道：东财**直连**（走出口代理）。akshare 内部不传 proxies，所以
    # "配了代理也没用"；这条路径是我们自己发的请求，代理才真正生效。
    direct = _flow_frame_via_em_direct(symbol, day)
    if direct is not None:
        return direct

    # 第三通道：Tushare（需 token；未配置时如实返回 None）
    return _flow_frame_via_tushare(symbol, day)


def _flow_frame_via_em_direct(symbol: str, day: str) -> pd.DataFrame | None:
    from .cn_eastmoney_direct import fetch_em_individual_fund_flow_direct

    try:
        frame = fetch_em_individual_fund_flow_direct(symbol)
    except Exception:  # noqa: BLE001
        return None
    if frame is None or len(frame) == 0 or "main_net_inflow" not in frame.columns:
        return None
    if "date" in frame.columns:
        frame = frame[frame["date"].astype(str) <= day]
    if len(frame) == 0:
        return None
    return frame.tail(1).reset_index(drop=True)


def _flow_frame_via_tushare(symbol: str, day: str) -> pd.DataFrame | None:
    """Tushare `moneyflow` → 内部契约列。

    单位与口径都按官方定义：
    * 净流入用 `net_mf_amount`（**万元**），它由 L2 主动买卖单算出；
      官方明确"不能简单把大小单总和相减"，所以不自己算。
    * 换算成**元**，与东财 `main_net_inflow` 口径一致。
    """
    from . import cn_tushare

    try:
        row = cn_tushare.fetch_moneyflow_row(symbol, day)
    except cn_tushare.TushareUnavailable:
        return None
    if not row:
        return None

    def _num(key: str) -> float:
        try:
            return float(row.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ① 必须用 Tushare 的 `net_mf_amount`。官方文档明确：净流入是**基于 L2 主动
    #    买卖单**计算，**不能简单把大小单总和相减**——自己相减会得到一个口径不同
    #    的数，而"主力净流入"正是判定的核心证据之一。
    # ② 单位是**万元**，内部契约（与东财口径一致）是**元**，差 10000 倍。
    #    单位错会让"净流入 1.2 亿"与"净流入 1200 万"张冠李戴。
    if row.get("net_mf_amount") is not None:
        main_net = _num("net_mf_amount") * 10_000.0
    else:
        # 兜底：只在官方净额缺失时按大单+特大单估算，并保持同一单位换算
        main_net = (
            _num("buy_lg_amount") + _num("buy_elg_amount")
            - _num("sell_lg_amount") - _num("sell_elg_amount")
        ) * 10_000.0
    trade_date = str(row.get("trade_date") or "").replace("-", "")
    normalized_day = (
        f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}" if len(trade_date) >= 8 else day
    )
    return pd.DataFrame([{"date": normalized_day, "main_net_inflow": main_net}])


def _chip_frame_via_model(symbol: str, day: str) -> pd.DataFrame | None:
    """用日线 + 换手率**自算**筹码分布（模型估计，非原始数据）。

    实测东财 API 被 IP 级封禁；而 akshare 的 `stock_cyq_em` 本身也是"取 K 线 +
    本地三角形分布算法"，即它本来就是估计。既然如此，我们用免费的新浪日线
    （自带 `turnover` 换手率列）自己算，既不受封禁影响，也不必买付费数据。

    **来源标记为估计**（`source=cyq_model_estimate`），调用方不得与交易所数据混同。
    """
    from tradingagents.signals.chips_model import compute_chip_distribution

    ak = _ak()
    try:
        import datetime as _dt

        end = _dt.date.fromisoformat(str(day)[:10])
        start = end - _dt.timedelta(days=400)
        raw = ak.stock_zh_a_daily(
            symbol=_sina_symbol(symbol),
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="qfq",
        )
    except Exception:  # noqa: BLE001 — 拿不到日线就没有估计
        return None
    if raw is None or len(raw) == 0:
        return None

    columns = {str(c).lower(): c for c in raw.columns}
    if not {"close", "high", "low", "open"}.issubset(columns):
        return None

    bars = []
    for _, row in raw.iterrows():
        bars.append(
            {
                "open": row[columns["open"]],
                "high": row[columns["high"]],
                "low": row[columns["low"]],
                "close": row[columns["close"]],
                "turnover": row[columns["turnover"]] if "turnover" in columns else None,
            }
        )
    estimate = compute_chip_distribution(bars)
    if estimate is None:
        return None

    return pd.DataFrame(
        [
            {
                "date": str(day)[:10],
                "profit_ratio": estimate["profit_ratio"],
                "avg_cost": estimate["avg_cost"],
                "concentration_90": estimate["concentration_90"],
                "source": estimate["source"],
            }
        ]
    )


def _chip_frame_via_tushare(symbol: str, day: str) -> pd.DataFrame | None:
    """Tushare `cyq_perf` → 内部契约列。

    获利比例（`winner_rate`）在 Tushare 里是**百分数**（58.3 = 58.3%），
    必须归一为 0-1 比率——与东财口径一致，否则"获利盘 58.3%"会被当成 58.3 倍。
    """
    from . import cn_tushare

    try:
        row = cn_tushare.fetch_chip_row(symbol, day)
    except cn_tushare.TushareUnavailable:
        return None
    if not row:
        return None

    try:
        winner = float(row.get("winner_rate"))
    except (TypeError, ValueError):
        return None
    ratio = winner / 100.0 if winner > 1.0 else winner

    trade_date = str(row.get("trade_date") or "").replace("-", "")
    normalized_day = (
        f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}" if len(trade_date) >= 8 else day
    )
    return pd.DataFrame([{"date": normalized_day, "profit_ratio": ratio}])


def fetch_stock_turnover(symbol: str, day: str) -> float | None:
    """个股换手率（百分比）。

    换手率的严格算法是「成交量 / 流通股本」，需要流通股本；若接口不提供，
    返回 None 并由上层把「相对换手」标为不可得——**不得**用成交量近似替代，
    因为那正是 D6 要消除的口径混淆。
    """
    ak = _ak()
    try:
        info = ak.stock_individual_info_em(symbol=_bare_code(symbol))
    except Exception:  # noqa: BLE001
        # 实测（2026-09-22）：该接口在本机**稳定抛**
        # `ConnectionError: ('Connection aborted.', RemoteDisconnected(...))`——
        # EM 会拒绝/断开这个端点（此前还观测到过 `ValueError: Length mismatch`，
        # 即响应内容与预期列不符）。两种都是"拿不到"，不是"没有"。
        #
        # 契约是「拿不到返回 None，由上层把相对换手标为不可得」，所以这里必须
        # 吞掉异常：让它冒出去会升级成**整个工具失败**，而实际只是**一个维度不可得**——
        # 那会把一次"部分可用"的查询表述成"数据源坏了"。
        return None
    if info is None or len(info) == 0:
        return None
    try:
        mapping = dict(zip(info["item"], info["value"]))
    except Exception:  # noqa: BLE001
        return None
    float_shares = mapping.get("流通股")
    if not float_shares:
        return None
    daily = _ak().stock_zh_a_hist(
        symbol=_bare_code(symbol),
        period="daily",
        start_date=day.replace("-", ""),
        end_date=day.replace("-", ""),
        adjust="",
    )
    if daily is None or len(daily) == 0 or "成交量" not in daily.columns:
        return None
    volume_hands = float(pd.to_numeric(daily.iloc[-1]["成交量"], errors="coerce"))
    shares = float(pd.to_numeric(float_shares, errors="coerce"))
    if shares <= 0:
        return None
    # 成交量单位为「手」（100 股）
    return volume_hands * 100.0 / shares * 100.0


# 新浪没有"按个股查行业"的接口，只能扫板块建映射。扫 49 个板块是有成本的，
# 而判定总预算只有 60s——因此映射必须落盘缓存（TTL 一天）。
# 与名称表缓存同一目录（`results/cache/`）：盘后数据缓存放一起便于清理与备份
_SECTOR_MAP_CACHE_FILE = (
    pathlib.Path(__file__).resolve().parents[3] / "results" / "cache" / "cn_sina_sector_map.json"
)
_SECTOR_MAP_TTL_SECONDS = 24 * 3600


def _sector_map_from_cache() -> dict[str, tuple[str, str]] | None:
    import json as _json
    import time as _time

    path = _SECTOR_MAP_CACHE_FILE
    try:
        if not path.exists():
            return None
        if _time.time() - path.stat().st_mtime > _SECTOR_MAP_TTL_SECONDS:
            return None
        payload = _json.loads(path.read_text(encoding="utf-8"))
        return {str(k): (str(v[0]), str(v[1])) for k, v in payload.items() if v}
    except Exception:  # noqa: BLE001 — 缓存坏了就重建，不该让判定失败
        return None


def _write_sector_map_cache(mapping: dict[str, tuple[str, str]]) -> None:
    import json as _json

    try:
        _SECTOR_MAP_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECTOR_MAP_CACHE_FILE.write_text(
            _json.dumps({k: list(v) for k, v in mapping.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 — 写不进去只是下次重扫
        pass


def _build_sector_map(ak) -> dict[str, tuple[str, str]]:
    """扫全部新浪行业，建 {裸代码: (label, 行业名)}。"""
    mapping: dict[str, tuple[str, str]] = {}
    sectors = ak.stock_sector_spot(indicator="新浪行业")
    for _, row in sectors.iterrows():
        label = str(row.get("label") or "")
        name = str(row.get("板块") or "")
        if not label:
            continue
        try:
            detail = ak.stock_sector_detail(sector=label)
        except Exception:  # noqa: BLE001 — 单个板块失败不该毁掉整张表
            continue
        if detail is None or len(detail) == 0:
            continue
        code_column = "code" if "code" in detail.columns else "symbol"
        for raw in detail[code_column].astype(str):
            code = "".join(ch for ch in raw if ch.isdigit())[-6:]
            if code:
                mapping.setdefault(code, (label, name))
    return mapping


def _peer_frames_via_sina(symbol: str, day: str):
    """新浪兜底：(行业名, 板块行情表, 成分股表)，列名按既有契约归一。"""
    ak = _ak()
    code = _bare_code(symbol)

    mapping = _sector_map_from_cache()
    if mapping is None:
        mapping = _build_sector_map(ak)
        if mapping:
            _write_sector_map_cache(mapping)
    found = mapping.get(code)
    if not found:
        return None
    label, industry = found

    detail = ak.stock_sector_detail(sector=label)
    if detail is None or len(detail) == 0:
        return None
    cons = detail.rename(columns={"turnoverratio": "换手率"}).copy()
    if "换手率" not in cons.columns:
        return None

    board = ak.stock_sector_spot(indicator="新浪行业").rename(
        columns={"板块": "板块名称"}
    ).copy()
    if "板块名称" not in board.columns or "涨跌幅" not in board.columns:
        return None
    return industry, board, cons


def fetch_peer_frames(symbol: str, day: str):
    """返回 (行业名, 板块行情表, 板块成分股表)。

    板块中位换手取自**成分股**换手率中位数（与个股同量纲）；取板块指数自身的
    换手率与个股不可比，比较没有意义。
    """
    ak = _ak()
    from tradingagents.dataflows.providers.cn_akshare_provider import (
        fetch_board_cons_df,
        fetch_board_spot_df,
    )

    # 东财被封是常态（实测 IP 级封禁）。注意：EM 路径**内部吞异常**，
    # 可能返回 industry=None 的半成品——那种情况下也必须退新浪，
    # 否则"行业拿不到"会被当成"取到了"，relative_strength 静默缺席。
    em_result = None
    try:
        em_result = _fetch_peer_frames_via_em(ak, symbol, day)
    except Exception:  # noqa: BLE001
        em_result = None
    if em_result is not None and em_result[0]:
        return em_result

    # 新浪兜底必须和 EM 一样吞异常。
    #
    # 这里原本是**裸调用**：EM 那一支有 try/except，这一支没有。于是新浪一旦抛
    # （`ConnectionError`、限流、字段变更都是实测中见过的形态），异常会直接穿出
    # `fetch_peer_frames`，**违反它自己声明的「三元组或 None」契约**——调用方拿到
    # 的是异常而不是 None，于是"相对强度拿不到"升级成"这个工具坏了"。
    # 与 `fetch_stock_turnover` 是同一个毛病：护栏只加在一个分支上。
    try:
        sina_result = _peer_frames_via_sina(symbol, day)
    except Exception:  # noqa: BLE001
        sina_result = None
    if sina_result is not None:
        return sina_result
    # 契约是「返回三元组**或 None**」。EM 路径在拿不到行业时会返回
    # `(None, None, None)` 这样的半成品——那种形状会让调用方以为"取到了"，
    # 于是 relative_strength 静默缺席。统一成 None：拿不到就是拿不到。
    return None


def _fetch_peer_frames_via_em(ak, symbol: str, day: str):
    industry = None
    try:
        info = ak.stock_individual_info_em(symbol=_bare_code(symbol))
        mapping = dict(zip(info["item"], info["value"]))
        industry = mapping.get("行业")
    except Exception:  # noqa: BLE001
        industry = None

    board_frame = None
    cons_frame = None
    try:
        board_frame = fetch_board_spot_df(ak, sector_type="industry")
    except Exception:  # noqa: BLE001
        board_frame = None
    if industry:
        try:
            cons_frame = fetch_board_cons_df(ak, board_name=str(industry), sector_type="industry")
        except Exception:  # noqa: BLE001
            cons_frame = None
    return (str(industry) if industry else None, board_frame, cons_frame)


# ---------------------------------------------------------------------------
# 经 vendor 路由的取数入口
#
# 应用层与工具层应调用这些入口，而不是直接调上面的实现——只有经
# `route_to_vendor` 才能共享三级降级链与失败冷却。
#
# ⚠️ 日线的"先库后网"**刻意不在这里**：本地日线库要读数据库、要写 `daily_bars`，
# 那是应用层的事，而 `tradingagents/` 不得 import `api/`（架构边界测试强制）。
# 因此本地库在 `api/services/daily_bar_store.py`，在装配处（`signal_service` /
# `signal_replay_service` 的端口工厂）包住日线端口：库内够用就不打外网，不够才
# 走这里的路由取数并顺手入库。本模块只负责取数，不承担缓存职责。
# ---------------------------------------------------------------------------


def _route(method: str, *args, **kwargs):
    from tradingagents.dataflows.interface import route_to_vendor

    return route_to_vendor(method, *args, **kwargs)


def fetch_daily_bars_via_router(symbol: str, start: str, end: str) -> pd.DataFrame:
    return _route("get_daily_bars_df", symbol, start, end)


def fetch_intraday_via_router(symbol: str, day: str, freq: str = "5"):
    return _route("get_intraday_bars", symbol, day, freq)


def fetch_chips_via_router(symbol: str, day: str):
    return _route("get_chip_distribution", symbol, day)


def fetch_flow_via_router(symbol: str, day: str):
    return _route("get_flow_frame", symbol, day)


def fetch_peer_via_router(symbol: str, day: str):
    return _route("get_peer_frames", symbol, day)


def fetch_turnover_via_router(symbol: str, day: str):
    return _route("get_stock_turnover", symbol, day)


def fetch_symbol_news_via_router(symbol: str, start: str, end: str):
    return _route("get_symbol_news", symbol, start, end)


_NEWS_TITLE_COLUMNS = ("新闻标题", "title")
_NEWS_TIME_COLUMNS = ("发布时间", "时间", "publish_time")
_NEWS_SOURCE_COLUMNS = ("文章来源", "来源", "source")
_NEWS_URL_COLUMNS = ("新闻链接", "链接", "url")
_NEWS_SUMMARY_COLUMNS = ("新闻内容", "内容", "summary")


def fetch_symbol_news(symbol: str, start: str, end: str) -> Sequence[NewsItem]:
    """个股新闻（结构化条目，含发布日期以便被回答层引用）。"""
    ak = _ak()
    frame = ak.stock_news_em(symbol=_bare_code(symbol))
    if frame is None or len(frame) == 0:
        return []

    columns = list(frame.columns)
    title_col = _pick_column(frame, _NEWS_TITLE_COLUMNS)
    time_col = _pick_column(frame, _NEWS_TIME_COLUMNS)
    if title_col is None or time_col is None:
        logger.info("news frame missing expected columns; got %s", columns)
        return []

    source_col = _pick_column(frame, _NEWS_SOURCE_COLUMNS)
    url_col = _pick_column(frame, _NEWS_URL_COLUMNS)
    summary_col = _pick_column(frame, _NEWS_SUMMARY_COLUMNS)

    items: list[NewsItem] = []
    for _, row in frame.iterrows():
        published = str(row[time_col])[:10]
        if start and published < start:
            continue
        if end and published > end:
            continue
        items.append(
            NewsItem(
                title=str(row[title_col]),
                published_at=published,
                source=str(row[source_col]) if source_col else "",
                url=str(row[url_col]) if url_col else "",
                summary=str(row[summary_col])[:500] if summary_col else "",
            )
        )
    return items


def _pick_column(frame: "pd.DataFrame", candidates: Sequence[str]) -> str | None:
    for name in candidates:
        if name in frame.columns:
            return name
    return None

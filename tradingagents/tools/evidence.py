"""L2 证据工具：**只给事实、不做判断**。

## 为什么要有这一层

L1 工具回答「是什么判断」（洗盘/派发、判定历史、平台推荐）；这一层回答
「数据本身是什么」：获利盘多少、主力净流入多少、那天分时怎么承接、
我的自选里有什么、这份数据是哪天的。对话里最常见的追问全是后者，
而它们都需要一条**可被校验器引用的数值**——没有这一层，模型只能从散文里
复述，编造与转述就分不清了。

## 四条纪律（每条都有对应的测试）

1. **只给事实**：证据是「字段 + 数值 + 单位」三元组（`ratio` / `pct` /
   `yuan` / `count` / `days`），日期进 `as_of`。本层不做「所以是洗盘」这类
   推断，也不带研究免责声明（免责声明约束的是**判断**，事实工具附加它反而
   会让用户以为数据被降级处理过）。
2. **弃权要说出来**：取不到 → `Coverage.unavailable` 里一条**可读名**
   （如 `chip_distribution（筹码分布）`），绝不返回空帧冒充「取到了」。
   空数据被读成「没有异常」是本平台最不能接受的失败形态。
3. **失败降级、不外传原文**：上游异常只留**类型名**，消息体不进对外结果——
   上游文案里可能带 URL、token、IP 封禁提示，那是基础设施细节，不属于结论。
4. **估计必须标注**：筹码的 `source` 以 `model_estimate` 结尾时（东财被封、
   Tushare 未配时的**自算**路径），证据里必须写明「模型估计，非交易所原始
   数据」。把估计说成事实，是本平台最不容忍的失败。

## 取数为什么经端口注入（而不是本模块直接持有）

取数实现是**既有**的
`tradingagents/dataflows/providers/cn_research_data.py`
（`fetch_chip_frame` / `fetch_intraday_frame` / `fetch_flow_frame`）与
`tradingagents/dataflows/providers/cn_announcement_index_data.py`
（`fetch_symbol_announcements` / `fetch_index_quotes`），本模块**不重写**任何一条
取数路径，只做「帧 → 可引用证据」的转换。但工具层不直接持有它们，而由组合根显式
注入（`make_evidence_tools(...)`）：

* 测试可以注入假帧：不联网、不依赖 akshare 的可用性；
* **未注入 → 不注册**，不留「在册但一调就炸」的条目；
* 应用层可以换成「先库后网」的实现（`api/services/daily_bar_store.py` 已有这套
  做法）而不需要改工具层。

端口里**不出现 user_id**：自选清单这类用户维度在应用层的闭包里绑定，
给模型一个 user_id 参数就等于给它越权的可能。

## 纯函数与 IO 分离

`chip_facts` / `intraday_facts` / `flow_facts` / `index_quote_facts` 是**纯函数**
（映射进、映射出，不取数、不着网），取数只发生在 handler 里那一行 `fetcher(...)`。
这样统计口径可以被逐条单测穷举边界，而不必构造网络环境。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta
from typing import Any, Callable, Mapping, Protocol, Sequence

from tradingagents.grounding.types import Coverage, EvidenceItem

from .spec import PLAN_LAYER_EVIDENCE, ToolResult, ToolSpec

# ── 输出契约名与源键 ──────────────────────────────────────────────────────
#
# 源键必须与 freshness 契约（`dataflows/freshness/contract.yaml`）一致：写成
# 别的字符串会让 freshness 归属到另一个源（`symbol_news` 与 `news` 就踩过）。

CHIP_CONTRACT = "CHIP_DISTRIBUTION"
INTRADAY_CONTRACT = "INTRADAY_BARS"
FLOW_CONTRACT = "CAPITAL_FLOW"
WATCHLIST_CONTRACT = "WATCHLIST"
ANNOUNCEMENTS_CONTRACT = "ANNOUNCEMENTS"
INDEX_CONTRACT = "INDEX_QUOTES"
FRESHNESS_CONTRACT = "DATA_FRESHNESS"

SOURCE_CHIP = "chip_distribution"
SOURCE_INTRADAY = "intraday_bars"
SOURCE_FLOW = "individual_fund_flow"
SOURCE_WATCHLIST = "watchlist"
SOURCE_ANNOUNCEMENTS = "symbol_announcements"
SOURCE_INDEX = "index_quotes"

# 未覆盖维度的**对外可读名**。校验器按**逐字相等**比对 `declared_unavailable`
# （不过模型这一关：编排层会把它们并进声明），但用户会读到这行字，所以仍然
# 写成「键（中文名）」这种可读形式，而不是裸键或长句。
UNAVAILABLE_CHIP = f"{SOURCE_CHIP}（筹码分布）"
UNAVAILABLE_INTRADAY = f"{SOURCE_INTRADAY}（分时）"
UNAVAILABLE_FLOW = f"{SOURCE_FLOW}（资金流）"
UNAVAILABLE_WATCHLIST = f"{SOURCE_WATCHLIST}（自选清单）"
UNAVAILABLE_ANNOUNCEMENTS = f"{SOURCE_ANNOUNCEMENTS}（公告）"
UNAVAILABLE_INDEX = f"{SOURCE_INDEX}（指数行情）"
UNAVAILABLE_FRESHNESS = "data_freshness（时效查询）"

# 估算口径的固定文案。写成常量而不是各处拼字符串：这句话是**契约**，
# 必须逐字包含「模型估计」与「非交易所原始数据」，测试也只断言这两点。
ESTIMATE_NOTE = "模型估计，非交易所原始数据"

# 筹码自算来源标记（与 `signals/chips_model.py::SOURCE_ESTIMATED` 同口径）。
SOURCE_ESTIMATED_SUFFIX = "model_estimate"

# 一次分时统计里认为「尾盘」的起点。时间戳不可用时退化为最后 3 根 bar，
# 此时**必须**在字段名与 coverage 里说明，不能把退化结果当成精确切分。
TAIL_CUTOFF = "14:30"
TAIL_FALLBACK_BARS = 3

_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")

MAX_WATCHLIST_ITEMS = 50
DEFAULT_WATCHLIST_ITEMS = 20
MAX_ANNOUNCEMENTS = 30
DEFAULT_ANNOUNCEMENTS = 10
MAX_INDEX_CODES = 12
MAX_FRESHNESS_SOURCES = 12

# 省略 `codes` 时查哪些指数：至少覆盖沪深两个市场与创业板（用户问「今天大盘」
# 时默认就是这三个）。这里**独立**持有一份（工具契约的一部分：schema 描述要
# 写明默认值），并由测试断言与取数层的 `DEFAULT_INDEX_CODES` 逐字一致——
# 两处各写一份会漂移，漂移的表现是「模型以为查了创业板，其实没查」。
DEFAULT_INDEX_CODES: tuple[str, ...] = ("sh000001", "sz399001", "sz399006")

# 留空时查哪些源：取**对话判定真正依赖**的那几个，而不是契约里的全部 20 个
# （全量会把证据刷成一堵墙，也让「这数据什么时候的」这个问题失去焦点）。
DEFAULT_FRESHNESS_SOURCES: tuple[str, ...] = (
    "stock_data",
    "intraday_bars",
    "chip_distribution",
    "individual_fund_flow",
    "symbol_news",
)

_STATUS_LABELS = {
    "fresh": "未过期",
    "stale": "已过期（截止日早于基准日）",
    "warning": "接近过期",
    "error": "本轮取数失败",
    "unknown": "截止日未知（无法判定是否过期）",
}


# ── 端口：取数与只读事实源 ────────────────────────────────────────────────
#
# 全是**结构类型**（Protocol / Callable），实现由应用层提供或由
# `default_market_fetchers()` 指向既有 provider。工具层因此不 import `api/`
# （AST 边界测试强制），也不需要 import akshare。

# (symbol, day) -> 帧；取不到返回 None（**不得**返回空帧冒充取到了）
ChipFetcher = Callable[[str, str], Any]
# (symbol, day, freq) -> 帧
IntradayFetcher = Callable[[str, str, str], Any]
FlowFetcher = Callable[[str, str], Any]
# (指数代码序列) -> 帧；取不到返回 None
IndexFetcher = Callable[[Sequence[str]], Any]


class WatchlistPort(Protocol):
    """当前用户的自选清单。实现由应用层提供（闭包内绑定 user_id）。"""

    def list_items(self, *, limit: int) -> Sequence[Mapping[str, Any]]: ...


class AnnouncementsPort(Protocol):
    """某标的的公告。实现由应用层提供（当前仓库尚无实现，见模块末尾说明）。"""

    def list_announcements(
        self, *, symbol: str, start: str, end: str, limit: int
    ) -> Sequence[Mapping[str, Any]]: ...


class FreshnessProbePort(Protocol):
    """各数据源的**已知**时效事实（截止日与出处）。

    返回形如 `{"stock_data": {"cutoff": "2026-09-18", "origin": "本地日线库"}}`。
    **拿不到就不返回该键**（=「截止日未知」），不要用今天日期兜底：把「不知道」
    填成「今天」正是本平台要消灭的伪造新鲜度。
    """

    def source_facts(
        self, *, source_keys: Sequence[str], symbol: str | None = None
    ) -> Mapping[str, Mapping[str, Any]]: ...


@dataclass(frozen=True)
class MarketEvidenceFetchers:
    """市场类证据工具的取数集合（逐个可缺省）。

    刻意用「一个聚合对象 + 每个字段可 None」而不是三个位置参数：组合根只写一行，
    测试只填自己关心的那一个，缺的那个即**不注册**对应的工具。
    """

    chip: ChipFetcher | None = None
    intraday: IntradayFetcher | None = None
    flow: FlowFetcher | None = None


def default_market_fetchers() -> MarketEvidenceFetchers:
    """既有取数实现的默认组合（**不重写取数**，只是把已有函数装配起来）。

    延迟 import：模块加载不应拉起 akshare，也不该让无网络环境的测试受影响。
    组合根显式调用本函数 = 「我确认用引擎提供的取数实现」，与「未注入」在
    `build_registry` 里是两条不同的分支。
    """
    from tradingagents.dataflows.providers.cn_research_data import (
        fetch_chip_frame,
        fetch_flow_frame,
        fetch_intraday_frame,
    )

    return MarketEvidenceFetchers(
        chip=fetch_chip_frame,
        intraday=fetch_intraday_frame,
        flow=fetch_flow_frame,
    )


# ── 纯函数：帧 → 事实 ─────────────────────────────────────────────────────


def _to_float(value: Any) -> float | None:
    """任何数值表示 → float；空/非数 → None（**不**当成 0）。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # NaN 只能这样判（`value != value`），pandas 缺列/空值都会走到这里
    if number != number:
        return None
    return number


def _as_ratio(value: float | None) -> float | None:
    """百分数口径 → 0-1 比率。

    取数实现已做过归一，这里是**防御**：比率不可能大于 1，一旦出现 58.3 这种
    值就说明口径是百分数，必须换算——否则「获利盘 58.3%」会被当成 58.3 倍。
    """
    if value is None:
        return None
    return value / 100.0 if value > 1.0 else value


def _round6(value: float | None) -> float | None:
    """保留 6 位小数（仍是 float）。

    浮点除法的尾数噪音（`-4.499999999999993`）会让证据读起来像实测精度；
    6 位远超 A 股任何口径的精度，而校验器的容差是 2% 量级，取整不影响比对。
    """
    return None if value is None else round(float(value), 6)


def _rows_of(frame: Any) -> list[Mapping[str, Any]]:
    """把帧/序列/单行统一成「一行一个 Mapping」。

    取数实现返回 pandas 帧，测试注入的是字典列表——两者都要能走同一套纯函数，
    否则口径会在「真实路径」与「测试路径」之间分叉。刻意不 import pandas：
    鸭子类型足够，且让本模块的加载不依赖数据栈。
    """
    if frame is None:
        return []
    if isinstance(frame, Mapping):
        return [frame]
    if hasattr(frame, "to_dict"):
        try:
            if hasattr(frame, "columns"):  # DataFrame
                return [dict(row) for row in frame.to_dict(orient="records")]
            return [dict(frame.to_dict())]  # Series（单行）
        except Exception:  # noqa: BLE001 — 结构不符就是没有事实
            return []
    try:
        return [dict(row) for row in frame if isinstance(row, Mapping)]
    except TypeError:
        return []


def _row_time(row: Mapping[str, Any]) -> str | None:
    """从一行里取 HH:MM；取不到返回 None（**不猜**）。"""
    raw = row.get("date", row.get("time", row.get("时间", row.get("datetime"))))
    if raw is None:
        return None
    match = _TIME_RE.search(str(raw))
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _ordered(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """按时间升序。

    分时帧在部分源（新浪多日区间）不保证顺序，而「最后一根 = 收盘」是本工具的
    核心语义——顺序错了会把盘中某一根当成收盘价，是静默的错误结论。
    无法排序（没有可解析的时间列）时保持原顺序。
    """
    keys = [str(row.get("date", row.get("time", "")) or "") for row in rows]
    if any(not key for key in keys):
        return list(rows)
    return [row for _, row in sorted(zip(keys, rows), key=lambda pair: pair[0])]


def _source_marker(frame: Any) -> str:
    """取筹码帧的来源标记（兼容 DataFrame / Series / 字典 / 行列表）。

    必须兼容 Series：`DataFrame.get("source")` 返回的是 Series，
    `str(Series)` 永远不以 `model_estimate` 结尾——照抄这个写法会让「这是估计」
    的标记**静默失效**（`signals/chips.py` 里记着这个坑）。

    也要兼容「一行一个 Mapping」的行列表（测试与部分 provider 就是这个形状）：
    那里没有 `.get`，只能取最后一行的 `source`。
    """
    if frame is None:
        return ""
    raw = None
    if isinstance(frame, Mapping):
        raw = frame.get("source")
    elif hasattr(frame, "get"):
        try:
            raw = frame.get("source")
        except Exception:  # noqa: BLE001
            raw = None
    if raw is None:
        rows = _rows_of(frame)
        raw = rows[-1].get("source") if rows else None
    if raw is None:
        return ""
    if hasattr(raw, "iloc"):
        try:
            return str(raw.iloc[-1])
        except Exception:  # noqa: BLE001 — 空序列
            return ""
    return str(raw)


def chip_facts(frame: Any) -> dict[str, Any] | None:
    """筹码帧 → 事实字典（纯函数）。获利比例缺失即视为该维度不可得。"""
    rows = _rows_of(frame)
    if not rows:
        return None
    row = rows[-1]
    profit = _as_ratio(_to_float(row.get("profit_ratio")))
    if profit is None:
        # 与 `signals/chips.py` 同口径：只有平均成本时**不**算可得——
        # 单一成本值不携带方向信息，替上去会伪造出一个不存在的判据。
        return None
    source = _source_marker(frame)
    return {
        "profit_ratio": profit,
        "avg_cost": _to_float(row.get("avg_cost")),
        "concentration_90": _as_ratio(_to_float(row.get("concentration_90"))),
        "concentration_70": _as_ratio(_to_float(row.get("concentration_70"))),
        "data_date": str(row.get("date") or "")[:10],
        "source": source,
        "estimated": source.endswith(SOURCE_ESTIMATED_SUFFIX),
    }


def _tail_slice(rows: Sequence[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], str, bool]:
    """尾盘切片 → (bars, 方法说明, 是否按时间戳精确切分)。"""
    times = [_row_time(row) for row in rows]
    if times and all(t is not None for t in times) and any(t >= TAIL_CUTOFF for t in times):
        return [row for t, row in zip(times, rows) if t >= TAIL_CUTOFF], f"{TAIL_CUTOFF} 后", True
    count = min(TAIL_FALLBACK_BARS, len(rows))
    return list(rows[-count:]), f"最后 {count} 根（时间戳不可用）", False


def intraday_facts(frame: Any, *, as_of: str, freq: str) -> dict[str, Any] | None:
    """分时帧 → 当日承接统计（纯函数）。

    样本不足（少于 2 根）返回 None：**样本不足就不猜**。
    """
    rows = _ordered(_rows_of(frame))
    if len(rows) < 2:
        return None

    closes = [_to_float(row.get("close")) for row in rows]
    opens = [_to_float(row.get("open")) for row in rows]
    highs = [v for v in (_to_float(row.get("high")) for row in rows) if v is not None]
    lows = [v for v in (_to_float(row.get("low")) for row in rows) if v is not None]
    volumes = [_to_float(row.get("volume")) for row in rows]

    priced = [v for v in closes if v is not None]
    last_close = priced[-1] if priced else None
    first_open = next((v for v in opens if v is not None), None)
    if first_open is None:
        first_open = priced[0] if priced else None
    if last_close is None or first_open is None or first_open == 0:
        return None

    high = max(highs) if highs else max(priced)
    low = min(lows) if lows else min(priced)

    total_volume = None
    known_volumes = [v for v in volumes if v is not None]
    if known_volumes:
        total_volume = float(sum(known_volumes))

    tail_rows, tail_method, tail_precise = _tail_slice(rows)
    tail_volumes = [v for v in (_to_float(row.get("volume")) for row in tail_rows) if v is not None]
    tail_volume = float(sum(tail_volumes)) if tail_volumes else None
    tail_closes = [v for v in (_to_float(row.get("close")) for row in tail_rows) if v is not None]
    tail_first = tail_closes[0] if tail_closes else None

    return {
        "bars": len(rows),
        # 相对**当日开盘**：分时帧里没有昨收，拿昨收只能靠日线 —— 不编。
        # 字段名因此写全「相对当日开盘」，避免被读成「当日涨跌幅」。
        "change_pct_vs_open": _round6((last_close - first_open) / first_open * 100.0),
        "last_close": last_close,
        "high": high,
        "low": low,
        "close_position": _round6((last_close - low) / (high - low) if high > low else None),
        "total_volume": total_volume,
        "tail_volume_ratio": _round6(
            tail_volume / total_volume
            if tail_volume is not None and total_volume not in (None, 0.0)
            else None
        ),
        "tail_change_pct": _round6(
            (last_close - tail_first) / tail_first * 100.0
            if tail_first not in (None, 0.0)
            else None
        ),
        "tail_method": tail_method,
        "tail_precise": tail_precise,
        "freq": str(freq),
        "as_of": as_of,
    }


def flow_facts(frame: Any) -> dict[str, Any] | None:
    """资金流帧 → 主力/超大单/大单净流入（**单位：元**，纯函数）。

    单位是这一层的硬约束：内部契约与东财口径一致（元），Tushare 的
    `net_mf_amount` 是**万元**（差 10000 倍）。这里对「误传原始 Tushare 帧」
    做一次显式换算——单位错会让「净流入 1.2 亿」与「净流入 1200 万」张冠李戴。
    """
    rows = _ordered(_rows_of(frame))
    if not rows:
        return None
    row = rows[-1]

    main = _to_float(row.get("main_net_inflow"))
    if main is None:
        raw_amount = _to_float(row.get("net_mf_amount"))
        if raw_amount is None:
            return None
        main = raw_amount * 10_000.0  # 万元 → 元

    return {
        "main_net_inflow": main,
        "super_large_net_inflow": _to_float(row.get("super_large_net_inflow")),
        "large_net_inflow": _to_float(row.get("large_net_inflow")),
        "data_date": str(row.get("date") or "")[:10],
    }


def _bare_code(value: Any) -> str:
    """任意代码表示 → 6 位裸码。

    跨源匹配**只能**用裸码：新浪给 `sh000001`、东财给 `000001`，按字面比会把
    「取到了」判成「没取到」——最隐蔽的一类静默失败。
    """
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:6]


def index_quote_facts(
    frame: Any, *, requested: Sequence[str], as_of: str
) -> dict[str, Any] | None:
    """指数行情帧 → 事实字典（纯函数）。

    没有点位（`price` 缺失/非数）的行直接丢掉：指数点位是这条证据的全部内容，
    缺了它剩下的涨跌幅无处依附。一行都没有 → None（**不返回空帧**）。
    """
    items: list[dict[str, Any]] = []
    for row in _rows_of(frame):
        price = _to_float(row.get("price", row.get("最新价")))
        if price is None:
            continue
        items.append(
            {
                "code": str(row.get("code", row.get("代码")) or ""),
                "name": str(row.get("name", row.get("名称")) or ""),
                "price": price,
                "change_pct": _to_float(row.get("change_pct", row.get("涨跌幅"))),
                "prev_close": _to_float(row.get("prev_close", row.get("昨收"))),
                "quote_date": str(row.get("quote_date") or "")[:10],
                "source": str(row.get("source") or ""),
            }
        )
    if not items:
        return None

    seen = {_bare_code(item["code"]) for item in items}
    missing = [str(code) for code in requested if _bare_code(code) not in seen]
    return {
        "items": items,
        "missing": missing,
        "quote_dates": sorted({item["quote_date"] for item in items if item["quote_date"]}),
        "as_of": as_of,
    }


# ── 证据与结果的公共构造 ──────────────────────────────────────────────────

def _evidence(
    items: Sequence[tuple[str, Any, str]],
    *,
    source_key: str,
    as_of: str,
) -> tuple[EvidenceItem, ...]:
    """(字段, 数值, 单位) 三元组 → 带稳定编号的证据项。

    数值一律 `float()`：校验器只对 `is_numeric` 的证据做量级比对
    （`unit` 必须是 ratio/pct/yuan/count/days 之一），整数或字符串会让
    「数字可溯源」这条检查静默失效。
    """
    out: list[EvidenceItem] = []
    for field, value, unit in items:
        if value is None:
            continue
        out.append(
            EvidenceItem(
                id=f"E{len(out) + 1}",
                field=field,
                value=float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value,
                unit=unit,
                source_key=source_key,
                as_of=as_of,
            )
        )
    return tuple(out)


def _text_evidence(
    evidence: tuple[EvidenceItem, ...],
    *,
    field: str,
    value: str,
    source_key: str,
    as_of: str,
) -> tuple[EvidenceItem, ...]:
    """追加一条文本证据（口径说明、数据时点之类）。编号接在现有证据之后。"""
    return evidence + (
        EvidenceItem(
            id=f"E{len(evidence) + 1}",
            field=field,
            value=value,
            unit="",
            source_key=source_key,
            as_of=as_of,
        ),
    )


def _freshness(source_key: str, *, as_of: str, cutoff: str = "", unavailable: bool = False):
    from .freshness import evaluate_tool_sources

    if unavailable:
        return evaluate_tool_sources(source_keys=(source_key,), as_of=as_of, unavailable=(source_key,))
    if cutoff:
        return evaluate_tool_sources(source_keys=(source_key,), as_of=as_of, cutoffs={source_key: cutoff})
    # 读了但不知道截止日 → unknown。**绝不默认 fresh**（那会把未验证伪装成已确认）。
    return evaluate_tool_sources(source_keys=(source_key,), as_of=as_of)


def _unavailable_result(
    *,
    data: Mapping[str, Any],
    unavailable_name: str,
    reason: str,
    as_of: str,
    source_key: str | None = None,
    freshness: Mapping[str, Any] | None = None,
) -> ToolResult:
    """取不到 / 取数失败的统一形状：声明未覆盖 + 降级 + 不外传原文。

    `freshness` 缺省时按 `source_key` 现算（取不到 = `error` / `fetch_error`）：
    让覆盖率声明与时效声明**同源**，不留「coverage 说没有、freshness 说新鲜」的缝。
    `source_key=None` 表示「本工具声明的是**别的源**的时效，自己没有源可声明」。
    """
    if freshness is not None:
        pool = dict(freshness)
    elif source_key is not None:
        pool = _freshness(source_key, as_of=as_of, unavailable=True)
    else:
        pool = {}
    return ToolResult(
        data=dict(data),
        evidence=(),
        coverage=Coverage(available=(), unavailable=(unavailable_name,)),
        degraded=True,
        degraded_reason=reason,
        as_of=as_of,
        freshness=pool,
    )


# ── 1. 筹码分布 ───────────────────────────────────────────────────────────


def _chip_tool(fetcher: ChipFetcher) -> ToolSpec:
    def chip_distribution_read(symbol: str, as_of: str) -> ToolResult:
        try:
            frame = fetcher(symbol, as_of)
        except Exception as exc:  # noqa: BLE001 — 失败降级，不抛给模型
            return _unavailable_result(
                data={"symbol": symbol, "as_of": as_of, "found": False, "error": type(exc).__name__},
                source_key=SOURCE_CHIP,
                unavailable_name=UNAVAILABLE_CHIP,
                reason=f"筹码分布取数失败（{type(exc).__name__}）",
                as_of=as_of,
            )

        facts = chip_facts(frame)
        if facts is None:
            return _unavailable_result(
                data={"symbol": symbol, "as_of": as_of, "found": False},
                source_key=SOURCE_CHIP,
                unavailable_name=UNAVAILABLE_CHIP,
                reason="未取到该标的的筹码分布（东财接口封锁 / 无 Tushare 凭证 / 日线不足时自算路径同样拿不到）",
                as_of=as_of,
            )

        data_date = facts["data_date"] or as_of
        evidence = _evidence(
            (
                ("获利比例", facts["profit_ratio"], "ratio"),
                ("平均成本", facts["avg_cost"], "yuan"),
                ("90%筹码集中度", facts["concentration_90"], "ratio"),
                ("70%筹码集中度", facts["concentration_70"], "ratio"),
            ),
            source_key=SOURCE_CHIP,
            as_of=data_date,
        )
        # 估算口径是**代码写死的一条事实**：模型可以不复述，但它一定在证据里，
        # 于是"这是估计"不可能在回答里消失（loop 也会按 estimated_dimensions 渲染）。
        if facts["estimated"]:
            evidence = _text_evidence(
                evidence,
                field="数据口径",
                value=f"{ESTIMATE_NOTE}：本行由日线与换手率自算（source={facts['source']}）",
                source_key=SOURCE_CHIP,
                as_of=data_date,
            )
        evidence = _text_evidence(
            evidence,
            field="数据日期与来源",
            value=f"数据日期 {facts['data_date'] or '未提供'}｜来源 {facts['source'] or '未提供'}",
            source_key=SOURCE_CHIP,
            as_of=data_date,
        )

        return ToolResult(
            data={
                "symbol": symbol,
                "as_of": as_of,
                "found": True,
                "profit_ratio": facts["profit_ratio"],
                "avg_cost": facts["avg_cost"],
                "concentration_90": facts["concentration_90"],
                "concentration_70": facts["concentration_70"],
                "source": facts["source"],
                "data_date": facts["data_date"],
                "estimated": facts["estimated"],
                # 让编排层用**代码**渲染「模型估计」一行，而不是指望模型复述
                "estimated_dimensions": ["筹码分布"] if facts["estimated"] else [],
            },
            evidence=evidence,
            coverage=Coverage(available=(SOURCE_CHIP,), unavailable=()),
            as_of=as_of,
            freshness=_freshness(SOURCE_CHIP, as_of=as_of, cutoff=facts["data_date"]),
        )

    return ToolSpec(
        name="chip_distribution_read",
        title="筹码分布",
        description=(
            "读取某只 A 股的筹码分布事实：获利比例（0-1 比率）、平均成本（元）、"
            "90%/70% 筹码集中度（0-1 比率）与数据日期。用于回答「获利盘多少」"
            "「平均成本在哪」「筹码集中还是分散」。"
            "来源为自算路径时（source 以 model_estimate 结尾）证据会明确标注"
            "「模型估计，非交易所原始数据」；取不到时声明未覆盖，不返回空数据。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "A 股代码，如 600519.SH"},
                "as_of": {"type": "string", "description": "交易日 YYYY-MM-DD（取该日及之前的最近一行）"},
            },
            "required": ["symbol", "as_of"],
        },
        output_contract=CHIP_CONTRACT,
        handler=chip_distribution_read,
        cost="medium",
        plan_layer=PLAN_LAYER_EVIDENCE,
        # 自算路径要拉 400 天日线再跑分布算法，15s 的预算会让这条路径永远超时。
        latency_budget_ms=30000,
        freshness_sources=(SOURCE_CHIP,),
    )


# ── 2. 分时（当日承接） ───────────────────────────────────────────────────


def _intraday_tool(fetcher: IntradayFetcher) -> ToolSpec:
    def intraday_bars_read(symbol: str, as_of: str, freq: str = "5") -> ToolResult:
        try:
            frame = fetcher(symbol, as_of, str(freq))
        except Exception as exc:  # noqa: BLE001
            return _unavailable_result(
                data={"symbol": symbol, "day": as_of, "found": False, "error": type(exc).__name__},
                source_key=SOURCE_INTRADAY,
                unavailable_name=UNAVAILABLE_INTRADAY,
                reason=f"分时数据取数失败（{type(exc).__name__}）",
                as_of=as_of,
            )

        facts = intraday_facts(frame, as_of=as_of, freq=str(freq))
        if facts is None:
            return _unavailable_result(
                data={"symbol": symbol, "day": as_of, "freq": str(freq), "found": False},
                source_key=SOURCE_INTRADAY,
                unavailable_name=UNAVAILABLE_INTRADAY,
                reason="该日分时数据未取到（可能非交易日，或数据源不覆盖该日分钟线）",
                as_of=as_of,
            )

        tail_ratio_field = "尾盘量占比" if facts["tail_precise"] else "末段量占比"
        tail_change_field = "尾盘涨跌幅" if facts["tail_precise"] else "末段涨跌幅"
        evidence = _evidence(
            (
                ("收盘价", facts["last_close"], "yuan"),
                ("相对当日开盘涨跌幅", facts["change_pct_vs_open"], "pct"),
                ("当日最高", facts["high"], "yuan"),
                ("当日最低", facts["low"], "yuan"),
                ("收盘位于当日区间位置", facts["close_position"], "ratio"),
                (tail_ratio_field, facts["tail_volume_ratio"], "ratio"),
                (tail_change_field, facts["tail_change_pct"], "pct"),
                ("分时根数", facts["bars"], "count"),
            ),
            source_key=SOURCE_INTRADAY,
            as_of=as_of,
        )
        evidence = _text_evidence(
            evidence,
            field="数据时点与粒度",
            value=(
                f"{as_of}｜频率 {facts['freq']} 分钟｜根数 {facts['bars']}｜"
                f"尾盘口径 {facts['tail_method']}"
            ),
            source_key=SOURCE_INTRADAY,
            as_of=as_of,
        )

        # 时间戳不可用时「尾盘」只是最后几根 bar 的代理，必须声明出来：
        # 把退化口径讲成精确的尾盘统计，等于给结论加了一层不存在的精度。
        unavailable = () if facts["tail_precise"] else ("尾盘分段（时间戳缺失）",)
        return ToolResult(
            data={"symbol": symbol, "day": as_of, "found": True, **facts},
            evidence=evidence,
            coverage=Coverage(available=(SOURCE_INTRADAY,), unavailable=unavailable),
            degraded=bool(unavailable),
            degraded_reason=(
                f"分时数据无可用时间戳，尾盘统计按{facts['tail_method']}计算" if unavailable else None
            ),
            as_of=as_of,
            freshness=_freshness(SOURCE_INTRADAY, as_of=as_of, cutoff=as_of),
        )

    return ToolSpec(
        name="intraday_bars_read",
        title="当日分时承接",
        description=(
            "读取某只 A 股**某一交易日**的分时（默认 5 分钟）承接统计：收盘价、"
            "相对当日开盘的涨跌幅、当日最高/最低、收盘在当日区间的位置、"
            "尾盘量占比与尾盘涨跌幅。用于回答「今天跌的时候有没有人接」"
            "「尾盘是砸还是拉」。分时帧里没有昨收，因此涨跌幅口径明确写为"
            "「相对当日开盘」；取不到时声明未覆盖（可能非交易日或该源不覆盖历史分钟线）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "A 股代码，如 600519.SH"},
                "as_of": {"type": "string", "description": "交易日 YYYY-MM-DD"},
                "freq": {"type": "string", "description": "分钟频率：1/5/15/30/60，默认 5"},
            },
            "required": ["symbol", "as_of"],
        },
        output_contract=INTRADAY_CONTRACT,
        handler=intraday_bars_read,
        cost="medium",
        plan_layer=PLAN_LAYER_EVIDENCE,
        # 新浪兜底返回的是**约两个月**的分钟线（近 2000 行）再本地过滤，
        # 预算必须覆盖这次传输，否则正常的兜底路径会被自己的超时保护判成失败。
        latency_budget_ms=30000,
        freshness_sources=(SOURCE_INTRADAY,),
    )


# ── 3. 资金流 ─────────────────────────────────────────────────────────────


def _flow_tool(fetcher: FlowFetcher) -> ToolSpec:
    def capital_flow_read(symbol: str, as_of: str) -> ToolResult:
        try:
            frame = fetcher(symbol, as_of)
        except Exception as exc:  # noqa: BLE001
            return _unavailable_result(
                data={"symbol": symbol, "as_of": as_of, "found": False, "error": type(exc).__name__},
                source_key=SOURCE_FLOW,
                unavailable_name=UNAVAILABLE_FLOW,
                reason=f"资金流取数失败（{type(exc).__name__}）",
                as_of=as_of,
            )

        facts = flow_facts(frame)
        if facts is None:
            return _unavailable_result(
                data={"symbol": symbol, "as_of": as_of, "found": False},
                source_key=SOURCE_FLOW,
                unavailable_name=UNAVAILABLE_FLOW,
                reason="未取到该标的的资金流（东财通道封锁且无可用 Tushare 凭证）",
                as_of=as_of,
            )

        data_date = facts["data_date"] or as_of
        # 单位**必须**是元（与东财口径一致）：Tushare 的 net_mf_amount 是万元，
        # 取数层已换算；这里只负责把「元」明写进证据，让校验器能核对量级。
        evidence = _evidence(
            (
                ("主力净流入", facts["main_net_inflow"], "yuan"),
                ("超大单净流入", facts["super_large_net_inflow"], "yuan"),
                ("大单净流入", facts["large_net_inflow"], "yuan"),
            ),
            source_key=SOURCE_FLOW,
            as_of=data_date,
        )
        evidence = _text_evidence(
            evidence,
            field="数据日期与单位",
            value=f"数据日期 {facts['data_date'] or '未提供'}｜金额单位 元（负数表示净流出）",
            source_key=SOURCE_FLOW,
            as_of=data_date,
        )

        return ToolResult(
            data={"symbol": symbol, "as_of": as_of, "found": True, "unit": "yuan", **facts},
            evidence=evidence,
            coverage=Coverage(available=(SOURCE_FLOW,), unavailable=()),
            as_of=as_of,
            freshness=_freshness(SOURCE_FLOW, as_of=as_of, cutoff=facts["data_date"]),
        )

    return ToolSpec(
        name="capital_flow_read",
        title="个股资金流",
        description=(
            "读取某只 A 股的资金流事实：主力净流入、超大单净流入、大单净流入，"
            "**单位一律为元**（负数表示净流出），并给出数据日期。"
            "用于回答「今天主力净流入多少」「资金在进还是在出」。"
            "取不到时声明未覆盖，不返回 0 冒充「无流入」。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "A 股代码，如 600519.SH"},
                "as_of": {"type": "string", "description": "交易日 YYYY-MM-DD（取该日及之前的最近一行）"},
            },
            "required": ["symbol", "as_of"],
        },
        output_contract=FLOW_CONTRACT,
        handler=capital_flow_read,
        cost="medium",
        plan_layer=PLAN_LAYER_EVIDENCE,
        # 三级通道回退（东财 → 东财直连+代理 → Tushare），冷启动可能都在等超时
        latency_budget_ms=30000,
        freshness_sources=(SOURCE_FLOW,),
    )


# ── 4. 自选清单 ───────────────────────────────────────────────────────────


def _watchlist_tool(port: WatchlistPort) -> ToolSpec:
    def watchlist_read(limit: int = DEFAULT_WATCHLIST_ITEMS, as_of: str = "") -> ToolResult:
        # 不能写 `limit or DEFAULT`：limit=0 会被 falsy 换成默认值（本仓库踩过四次）
        requested = DEFAULT_WATCHLIST_ITEMS if limit is None else int(limit)
        capped = max(1, min(requested, MAX_WATCHLIST_ITEMS))
        try:
            rows = [dict(row) for row in (port.list_items(limit=capped) or []) if isinstance(row, Mapping)]
        except Exception as exc:  # noqa: BLE001 — 读不到就如实声明，不抛给模型
            return _unavailable_result(
                data={"items": [], "count": 0, "error": type(exc).__name__},
                source_key=SOURCE_WATCHLIST,
                unavailable_name=UNAVAILABLE_WATCHLIST,
                reason=f"自选清单读取失败（{type(exc).__name__}）",
                as_of=as_of,
            )

        rows = rows[:capped]
        evidence = _evidence(
            (("自选股数量", len(rows), "count"),),
            source_key=SOURCE_WATCHLIST,
            as_of=as_of,
        )
        for index, row in enumerate(rows, start=1):
            parts = [str(row.get("symbol") or "未提供")]
            name = row.get("name") or row.get("security_name")
            if name:
                parts.append(f"（{name}）")
            created = str(row.get("created_at") or "")[:10]
            if created:
                parts.append(f"｜加入时间 {created}")
            if row.get("has_scheduled"):
                parts.append("｜已设定时分析")
            evidence = evidence + (
                EvidenceItem(
                    id=f"E{len(evidence) + 1}",
                    field=f"自选股{index}",
                    value="".join(parts),
                    unit="",
                    source_key=SOURCE_WATCHLIST,
                    as_of=created or as_of,
                ),
            )

        # 空清单是**事实**（用户问「我的自选有什么」时，"没有"就是答案），
        # 与「读不到」必须分开：后者走上面的降级分支。数量 0 本身进证据，
        # 使回答里的「0 只」可溯源。
        data = {
            "items": rows,
            "count": len(rows),
            "requested": requested,
            "truncated": len(rows) >= capped,
        }
        if not rows:
            data["note"] = "自选清单为空"
        return ToolResult(
            data=data,
            evidence=evidence,
            coverage=Coverage(available=(SOURCE_WATCHLIST,), unavailable=()),
            as_of=as_of,
            freshness=_freshness(SOURCE_WATCHLIST, as_of=as_of),
        )

    return ToolSpec(
        name="watchlist_read",
        title="我的自选清单",
        description=(
            "读取**当前用户**的自选股清单（代码、可选名称、加入时间、是否已设定时分析）"
            "与数量。用于回答「我的自选里有什么」「我加了几只」。"
            "用户维度由服务端绑定，工具参数里没有 user_id；读取失败时声明未覆盖，"
            "空清单则作为事实返回「0 只」。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"返回条数，1-{MAX_WATCHLIST_ITEMS}，默认 {DEFAULT_WATCHLIST_ITEMS}",
                },
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
            },
            "required": [],
        },
        output_contract=WATCHLIST_CONTRACT,
        handler=watchlist_read,
        cost="cheap",
        plan_layer=PLAN_LAYER_EVIDENCE,
        latency_budget_ms=8000,
        freshness_sources=(SOURCE_WATCHLIST,),
    )


# ── 5. 公告 ───────────────────────────────────────────────────────────────


def _announcements_tool(port: AnnouncementsPort) -> ToolSpec:
    def announcements_read(
        symbol: str, as_of: str, lookback_days: int = 7, limit: int = DEFAULT_ANNOUNCEMENTS
    ) -> ToolResult:
        requested = DEFAULT_ANNOUNCEMENTS if limit is None else int(limit)
        capped = max(1, min(requested, MAX_ANNOUNCEMENTS))
        window = 7 if lookback_days is None else int(lookback_days)
        window = max(1, min(window, 120))

        start = ""
        try:
            start = (_date.fromisoformat(as_of[:10]) - timedelta(days=window)).isoformat()
        except (TypeError, ValueError):
            # 基准日不可解析 → 不设下界（**不猜**今天；工具不接受环境时钟）
            start = ""

        try:
            rows = [
                dict(row)
                for row in (port.list_announcements(symbol=symbol, start=start or as_of, end=as_of, limit=capped) or [])
                if isinstance(row, Mapping)
            ]
        except Exception as exc:  # noqa: BLE001
            return _unavailable_result(
                data={"symbol": symbol, "items": [], "count": 0, "error": type(exc).__name__},
                source_key=SOURCE_ANNOUNCEMENTS,
                unavailable_name=UNAVAILABLE_ANNOUNCEMENTS,
                reason=f"公告取数失败（{type(exc).__name__}）",
                as_of=as_of,
            )

        rows = rows[:capped]
        if not rows:
            return _unavailable_result(
                data={"symbol": symbol, "items": [], "count": 0, "start": start, "as_of": as_of},
                source_key=SOURCE_ANNOUNCEMENTS,
                unavailable_name=UNAVAILABLE_ANNOUNCEMENTS,
                reason=(
                    f"窗口 {start or '不限'} ~ {as_of or '不限'} 内未取到公告，该维度未覆盖"
                    "（「没拉到」不等于「没有公告」）"
                ),
                as_of=as_of,
            )

        evidence = _evidence(
            (("公告数量", len(rows), "count"),),
            source_key=SOURCE_ANNOUNCEMENTS,
            as_of=as_of,
        )
        for index, row in enumerate(rows, start=1):
            # 发布日期即该条证据的 as_of：回答写「9 月 18 日公告了减持」时，
            # 校验器就是靠它核对日期断言的。
            published = str(row.get("published_at") or "")[:10]
            parts = [published or "日期未提供"]
            kind = row.get("type") or row.get("category")
            if kind:
                parts.append(f"｜类型 {kind}")
            parts.append(f"｜{row.get('title') or '标题未提供'}")
            if row.get("source"):
                parts.append(f"｜来源 {row['source']}")
            evidence = evidence + (
                EvidenceItem(
                    id=f"E{len(evidence) + 1}",
                    field=f"公告{index}",
                    value="".join(parts),
                    unit="",
                    source_key=SOURCE_ANNOUNCEMENTS,
                    as_of=published or as_of,
                ),
            )

        return ToolResult(
            data={
                "symbol": symbol,
                "as_of": as_of,
                "start": start,
                "count": len(rows),
                "items": rows,
                "truncated": len(rows) >= capped,
            },
            evidence=evidence,
            coverage=Coverage(available=(SOURCE_ANNOUNCEMENTS,), unavailable=()),
            as_of=as_of,
            freshness=_freshness(SOURCE_ANNOUNCEMENTS, as_of=as_of),
        )

    return ToolSpec(
        name="announcements_read",
        title="个股公告",
        description=(
            "读取某只 A 股在回看窗口内的**公告**（标题、披露日期、类型、来源）。"
            "用于回答「最近有什么公告」「有没有减持/业绩预告」。"
            "公告是事实来源，不接受模型从价格行为倒推；"
            "窗口内没有拉到时会声明未覆盖，而不是说成「没有公告」。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "A 股代码，如 600519.SH"},
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
                "lookback_days": {"type": "integer", "description": "回看自然日数，1-120，默认 7"},
                "limit": {"type": "integer", "description": f"返回条数，1-{MAX_ANNOUNCEMENTS}，默认 {DEFAULT_ANNOUNCEMENTS}"},
            },
            "required": ["symbol", "as_of"],
        },
        output_contract=ANNOUNCEMENTS_CONTRACT,
        handler=announcements_read,
        cost="cheap",
        plan_layer=PLAN_LAYER_EVIDENCE,
        # 实测：东财 1.1–3.4s、巨潮 0.4–3.2s（首次还要拉一次 orgId 表约 1s）。
        # 最坏路径 = 东财超时后巨潮接手 ≈ 8s，早先的 8000ms 预算会把这条**正常**
        # 的兜底路径判成超时降级——预算必须覆盖兜底，而不是只覆盖最快的源。
        latency_budget_ms=20000,
        freshness_sources=(SOURCE_ANNOUNCEMENTS,),
    )


# ── 6. 指数行情 ───────────────────────────────────────────────────────────


def _index_tool(fetcher: IndexFetcher) -> ToolSpec:
    def index_quote_read(as_of: str = "", codes: Sequence[str] | None = None) -> ToolResult:
        # `codes is None` 才用默认集：**不写 `codes or DEFAULT`**——显式传空列表
        # 是「什么都不查」，被 falsy 换成默认集就会去查用户没问的三个指数。
        raw = DEFAULT_INDEX_CODES if codes is None else tuple(codes)
        requested: list[str] = []
        for item in raw:
            text = str(item).strip()
            if text and text not in requested:
                requested.append(text)
        requested = requested[:MAX_INDEX_CODES]

        if not requested:
            return _unavailable_result(
                data={"as_of": as_of, "codes": [], "count": 0, "found": False},
                source_key=SOURCE_INDEX,
                unavailable_name=UNAVAILABLE_INDEX,
                reason="未指定要查询的指数代码，本次未查询任何指数的行情",
                as_of=as_of,
            )

        try:
            frame = fetcher(tuple(requested))
        except Exception as exc:  # noqa: BLE001 — 失败降级，不抛给模型
            return _unavailable_result(
                data={"as_of": as_of, "codes": requested, "found": False, "error": type(exc).__name__},
                source_key=SOURCE_INDEX,
                unavailable_name=UNAVAILABLE_INDEX,
                reason=f"指数行情取数失败（{type(exc).__name__}）",
                as_of=as_of,
            )

        facts = index_quote_facts(frame, requested=requested, as_of=as_of)
        if facts is None:
            return _unavailable_result(
                data={"as_of": as_of, "codes": requested, "count": 0, "found": False},
                source_key=SOURCE_INDEX,
                unavailable_name=UNAVAILABLE_INDEX,
                reason="未取到所请求指数的行情（数据源不覆盖该代码，或快照本轮不可用）",
                as_of=as_of,
            )

        items = facts["items"]
        quote_dates = facts["quote_dates"]
        # 快照日期唯一时，**所有**证据都以它为准：连「指数数量」这条聚合事实也
        # 不该挂着一个数据并不属于的日期（否则引用它就能给历史断言背书）。
        single_date = quote_dates[0] if len(quote_dates) == 1 else ""
        snapshot_as_of = single_date or as_of
        evidence = _evidence(
            (("指数数量", len(items), "count"),),
            source_key=SOURCE_INDEX,
            as_of=snapshot_as_of,
        )
        for item in items:
            # 名称写进 field：点位与涨跌幅各自成一条**数值**证据才有量纲可校验
            # （合成一条文本会让校验器失去标量，涨跌幅的对错就无从核对）。
            label = f"{item['name'] or item['code']}（{item['code']}）"
            item_as_of = item["quote_date"] or as_of
            evidence = evidence + (
                EvidenceItem(
                    id=f"E{len(evidence) + 1}",
                    field=f"{label}点位",
                    value=float(item["price"]),
                    unit="",  # 指数点位无量纲：写 yuan 会把「3962 点」读成 3962 元
                    source_key=SOURCE_INDEX,
                    as_of=item_as_of,
                ),
            )
            if item["change_pct"] is not None:
                evidence = evidence + (
                    EvidenceItem(
                        id=f"E{len(evidence) + 1}",
                        field=f"{label}涨跌幅",
                        value=float(item["change_pct"]),
                        unit="pct",
                        source_key=SOURCE_INDEX,
                        as_of=item_as_of,
                    ),
                )

        quote_dates = facts["quote_dates"]
        sources = sorted({item["source"] for item in items if item["source"]})
        evidence = _text_evidence(
            evidence,
            field="数据时点与来源",
            value=(
                f"快照日期 {'、'.join(quote_dates) if quote_dates else '源未提供'}"
                f"｜来源 {'、'.join(sources) if sources else '未提供'}"
                "｜涨跌幅单位 %（正数为涨）"
            ),
            source_key=SOURCE_INDEX,
            as_of=quote_dates[-1] if quote_dates else as_of,
        )

        # 请求了却没回来的代码逐条声明未覆盖：「没取到」不能读成「这个指数没动」。
        unavailable = tuple(f"{SOURCE_INDEX}（{code} 未返回）" for code in facts["missing"])
        # 基准日与快照日期不一致时写明白：本工具给的是**实时快照**，历史上某一天的
        # 收盘点位它拿不到。证据的 as_of 已经记的是快照日期（校验器据此拦住把今天
        # 的数字安到历史日期上的断言），这里再给一句人类可读的说明。
        stale_reference_note = (
            f"本次为实时快照（{single_date}），不含基准日 {as_of} 当日的收盘数据"
            if single_date and as_of and single_date != as_of
            else ""
        )
        return ToolResult(
            data={
                "as_of": as_of,
                "requested": requested,
                "count": len(items),
                "items": items,
                "missing": facts["missing"],
                "quote_date": single_date or None,
                "quote_date_matches_as_of": (single_date == as_of) if single_date else None,
                "source": "、".join(sources),
                **({"note": stale_reference_note} if stale_reference_note else {}),
            },
            evidence=evidence,
            coverage=Coverage(available=(SOURCE_INDEX,), unavailable=unavailable),
            degraded=bool(unavailable),
            degraded_reason=(
                f"请求的指数中有 {len(unavailable)} 个未返回（{'、'.join(facts['missing'])}），"
                "这些指数本维度未覆盖"
                if unavailable
                else None
            ),
            as_of=as_of,
            # 快照日期不明时**不传 cutoff** → `unknown`，绝不默认 fresh：
            # 把「不知道是哪天的报价」标成「最新」，正是本平台要消灭的伪造新鲜度。
            freshness=(
                _freshness(SOURCE_INDEX, as_of=as_of, cutoff=single_date)
                if single_date
                else _freshness(SOURCE_INDEX, as_of=as_of)
            ),
        )

    return ToolSpec(
        name="index_quote_read",
        title="指数行情（点位与涨跌幅）",
        description=(
            "读取主要指数（默认上证指数/深证成指/创业板指）的**点位**与**涨跌幅**"
            "（百分数，正数为涨），并声明快照日期与来源。用于回答「今天大盘怎么样」"
            "「创业板跌了多少」「指数在什么位置」。"
            "指数点位无量纲、涨跌幅单位为 pct；取不到时声明未覆盖，"
            "请求了但未返回的指数逐条列出，不返回 0 点冒充「没动」。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "as_of": {"type": "string", "description": "基准日 YYYY-MM-DD"},
                "codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "指数代码，形如 sh000001 / sz399001 / sz399006；"
                        f"留空则查默认 {'、'.join(DEFAULT_INDEX_CODES)}"
                    ),
                },
            },
            "required": ["as_of"],
        },
        output_contract=INDEX_CONTRACT,
        handler=index_quote_read,
        cost="cheap",
        plan_layer=PLAN_LAYER_EVIDENCE,
        # 直连通道实测 0.6s，但兜底要经 akshare 的两个快照接口（实测 7.8s / 38.8s），
        # 预算必须覆盖兜底路径，否则正常的换源会被自己的超时保护判成失败。
        latency_budget_ms=30000,
        freshness_sources=(SOURCE_INDEX,),
    )


# ── 7. 数据时效 ───────────────────────────────────────────────────────────


def _freshness_tool(probe: FreshnessProbePort) -> ToolSpec:
    def data_freshness_check(
        as_of: str, sources: Sequence[str] | None = None, symbol: str | None = None
    ) -> ToolResult:
        from .freshness import evaluate_tool_sources

        # `sources is None` 才用默认集：**不写 `sources or DEFAULT`**——显式传空列表
        # 是「什么都不查」，被 falsy 换成默认集会查出一堆用户没问的源（本仓库
        # 已经四次踩过 `x or default` 这类替换）。
        selected = DEFAULT_FRESHNESS_SOURCES if sources is None else tuple(sources)
        requested = [str(item).strip() for item in selected if str(item).strip()]
        truncated = len(requested) > MAX_FRESHNESS_SOURCES
        keys = requested[:MAX_FRESHNESS_SOURCES]

        if not keys:
            return _unavailable_result(
                data={"as_of": as_of, "sources": [], "requested": 0, "checked": 0},
                unavailable_name=UNAVAILABLE_FRESHNESS,
                reason="未指定要查询的数据源，本次未查询任何源的时效",
                as_of=as_of,
            )

        try:
            facts = {str(k): dict(v) for k, v in (probe.source_facts(source_keys=tuple(keys), symbol=symbol) or {}).items()}
        except Exception as exc:  # noqa: BLE001 — 探针失败=整次查询失败，如实降级
            return _unavailable_result(
                data={"as_of": as_of, "sources": [], "error": type(exc).__name__},
                unavailable_name=UNAVAILABLE_FRESHNESS,
                reason=f"数据时效查询失败（{type(exc).__name__}）",
                as_of=as_of,
            )

        try:
            from tradingagents.dataflows.freshness.contract import load_contract

            contract = dict(load_contract() or {})
        except Exception:  # noqa: BLE001 — 契约读不到就退化为「无元数据」，不编
            contract = {}

        evidence: tuple[EvidenceItem, ...] = ()
        rows: list[dict[str, Any]] = []
        available: list[str] = []
        unavailable: list[str] = []
        unregistered: list[str] = []

        for key in keys:
            fact = facts.get(key) or {}
            cutoff = str(fact.get("cutoff") or "")[:10]
            origin = str(fact.get("origin") or "")
            entry = contract.get(key) or {}
            category = str(entry.get("category") or "未登记")
            criticality = str(entry.get("criticality") or "未登记")
            declared_unavailable = bool(fact.get("unavailable"))

            if not entry:
                unregistered.append(key)

            pool = evaluate_tool_sources(
                source_keys=(key,),
                as_of=as_of,
                unavailable=(key,) if declared_unavailable else (),
                cutoffs={key: cutoff} if cutoff else None,
            )
            status = str(pool[key].get("status") or "unknown")
            label = _STATUS_LABELS.get(status, status)
            lag_days = None
            try:
                if cutoff:
                    lag_days = (_date.fromisoformat(as_of[:10]) - _date.fromisoformat(cutoff)).days
            except (TypeError, ValueError):
                lag_days = None

            if not entry:
                unavailable.append(f"{key}（未登记在时效契约）")
            elif declared_unavailable:
                unavailable.append(f"{key}（本轮取数失败）")
            elif not cutoff:
                # 截止日未知 = 无法回答「这数据什么时候的」。这是未覆盖，不是 fresh。
                unavailable.append(f"{key}（时效未知）")
            else:
                available.append(key)

            evidence = evidence + (
                EvidenceItem(
                    id=f"E{len(evidence) + 1}",
                    field=f"时效：{key}",
                    value=(
                        f"状态 {label}｜截止日 {cutoff or '未知'}｜来源 {origin or '未提供'}｜"
                        f"契约分类 {category}（{criticality}）｜判定基准日 {as_of or '未提供'}"
                    ),
                    unit="",
                    source_key=key,
                    as_of=cutoff or as_of,
                ),
            )
            if lag_days is not None:
                evidence = evidence + (
                    EvidenceItem(
                        id=f"E{len(evidence) + 1}",
                        field=f"{key} 滞后天数",
                        value=float(lag_days),
                        unit="days",
                        source_key=key,
                        as_of=cutoff,
                    ),
                )

            rows.append(
                {
                    "source_key": key,
                    "status": status,
                    "status_label": label,
                    "cutoff": cutoff or None,
                    "origin": origin,
                    "category": category,
                    "criticality": criticality,
                    "grace_minutes": entry.get("grace_minutes"),
                    "stale": status == "stale",
                    "lag_days": lag_days,
                    "registered": bool(entry),
                }
            )

        if truncated:
            unavailable.append(f"时效查询（仅查前 {MAX_FRESHNESS_SOURCES} 个源）")
        if unregistered:
            data_note = "部分源未登记在时效契约中，无法判定时效"
        else:
            data_note = ""

        reason_parts = []
        unknown = [row["source_key"] for row in rows if row["status"] == "unknown"]
        if unknown:
            reason_parts.append(f"{len(unknown)} 个源的截止日未知（{'、'.join(unknown)}），其时效无法确认")
        if truncated:
            reason_parts.append(f"请求 {len(requested)} 个源，仅查询前 {MAX_FRESHNESS_SOURCES} 个")

        return ToolResult(
            data={
                "as_of": as_of,
                "symbol": symbol,
                "sources": rows,
                "requested": len(requested),
                "checked": len(keys),
                "truncated": truncated,
                "unregistered": unregistered,
                "note": data_note,
            },
            evidence=evidence,
            coverage=Coverage(available=tuple(available), unavailable=tuple(unavailable)),
            degraded=bool(unavailable),
            degraded_reason="；".join(reason_parts) if reason_parts else None,
            as_of=as_of,
            # 本工具说的是**别的源**的时效，自己不再声明一次 freshness
            freshness={},
        )

    return ToolSpec(
        name="data_freshness_check",
        title="数据时效（截止日/来源/是否过期）",
        description=(
            "查询若干数据源的**时效事实**：数据截止日、来源出处、契约分类与关键度、"
            "以及相对基准日是否过期（滞后天数）。用于回答「这数据什么时候的」"
            "「这份日线是今天的吗」。截止日无法确证时明确说「未知」——"
            "**不得**用今天日期兜底，那会把「未验证」伪装成「已确认最新」。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "as_of": {"type": "string", "description": "判定基准日 YYYY-MM-DD"},
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "数据源键列表，如 stock_data / intraday_bars / chip_distribution / "
                        "individual_fund_flow / symbol_news；留空查核心几个"
                    ),
                },
                "symbol": {"type": "string", "description": "标的代码（可选，部分源的截止日按标的查）"},
            },
            "required": ["as_of"],
        },
        output_contract=FRESHNESS_CONTRACT,
        handler=data_freshness_check,
        cost="cheap",
        plan_layer=PLAN_LAYER_EVIDENCE,
        latency_budget_ms=8000,
        freshness_sources=(),
    )


# ── 装配 ──────────────────────────────────────────────────────────────────


def make_evidence_tools(
    *,
    fetchers: MarketEvidenceFetchers | None = None,
    watchlist_port: WatchlistPort | None = None,
    announcements_port: AnnouncementsPort | None = None,
    freshness_port: FreshnessProbePort | None = None,
    index_fetcher: IndexFetcher | None = None,
) -> tuple[ToolSpec, ...]:
    """按**已注入的依赖**逐个装配；没注入的工具**不返回**（= 不注册）。

    这条纪律的意义不是「测试方便」，而是不留「在册但一调就炸」的条目：
    Agent 会把预算花在必然失败的调用上，而用户看到的是一次"完整的"取数过程。

    关于 5 号工具（公告）的如实说明：取数实现在
    `tradingagents/dataflows/providers/cn_announcement_index_data.py`
    （东财按个股公告 → 巨潮兜底，实测可用），组合根传入
    `announcements_port=SymbolAnnouncementsPort()` 后它才在册。**不用新闻冒充公告**：
    新闻是别人的解读，公告是标的自己的披露，两者不能互相替代。

    6 号工具（指数行情）同理：取数实现是同一个 provider 的 `fetch_index_quotes`
    （新浪直连 → 新浪 akshare → 东财 akshare），组合根传入 `index_fetcher=` 才在册。
    """
    specs: list[ToolSpec] = []

    market = fetchers or MarketEvidenceFetchers()
    if market.chip is not None:
        specs.append(_chip_tool(market.chip))
    if market.intraday is not None:
        specs.append(_intraday_tool(market.intraday))
    if market.flow is not None:
        specs.append(_flow_tool(market.flow))
    if watchlist_port is not None:
        specs.append(_watchlist_tool(watchlist_port))
    if announcements_port is not None:
        specs.append(_announcements_tool(announcements_port))
    if index_fetcher is not None:
        specs.append(_index_tool(index_fetcher))
    if freshness_port is not None:
        specs.append(_freshness_tool(freshness_port))

    return tuple(specs)


__all__ = [
    "make_evidence_tools",
    "default_market_fetchers",
    "MarketEvidenceFetchers",
    "chip_facts",
    "intraday_facts",
    "flow_facts",
    "index_quote_facts",
    "DEFAULT_INDEX_CODES",
    "MAX_INDEX_CODES",
    "SOURCE_INDEX",
    "UNAVAILABLE_INDEX",
    "WatchlistPort",
    "AnnouncementsPort",
    "IndexFetcher",
    "FreshnessProbePort",
    "ESTIMATE_NOTE",
]

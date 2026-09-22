"""既有 19 个分析师数据工具中 **11 个在工具面 + 8 个已退役**的实现来源（design D1/D2）。

## 收敛前的状态

`agents/utils/*_tools.py` 里 19 个 `@tool` 各自写着
`return route_to_vendor("<name>", <args>)`——即 19 处独立调用点，参数顺序散落在
各文件里。一旦对话层或 MCP 需要同一能力，就会出现第二份定义；两份定义的参数
顺序或默认值一旦分叉，表现为「同一个名字在不同入口取到不同数据」，极难排查。

## 收敛方式：声明表 + 单次实现

`DATA_TOOL_DECLS` 是工具面（`make_data_tools()` → `ToolSpec`）的唯一来源
（名字、参数顺序、JSON Schema、来源类别）。`call_vendor` 是唯一的调用点。
既有 `@tool` 只做一行委托，签名与 docstring 保持原样——**分析图的行为逐字不变**。

## 8 个工具**已从工具面退役**（不是遗漏）

`get_stock_data` / `get_cashflow` / `get_indicators` / `get_lhb_detail` /
`get_insider_transactions` / `get_individual_fund_flow` / `get_board_cons` /
`get_news` 这 8 个已**不再注册进工具面**，理由是同一条：

1. 它们返回**预格式化的中文散文**且 `evidence=()`，于是取到的任何数字都**无法被
   回答层的机械校验器引用**——「没有证据就没有回答」，注册它们等于给 Agent 一个
   永远出不了结论的入口。
2. 41 轮真实对话回放里，规划层**一次都没有**选中过它们：面对同一问句，它总是
   落到结构化更好的落点（资金流 → `capital_flow_read`、新闻 → `data_symbol_news`、
   指数 → `index_quote_read` 等）。留着 = 增加选择噪声而不增加能力。

**它们的声明为何仍在本模块**：两个消费者的边界不同。
`agents/utils/*_tools.py` 的 8 个 `@tool`（LangGraph 7 分析师管道）**必须继续可用**，
而它们委托的是 `data_tool_impl(name)` → `_DECL_BY_NAME`。因此这 8 条声明移入
`RETIRED_DATA_TOOL_DECLS`：**工具面不再有它们，分析图逐字不变**。
不要因为它们出现在本文件里就以为可以重新注册——重新注册必须先解决「证据」问题。

## 为什么 `structured=False`

这些 vendor 返回预格式化的中文文本（表格/段落），无法拆出可被校验器引用的
标量。诚实做法是显式标注「不可逐值引用」，让回答层在需要引用数字时改用结构化
工具，而不是假装这段文本可以被核对。`data["rendered"]` 是**兼容性渲染**，
不是对外契约——契约是 `data["structured"]` 所声明的能力边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.grounding.types import EvidenceItem

from .freshness import evaluate_tool_sources
from .spec import ToolResult, ToolSpec

VENDOR_NOTE = (
    "legacy prose vendor：返回预格式化文本，单个数值不可引用（not citable）。"
    "若回答需要引用具体数字，请改用这些落点：数值型用 `capital_flow_read`（资金流）/ "
    "`chip_distribution_read`（筹码）/ `data_symbol_news`（新闻）/ `index_quote_read`（指数）；"
    "或返回 JSON、能产出可引用证据的那些（`get_realtime_quotes`、`get_zt_pool` 等）。"
)
# 注：此处曾写「请使用结构化 data_* 工具（kline / news / washout 等）」——
# `kline` 在注册表里**根本不存在**。把不存在的工具写进给模型的提示里，模型会照着
# 编一个名字出来，然后收到 unknown arguments。提示词里只允许出现真名。
# （本轮退役 8 个 vendor 工具时复查了这段：上面点到的名字均为在册工具。）

OUTPUT_CONTRACT = "DATA_PROSE"

_STR = {"type": "string"}
_INT = {"type": "integer"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}

_SYMBOL = {**_STR, "description": "A 股代码，如 600519.SH"}
_TICKER = {**_STR, "description": "标的代码"}
_DATE = {**_STR, "description": "日期 YYYY-MM-DD"}
_START = {**_STR, "description": "开始日期 YYYY-MM-DD"}
_END = {**_STR, "description": "结束日期 YYYY-MM-DD"}

# 从哪个参数取 as_of（用于回答层的日期锚点）。没有日期参数的工具为 None。
_AS_OF_ARGS = ("end_date", "curr_date", "date", "start_date")


@dataclass(frozen=True)
class DataToolDecl:
    """一个数据工具的唯一声明。

    `args` 的顺序**就是** vendor 调用的位置参数顺序——这是委托正确性的前提，
    因此由测试断言它与 schema 的属性顺序一致。
    """

    name: str
    args: tuple[str, ...]
    schema: Mapping[str, Any]
    category: str


def _decl(name: str, args: tuple[str, ...], props: Mapping[str, Any], category: str, required: tuple[str, ...] = ()) -> DataToolDecl:
    return DataToolDecl(
        name=name,
        args=args,
        schema={"type": "object", "properties": dict(props), "required": list(required)},
        category=category,
    )


# ── 工具面（11 个）：`make_data_tools()` 只遍历这一份 ───────────────────────
#
# 19 → 11。退掉的 8 个见下方 `RETIRED_DATA_TOOL_DECLS`（连同退役理由）。
DATA_TOOL_DECLS: tuple[DataToolDecl, ...] = (
    _decl(
        "get_realtime_quotes", ("symbols",),
        {"symbols": {**_STR_LIST, "description": "标的代码列表"}},
        "core_stock_apis", ("symbols",),
    ),
    _decl(
        "get_fundamentals", ("ticker", "curr_date"),
        {"ticker": _TICKER, "curr_date": {**_DATE, "default": None}},
        "fundamental_data", ("ticker",),
    ),
    _decl(
        "get_balance_sheet", ("ticker", "freq", "curr_date"),
        {"ticker": _TICKER, "freq": {**_STR, "default": "quarterly"}, "curr_date": {**_DATE, "default": None}},
        "fundamental_data", ("ticker",),
    ),
    _decl(
        "get_income_statement", ("ticker", "freq", "curr_date"),
        {"ticker": _TICKER, "freq": {**_STR, "default": "quarterly"}, "curr_date": {**_DATE, "default": None}},
        "fundamental_data", ("ticker",),
    ),
    _decl(
        "get_global_news", ("curr_date", "look_back_days", "limit"),
        {
            "curr_date": _DATE,
            "look_back_days": {**_INT, "default": 7},
            "limit": {**_INT, "default": 5},
        },
        "news_data", ("curr_date",),
    ),
    _decl(
        "get_board_spot", ("sector_type",),
        {"sector_type": {**_STR, "default": "industry"}},
        "board_data", (),
    ),
    _decl(
        "get_board_rank", ("sector_type", "period"),
        {"sector_type": {**_STR, "default": "industry"}, "period": {**_STR, "default": "1d"}},
        "board_data", (),
    ),
    _decl(
        "get_board_hist", ("board_name", "sector_type"),
        {"board_name": {**_STR, "description": "板块名称"}, "sector_type": {**_STR, "default": "industry"}},
        "board_data", ("board_name",),
    ),
    _decl("get_board_fund_flow", (), {}, "board_data", ()),
    _decl(
        "get_zt_pool", ("date",),
        {"date": _DATE}, "board_data", ("date",),
    ),
    _decl("get_hot_stocks_xq", (), {}, "board_data", ()),
)


# ── 已从工具面退役的 8 个（声明保留，仅为分析图委托可解析）─────────────────
#
# **不要因为它们在文件里就重新注册。** 退役理由是它们返回散文且 `evidence=()`，
# 任何数字都无法被校验器引用——「没有证据就没有回答」。41 轮真实对话里规划层
# 从未选中过它们（详见模块 docstring）。
#
# 这份声明的**唯一**用途：`agents/utils/*_tools.py` 的 8 个 `@tool` 通过
# `data_tool_impl(name) → _DECL_BY_NAME` 取参数顺序。删掉这里，分析图会在调用时
# 抛 `KeyError`（边界：**分析图的 8 个 `@tool` 必须继续可用**）。
RETIRED_DATA_TOOL_DECLS: tuple[DataToolDecl, ...] = (
    _decl(
        "get_stock_data", ("symbol", "start_date", "end_date"),
        {"symbol": _SYMBOL, "start_date": _START, "end_date": _END},
        "core_stock_apis", ("symbol", "start_date", "end_date"),
    ),
    _decl(
        "get_indicators", ("symbol", "indicator", "curr_date", "look_back_days"),
        {
            "symbol": _SYMBOL,
            "indicator": {**_STR, "description": "指标名，如 rsi / macd / close_50_sma"},
            "curr_date": _DATE,
            "look_back_days": {**_INT, "default": 30, "description": "回看天数"},
        },
        "technical_indicators", ("symbol", "indicator", "curr_date"),
    ),
    _decl(
        "get_cashflow", ("ticker", "freq", "curr_date"),
        {"ticker": _TICKER, "freq": {**_STR, "default": "quarterly"}, "curr_date": {**_DATE, "default": None}},
        "fundamental_data", ("ticker",),
    ),
    _decl(
        "get_news", ("ticker", "start_date", "end_date"),
        {"ticker": _TICKER, "start_date": _START, "end_date": _END},
        "news_data", ("ticker", "start_date", "end_date"),
    ),
    _decl(
        "get_insider_transactions", ("ticker",),
        {"ticker": _TICKER}, "news_data", ("ticker",),
    ),
    _decl(
        "get_board_cons", ("board_name", "sector_type"),
        {"board_name": {**_STR, "description": "板块名称"}, "sector_type": {**_STR, "default": "industry"}},
        "board_data", ("board_name",),
    ),
    _decl(
        "get_individual_fund_flow", ("symbol",),
        {"symbol": _SYMBOL}, "board_data", ("symbol",),
    ),
    _decl(
        "get_lhb_detail", ("symbol", "date"),
        {"symbol": _SYMBOL, "date": _DATE}, "board_data", ("symbol", "date"),
    ),
)

# 19 条声明的全集。**工具面用 `DATA_TOOL_DECLS`（11）**；这一份只喂
# `data_tool_impl`，让分析图的 19 个 `@tool` 全部仍能解析参数顺序。
ALL_DATA_TOOL_DECLS: tuple[DataToolDecl, ...] = (
    DATA_TOOL_DECLS + RETIRED_DATA_TOOL_DECLS
)

_DECL_BY_NAME = {decl.name: decl for decl in ALL_DATA_TOOL_DECLS}


def call_vendor(name: str, *args: Any, **kwargs: Any) -> str:
    """唯一的数据取数调用点。

    既有 `@tool` 与注册表 handler 都走这里，因此参数顺序只有一份定义。
    """
    return route_to_vendor(name, *args, **kwargs)


def data_tool_impl(name: str) -> Callable[..., str]:
    """返回一个按声明表顺序调用 vendor 的实现函数，供既有 `@tool` 委托。

    查的是 `ALL_DATA_TOOL_DECLS`（19 条）：退役的 8 个**也要能解析**，
    否则 `agents/utils/*_tools.py` 里那 8 个 `@tool` 会在调用时 KeyError，
    分析图直接坏掉——「从工具面摘掉」不等于「让分析图不能取数」。
    """
    decl = _DECL_BY_NAME[name]

    def impl(*args: Any, **kwargs: Any) -> str:
        if kwargs:
            positional = [kwargs[arg] for arg in decl.args]
        else:
            positional = list(args)
        return call_vendor(name, *positional)

    impl.__name__ = name
    return impl


def _as_of_from(args: Mapping[str, Any]) -> str:
    for key in _AS_OF_ARGS:
        value = args.get(key)
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    return ""



# 证据条数上限：一次榜单几十行会把提示词撑爆，也会让校验器的比对成本失控。
_MAX_EVIDENCE_ITEMS = 40

# 字段名 → 中文标签与单位。只列**已知语义**的字段：不认识的字段按原样进证据，
# 但单位留空——给错单位（例如把比率当百分比）会让校验器的换算判断出错。
_FIELD_LABELS: dict[str, tuple[str, str]] = {
    "price": ("现价", "元"),
    "open": ("今开", "元"),
    "high": ("最高", "元"),
    "low": ("最低", "元"),
    "previous_close": ("昨收", "元"),
    "change": ("涨跌额", "元"),
    "change_pct": ("涨跌幅", "pct"),
    "volume": ("成交量", "股"),
    "amount": ("成交额", "元"),
    "turnover_rate": ("换手率", "pct"),
    "volume_ratio": ("量比", "ratio"),
    "quote_time": ("数据时点", ""),
    "source": ("数据来源", ""),
}

_NON_FACT_KEYS = {"note", "structured", "vendor_args", "category", "rendered"}


def _as_of_from_payload(rendered: Any) -> str:
    """从 JSON 载荷里取数据自身时点的**日期部分**（如 `quote_time`）。"""
    import json as _json

    text = str(rendered or "").strip()
    if not text or text[0] not in "[{":
        return ""
    try:
        payload = _json.loads(text)
    except (ValueError, TypeError):
        return ""
    candidates: list[Any] = []
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, Mapping):
                candidates.extend(value.get(k) for k in ("quote_time", "time", "date"))
    elif isinstance(payload, list):
        for row in payload:
            if isinstance(row, Mapping):
                candidates.extend(row.get(k) for k in ("quote_time", "时间", "date", "日期"))
    for value in candidates:
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    return ""


def _evidence_items_from_json(rendered: str, *, source_key: str, as_of: str) -> tuple[EvidenceItem, ...]:
    """把 JSON 型 vendor 输出转成可引用证据。

    为什么只认 JSON：19 个既有数据工具里，报价/榜单类返回的是 JSON，而日线等
    返回的是预格式化表格。**表格不做解析**——从散文里抠"标签=数字"极易错配，
    而错配的证据比没有证据糟得多（校验器的全部意义就是不让错的东西出去）。
    """
    import json as _json

    text = str(rendered or "").strip()
    if not text or text[0] not in "[{":
        return ()
    try:
        payload = _json.loads(text)
    except (ValueError, TypeError):
        return ()
    if not isinstance(payload, (dict, list)):
        return ()

    items: list[EvidenceItem] = []

    def emit(field: str, value: Any, prefix: str = "") -> None:
        if len(items) >= _MAX_EVIDENCE_ITEMS:
            return
        label, unit = _FIELD_LABELS.get(field, (field, ""))
        name = f"{prefix}{label}" if prefix else label
        items.append(
            EvidenceItem(
                id=f"E{len(items) + 1}",
                field=name,
                value=value,
                unit=unit,
                source_key=source_key,
                as_of=as_of,
            )
        )

    def emit_mapping(mapping: Mapping[str, Any], prefix: str) -> None:
        for field, value in mapping.items():
            if field in _NON_FACT_KEYS:
                continue
            if isinstance(value, bool) or value is None or value == "":
                continue
            if isinstance(value, (int, float)):
                emit(str(field), float(value) if isinstance(value, float) else value, prefix)
            elif isinstance(value, str):
                emit(str(field), value, prefix)
            # 嵌套结构不再递归：证据要扁平，深层字段的语义无法自动判定

    if isinstance(payload, dict):
        # 逐标的字典：{"600519.SH": {...}}
        for key, value in payload.items():
            if isinstance(value, Mapping):
                emit_mapping(value, prefix=f"{key} ")
            else:
                emit_mapping({key: value}, prefix="")
    else:
        # 行式列表：每行一条证据，便于逐一引用
        for row in payload:
            if not isinstance(row, Mapping):
                continue
            if len(items) >= _MAX_EVIDENCE_ITEMS:
                break
            parts = []
            for field, value in row.items():
                if field in _NON_FACT_KEYS or value in (None, ""):
                    continue
                parts.append(f"{field} {value}")
            if parts:
                items.append(
                    EvidenceItem(
                        id=f"E{len(items) + 1}",
                        field="记录",
                        value="｜".join(parts),
                        unit="",
                        source_key=source_key,
                        as_of=as_of,
                    )
                )
    return tuple(items)


def make_data_tools() -> tuple[ToolSpec, ...]:
    """构造**工具面**上的 11 个数据工具 ToolSpec（19 减掉退役的 8 个）。

    只遍历 `DATA_TOOL_DECLS`——`RETIRED_DATA_TOOL_DECLS` 刻意不在这里出现，
    那 8 个是「分析图能用、Agent 工具面不给」的边界。

    handler 返回的 `data["rendered"]` 保留 vendor 的原始字符串（用于与分析图
    保持逐字一致），同时显式声明 `structured=False`。
    """
    specs: list[ToolSpec] = []
    for decl in DATA_TOOL_DECLS:

        def handler(_decl: DataToolDecl = decl, **kwargs: Any) -> ToolResult:
            arguments = {arg: kwargs.get(arg) for arg in _decl.args}
            rendered = data_tool_impl(_decl.name)(**arguments)
            as_of = _as_of_from(arguments)
            # 这些 vendor 返回预格式化文本，无法从中确证数据截止日。
            # 因此标 unknown 而不是 fresh——把「未验证」当「最新」会让下游不降级。
            freshness = evaluate_tool_sources(
                source_keys=(_decl.category,),
                as_of=as_of or "",
            ) if as_of else {}
            failed = _looks_like_vendor_failure(rendered)
            if failed:
                # 让 freshness 与 coverage 同源：失败即 error，不留「看起来正常」的缝
                from .freshness import evaluate_tool_sources as _ev

                freshness = _ev(
                    source_keys=(_decl.category,), as_of=as_of or "",
                    unavailable=(_decl.category,),
                )
            # JSON 型输出转成可引用证据；失败文案一律不产出证据（里面即使有数字
            # 也不是事实）。表格/散文型输出保持空证据——诚实优先于覆盖率。
            evidence = (
                ()
                if failed
                else _evidence_items_from_json(
                    rendered,
                    source_key=_decl.category,
                    # 实时类工具没有日期参数，但数据自带时点（`quote_time`）。
                    # 空 as_of 会让证据无法通过日期校验，也会让"这是什么时候的价格"
                    # 无从回答——用数据自己的时点比用"今天"更诚实。
                    as_of=as_of or _as_of_from_payload(rendered),
                )
            )
            return ToolResult(
                data={
                    "rendered": rendered,
                    "structured": bool(evidence),
                    "note": VENDOR_NOTE,
                    "vendor_args": arguments,
                    "category": _decl.category,
                },
                evidence=evidence,
                coverage=_coverage_for(_decl, rendered),
                as_of=as_of,
                freshness=freshness,
                degraded=failed,
                degraded_reason=(
                    f"数据源 {_decl.category} 未返回有效数据（vendor 文案：{str(rendered)[:120]}）"
                    if failed
                    else None
                ),
            )

        specs.append(
            ToolSpec(
                name=decl.name,
                title=decl.name,
                description=(
                    f"既有分析管道使用的数据工具 {decl.name}（vendor 类别：{decl.category}）。"
                    "返回 vendor 文本；结构化（JSON）输出会同时产出可引用证据，"
                    "表格型输出则只给文本、不含可引用数值。"
                ),
                input_schema=decl.schema,
                output_contract=OUTPUT_CONTRACT,
                handler=handler,
                cost="cheap",
                latency_budget_ms=30000,
                freshness_sources=(decl.category,),
            )
        )
    return tuple(specs)


# vendor 在「三级源全失败」时会**返回错误文本而不是抛异常**。若不识别，
# 工具会一边把错误文本当作数据交给模型，一边声明「该源可用」——这是伪造的
# 覆盖声明，也正是 D23 里「拿不到 ≠ 没有」要堵的漏洞。
#
# 判据取自 provider 的实际输出（`cn_akshare_provider.py` 的 unavailable 文案），
# 以及 vendor 路由层的统一失败前缀。
_VENDOR_FAILURE_MARKERS = (
    "temporarily unavailable",
    "unavailable for",
    "all failed",
    "调用失败",
    "获取失败",
    "查询失败",
    "数据获取异常",
    # 实测漏网的一次：`get_zt_pool` 在非交易日返回「涨停板情绪池数据暂不可用。」
    # ——里面还带着日期，看上去像数据。这类"客气话"必须被识别为失败，
    # 否则一条不可用的返回会被当成有效数据往下走。
    "暂不可用",
    "暂时不可用",
    "暂无数据",
    "无数据",
    # 实测（2026-09-22）：`get_global_news` 真实返回
    #     'Error fetching global news: Too Many Requests. Rate limited. Try after a while.'
    # 上面所有标记一个都不命中，于是**限流被当成有效数据**：工具一边把这段英文
    # 错误文本交给模型，一边声明「该源可用」。这是与「拿不到 ≠ 没有」同一条原则
    # 的直接违反，而且它比缺数据更糟——模型可能拿这句话当新闻内容来解读。
    #
    # `Error fetching` 是本仓库自己产生的前缀（`dataflows/yfinance_news.py`），
    # 不是供应商文案，所以它比任何猜测的关键词都可靠。
    "error fetching",
    "too many requests",
    "rate limited",
    "try after a while",
)


def _looks_like_vendor_failure(rendered: Any) -> bool:
    """判断 vendor 返回的是否为失败说明而非数据。"""
    if not isinstance(rendered, str) or not rendered.strip():
        # 空返回同样是「拿不到」——不能声明该源可用
        return True
    lowered = rendered.lower()
    return any(marker.lower() in lowered for marker in _VENDOR_FAILURE_MARKERS)


def _coverage_for(decl: DataToolDecl, rendered: Any = None):
    """按 vendor 的**实际返回**声明覆盖。

    修复审计 A3：原实现恒返回 `available=(category,)`，于是 vendor 返回
    "cn_akshare is temporarily unavailable ... all failed" 时，工具仍声称该源可用。
    """
    from tradingagents.grounding.types import Coverage

    if rendered is None or _looks_like_vendor_failure(rendered):
        return Coverage(available=(), unavailable=(decl.category,))
    return Coverage(available=(decl.category,), unavailable=())

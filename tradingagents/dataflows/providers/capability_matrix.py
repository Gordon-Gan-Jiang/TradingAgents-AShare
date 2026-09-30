"""数据源能力矩阵：**谁能提供哪个维度**，以及当多源都能时的优先级。

## 为什么要有这个模块

本次排查（2026-09-24）暴露的问题不是"某个源坏了"，而是**配置与现实脱节且无人发现**：

* `cn_baostock` 只实现了 `get_stock_data` / `get_indicators`，却被列进
  `fundamental_data`、`news_data`、`cn_market_data` 三条链——三个**死成员**，
  在链上永远只能抛 `NotImplementedError`，于是"链上有三源覆盖"是假的。
* `cn_research_data`（日线、分时、筹码、资金流、板块基准、换手、个股新闻）
  **压根没写进 `data_vendors`**，`get_vendor` 于是落到默认值 `"yfinance"`，
  再被 `_resolve_vendor_chain` 追加全部注册 provider。实际效果是：
  **A 股日线的第一顺位是 yfinance**（美股/全球源），一个没人有意做过的决定。

根因是"数据源分工"这份知识只存在于人脑与文档里，代码无法校验它。本模块把它变成
**可执行、可校验**的单一事实源：

* `CAPABILITIES` —— 每个 tool 一行：中文维度名、所属类别、**分层的源**（tier 越小越优先）。
* 校验规则见 `validate_matrix()`，由 `tests/test_provider_capability_matrix.py` 执行。

## 结合形式：Baostock 当地基，AKShare 当广度

两个源的**结构**不同，所以结合方式是分层分工，而不是"两个源互相兜底"：

* **Baostock**（官方 TCP 协议、免费、无 IP 封禁、可批量）——适合当**历史地基**：
  日线、分钟线。（财务也实现了，但见下条。）
* **AKShare 系**（聚合爬虫）——独占**在途与广度**：实时快照、板块、资金流、
  龙虎榜、涨停池、雪球热榜、筹码、新闻。Baostock 没有这些。

财务一栏特意**没有**把 Baostock 提到 tier 1：AKShare 的同花顺/新浪口径字段更丰富，
且分析师提示词与历史报告都按该口径写。把 Baostock 放在 tier 2 既拿到"上游改版时
还有一条稳定退路"，又不改变已成型的分析口径——**接入的第一原则是不要静默改变
已有数字的含义**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

#: tier 语义：1=主源，2=兜底，3=最后手段。数值越小越优先。
TIER_PRIMARY = 1
TIER_FALLBACK = 2
TIER_LAST_RESORT = 3


@dataclass(frozen=True)
class CapabilitySource:
    """某个源在该维度上的角色。"""

    source: str
    tier: int
    #: True 表示该源可批量取全市场（决定它能不能用来建本地库）
    batchable: bool = False
    #: True 表示需要凭证（token/key）才能用
    needs_credential: bool = False
    note: str = ""


@dataclass(frozen=True)
class Capability:
    """一个数据维度 = 一个 tool 名（与 `interface.TOOLS_CATEGORIES` 对齐）。"""

    tool: str
    label: str
    category: str
    sources: tuple[CapabilitySource, ...]
    #: 是否已确认在本机可用（用于给前端"预期/实际"对照做底）
    note: str = ""
    #: 该能力是否**经过 `route_to_vendor` 路由器**。
    #: True = 属于 `TOOLS_CATEGORIES` 的常规维度，受 R5/R6（链上可覆盖、
    #: tier 1 与链首一致）约束。
    #: False = **真实存在但由代码直接调用**的能力（当前唯一一例：基准指数日线，
    #: 由 `mainline_scoring._default_fetch_benchmark` 直接取）。
    #: 这类能力同样要登记在这里，理由有二：
    #:   1) 它是"最关键的那条路"——基准指数断了，主线评分整条链就废了，
    #:      而它此前只有 AKShare 一家，东财一抖就静默失败；
    #:   2) 探测清单必须能覆盖它，否则页面和 CI 都盯不到。
    #: 不设这个标记的后果就是"两个事实源"：矩阵说一套，探测另抄一份。
    routed: bool = True

    @property
    def primary(self) -> CapabilitySource:
        return min(self.sources, key=lambda s: s.tier)

    def source_names(self) -> tuple[str, ...]:
        return tuple(s.source for s in sorted(self.sources, key=lambda s: s.tier))


def _cs(source: str, tier: int, **kw) -> CapabilitySource:
    return CapabilitySource(source=source, tier=tier, **kw)


# 说明：tier 1 的选择必须与 `default_config.data_vendors` / `tool_vendors` 的
# 有效链一致——`validate_matrix` 会强制这一点（规则 R6），所以这份表不是注释，
# 而是可执行的裁决。
CAPABILITIES: tuple[Capability, ...] = (
    # ---------- 历史地基：Baostock 主，AKShare 兜底 ----------
    Capability(
        tool="get_daily_bars_df",
        label="历史日线（结构化）",
        category="cn_research_data",
        sources=(
            _cs("cn_baostock", TIER_PRIMARY, batchable=True,
                note="官方协议、可批量、无 IP 封禁；日线 + 复权因子"),
            _cs("cn_akshare", TIER_FALLBACK, note="东财已封，实际走腾讯/新浪"),
        ),
        note="本地 K 线库的生产源",
    ),
    Capability(
        tool="get_intraday_bars",
        label="分钟/分时线",
        category="cn_research_data",
        sources=(
            _cs("cn_baostock", TIER_PRIMARY, note="历史分钟线 5/15/30/60"),
            _cs("cn_akshare", TIER_FALLBACK, note="新浪仅当日分时"),
        ),
    ),
    # ---------- 行情与指标 ----------
    Capability(
        tool="get_stock_data",
        label="个股行情（文本）",
        category="core_stock_apis",
        sources=(
            _cs("cn_akshare", TIER_PRIMARY),
            _cs("cn_baostock", TIER_FALLBACK),
            _cs("yfinance", TIER_LAST_RESORT, note="非 A 股主源，仅在 CN 源全败时兜底"),
        ),
    ),
    Capability(
        tool="get_indicators",
        label="技术指标",
        category="technical_indicators",
        sources=(
            _cs("cn_akshare", TIER_PRIMARY),
            _cs("cn_baostock", TIER_FALLBACK),
            _cs("yfinance", TIER_LAST_RESORT),
        ),
    ),
    # ---------- 财务：保持既有口径，Baostock 做退路 ----------
    Capability(
        tool="get_fundamentals",
        label="财务摘要",
        category="fundamental_data",
        sources=(
            _cs("cn_akshare", TIER_PRIMARY, note="同花顺/新浪口径，历史报告按此写"),
            _cs("cn_baostock", TIER_FALLBACK, note="季频规范三表，口径不同，仅兜底"),
            _cs("yfinance", TIER_LAST_RESORT),
        ),
    ),
    Capability(
        tool="get_balance_sheet",
        label="资产负债表",
        category="fundamental_data",
        sources=(
            _cs("cn_akshare", TIER_PRIMARY),
            _cs("cn_baostock", TIER_FALLBACK),
            _cs("yfinance", TIER_LAST_RESORT),
        ),
    ),
    Capability(
        tool="get_cashflow",
        label="现金流量表",
        category="fundamental_data",
        sources=(
            _cs("cn_akshare", TIER_PRIMARY),
            _cs("cn_baostock", TIER_FALLBACK),
            _cs("yfinance", TIER_LAST_RESORT),
        ),
    ),
    Capability(
        tool="get_income_statement",
        label="利润表",
        category="fundamental_data",
        sources=(
            _cs("cn_akshare", TIER_PRIMARY),
            _cs("cn_baostock", TIER_FALLBACK),
            _cs("yfinance", TIER_LAST_RESORT),
        ),
    ),
    # ---------- 实时与情绪：AKShare 独占（Baostock 无此能力） ----------
    Capability(
        tool="get_realtime_quotes",
        label="实时快照",
        category="realtime_data",
        sources=(_cs("cn_akshare", TIER_PRIMARY, note="Baostock 无实时能力"),),
    ),
    Capability(
        tool="get_news",
        label="个股新闻",
        category="news_data",
        sources=(
            _cs("cn_akshare", TIER_PRIMARY),
            _cs("yfinance", TIER_LAST_RESORT),
        ),
    ),
    Capability(
        tool="get_global_news",
        label="全局/宏观新闻",
        category="news_data",
        sources=(
            _cs("cn_akshare", TIER_PRIMARY),
            _cs("yfinance", TIER_LAST_RESORT),
        ),
    ),
    Capability(
        tool="get_insider_transactions",
        label="内部人/股东交易",
        category="news_data",
        sources=(
            _cs("cn_akshare", TIER_PRIMARY),
            _cs("yfinance", TIER_LAST_RESORT),
        ),
    ),
    Capability(
        tool="get_board_spot",
        label="板块快照",
        category="cn_board_data",
        sources=(
            _cs("cn_sina_moneyflow", TIER_PRIMARY, note="新浪行业快照，实测可用（仅 industry）"),
            _cs("cn_akshare", TIER_FALLBACK, note="东财已封；concept 等维度仍靠它"),
        ),
    ),
    Capability(
        tool="get_board_rank",
        label="板块排行",
        category="cn_board_data",
        sources=(_cs("cn_akshare", TIER_PRIMARY),),
    ),
    Capability(
        tool="get_board_hist",
        label="板块历史",
        category="cn_board_data",
        sources=(_cs("cn_akshare", TIER_PRIMARY),),
    ),
    Capability(
        tool="get_board_cons",
        label="板块成分股",
        category="cn_board_data",
        sources=(_cs("cn_akshare", TIER_PRIMARY),),
    ),
    Capability(
        tool="get_board_fund_flow",
        label="板块资金流",
        category="cn_market_data",
        sources=(
            _cs("cn_sina_moneyflow", TIER_PRIMARY),
            _cs("cn_akshare", TIER_FALLBACK, note="东财 push2 本机不通"),
        ),
    ),
    Capability(
        tool="get_individual_fund_flow",
        label="个股资金流",
        category="cn_market_data",
        sources=(
            _cs("cn_sina_moneyflow", TIER_PRIMARY),
            _cs("cn_eastmoney_http", TIER_FALLBACK),
            _cs("cn_akshare", TIER_LAST_RESORT, note="东财接口本机被 reset"),
        ),
    ),
    Capability(
        tool="get_lhb_detail",
        label="龙虎榜",
        category="cn_market_data",
        sources=(_cs("cn_akshare", TIER_PRIMARY),),
    ),
    Capability(
        tool="get_zt_pool",
        label="涨停池",
        category="cn_market_data",
        sources=(_cs("cn_akshare", TIER_PRIMARY),),
    ),
    Capability(
        tool="get_hot_stocks_xq",
        label="雪球热榜",
        category="cn_market_data",
        sources=(
            _cs("cn_akshare", TIER_PRIMARY, needs_credential=True,
                note="雪球匿名态受限，需 xq_a_token"),
        ),
    ),
    # ---------- 研究类：只有 AKShare 系覆盖 ----------
    Capability(
        tool="get_chip_distribution",
        label="筹码分布",
        category="cn_research_data",
        sources=(_cs("cn_akshare", TIER_PRIMARY, note="py 内还有 Tushare 直连兜底"),),
    ),
    Capability(
        tool="get_flow_frame",
        label="资金流帧",
        category="cn_research_data",
        sources=(_cs("cn_akshare", TIER_PRIMARY),),
    ),
    Capability(
        tool="get_peer_frames",
        label="板块基准（同伴）",
        category="cn_research_data",
        sources=(_cs("cn_akshare", TIER_PRIMARY),),
    ),
    Capability(
        tool="get_stock_turnover",
        label="换手率序列",
        category="cn_research_data",
        sources=(_cs("cn_akshare", TIER_PRIMARY),),
    ),
    Capability(
        tool="get_symbol_news",
        label="个股新闻（结构化）",
        category="cn_research_data",
        sources=(_cs("cn_akshare", TIER_PRIMARY),),
    ),
    # ---------- 直连能力（不走路由器，但必须被登记与探测） ----------
    Capability(
        tool="get_index_daily_bars_df",
        label="指数日线（基准）",
        category="cn_research_data",
        sources=(
            _cs("cn_baostock", TIER_PRIMARY, note="官方协议，实测 274ms/9 行"),
        ),
        note=(
            "主线评分的沪深300基准；不走 route_to_vendor，由 mainline_scoring 直接取。"
            "**只登记 cn_baostock**：AKShare 侧取指数是 `mainline_scoring` 里对 "
            "akshare 模块的直接调用（stock_zh_index_daily_em/stock_zh_index_daily），"
            "并没有对应的 provider 方法，因此按 R2（矩阵不说谎）不能写进来——"
            "写进来只会让探测去调一个不存在的方法。把它统一成 provider 方法是后续工作。"
        ),
        routed=False,
    ),
)

BY_TOOL: Mapping[str, Capability] = {c.tool: c for c in CAPABILITIES}


def capabilities_for_category(category: str) -> tuple[Capability, ...]:
    return tuple(c for c in CAPABILITIES if c.category == category)


def capabilities_for_source(source: str) -> tuple[Capability, ...]:
    return tuple(c for c in CAPABILITIES if any(s.source == source for s in c.sources))


def source_names() -> tuple[str, ...]:
    """矩阵中出现过的全部源名（去重、稳定顺序）。"""
    seen: list[str] = []
    for cap in sorted(CAPABILITIES, key=lambda c: c.category):
        for s in cap.sources:
            if s.source not in seen:
                seen.append(s.source)
    return tuple(seen)


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MatrixProblem:
    rule: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - 只为可读报错
        return f"[{self.rule}] {self.detail}"


def _effective_chain(config: Mapping, category: str, tool: str) -> tuple[str, ...]:
    """按 `interface.get_vendor` 的语义求该 tool 的有效链（含 tool_vendors 覆盖）。"""
    from tradingagents.dataflows.config import get_config

    cfg = get_config() if config is None else config
    tool_vendors = cfg.get("tool_vendors", {}) or {}
    data_vendors = cfg.get("data_vendors", {}) or {}
    raw = tool_vendors.get(tool) or data_vendors.get(category)
    if not raw:
        return ()
    return tuple(v.strip() for v in str(raw).split(",") if v.strip())


def validate_matrix(
    *,
    config: Mapping | None = None,
    provider_classes: Mapping[str, type] | None = None,
) -> list[MatrixProblem]:
    """执行全部一致性规则，返回问题列表（空 = 通过）。

    规则：
      R1 覆盖完整——`TOOLS_CATEGORIES` 里每个 tool 都必须在矩阵里出现。
      R2 矩阵不说谎——矩阵声称支持的 (tool, source) 必须真的被该 provider 实现。
      R3 配置不遗漏——每个 category 都必须在 `data_vendors` 里有显式条目
         （否则会静默落到默认 `yfinance`）。
      R4 链上无死成员——类别链里每个 provider 至少要能服务该类别的一个 tool。
      R5 维度可覆盖——每个 tool 的有效链里至少有一个能服务它的 provider。
      R6 优先级一致——有效链里**第一个能服务该 tool 的 provider**必须等于矩阵的 tier 1。
    """
    from tradingagents.dataflows.interface import TOOLS_CATEGORIES
    from tradingagents.dataflows.providers import support as provider_support

    problems: list[MatrixProblem] = []
    cfg = config if config is not None else _load_config()
    data_vendors = (cfg.get("data_vendors", {}) or {})

    if provider_classes is None:
        provider_classes = _default_provider_classes()

    def implemented(source: str) -> frozenset[str] | None:
        cls = provider_classes.get(source)
        if cls is None:
            return None
        return provider_support.implemented_methods(cls)

    matrix_tools = set(BY_TOOL)
    all_tools = {t for info in TOOLS_CATEGORIES.values() for t in info["tools"]}

    # R1
    for tool in sorted(all_tools - matrix_tools):
        problems.append(MatrixProblem("R1", f"tool {tool!r} 未在能力矩阵中声明"))
    for tool in sorted(matrix_tools - all_tools):
        # 非路由能力（如基准指数日线）本来就不在 `TOOLS_CATEGORIES` 里，
        # 那是它的定义而不是错误：它由代码直接调用，不经过 vendor 路由。
        cap = BY_TOOL.get(tool)
        if cap is not None and not cap.routed:
            continue
        problems.append(MatrixProblem("R1", f"矩阵里的 {tool!r} 不属于任何 category"))

    # R2
    for cap in CAPABILITIES:
        for src in cap.sources:
            impl = implemented(src.source)
            if impl is None:
                problems.append(
                    MatrixProblem("R2", f"{cap.tool}: 源 {src.source!r} 不在注册表中")
                )
            elif cap.tool not in impl:
                problems.append(
                    MatrixProblem(
                        "R2",
                        f"{cap.tool}: 矩阵声称 {src.source!r} 支持，但它只是占位实现",
                    )
                )

    # R3 / R4 / R5 / R6
    for category, info in TOOLS_CATEGORIES.items():
        tools = list(info["tools"])
        chain = [
            v.strip()
            for v in str(data_vendors.get(category, "")).split(",")
            if v.strip()
        ]
        if category not in data_vendors:
            problems.append(
                MatrixProblem(
                    "R3",
                    f"category {category!r} 未在 data_vendors 中显式配置"
                    f"（会静默落到默认 yfinance，本类 tools={tools}）",
                )
            )
            continue

        for name in chain:
            impl = implemented(name)
            if impl is None:
                problems.append(MatrixProblem("R4", f"{category}: 链上 {name!r} 未注册"))
                continue
            if not any(t in impl for t in tools):
                problems.append(
                    MatrixProblem(
                        "R4",
                        f"{category}: 链上 {name!r} 对该类别 0/{len(tools)} 可服务（死成员）",
                    )
                )

        for tool in tools:
            effective = _effective_chain(cfg, category, tool)
            serving = []
            for name in effective:
                impl = implemented(name)
                if impl and tool in impl:
                    serving.append(name)
            if not serving:
                problems.append(
                    MatrixProblem(
                        "R5",
                        f"{tool}: 有效链 {list(effective)} 中没有任何 provider 能服务它",
                    )
                )
                continue
            cap = BY_TOOL.get(tool)
            if cap is None:
                continue
            expected = cap.primary.source
            if serving[0] != expected:
                problems.append(
                    MatrixProblem(
                        "R6",
                        f"{tool}: 有效链里第一个能服务它的是 {serving[0]!r}，"
                        f"而矩阵 tier 1 声明为 {expected!r}",
                    )
                )

    return problems


def _load_config() -> Mapping:
    from tradingagents.default_config import DEFAULT_CONFIG

    return DEFAULT_CONFIG


def _default_provider_classes() -> dict[str, type]:
    from tradingagents.dataflows.providers import build_default_registry

    registry = build_default_registry()
    out: dict[str, type] = {}
    for name in registry.list_names():
        try:
            out[name] = type(registry.get(name))
        except Exception:  # pragma: no cover - 注册表取用失败不应静默
            continue
    return out


def matrix_snapshot() -> Sequence[dict]:
    """给前端/端点用的可序列化快照（只描述**预期**，不含实测）。"""
    return [
        {
            "tool": cap.tool,
            "label": cap.label,
            "category": cap.category,
            "note": cap.note,
            #: False = 直连能力（不走 route_to_vendor），前端据此说明"无兜底链"
            "routed": cap.routed,
            "sources": [
                {
                    "source": s.source,
                    "tier": s.tier,
                    "batchable": s.batchable,
                    "needs_credential": s.needs_credential,
                    "note": s.note,
                }
                for s in sorted(cap.sources, key=lambda s: s.tier)
            ],
        }
        for cap in CAPABILITIES
    ]

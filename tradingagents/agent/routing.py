"""三档意图路由：成本与体验的全部杠杆。

## 现状与目标

`POST /v1/chat/completions` 今天的行为是：抽取标的 → **无条件**启动 7 分析师
完整管道。于是「今天主力净流入多少」（1 次数据调用）与「全面分析一下」
（分钟级、约 100× 成本）走同一条链路，且响应只有一个任务回执。

三档把这件事拆开：

| 档 | 典型问句 | 执行 | 目标延迟 | 相对成本 |
|---|---|---|---|---|
| light | 「现在多少钱」「今天主力净流入多少」 | 1 个数据工具 + 1 次汇总 | < 5s | 1× |
| medium | **「是不是在洗盘」「还能不能拿」** | 确定性探针 + 1 次归纳 | < 30s | ~5× |
| heavy | 「全面分析一下」「出份研报」 | 现有 7 分析师管道 | 分钟级 | ~100× |

## 三条 normative 规则

1. **仅解析到标的不足以判为重档**。这是现状最大的成本漏洞。
2. **降级只能向下**：分类失败 / 超时 / 低置信 → 中档；相邻两档模糊 → 取更低档。
   重档成本 100×，误升级不可逆；而等待时间过长比「中等深度但立刻可读」更糟。
3. **无法定档必须追问**：缺标的就猜，等于制造一个错误答案。

## 为什么不用 LLM 做规则层

规则层零 LLM，可以在毫秒内穷举。LLM 分类器作为可插拔实现（`TierClassifier`），
由 `DegradingClassifier` 包裹后获得统一的降级语义——这样「降级」是一处实现，
而不是散落在每个分类器里的 try/except。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Any, Iterable, Mapping, Protocol, Sequence

TIER_LIGHT = "light"
TIER_MEDIUM = "medium"
TIER_HEAVY = "heavy"
TIERS: tuple[str, ...] = (TIER_LIGHT, TIER_MEDIUM, TIER_HEAVY)

# 向下降级顺序：索引越小越廉阶
_LOWER: dict[str, str] = {TIER_HEAVY: TIER_MEDIUM, TIER_MEDIUM: TIER_LIGHT, TIER_LIGHT: TIER_LIGHT}


@dataclass(frozen=True)
class TierBudget:
    """档位的延迟与调用预算。编排层必须按它执行并显式报告截断。"""

    target_latency_ms: int
    max_tool_calls: int
    max_model_calls: int


TIER_BUDGETS: dict[str, TierBudget] = {
    TIER_LIGHT: TierBudget(target_latency_ms=5000, max_tool_calls=2, max_model_calls=1),
    TIER_MEDIUM: TierBudget(target_latency_ms=30000, max_tool_calls=5, max_model_calls=2),
    # 重档由既有管道自管预算，这里只声明「不进同步工具面」的语义
    TIER_HEAVY: TierBudget(target_latency_ms=600000, max_tool_calls=0, max_model_calls=0),
}



# ── 事实型问句 → 一个便宜的数据工具 ──────────────────────────────────────
#
# 档位表里写着轻档 =「1 个数据工具 + 1 次汇总」，但实现一度对轻档返回**空计划**，
# 于是最高频的问题（"现在多少钱"）永远得到"本轮无可用证据"。这里把那份承诺
# 落成规则：按问句语义挑**一个**便宜的取数工具，零 LLM、毫秒级、可穷举。

FACTUAL_TOOL_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    # 指向**能产出可引用证据**的工具：旧 `get_individual_fund_flow` 返回预格式化
    # 文本、`evidence=()`，于是"主力净流入多少"即使取到数也无法被引用，回答只能
    # 答"无可用证据"。`capital_flow_read` 给数值（单位元）+ 弃权声明。
    # 这不是"新工具更好看"，而是**没有证据就没有回答**这条硬约束的直接后果。
    #
    # 顺序 = 优先级。更具体的语义必须排在更泛的前面，否则会被泛化的条目抢走。
    #
    # 本轮补齐的四条（筹码/分时/公告/指数）：它们此前**没有任何规则入口**。
    # 这一层不是可选的——带代码的问句一定能解析出标的，于是永远走不到规划层
    # （规则优先的代价），只能靠这张表选工具。表里没有 = 那个工具对这类问句
    # 根本不可选，"封了但用不上"。
    #
    # 反向的一次整理（本轮）：三个**已从工具面退役**的工具（`get_cashflow` /
    # `get_indicators` / `get_lhb_detail`）的入口已从这里摘掉，各自的理由就地写在
    # 摘除处。读到这里请分清两种"表里没有"：
    #   * **退役**——能力被有意摘除（数字不可引用），对应问句现在**没有落点**，
    #     见 `RETIRED_CAPABILITY_HINTS`，正确行为是弃权；
    #   * **遗忘**——能力存在却没有入口，上面那句"封了但用不上"说的就是它。
    # 两者不能混为一谈：前者补一条规则是错的（会把问题指向别的工具），后者才是 bug。
    (("筹码", "集中度", "获利比例", "平均成本"), "chip_distribution_read"),
    (("分时", "承接", "尾盘", "盘口"), "intraday_bars_read"),
    # "读既有的报告结论"是 L1 工具，不是重档管道：重档判定（`_requests_report_generation`）
    # 在档位分类里排在前面，"出个研报"仍走重档，这里只接"读上一份报告"的问法。
    # 没有这两条时，带代码/名称的问句会落到中档去跑洗盘探针——用户问"这份报告的多视角
    # 共识怎么样"，平台答"洗盘还是派发"。
    (("多视角", "共识"), "report_consensus_read"),
    (("研报结论", "报告结论", "研报怎么说", "报告怎么说的", "历史报告"), "report_conclusion_lookup"),
    # 公告要盖过新闻（新闻那段也含"公告"二字）：公告是**权威披露**，新闻不是。
    # 把两者混在一起会让"最近有什么公告"拿到一堆媒体稿。
    (("公告", "披露", "年报预约", "问询函"), "announcements_read"),
    (("指数", "上证", "深证", "创业板指", "沪深300", "大盘", "点位"), "index_quote_read"),
    (("自选股", "自选"), "watchlist_read"),
    (("数据是新的", "数据时效", "数据新鲜", "更新到几号", "数据准不准"), "data_freshness_check"),
    (("净流入", "净流出", "主力", "资金流"), "capital_flow_read"),
    # **比率必须留在本行（`get_fundamentals`），不能被下面两条接走**。
    #
    # 事故记录（2026-09-22 实测）：本行原本只写「市盈率/市净率/估值/基本面/每股/
    # 营收/净利润」，而「毛利率」被下面第 110 行接走指向 `get_income_statement`。
    # 实测两者的证据内容**完全不同**：
    #   * `get_fundamentals`     —— 40 条，**含**毛利率 89.5552 / ROE 16.75 /
    #                                资产负债率 15.1931 / 销售净利率 50.7516 / ROA 20.0558；
    #   * `get_income_statement` —— 40 条，比率类字段 **0 条**（只有营业收入/营业成本等原始项）。
    #
    # 于是「600519 的毛利率是多少」注定拿不到比率：模型只能自己算，而
    # 「判定权在确定性层」——自算值被校验器判 `unsourced_number`，整轮降级。
    # 用户拿到的是「没能给出可溯源的回答」，而数据**本来就在**另一个工具里。
    #
    # 顺序仍是优先级：本行必须在下面「资产负债 → get_balance_sheet」与
    # 「毛利率 → get_income_statement」**之前**命中，所以补在这里。
    # 注意「资产负债率」与「资产负债表」是不同问题：前者要比率（本行），后者要
    # 报表（下面那条）。两者互不构成子串，不会互相抢走。
    (
        (
            "市盈率", "市净率", "估值", "基本面", "每股", "营收", "净利润",
            "毛利率", "净利率", "收益率", "报酬率", "资产负债率", "周转率",
            "roe", "roa",
        ),
        "get_fundamentals",
    ),
    (("资产负债", "负债率"), "get_balance_sheet"),
    # 此处曾有 `(("现金流",), "get_cashflow")`。**该工具已从工具面退役**（不是遗漏）：
    # 它返回预格式化散文、`evidence=()`，取到的数字无法被校验器引用。见
    # `RETIRED_CAPABILITY_HINTS`——退役能力的问句现在返回「没有落点」而不是兜底到
    # 另一个工具。
    (("利润表", "营业利润", "毛利率"), "get_income_statement"),
    (("新闻", "公告", "消息面"), "data_symbol_news"),
    # 此处曾有 `(("macd", "kdj", "rsi", "均线", "金叉", "死叉", "技术指标"), "get_indicators")`。
    # `get_indicators` **已从工具面退役**（同上）。**刻意不指向洗盘探针**：探针答的是
    # "洗盘还是派发"，拿它回答"MACD 金叉了吗"就是拿另一个问题的结论冒充这个问题的答案。
    # 此处曾有 `(("龙虎榜",), "get_lhb_detail")`。`get_lhb_detail` **已从工具面退役**；
    # 也不改指 `get_zt_pool`（涨停池 ≠ 龙虎榜）。
    (("涨停",), "get_zt_pool"),
    (("板块", "行业涨幅"), "get_board_spot"),
)

# ── 已退役能力：规则层必须能说「没有落点」────────────────────────────────
#
# 上表摘掉的三条入口对应三个**已从工具面退役**的工具（见 `tools/data.py`）。
# 摘掉入口是必须的，但**只摘入口还不够**：规则层若返回 `None`，执行层的
# `DEFAULT_*` 兜底会顶上，于是用另一个问题的数据回答这个问题。实测（注册表摘掉
# 这 8 个工具之后、规则表摘掉之前）：
#
#     '今天龙虎榜有多少只' → tier=light market_level=True planned=['get_board_spot']
#     '600519 的现金流是多少' → tier=light planned=['get_realtime_quotes']
#
# 用户问龙虎榜，平台答板块行情——**比诚实说"数据未覆盖"糟得多**。因此这里显式
# 登记"已退役能力"的问句标记：命中且没有别的活规则匹配时，规则层返回
# `NO_FACTUAL_TOOL`，由执行层落成**空计划**，回答层如实报"数据未覆盖"。
#
# 为什么不 redirect：把「MACD」指到洗盘探针、把「龙虎榜」指到涨停池，都是在拿另
# 一个问题的答案冒充这个问题的答案。**没有落点的正确形态是弃权。**
RETIRED_CAPABILITY_HINTS: tuple[str, ...] = (
    # get_cashflow（现金流）——资金流是另一件事，`capital_flow_read` 已在上面接住
    # 「净流入/主力/资金流」，这里刻意**不含**这些词。
    "现金流",
    # get_indicators（技术指标）
    "macd", "kdj", "rsi", "均线", "金叉", "死叉", "技术指标",
    # get_lhb_detail（龙虎榜）
    "龙虎榜",
)

# 规则层的「本平台没有能回答它的工具」。空串不是合法工具名，因此永远不会被
# 误当成注册表里的条目；执行层见到它必须**不做兜底替换**。
NO_FACTUAL_TOOL = ""

# 这些工具需要代码作为参数；无标的的问句只能用市场级工具
#
# 已退役的 4 个（`get_stock_data` / `get_indicators` / `get_cashflow` /
# `get_individual_fund_flow`）不在此列——它们已不在工具面上。**登记本身是强制的**：
# 漏登记的后果不是"选不出来"，而是无标的的问句也能选中它，然后必然失败。
SYMBOL_REQUIRED_TOOLS: frozenset[str] = frozenset(
    {
        "get_realtime_quotes",
        "get_fundamentals",
        "get_balance_sheet",
        "get_income_statement",
        "data_symbol_news",
        "get_hot_stocks_xq",
        # 本轮新增的三条按标的取数的证据工具。**必须登记**：漏登记的后果不是
        # "选不出来"，而是无标的的问句也能选中它，然后必然失败——用户看到的是
        # "查过了但没数据"，比不查更误导。
        "chip_distribution_read",
        "intraday_bars_read",
        "announcements_read",
        # 共识是**某一个标的**的报告共识；无标的时选它是一次必然失败的调用。
        # （`report_conclusion_lookup` 不在此列：它读的是用户自己的报告列表。）
        "report_consensus_read",
    }
)

# 无标的时可用的**市场级**工具，以及默认选谁
#
# `index_quote_read` 只需要 `as_of`（不需要代码），所以它在无标的时是合法落点。
# 不登记它的后果是「今天上证指数多少点」被判成"用户没说清楚"而追问代码——
# 平台明明查得到指数点位。
#
# `get_lhb_detail` 已从这里移除：它**已从工具面退役**。这不是"忘了登记"——
# 退役后无标的的龙虎榜问句**没有落点**，由 `RETIRED_CAPABILITY_HINTS` 显式声明，
# 正确形态是如实报"数据未覆盖"，而不是拿板块行情去顶。
MARKET_LEVEL_TOOLS: tuple[str, ...] = (
    "get_zt_pool",
    "get_board_spot",
    "get_global_news",
    "index_quote_read",
)
DEFAULT_MARKET_TOOL = "get_board_spot"
DEFAULT_FACTUAL_TOOL = "get_realtime_quotes"


# 参数名必须**从工具自己的 schema 推导**，不能猜：实测各工具并不统一
# （`get_realtime_quotes` 要 `symbols`（列表）、`get_fundamentals` 要 `ticker`、
# `get_zt_pool` 要 `date`）。曾经按 `symbol`/`as_of` 硬猜，结果每次调用都被
# 注册表以 `unknown arguments` 拒掉——而回答只说"无可用证据"，原因看不出来。
_INSTRUMENT_ARGS: tuple[str, ...] = ("symbol", "ticker", "code", "symbols")
_DATE_ARGS: tuple[str, ...] = ("as_of", "curr_date", "date", "end_date", "trade_date")

_INDICATOR_HINTS: tuple[tuple[str, str], ...] = (
    ("macd", "macd"), ("kdj", "kdj"), ("rsi", "rsi"),
    ("boll", "boll"), ("均线", "ma"), ("金叉", "macd"), ("死叉", "macd"),
)


def build_factual_arguments(
    spec: Any, *, symbol: str | None, as_of: str, text: str = ""
) -> dict[str, Any] | None:
    """按工具 schema 生成参数；**填不满必填项就返回 None**（由调用方换工具）。

    返回 None 表示"这个工具用不了"，而不是"随便填一个"——后者会变成一次必然
    失败的调用，而失败原因（unknown arguments / missing required）对用户毫无意义。
    """
    schema = getattr(spec, "input_schema", None) or {}
    properties = dict(schema.get("properties") or {})
    required = set(schema.get("required") or ())

    args: dict[str, Any] = {}
    for key in _INSTRUMENT_ARGS:
        if key in properties and symbol:
            args[key] = [symbol] if key == "symbols" else symbol
            break
    for key in _DATE_ARGS:
        if key in properties:
            args[key] = as_of
            break
    if "indicator" in properties:
        lowered = (text or "").lower()
        for hint, value in _INDICATOR_HINTS:
            if hint in lowered or hint in (text or ""):
                args["indicator"] = value
                break
    if "lookback_days" in properties:
        args["lookback_days"] = 30

    if not required <= set(args):
        return None
    return args


def _is_retired_capability_question(lowered: str, text: str) -> bool:
    """问句问的是一件**已从工具面退役**的能力（现金流的绝对值 / 技术指标 / 龙虎榜）。"""
    return any(
        hint in lowered or hint in text for hint in RETIRED_CAPABILITY_HINTS
    )


def plan_factual_tool(text: str, *, has_symbol: bool) -> str:
    """选一个便宜的数据工具。规则优先，零 LLM。

    无标的时**绝不**选需要代码的工具：那是一次注定失败的调用，还会让回答
    看起来"查过了但没有数据"，比不查更误导。

    返回 `NO_FACTUAL_TOOL`（空串）表示**本平台没有能回答它的工具**——该能力
    已从工具面退役，且没有等价的活工具。这与"规则没匹配上"必须分开：后者才该
    落到 `DEFAULT_*` 兜底。执行层见到空串**不做兜底替换**，而是落成空计划让
    回答层如实报"数据未覆盖"。
    """
    lowered = (text or "").lower()
    matched: str | None = None
    for hints, tool in FACTUAL_TOOL_HINTS:
        if any(hint in lowered or hint in (text or "") for hint in hints):
            matched = tool
            break

    if matched is not None:
        if has_symbol:
            return matched
        return matched if matched not in SYMBOL_REQUIRED_TOOLS else DEFAULT_MARKET_TOOL

    # 没有活规则匹配。这里先问一句"是不是问了一件已退役的能力"：
    # 是 → 没有落点（弃权）；不是 → 走既有的最泛兜底（"600519 怎么样" → 报价）。
    if _is_retired_capability_question(lowered, text or ""):
        return NO_FACTUAL_TOOL

    return DEFAULT_FACTUAL_TOOL if has_symbol else DEFAULT_MARKET_TOOL


def looks_like_market_level_factual(text: str) -> bool:
    """无标的的**市场级事实**问句（「今天涨停板有多少只」）。

    这类问句此前只有一个落点：全市场筛选（且要先过筛选标记词），否则直接 400
    追问"请输入代码"。而平台本就有涨停池/板块/全局新闻这些**不需要标的**的工具，
    让用户为一句"涨停板有多少只"去补一个代码，是把平台的能力藏起来。

    判据刻意收窄：必须命中轻档事实标记，且**不需要标的**的工具确实存在。
    这样"今天大盘怎么样""现在市场情绪如何"仍不会被误放进流程——它们需要的是
    P2 才有的主线/情绪工具，今天放进来只会答"无可用证据"。

    退役能力造成的"无落点"（`NO_FACTUAL_TOOL`）同样算这一档，但**不是**因为
    "有工具可答"：它是无标的的市场级问句，必须走这条路才能同时避开两个错误——
    走追问分支会为一句"龙虎榜有多少只"去要股票代码，走 `DEFAULT_*` 兜底会拿
    板块行情来答。落成市场级回合 + 空计划后，回答层如实报"数据未覆盖"。
    """
    t = (text or "").strip()
    if not t:
        return False
    if any(marker in t for marker in _PORTFOLIO_MARKERS):
        return False
    if not any(marker in t for marker in _LIGHT_MARKERS):
        return False
    planned = plan_factual_tool(t, has_symbol=False)
    if planned == NO_FACTUAL_TOOL:
        return True
    return planned in MARKET_LEVEL_TOOLS


@dataclass(frozen=True)
class Resolution:
    """标的解析结果。`candidates` 非空表示需要用户选择。"""

    symbols: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteDecision:
    """一次路由决策，同时是审计记录。"""

    tier: str
    confidence: float
    symbol: str | None = None
    symbols: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()
    degraded_from: str | None = None
    reason: str = ""
    needs_clarification: bool = False
    classifier: str = ""

    @property
    def budget(self) -> TierBudget:
        return TIER_BUDGETS[self.tier]

    def as_record(self) -> dict[str, object]:
        """落库/日志用的扁平记录。"""
        return {
            "tier": self.tier,
            "confidence": self.confidence,
            "symbol": self.symbol,
            "symbols": list(self.symbols),
            "candidates": list(self.candidates),
            "degraded_from": self.degraded_from,
            "reason": self.reason,
            "needs_clarification": self.needs_clarification,
            "classifier": self.classifier,
        }


# ── 第 4 类意图：无标的的全市场筛选 ──────────────────────────────────────
#
# 用户问「今天走到好的票，放量票找十个给我」，旧行为是 400「未能识别标的」——
# 把一个**合法的筛选需求**误报成「你没说清楚」。而平台其实已有成熟的全市场
# 扫描器（`api/services/market_scanner_service.py`），只是没接进对话工具面。
#
# 检测刻意放在这一层（零 LLM 规则层）：可在毫秒内穷举、可单测、不产生成本。

# 强筛选名词：单独出现即可判为筛选意图
_SCREEN_NOUNS: tuple[str, ...] = (
    "选股",
    "筛选",
    "涨幅榜",
    "涨停板",
    "全市场",
    "整个市场",
    "扫一遍",
    "扫全市场",
    "放量票",
    "强势票",
    "哪些票",
    "哪几只",
)

# 账户/持仓类措辞：这些问题的答案是**用户的组合**，不是市场行情。
# 缺了这道护栏，「我账户现在仓位多少」会被当成市场级事实问题，然后用板块行情
# 去回答——比诚实追问糟得多（黄金集 clarify 指标抓到的那次回归）。
_PORTFOLIO_MARKERS: tuple[str, ...] = (
    "我持仓", "我的持仓", "我账户", "我的账户", "我的仓位", "我仓位", "我买", "我持有",
    "自选", "跟踪池", "持仓里", "仓位", "成本价", "我的票", "我这几只",
)

# 计数问句：要的是一个数，不是一份名单
_COUNT_QUESTION_MARKERS: tuple[str, ...] = (
    "有多少只", "有多少家", "有多少个", "几家", "几只涨停", "多少个涨停", "涨停家数",
)

# 数量信号（「找十只」「挑5个」）
_QUANTITY_RE = None  # 延迟构造，见 `_quantity_requested`

_SCREEN_ACTIONS: tuple[str, ...] = ("找", "挑", "选", "推荐", "来", "给")

# 扫描器允许的候选数区间（与 `scan_market_candidates` 的夹取一致）
SCREEN_MAX_TOP_K = 20
SCREEN_DEFAULT_TOP_K = 10

_CN_DIGITS: dict[str, int] = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

# 措辞 → 评分档案。顺序即优先级：先匹配更具体的（「放量」先于「强势」）。
_PROFILE_HINTS: tuple[tuple[str, str], ...] = (
    ("放量", "breakout"),
    ("突破", "breakout"),
    ("强势", "ashare_aggressive"),
    ("进攻", "ashare_aggressive"),
    ("回调", "reversal"),
    ("低吸", "reversal"),
    ("超跌", "reversal"),
    ("轮动", "sector_rotation"),
    ("板块", "sector_rotation"),
    ("稳健", "ashare_robust"),
    ("低波动", "low_volatility_value"),
    ("美股", "us_balanced"),
)


def _quantity_requested(text: str) -> bool:
    """是否明确要了**数量**（几只/十只/5个）。"""
    import re

    if re.search(r"\d+\s*(?:只|个|支|票)", text):
        return True
    if re.search(r"[一二两三四五六七八九十]\s*(?:只|个|支|票)", text):
        return True
    return any(word in text for word in ("几只", "几支", "几个票"))


def looks_like_market_screen(text: str) -> bool:
    """是否属于「无标的的全市场筛选」。

    **只判措辞，不判标的**：调用方必须在标的解析失败时才会用到它，
    也因此「含显式代码」的问句不会走到这里（见 `build_turn_request`）。

    两档判据，避免过宽：
    * 强名词（选股/筛选/放量票/哪些票…）单独命中即可；
    * 否则必须**同时**有动作词与数量词（「帮我找十只」），
      这样「帮我看看哪只最危险」这类单标的/组合问句不会被误判成筛选。
    """
    t = (text or "").strip()
    if not t:
        return False
    # 计数问句不是筛选需求：「今天涨停板有多少只」问的是一个**数**，
    # 平台有现成的涨停池工具，去跑一遍全市场扫描既慢又答非所问。
    if any(marker in t for marker in _COUNT_QUESTION_MARKERS):
        return False
    # 账户类问句优先，**除非**同时明确要了数量。
    #
    # 实测：「我的自选股有哪些」被判成全市场筛选——子串「自**选股**」里含强名词
    # 「选股」。而平台本来有 `watchlist_read` 能直接回答，结果却去跑了一遍扫描
    # （还因参数错误失败），回答成了"本轮无可用证据"。
    #
    # 判据只有一条（数量词），不用逐个往例外表里加词：要了数量就是想要一份名单，
    # 没有数量就是在问自己的账户。
    if any(marker in t for marker in _PORTFOLIO_MARKERS) and not _quantity_requested(t):
        return False
    if any(noun in t for noun in _SCREEN_NOUNS):
        return True
    return any(action in t for action in _SCREEN_ACTIONS) and _quantity_requested(t)


@dataclass(frozen=True)
class ScreenRequest:
    """一次筛选请求的解析结果。纯函数产出，不触网、不调模型。"""

    top_k: int = SCREEN_DEFAULT_TOP_K
    profile: str | None = None
    min_change_pct: float = 0.0


def parse_screen_request(text: str) -> ScreenRequest:
    """从筛选问句里解析候选数量、评分档案与最低涨幅。

    数量**必须夹取**到扫描器区间内：让正则或模型去猜「多一些」是几十只，
    必然两难——猜大了白扫，猜小了不够用。
    """
    import re

    t = text or ""

    top_k = SCREEN_DEFAULT_TOP_K
    match = re.search(r"(\d+)\s*(?:只|个|支|票)", t)
    if match:
        top_k = int(match.group(1))
    else:
        match = re.search(r"([一二两三四五六七八九十])\s*(?:只|个|支|票)", t)
        if match:
            top_k = _CN_DIGITS.get(match.group(1), SCREEN_DEFAULT_TOP_K)
    top_k = max(1, min(top_k, SCREEN_MAX_TOP_K))

    profile: str | None = None
    for hint, name in _PROFILE_HINTS:
        if hint in t:
            profile = name
            break

    # 回调/低吸要找的是**下跌或整理**中的票，用「最低涨幅 0」的过滤会自相矛盾
    min_change_pct = -10.0 if profile == "reversal" else 0.0
    return ScreenRequest(top_k=top_k, profile=profile, min_change_pct=min_change_pct)


class SymbolResolver(Protocol):
    """标的解析端口。实现由应用层提供（复用既有股票名解析能力）。"""

    def resolve(self, text: str) -> Resolution: ...


class TierClassifier(Protocol):
    def classify(self, text: str) -> RouteDecision: ...


# ── 规则分类器 ────────────────────────────────────────────────────────────

# 事实型：问「是多少」而不是「怎么样」。这些只需取数。
_LIGHT_MARKERS = (
    "多少钱",
    "什么价",
    "现在价",
    "价格是",
    "换手",
    "涨幅",
    "跌幅",
    "净流入多少",
    "净流出多少",
    "成交量",
    "市值",
    "市盈率",
    "多少",
    # 新闻/公告是事实型：此前没进这份表，于是「最近有什么新闻」落到中档去跑探针
    # （5× 成本且答非所问）——黄金集 f06 抓到的。
    "新闻",
    "公告",
    "消息面",
    # 本轮新增的证据工具对应的问法。缺了它们，「最近筹码集中度怎么样」会落进中档
    # 去跑洗盘探针——用户问筹码集中度，拿到的是洗盘判定。**档位标记表就是路由**：
    # 不在表里的问法不是"分类不准"，而是根本走不到那个工具。
    "筹码",
    "集中度",
    "获利比例",
    "分时",
    "承接",
    "指数",
    "点位",
    # 报告类：**读**既有结论（重档判定在前，"出个研报"不会被这里截走）
    "多视角",
    "共识",
    "研报结论",
    "报告结论",
)

# 研判型：需要把多维证据合成一个判断。这是本变更的核心新增能力。
_MEDIUM_MARKERS = (
    "洗盘",
    "震仓",
    "派发",
    "出货",
    "是不是在",
    "还能不能拿",
    "还能拿",
    "该不该",
    "要不要",
    "止损",
    "止盈",
    "会不会",
    "怎么看",
    "能买吗",
    "可以买",
    "风险大不大",
    "能不能拿",
)

# 重档：必须由用户**明示**要求完整研究，而不是模型推断「他大概想要研报」
_HEAVY_MARKERS = (
    "全面分析",
    "完整分析",
    "深度分析",
    "多智能体",
    "全流程",
    "全面研判",
)

# 「研报/报告」类是**组合判据**：必须同时出现产物名与生成动作。
# 单看产物名会把「这份研报说了什么」（阅读既有报告）误判为重档——
# 那是一个事实型问句，启动完整管道完全是浪费。
_REPORT_NOUNS = ("研报", "研究报告", "分析报告", "报告")
_REPORT_VERBS = ("出", "生成", "写", "做", "来一份", "来份", "给我一份", "整理")


def _requests_report_generation(text: str) -> bool:
    return any(noun in text for noun in _REPORT_NOUNS) and any(verb in text for verb in _REPORT_VERBS)


class RuleTierClassifier:
    """零 LLM 的规则分类器。

    刻意保守：只认明确的措辞。含糊的问句落到中档（lower-cost adjacent tier），
    宁可多花 5× 也不冒 100× 误升级的风险。
    """

    name = "rule"

    def __init__(self, resolver: SymbolResolver) -> None:
        self._resolver = resolver

    def classify(self, text: str) -> RouteDecision:
        text = (text or "").strip()
        resolution = self._resolver.resolve(text)
        symbols: Sequence[str] = resolution.symbols
        candidates: Sequence[str] = resolution.candidates
        symbol = symbols[0] if symbols else None

        # 重档必须显式；不含重档措辞的一律向下
        tier = TIER_MEDIUM
        confidence = 0.6
        reason = "default medium"

        if any(marker in text for marker in _HEAVY_MARKERS) or _requests_report_generation(text):
            tier, confidence, reason = TIER_HEAVY, 0.9, "explicit full-research request"
        elif any(marker in text for marker in _MEDIUM_MARKERS):
            tier, confidence, reason = TIER_MEDIUM, 0.75, "judgement question"
        elif any(marker in text for marker in _LIGHT_MARKERS):
            tier, confidence, reason = TIER_LIGHT, 0.8, "factual data question"

        needs_clarification = not symbols and not candidates
        if candidates and not symbols:
            needs_clarification = True
            reason = "multiple candidate instruments; awaiting selection"
        elif needs_clarification:
            reason = "no instrument resolved; awaiting clarification"

        return RouteDecision(
            tier=tier,
            confidence=confidence,
            symbol=symbol,
            symbols=tuple(symbols),
            candidates=tuple(candidates),
            reason=reason,
            needs_clarification=needs_clarification,
            classifier=self.name,
        )


class DegradingClassifier:
    """包装任意分类器，施加统一的向下降级语义。

    这样「分类器失败怎么办」只有一处实现，而不是每个分类器各写一遍
    try/except（那必然导致某些路径忘记降级而直接抛错或误判重档）。
    """

    name = "degrading"

    def __init__(
        self,
        inner: TierClassifier,
        *,
        min_confidence: float = 0.6,
        fallback_tier: str = TIER_MEDIUM,
    ) -> None:
        self._inner = inner
        self._min_confidence = min_confidence
        self._fallback_tier = fallback_tier

    def classify(self, text: str) -> RouteDecision:
        try:
            decision = self._inner.classify(text)
        except Exception as exc:  # noqa: BLE001 — 任何分类失败都必须降级而非中断
            return RouteDecision(
                tier=self._fallback_tier,
                confidence=0.0,
                reason=f"classifier unavailable ({type(exc).__name__}); degraded to {self._fallback_tier}",
                classifier=self.name,
            )

        if decision.confidence >= self._min_confidence:
            return replace(decision, classifier=decision.classifier or self.name)

        lower = _LOWER.get(decision.tier, TIER_LIGHT)
        return replace(
            decision,
            tier=lower,
            degraded_from=decision.tier,
            reason=f"{decision.reason}; low confidence {decision.confidence:.2f} degraded {decision.tier}→{lower}",
            classifier=self.name,
        )


class FixedClassifier:
    """测试与调试用的固定分类器。"""

    name = "fixed"

    def __init__(self, tier: str, confidence: float = 0.9) -> None:
        self._tier = tier
        self._confidence = confidence

    def classify(self, text: str) -> RouteDecision:
        return RouteDecision(
            tier=self._tier,
            confidence=self._confidence,
            reason="fixed",
            classifier=self.name,
        )


def tier_distribution(decisions: Sequence[RouteDecision]) -> dict[str, int]:
    """档位分布：发现「重档失控」的唯一手段。"""
    out = {tier: 0 for tier in TIERS}
    for decision in decisions:
        out[decision.tier] = out.get(decision.tier, 0) + 1
    return out


# ── 计划（Plan）：LLM 规划层与规则层共用的落点 ────────────────────────────
#
# 为什么要有独立的结构而不是直接返回 RouteDecision：规划层要回答的不只是
# "哪一档"，还有"打算调哪些工具、要不要追问"。计划**必须可校验、可落库、可重放**
# ——否则同一句话两次运行走不同路径，校准与回测就失去意义（这正是本平台与
# "随手写个 agent" 的分界）。

# 意图枚举与黄金集（tests/golden/questions.jsonl）保持一致：同一套词汇才能用
# 同一把尺子度量，否则规划准确率无从计算。
PLAN_INTENTS: tuple[str, ...] = (
    "single_symbol_judgement",
    "factual_lookup",
    "portfolio_review",
    "market_screen",
    "multi_symbol_compare",
    "market_view",
    "history_lookup",
    "concept_qa",
    "action_request",
)

PLAN_SOURCES: tuple[str, ...] = ("rules", "llm")

# 单轮计划最多几个工具步骤。上限存在的意义是成本：步数不可预知 = 成本不可预知，
# 而档位预算是硬约束。
MAX_PLAN_STEPS = 3


@dataclass(frozen=True)
class PlanStep:
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class Plan:
    """一次对话回合的**计划**。可校验、可落库、可重放。"""

    intent: str
    tier: str = TIER_MEDIUM
    symbol: str | None = None
    symbols: tuple[str, ...] = ()
    screen: bool = False
    market_level: bool = False
    steps: tuple[PlanStep, ...] = ()
    need_clarify: bool = False
    clarify_question: str = ""
    confidence: float = 0.0
    source: str = "rules"

    def as_record(self) -> dict[str, Any]:
        """落库用扁平记录。"""
        return {
            "intent": self.intent,
            "tier": self.tier,
            "symbol": self.symbol,
            "symbols": list(self.symbols),
            "screen": self.screen,
            "market_level": self.market_level,
            "need_clarify": self.need_clarify,
            "confidence": round(float(self.confidence), 4),
            "source": self.source,
            "steps": [
                {"tool": step.tool, "arguments": dict(step.arguments), "reason": step.reason}
                for step in self.steps
            ],
        }


# 模型偶尔用词而不是数字表达置信度。理解它们不是"放水"：`confidence` 只进
# `RouteDecision.confidence` 与 trace，不改变答案的含义，而为了它丢掉一个正确的
# 计划，用户拿到的是"这轮什么都没查"。（实测：「这只票要不要止损」的计划意图、
# 追问文本全对，只因写了 `"high"` 被整体拒绝。）
_CONFIDENCE_WORDS: Mapping[str, float] = {
    "high": 0.9, "高": 0.9,
    "medium": 0.6, "mid": 0.6, "中": 0.6,
    "low": 0.3, "低": 0.3,
}


def _coerce_confidence(raw: Any) -> float | None:
    """置信度 → [0,1]。**读不懂返回 None**（调用方整体拒绝）。"""
    if raw is None or raw == "":
        return 0.0
    if isinstance(raw, bool):  # bool 是 int 的子类，先挡掉
        return None
    if isinstance(raw, (int, float)):
        return min(max(float(raw), 0.0), 1.0)
    if isinstance(raw, str):
        text = raw.strip()
        word = _CONFIDENCE_WORDS.get(text.lower())
        if word is not None:
            return word
        try:
            return min(max(float(text), 0.0), 1.0)
        except ValueError:
            return None
    return None


def parse_plan(
    payload: Mapping[str, Any] | None,
    *,
    known_tools: Iterable[str],
    max_steps: int = MAX_PLAN_STEPS,
) -> Plan | None:
    """把 LLM 的结构化输出解析成 `Plan`；**语义违规一律返回 None**（由调用方降级）。

    为什么整体拒绝而不是"修一修"：计划的每一步都改变答案的含义。悄悄丢掉一个
    未知工具步骤，会让回答看起来"查过了"，而实际上少了一大块证据——那正是
    本平台最不能接受的失败形态。宁可降级到规则层 + 追问。

    **"归一化"与"修一修"的分界**：只接受**不丢失信息**的形状转换——把模型显然
    想表达的东西读对。以下两类属于归一化，实测都真实发生过，代价是整轮不可用：

      * `steps: ["watchlist_read"]`（工具名数组）→ `{tool, arguments:{}}`
      * `confidence: "high"`（表述型）→ 0.9

    而"未知工具""意图不在枚举""步数超限"这类**语义**违规仍然整体拒绝：它们各自
    都改变了答案的含义，猜测只会让回答骗取信任。
    """
    if not isinstance(payload, Mapping):
        return None

    intent = str(payload.get("intent") or "").strip()
    if intent not in PLAN_INTENTS:
        return None

    tier = str(payload.get("tier") or TIER_MEDIUM).strip()
    if tier not in TIERS:
        return None

    known = {str(name) for name in known_tools}
    raw_steps = payload.get("steps") or []
    if not isinstance(raw_steps, (list, tuple)):
        return None
    if len(raw_steps) > max_steps:
        return None

    steps: list[PlanStep] = []
    seen_steps: set[tuple[str, str]] = set()
    for raw in raw_steps:
        if isinstance(raw, str):
            # 工具名数组：模型显然想调这个工具，信息一点没丢。
            raw = {"tool": raw}
        if not isinstance(raw, Mapping):
            return None
        tool = str(raw.get("tool") or "").strip()
        if not tool or tool not in known:
            # 未知工具 = 计划不可执行。整体拒绝，不静默跳过。
            return None
        arguments = raw.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            return None
        # 去重：同工具 + 同参数的重复步骤是纯浪费（实测规划器把
        # `report_consensus_read` 连排两次）。不去重的话，一次对话会打两遍同一网络请求，
        # 证据也会重复占用引用编号。
        import json as _json

        fingerprint = (tool, _json.dumps(dict(arguments), sort_keys=True, ensure_ascii=False))
        if fingerprint in seen_steps:
            continue
        seen_steps.add(fingerprint)
        steps.append(
            PlanStep(tool=tool, arguments=dict(arguments), reason=str(raw.get("reason") or ""))
        )

    symbol_raw = payload.get("symbol")
    symbol = str(symbol_raw).strip() if isinstance(symbol_raw, str) and symbol_raw.strip() else None

    symbols_raw = payload.get("symbols") or ()
    if not isinstance(symbols_raw, (list, tuple)):
        return None
    symbols: list[str] = []
    for item in symbols_raw:
        text = str(item or "").strip()
        if text and text not in symbols:
            symbols.append(text)

    need_clarify = bool(payload.get("need_clarify"))
    clarify_question = str(payload.get("clarify_question") or "").strip()
    if need_clarify and not clarify_question:
        # 追问却没有问题文本 = 用户只会看到空白
        return None

    confidence = _coerce_confidence(payload.get("confidence"))
    if confidence is None:
        return None

    source = str(payload.get("source") or "llm").strip()
    if source not in PLAN_SOURCES:
        return None

    return Plan(
        intent=intent,
        tier=tier,
        symbol=symbol,
        symbols=tuple(symbols),
        screen=bool(payload.get("screen")),
        market_level=bool(payload.get("market_level")),
        steps=tuple(steps),
        need_clarify=need_clarify,
        clarify_question=clarify_question,
        confidence=confidence,
        source=source,
    )


# ── LLM 规划层 ────────────────────────────────────────────────────────────
#
# 定位：**规则定不了标的时候才用它**。规则层已经能判定的高频问句（带代码/公司名
# 的研判、事实、筛选）走零 LLM 路径；规则层判不出标的——也就是"任意自然语言"
# 失败的场景——才交给 LLM 规划。这样成本花在真正需要的地方，且规则层仍是
# 确定性降级目标（LLM 一挂就回落）。

# 规则判定"已有落点"的最低置信度。低于它说明分类器自己也没把握，交给 LLM。
RULES_DECISIVE_CONFIDENCE = 0.75


class PlanError(RuntimeError):
    """规划失败。调用方据此降级到规则层，而不是把异常抛给用户。"""


def _rules_are_decisive(decision: RouteDecision) -> bool:
    """规则层是否已经定下了这一回合的落点。

    判据只有一条但很关键：**是否已解析出标的或多候选**。已解析出来的问句不需要
    规划（工具是机械决定的），而"没解析出任何标的"正是自然语言失败的场景——
    旧实现在这里直接 400 追问，正是要交给 LLM 的地方。
    """
    if decision.needs_clarification:
        return False
    return decision.confidence >= RULES_DECISIVE_CONFIDENCE


class LlmPlanner:
    """让 LLM 给"规则定不了"的问句出结构化计划。

    实现 `TierClassifier` 协议，因此可以被既有的 `DegradingClassifier` 包裹
    ——降级语义只有一处实现，不必在这里重复写 try/except。
    """

    name = "llm"

    def __init__(
        self,
        *,
        inner: TierClassifier,
        complete: Callable[[str], str],
        known_tools: Iterable[str] | Sequence[str] = (),
        tool_digest: str = "",
        max_steps: int = MAX_PLAN_STEPS,
    ) -> None:
        self._inner = inner
        self._complete = complete
        self._known_tools = tuple(known_tools)
        self._tool_digest = tool_digest
        self._max_steps = max_steps

    # ── 规划 ──────────────────────────────────────────────────────────────
    def plan(self, text: str) -> Plan | None:
        """返回计划；规则层已定或 LLM 不可用时返回 None（调用方用规则结果）。"""
        try:
            decision = self._inner.classify(text)
        except Exception:  # noqa: BLE001 — 规则层异常等于"定不了"
            decision = None

        if decision is not None and _rules_are_decisive(decision):
            return None  # 零 LLM 路径：高频问句不该付规划成本

        prompt = self.build_prompt(text)
        try:
            raw = self._complete(prompt)
        except Exception as exc:  # noqa: BLE001 — 规划失败必须降级而非中断
            raise PlanError(f"规划器调用失败：{type(exc).__name__}") from exc

        payload = _loads_json(raw)
        plan = parse_plan(payload, known_tools=self._known_tools, max_steps=self._max_steps)
        if plan is None:
            # 计划被拒时必须留下**原始输出**：否则只能靠猜"为什么没走规划"。
            # 实测踩到：同一台服务里一个问句走通了规划、另一个静默退回规则层，
            # 而日志里什么都没有——这类静默降级是最难排查的形态。
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "plan rejected by schema; raw=%s known_tools=%d",
                str(raw)[:500], len(self._known_tools),
            )
        return plan

    def classify(self, text: str) -> RouteDecision:
        """`TierClassifier` 兼容入口：计划 → 路由决策（供既有编排使用）。"""
        try:
            plan = self.plan(text)
        except PlanError:
            return self._fallback(text)
        if plan is None:
            return self._inner.classify(text)
        return route_decision_from_plan(plan)

    # ── 提示词 ────────────────────────────────────────────────────────────
    def build_prompt(self, text: str) -> str:
        """把可用工具摘要与问句交给模型，要求**只输出 JSON**。

        工具摘要由调用方注入（`tool_digest`）：规划层不该自己去翻注册表——
        它只该看到一个名字与一句话说明的清单，避免把整个工具 schema 塞进上下文。

        ## 为什么要写"选择纪律"而不只是给清单

        实测规划器在"这只票要不要止损"上给出了判定工具**加**一个板块取数工具；
        "600519 和 300750 哪个好"同理多带了 `get_board_spot`。这些多出来的工具
        既不是必需、也不产生可引用证据，只增加成本与噪声（工具精确率因此被压到
        60.9%）。清单只解决"看不看得清"，"该不该多选"必须写成明文约束——指望
        模型从描述里自己悟出来是不现实的。
        """
        return (
            "你是投研对话的规划器。根据用户问句产出一个 JSON 计划，只输出 JSON。\n"
            f"可用工具：\n{self._tool_digest or '（无）'}\n\n"
            f"意图取值：{'/'.join(PLAN_INTENTS)}\n"
            f"档位取值：{'/'.join(TIERS)}\n"
            f"最多 {self._max_steps} 个工具步骤；不需要工具时 steps 为空数组。\n"
            "选择纪律（务必遵守）：\n"
            "1. 只选**回答这一句问句必需**的工具，宁少勿多；能用 1 个就不用 2 个。\n"
            "2. 工具面分三层：L1 是判断类（一个工具直接给出结论与依据），"
            "L2 是取数类（只给事实），L3 是分钟级任务（异步）。\n"
            "3. **不要**为同一个问题既选 L1 判断工具、又选它底下的 L2 取数工具"
            "——判断工具的结果里已经包含那些事实；禁止重复取同一份数据。\n"
            "4. 只有问句明确要求原始事实（例如「主力净流入多少」）时才选 L2。\n"
            "5. 用户没有指定标的、且问题是组合级（我的持仓/我的账户/我的仓位/我的计划/"
            "模拟盘/我的台账/我的自选/跟踪池）时，"
            "选 L1 里对应的「读取当前用户…」工具，并把 symbol 留空。\n"
            '必须输出这些键：intent, tier, symbol, symbols, screen, market_level, '
            'steps, need_clarify, clarify_question, confidence。\n'
            # 形状必须写出来：只列键名时模型会把 steps 写成工具名数组、把 confidence
            # 写成 "high"。虽然解析层现在能读懂这两种写法，但每读懂一次都是一次
            # 侥幸——把样例摆在眼前更省事，也更容易被审计。
            "steps 是**对象数组**，每项形如 "
            '{"tool": "工具名", "arguments": {}, "reason": "为什么选它"}，'
            "arguments 必须是对象（不是数组、不是字符串）。\n"
            "confidence 是 0 到 1 之间的**数字**。\n"
            '例如：{"intent": "single_symbol_judgement", "tier": "medium", '
            '"symbol": "600519.SH", "symbols": [], "screen": false, "market_level": false, '
            '"steps": [{"tool": "signal_washout_probe", "arguments": {"symbol": "600519.SH"}, '
            '"reason": "需要判定"}], "need_clarify": false, "clarify_question": "", '
            '"confidence": 0.8}\n'
            f"用户问句：{text}"
        )

    def _fallback(self, text: str) -> RouteDecision:
        """规划失败时的降级：沿用规则结果；规则也没结果则追问。"""
        try:
            return self._inner.classify(text)
        except Exception:  # noqa: BLE001
            return RouteDecision(
                tier=TIER_MEDIUM, confidence=0.0,
                reason="planner unavailable and rules failed; awaiting clarification",
                needs_clarification=True, classifier=self.name,
            )


def route_decision_from_plan(plan: Plan) -> RouteDecision:
    """计划 → 路由决策。让既有编排（档位预算、工具计划）无需改签名即可用上规划。"""
    return RouteDecision(
        tier=plan.tier,
        confidence=plan.confidence or 0.5,
        symbol=plan.symbol,
        symbols=plan.symbols,
        reason=f"llm plan: {plan.intent}",
        needs_clarification=plan.need_clarify,
        classifier="llm",
    )


def _loads_json(raw: str) -> Any:
    """解析模型输出；容忍 ```json 代码围栏（模型很爱加）。"""
    import json as _json

    text = str(raw or "").strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return _json.loads(text)
    except (ValueError, TypeError):
        return None

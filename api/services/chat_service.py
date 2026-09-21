"""对话服务：把路由、Agent 循环、持久化与真实依赖接起来。

## 这里是唯一的接线点

路由只管 HTTP；循环只管编排；本模块负责：

* 把请求体转成 `TurnRequest`（含标的解析）
* 装配端口工厂、回答模型、工具注册表
* 把循环事件落库（会话与消息）
* 供测试替换依赖（`build_answer_model` / `build_ports_factory` 是模块级函数）

## 为什么依赖是模块级函数而不是构造参数

测试要能在**没有网络、没有模型**的条件下跑通整条 HTTP 链路。用模块级工厂
函数，测试直接替换它即可；用构造参数则要穿透路由的依赖注入，成本高得多。
"""

from __future__ import annotations

import logging
import queue
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from pydantic import BaseModel, Field

from tradingagents.agent.events import (
    STATUS_COMPLETED,
    STATUS_DEGRADED,
    STATUS_FAILED,
    AnswerDone,
    EndEvent,
    ErrorEvent,
    EventSink,
    Notice,
    ToolDone,
    ToolStart,
    VerdictEvent,
)
from tradingagents.agent.loop import TurnRequest, TurnResult, run_turn
from tradingagents.grounding.types import TurnVerdict
from tradingagents.signals import DEFAULT_WEIGHTS

logger = logging.getLogger(__name__)

DISCLAIMER = (
    "本结论为基于可观测数据的概率化研究判断，不构成投资建议。"
)


class ChatRequest(BaseModel):
    """对话请求体。沿用 OpenAI 风格的 messages，兼容既有前端调用习惯。"""

    messages: list[dict[str, Any]] = Field(default_factory=list)
    tier: str | None = Field(default=None, description="light / medium / heavy，留空则自动判定")
    symbol: str | None = Field(default=None, description="显式指定标的，跳过解析")
    as_of: str | None = Field(default=None, description="评估基准日 YYYY-MM-DD")
    window_days: int = Field(default=20, ge=6, le=120)
    conversation_id: str | None = None
    model_profile_id: str | None = None


@dataclass
class _TurnDeps:
    registry: Any
    model: Any
    model_label: str = ""
    clock: Any = None
    middleware: Sequence[Any] = ()
    reference_date: str | None = None
    previous_trading_day: str | None = None


# ── 可替换的依赖工厂（测试替换点） ─────────────────────────────────────────


def build_ports_factory(user_id: str | None = None) -> Callable[[str, str], Any]:
    from api.services import signal_service

    return signal_service.build_ports_factory(user_id)


def build_news_fetcher():
    """新闻取数器。失败由新闻工具降级为「未覆盖」，不中断对话。"""
    from api.services.news_service import akshare_news_fetcher

    return akshare_news_fetcher


def build_registry(user_id: str | None = None) -> Any:
    """装配工具注册表。

    端口工厂与新闻取数器以**惰性包装**传入：注册表可能在 import 期（MCP 挂载）
    构建，而真实取数依赖应用层配置与网络。包装后，模块级工厂函数在**调用时**
    才被解析，因此：

    * import 期不触网，应用启动不受数据源可用性影响
    * 测试替换 `build_ports_factory` / `build_news_fetcher` 能真正生效
      （否则注册表会在替换之前就把真实实现捕获进闭包）
    """
    from tradingagents.tools import build_registry as _build

    def lazy_ports(symbol: str, as_of: str) -> Any:
        return build_ports_factory(user_id)(symbol, as_of)

    def lazy_news(symbol: str, start: str, end: str) -> Any:
        return build_news_fetcher()(symbol, start, end)

    def lazy_screener() -> Any:
        from api.services.market_screen_ports import build_market_screener

        return build_market_screener()

    # 扫描器也惰性构造：注册表可能在 import 期（MCP 挂载）构建，而真实扫描
    # 依赖应用层配置与网络。缺实现时不注册该工具（`build_registry` 既有语义），
    # 于是这里必须传一个真实对象而不是 None。
    return _build(
        ports_factory=lazy_ports, news_fetcher=lazy_news, market_screener=lazy_screener()
    )


def build_answer_model(
    *,
    user_id: str | None = None,
    model_profile_id: str | None = None,
    completer: Callable[[str], str] | None = None,
) -> Any:
    """构造回答模型。

    真实补全器**延迟到函数内部**构造。早期版本在调用处写成
    `build_answer_model(completer=build_llm_completer(...))`，默认参数会被
    预先求值——于是即使测试替换了本函数，真实的 LLM 客户端仍会被创建并因
    缺少 API Key 而失败。这类「装配顺序被隐式求值破坏」的问题在测试里表现为
    「明明打了补丁却没生效」，排查成本很高。
    """
    from tradingagents.agent.model import LlmAnswerModel

    if completer is not None:
        return LlmAnswerModel(completer)
    return LlmAnswerModel(
        build_llm_completer(user_id=user_id, model_profile_id=model_profile_id)
    )


def build_llm_completer(*, user_id: str | None = None, model_profile_id: str | None = None):
    """用用户配置的模型档案构造文本补全器。

    延迟导入 api.main 的配置装配逻辑，避免与入口模块形成循环导入。
    """
    from api.database import get_db_ctx
    from api.main import (
        _build_runtime_config,
        _merge_model_profile_overrides,
        _resolve_effective_model_profile_id,
    )
    from tradingagents.llm_clients.factory import create_llm_client

    # 与报告分析路径**同一套装配**（`api/main.py` 的 run-analysis 分支）：
    # 解析生效档案 → 取档案 overrides → 以 profile_selected 策略建运行时配置。
    #
    # 事故记录：这里曾经是 `_build_runtime_config({}, user_id=user_id, db=None)`——
    # 上面刚解析出的 `profile_id` 被丢弃，于是用户在设置里新建并设为默认的模型档案
    # 对对话**完全无效**，对话永远用 DEFAULT_CONFIG 的模型；缺 Key 时还会提示
    # "尚未配置可用的模型"，而用户明明配了（提示指错方向）。两条路径口径分叉，
    # 同一个账号做报告和做对话会用不同模型。
    with get_db_ctx() as db:
        profile_id = _resolve_effective_model_profile_id(
            db, user_id=user_id, model_profile_id=model_profile_id
        )
        overrides = _merge_model_profile_overrides(
            db, user_id=user_id, model_profile_id=profile_id, request_overrides={}
        )
    config = _build_runtime_config(
        overrides,
        user_id=user_id,
        db=None,
        trusted_overrides=True,
        # 选中档案时必须用该策略：它会剔除全局模型覆盖键，否则「设置页改的默认模型」
        # 会盖掉用户在当前档案里的选择，表现为"选了却不生效"。
        strategy="profile_selected" if profile_id else "default",
    )
    provider = config.get("llm_provider", "openai")
    model = config.get("deep_think_llm") or config.get("quick_think_llm")
    base_url = config.get("backend_url")
    # api_key 必须显式传入：`OpenAIClient` 只从 kwargs 透传它，否则会退到
    # OPENAI_API_KEY 环境变量——档案里的 Key 形同不存在。
    llm = create_llm_client(
        provider, model, base_url=base_url, api_key=config.get("api_key")
    ).get_llm()

    def complete(prompt: str):
        """补全并**如实**带回用量。

        `reported=False` 表示该 provider 未提供 usage——此时记 0，不估算。
        估算出来的 token 会把成本归因带偏，而成本归因是 14.2 验收的依据。
        """
        from tradingagents.agent.model import Completion

        response = llm.invoke(prompt)
        text = getattr(response, "content", str(response))
        usage = getattr(response, "usage_metadata", None) or {}
        if not usage:
            usage = (getattr(response, "response_metadata", None) or {}).get("token_usage") or {}
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        return Completion(
            text=str(text),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reported=bool(input_tokens or output_tokens),
        )

    complete.model_label = f"{provider}:{model}"  # type: ignore[attr-defined]
    return complete


def _resolve_model_label(*, user_id: str, body: ChatRequest) -> str:
    """取本次实际使用的模型标识（用于落库与按模型归因）。

    取不到时返回空串——**不猜**：填一个错误的模型名比留空更糟，
    因为它会让「哪个模型更准」的统计把两个模型混在一起。
    """
    try:
        from api.main import _build_runtime_config, _resolve_effective_model_profile_id
        from api.database import get_db_ctx

        with get_db_ctx() as db:
            profile_id = _resolve_effective_model_profile_id(
                db, user_id=user_id, model_profile_id=body.model_profile_id
            )
        config = _build_runtime_config({}, user_id=user_id, db=None)
        provider = config.get("llm_provider", "")
        model = config.get("deep_think_llm") or config.get("quick_think_llm") or ""
        return f"{provider}:{model}" if model else ""
    except Exception:  # noqa: BLE001
        return ""


def build_clock() -> Any:
    import time

    class _Clock:
        def now(self) -> float:
            return time.monotonic()

    return _Clock()


def resolve_symbol(text: str, explicit: str | None) -> tuple[str | None, tuple[str, ...]]:
    """解析标的。返回 (symbol, candidates)。

    复用既有的股票名解析能力（`api.main._normalize_symbol` 与本地股票映射），
    而不是另写一套——解析口径分叉会让「同一个名字在两个入口解析成不同代码」。
    """
    if explicit:
        from api.main import _normalize_symbol

        resolved = _normalize_symbol(explicit) or explicit
        return resolved, ()

    # 复用既有访问器（`/v1/market/stock-search` 用的同一个）：
    # 它明确「只用已预热缓存、不阻塞」，并且是 name → code 的正确极性。
    #
    # 事故记录：这里曾写成 `from api.main import _dual_stock_map`——该名字不存在，
    # 运行时 ImportError → 未处理异常 → 500。而 500 响应不带 CORS 头
    # （ServerErrorMiddleware 在 CORS 中间件之外），浏览器只报 Failed to fetch。
    from api.main import _get_stock_name_map_cached_only

    text = text or ""
    mapping = _get_stock_name_map_cached_only() or {}
    hits: list[str] = []
    for name, code in mapping.items():
        # 单字名不得参与匹配：「美」会让任何含该字的问句都命中
        if name and len(str(name)) >= 2 and str(name) in text:
            hits.append(str(code))

    # 显式写出的代码优先级最高：代码是用户的**显式陈述**，不能被模糊别名顶掉。
    # 顺序很关键——早期把代码匹配放在最后，于是 "600519.SH 是不是在洗盘" 被
    # 基金别名 "00" 抢先解析成了 007737.SZ。
    explicit_code = _match_explicit_code(text)
    if explicit_code:
        return explicit_code, ()

    # 用户常打简称（「茅台」而映射表是「贵州茅台」），全名匹配不到时要靠别名。
    # 两路结果**合并去重**而不是二选一：问句里同时出现一个全名和一个简称
    # （「茅台和宁德时代哪个好」）时，必须两个都进候选去问用户，不能只认全名那个。
    # 别名只接受全表唯一的中文后缀，有歧义的一律不猜——猜错标的比问一句糟得多。
    for code in _match_unique_aliases(text, mapping):
        if code not in hits:
            hits.append(code)
    # 去重并保持稳定顺序
    unique: list[str] = []
    for code in hits:
        if code not in unique:
            unique.append(code)
    if len(unique) == 1:
        return unique[0], ()
    if len(unique) > 1:
        return None, tuple(unique[:5])

    return None, ()


# 恰好 6 位、且前后不与其他数字相连——这样 "20260921" 不会被切成一个代码，
# 而 "SH600519" / "600519.SS" 仍能识别。
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?:\.(SH|SZ|SS))?(?!\d)", re.IGNORECASE)
_CODE_MARKETS = {"SH": "SH", "SS": "SH", "SZ": "SZ"}


def extract_explicit_codes(text: str) -> tuple[str, ...]:
    """抽出问句里**全部**显式写出的 A 股代码（去重、保序）。

    保留"全部"是关键：旧实现命中第一个就返回，于是
    「600519 和 300750 哪个好」只解析出 600519 —— 第二个标的被**静默丢掉**，
    回答以"我听懂了"的姿态只谈一只。那比 400 追问更危险，因为用户不会察觉。

    只认 6 位数字主体（可带 .SH/.SZ/.SS），避免把「10 只」「2026」当标的。
    """
    out: list[str] = []
    for match in _CODE_RE.finditer(text or ""):
        code = match.group(1)
        suffix = (match.group(2) or "").upper()
        # 中文市场的默认后缀：5/6/9 开头是沪市，其余深市（与 `_normalize_symbol` 一致）
        market = _CODE_MARKETS.get(suffix) or ("SH" if code[0] in "569" else "SZ")
        symbol = f"{code}.{market}"
        if symbol not in out:
            out.append(symbol)
    return tuple(out)


def _match_explicit_code(text: str) -> str | None:
    """单个代码场景：取第一个（语义保持不变），完整列表见 `extract_explicit_codes`。"""
    codes = extract_explicit_codes(text)
    return codes[0] if codes else None


# 对外措辞的实现放在 agent 层（`tradingagents/agent/failures.py`）：Agent 循环
# 自己的兜底分支也要用同一套说法，分层方向是 api → agent，所以实现不能在 api 侧。
# 这里再导出，保持 `chat_service.describe_turn_failure` 这个既有调用点不变。
from tradingagents.agent.failures import describe_turn_failure  # noqa: F401


def extract_text(messages: Sequence[Mapping[str, Any]]) -> str:
    """取最后一条用户消息。"""
    for message in reversed(list(messages or [])):
        if str(message.get("role", "")) == "user":
            return str(message.get("content", ""))
    return ""


def build_turn_request(body: ChatRequest, *, user_id: str) -> TurnRequest | None:
    """构造回合请求。无可用标的时返回 None（路由转成 400 追问）。

    解析异常被收敛成「无标的」而不是向外抛：未处理异常会变成 500，而 500 的
    响应缺 CORS 头，浏览器只能显示 Failed to fetch——真实原因被完全掩盖。
    """
    text = extract_text(body.messages)
    try:
        symbol, candidates = resolve_symbol(text, body.symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("symbol resolution failed, asking for clarification: %s", exc)
        symbol, candidates = None, ()

    # 多个显式代码 = 比较需求，必须**逐只**判定。放在解析之前：`resolve_symbol`
    # 只会给出第一个代码，走到下面就已经丢了一个标的。
    if not body.symbol:
        codes = extract_explicit_codes(text)
        if len(codes) >= 2:
            from tradingagents.agent.routing import DegradingClassifier, RuleTierClassifier
            from tradingagents.dataflows.trade_calendar import cn_today_str

            decision = DegradingClassifier(RuleTierClassifier(_Resolver(symbols=codes))).classify(text)
            return TurnRequest(
                text=text,
                user_id=user_id,
                tier=body.tier or decision.tier,
                symbol=None,
                symbols=codes,
                as_of=body.as_of or cn_today_str(),
                conversation_id=body.conversation_id,
            )

    if symbol is None and candidates:
        # 多候选：交给对话层追问，但仍构造一个无标的回合以产出追问文本。
        #
        # `candidates` **必须传下去**：此前这里漏了它，于是循环看到的是
        # "没标的也没候选"，只能给出泛泛的「请告诉我代码」，而解析层明明
        # 已经算出了候选名单——「你说的可能是以下哪一只？600519.SH、300750.SZ」
        # 这句追问因此从未在生产里出现过。同一个毛病：信息算出来了却在边界丢掉。
        return TurnRequest(
            text=text, user_id=user_id, tier=body.tier or "light",
            symbol=None, candidates=tuple(candidates),
            as_of=body.as_of or "", conversation_id=body.conversation_id,
        )
    if symbol is None:
        from tradingagents.agent.routing import TIER_MEDIUM, looks_like_market_screen

        # 无标的的**全市场筛选**（「今天放量票找十个给我」）是合法需求，不是
        # 「你没说清楚」。旧行为把它 400 掉，等于把平台已有的扫描能力藏起来。
        #
        # 「标优先」由这里的顺序保证：只有解析不出标的时才可能走到这一段，
        # 因此含显式代码的问句（「600519.SH 放量了吗」）绝不会触发全市场扫描。
        from tradingagents.agent.routing import looks_like_market_level_factual

        if looks_like_market_level_factual(text):
            # 无标的但**市场级事实**可答（涨停池/龙虎榜/板块）：不该逼用户补代码
            from tradingagents.dataflows.trade_calendar import cn_today_str

            return TurnRequest(
                text=text,
                user_id=user_id,
                tier=body.tier or "light",
                symbol=None,
                market_level=True,
                as_of=body.as_of or cn_today_str(),
                conversation_id=body.conversation_id,
            )
        if looks_like_market_screen(text):
            from tradingagents.dataflows.trade_calendar import cn_today_str

            return TurnRequest(
                text=text,
                user_id=user_id,
                # 扫描实测 10–60s，远超轻档 5s 目标延迟；给中档才有工具预算可用。
                tier=body.tier or TIER_MEDIUM,
                symbol=None,
                screen=True,
                as_of=body.as_of or cn_today_str(),
                conversation_id=body.conversation_id,
            )
        return None

    from tradingagents.agent.routing import (
        DegradingClassifier,
        RuleTierClassifier,
    )

    classifier = DegradingClassifier(
        RuleTierClassifier(_Resolver(symbol, candidates))
    )
    decision = classifier.classify(text)
    tier = body.tier or decision.tier

    from tradingagents.dataflows.trade_calendar import cn_today_str

    return TurnRequest(
        text=text,
        user_id=user_id,
        tier=tier,
        symbol=symbol,
        as_of=body.as_of or cn_today_str(),
        conversation_id=body.conversation_id,
    )


_ALIAS_MIN_LEN = 2
_ALIAS_MAX_LEN = 4
# 按映射对象身份缓存别名索引；value 里保留映射本身，确保 id 不会被回收复用
_ALIAS_INDEX_CACHE: dict[int, tuple[Mapping[str, str], dict[str, str]]] = {}
_AMBIGUOUS = ""


def _build_alias_index(mapping: Mapping[str, str]) -> dict[str, str]:
    """构建「唯一别名 → 代码」索引。

    别名取名称的所有后缀（2~4 字）。同一后缀若对应多个代码，记为歧义并
    丢弃——「银行」「证券」这类通用词因此不会被误判成某只股票。
    """
    index: dict[str, str] = {}
    for name, code in mapping.items():
        name = str(name or "")
        code = str(code or "")
        if len(name) < _ALIAS_MIN_LEN or not code:
            continue
        for start in range(len(name)):
            alias = name[start:]
            if not (_ALIAS_MIN_LEN <= len(alias) <= _ALIAS_MAX_LEN):
                continue
            # 只取**全中文**后缀。真实事故：基金「诺德研发创新100」的后缀产生
            # 纯数字别名 "00"，它在全表唯一，于是用户输入的 "600519" 里恰好含
            # "00" → 解析成那只基金 007737.SZ。用户问 A 股、系统答基金，
            # 而且是静默的——这类"数字串里抠出别名"必须从源头堵掉。
            if not all("\u4e00" <= ch <= "\u9fff" for ch in alias):
                continue
            existing = index.get(alias)
            if existing is None:
                index[alias] = code
            elif existing != code:
                index[alias] = _AMBIGUOUS
    return {alias: code for alias, code in index.items() if code}


def _alias_index(mapping: Mapping[str, str]) -> dict[str, str]:
    key = id(mapping)
    cached = _ALIAS_INDEX_CACHE.get(key)
    if cached is not None and cached[0] is mapping:
        return cached[1]
    index = _build_alias_index(mapping)
    if len(_ALIAS_INDEX_CACHE) > 3:
        _ALIAS_INDEX_CACHE.clear()
    _ALIAS_INDEX_CACHE[key] = (mapping, index)
    return index


def _match_unique_aliases(text: str, mapping: Mapping[str, str]) -> list[str]:
    """在问句上滑窗查别名表，长别名优先。

    只扫问句（几十字）而不是整张别名表（几十万条），单次请求开销与
    表规模无关。
    """
    index = _alias_index(mapping)
    found: list[str] = []
    for size in range(_ALIAS_MAX_LEN, _ALIAS_MIN_LEN - 1, -1):
        for start in range(len(text) - size + 1):
            code = index.get(text[start:start + size])
            if code and code not in found:
                found.append(code)
    return found


@dataclass
class _Resolver:
    symbol: str | None = None
    candidates: tuple[str, ...] = field(default_factory=tuple)
    symbols: tuple[str, ...] = field(default_factory=tuple)

    def resolve(self, text: str):
        from tradingagents.agent.routing import Resolution

        if self.symbols:
            return Resolution(symbols=tuple(self.symbols))
        if self.symbol:
            return Resolution(symbols=(self.symbol,))
        return Resolution(candidates=self.candidates)


# ── 事件出口：写队列 + 落库 ────────────────────────────────────────────────


class QueueSink:
    """把事件推给 SSE 生成器，同时记录终止状态、回答文本与判定用于落库。

    取消通过在此抛出 `KeyboardInterrupt` 实现——循环已经能捕获它并统一按
    `cancelled` 记账。用同一个机制而不是另加一条取消通道，避免「两条路径
    对取消的处理不一致」这种最难排查的分歧。
    """

    def __init__(self, events: "queue.Queue[Any]", is_cancelled: Callable[[], bool] | None = None) -> None:
        self._events = events
        self._is_cancelled = is_cancelled
        self.terminal_status: str | None = None
        self.answer = ""
        self.verdict: TurnVerdict | None = None

    def emit(self, event: Any) -> None:
        if self._is_cancelled is not None and self._is_cancelled():
            raise KeyboardInterrupt("client disconnected")
        self._events.put(event)
        if isinstance(event, EndEvent):
            self.terminal_status = event.status
        elif isinstance(event, AnswerDone):
            self.answer = event.text
        elif isinstance(event, VerdictEvent):
            self.verdict = event.verdict


class _Cancelled(Exception):
    pass


def run_turn_streaming(
    *,
    request: TurnRequest,
    user_id: str,
    body: ChatRequest,
    events: "queue.Queue[Any]",
    is_cancelled: Callable[[], bool],
    deps: _TurnDeps | None = None,
) -> TurnResult:
    """执行回合并落库。取消时按 cancelled 记账，不落为 completed。"""
    from tradingagents.agent.middleware import DEFAULT_MIDDLEWARE

    deps = deps or _TurnDeps(
        registry=build_registry(user_id),
        model=build_answer_model(
            user_id=user_id, model_profile_id=body.model_profile_id
        ),
        model_label=_resolve_model_label(user_id=user_id, body=body),
        clock=build_clock(),
        middleware=DEFAULT_MIDDLEWARE,
    )

    sink = QueueSink(events, is_cancelled)
    conversation_id = _ensure_conversation(
        user_id=user_id, conversation_id=body.conversation_id, symbol=request.symbol
    )
    # 续接同一会话：把 id 与历史一并交给回合。
    # 修复审计 A1——此前 id 只落库不回传、历史从不注入，多轮在结构上不可能发生。
    history = load_recent_history(user_id=user_id, conversation_id=conversation_id)
    model_label = getattr(deps, "model_label", "")
    request = replace(request, conversation_id=conversation_id, history=history)
    _persist_message(
        conversation_id=conversation_id, user_id=user_id, role="user", content=request.text
    )

    # 持仓上下文：已知则提供可引用证据，未知则强制回答声明「未提供持仓」。
    # 这是「不假装持仓感知已通」的落点——未知不是被忽略，而是被显式披露。
    position_context = None
    if request.symbol:
        try:
            from api.services.position_context_service import resolve_position

            position_context = resolve_position(user_id=user_id, symbol=request.symbol)
        except Exception as exc:  # noqa: BLE001 — 持仓解析失败不得阻断回答
            logger.info("position resolution failed for %s: %s", request.symbol, exc)

    result = run_turn(
        request=request,
        registry=deps.registry,
        model=deps.model,
        sink=sink,
        clock=deps.clock,
        middleware=deps.middleware,
        reference_date=deps.reference_date,
        previous_trading_day=deps.previous_trading_day,
        position_context=position_context,
        model_label=model_label,
    )

    verdict_id = _persist_verdict_if_any(
        sink.verdict, user_id=user_id, conversation_id=conversation_id
    )
    _persist_message(
        conversation_id=conversation_id,
        user_id=user_id,
        role="assistant",
        content=result.answer,
        tier=result.tier,
        tokens=result.tokens,
        latency_ms=result.latency_ms,
        status=result.status,
        model=result.model,
        verdict_id=verdict_id,
        tool_calls=result.tool_calls,
        weak_verification=result.weak_verification,
    )
    _persist_trace(
        request=request,
        result=result,
        user_id=user_id,
        conversation_id=conversation_id,
        verdict_id=verdict_id,
    )
    return result


# ── 持久化接线 ────────────────────────────────────────────────────────────


def _ensure_conversation(*, user_id: str, conversation_id: str | None, symbol: str | None) -> str:
    from api.database import ChatConversationDB, get_db_ctx
    from tradingagents.agent.repository import new_id

    with get_db_ctx() as db:
        if conversation_id:
            existing = (
                db.query(ChatConversationDB)
                .filter(ChatConversationDB.id == conversation_id,
                        ChatConversationDB.user_id == user_id)
                .first()
            )
            if existing is not None:
                return existing.id
        cid = conversation_id or new_id()
        db.add(
            ChatConversationDB(
                id=cid, user_id=user_id, title=(symbol or ""), primary_symbol=symbol
            )
        )
        db.commit()
    return cid


def _persist_trace(
    *,
    request: TurnRequest,
    result: TurnResult,
    user_id: str,
    conversation_id: str,
    verdict_id: str | None = None,
) -> None:
    """写这一轮的 trace（计划 / 逐步观测 / 校验结论 / 终态）。

    **失败只记日志、不外抛**：trace 是诊断数据，让它把一次已经成功产出的回答
    变成 500 是本末倒置。同理它也不该被 `_persist_message` 的失败牵连。
    """
    from api.services import trace_service

    try:
        trace_service.save_turn_trace(
            conversation_id=conversation_id,
            user_id=user_id,
            text=request.text,
            tier=result.tier,
            status=result.status,
            plan=result.plan,
            steps=result.steps,
            tool_calls=result.tool_calls,
            tokens=result.tokens,
            model_calls=result.model_calls,
            model=result.model,
            latency_ms=result.latency_ms,
            rejected_claims=result.rejected_claims,
            weak_verification=result.weak_verification,
            verdict_id=verdict_id,
            unavailable=result.unavailable_declared,
        )
    except Exception as exc:  # noqa: BLE001 — 诊断数据不得影响回答
        logger.warning("turn trace not persisted: %s", exc)


def _persist_message(
    *,
    conversation_id: str,
    user_id: str,
    role: str,
    content: str,
    tier: str = "",
    tokens: int = 0,
    latency_ms: int = 0,
    model: str = "",
    status: str = STATUS_COMPLETED,
    verdict_id: str | None = None,
    tool_calls: Sequence[str] = (),
    weak_verification: bool = False,
) -> None:
    from api.database import ChatMessageDB, get_db_ctx
    from tradingagents.agent.repository import new_id

    if not conversation_id:
        return
    with get_db_ctx() as db:
        db.add(
            ChatMessageDB(
                id=new_id(),
                conversation_id=conversation_id,
                user_id=user_id,
                role=role,
                content=content,
                tier=tier,
                tokens=tokens,
                latency_ms=latency_ms,
                model=model,
                status=status,
                verdict_id=verdict_id,
                # 只存摘要：单轮完整工具返回可达 10-50KB，会让年增量从 ~1.5GB
                # 推到 ~18GB；结构化判定已在 signal_verdicts 里
                tool_calls=list(tool_calls) or None,
                weak_verification=weak_verification,
            )
        )
        db.commit()


def _persist_verdict_if_any(
    verdict: TurnVerdict | None, *, user_id: str, conversation_id: str
) -> str | None:
    """把本轮的探针判定落库，供 T+3/T+5 回填。

    只在**非弃权**时落库：弃权判定没有可检验的预测，回填时无法定义对错，
    把它计入统计会污染命中率——弃权率是单独的一号指标，不是命中率的分母。
    """
    if verdict is None or verdict.state == "insufficient_evidence":
        return None
    from api.services import signal_service

    try:
        return signal_service.save_verdict_payload(
            verdict.payload, user_id=user_id, conversation_id=conversation_id
        )
    except Exception as exc:  # noqa: BLE001 — 落库失败不应让用户拿不到回答
        logger.warning("failed to persist verdict: %s", exc)
        return None


def load_recent_history(
    *, user_id: str, conversation_id: str, limit: int = 6
) -> tuple[dict[str, Any], ...]:
    """读取会话内最近若干轮问答，按时间升序。

    只取 `user_id` 匹配的行：跨用户读取会让别人的问题进入本次提示词——
    这是最严重的一类串号。
    """
    from api.database import ChatMessageDB, get_db_ctx

    if not conversation_id:
        return ()
    with get_db_ctx() as db:
        rows = (
            db.query(ChatMessageDB)
            .filter(
                ChatMessageDB.conversation_id == conversation_id,
                ChatMessageDB.user_id == user_id,
            )
            .order_by(ChatMessageDB.created_at.desc())
            .limit(max(limit, 1) * 2)
            .all()
        )
    ordered = list(reversed(rows))
    return tuple(
        {"role": str(r.role), "content": str(r.content or "")}
        for r in ordered
        if str(r.content or "").strip()
    )


def cost_by_tier(*, user_id: str) -> dict[str, dict[str, Any]]:
    """按档位归因成本：轮次、token、平均延迟。

    基于**真实落库的记账**，而不是测试替身的调用计数——14.2 的验收口径
    依赖它，所以它必须是真数据。
    """
    from api.database import ChatMessageDB, get_db_ctx

    report: dict[str, dict[str, Any]] = {}
    with get_db_ctx() as db:
        rows = (
            db.query(ChatMessageDB)
            .filter(ChatMessageDB.user_id == user_id, ChatMessageDB.role == "assistant")
            .all()
        )
    for row in rows:
        tier = str(row.tier or "unknown")
        bucket = report.setdefault(
            tier, {"turns": 0, "tokens": 0, "latency_ms": 0, "avg_latency_ms": 0}
        )
        bucket["turns"] += 1
        bucket["tokens"] += int(row.tokens or 0)
        bucket["latency_ms"] += int(row.latency_ms or 0)
    for bucket in report.values():
        bucket["avg_latency_ms"] = (
            round(bucket["latency_ms"] / bucket["turns"]) if bucket["turns"] else 0
        )
    return report


def list_conversations(*, user_id: str, limit: int = 50) -> dict[str, Any]:
    from api.database import ChatConversationDB, get_db_ctx

    with get_db_ctx() as db:
        rows = (
            db.query(ChatConversationDB)
            .filter(ChatConversationDB.user_id == user_id)
            .order_by(ChatConversationDB.created_at.desc())
            .limit(limit)
            .all()
        )
        items = [
            {
                "conversation_id": row.id,
                "title": row.title,
                "primary_symbol": row.primary_symbol,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    return {"items": items, "count": len(items)}


def get_conversation(*, user_id: str, conversation_id: str, limit: int = 100) -> dict[str, Any] | None:
    from api.database import ChatConversationDB, ChatMessageDB, get_db_ctx

    with get_db_ctx() as db:
        conversation = (
            db.query(ChatConversationDB)
            .filter(ChatConversationDB.id == conversation_id,
                    ChatConversationDB.user_id == user_id)
            .first()
        )
        if conversation is None:
            return None
        messages = (
            db.query(ChatMessageDB)
            .filter(ChatMessageDB.conversation_id == conversation_id,
                    ChatMessageDB.user_id == user_id)
            .order_by(ChatMessageDB.created_at.asc())
            .limit(limit)
            .all()
        )
        return {
            "conversation_id": conversation.id,
            "primary_symbol": conversation.primary_symbol,
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "tier": m.tier,
                    "status": m.status,
                    "verdict_id": m.verdict_id,
                    "tool_calls": m.tool_calls or [],
                    "weak_verification": bool(m.weak_verification),
                    "latency_ms": m.latency_ms,
                }
                for m in messages
            ],
        }

"""数据源探测：对「数据源 × 能力维度」发一次**真实最小请求**，把结果落库。

## 为什么是真实调用，不是 ping

"能不能连通"和"能不能取到数"是两件不同的事，本项目踩过两次：

* **东财**：TCP 连得上，接口被 anti-bot 直接 `RemoteDisconnected`。ping 全绿，数据全无。
* **Baostock（旧版）**：拨的是上游已废弃的 `www.baostock.com:10030`，TCP 直接不通
  （而 443 是开的）——单看"端口开放"会得出完全相反的结论。

所以探测必须真的走一次取数路径（provider 方法 + 最小参数），并记录**行数**。

## 为什么记录库版本

本次事故的核心变量就是**版本漂移**：`baostock 0.8.9` 拨老主机名（挂死 75 秒）、
上游迁到新域名后只有 `0.9.4` 能用。如果探测只记"失败"，事后根本无从判断
"是网络坏了，还是库太旧"。同理 `akshare` 1.18.30 → 1.18.97 之间修了多个本项目
在用的接口。版本指纹必须和时间一起存下来，才能回答"哪天开始坏的、当时什么版本"。

## 为什么串行而不是并发

这些源大多免费且按 IP 限流/封禁（东财已经封了）。探测的目的是**长期可用**，
不是为了快 10 秒——并发探测只会加速被封。逐个跑，代价是十几秒，换来的是不惹事。
"""

from __future__ import annotations

import importlib
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from sqlalchemy import desc
from sqlalchemy.orm import Session

from api.database import DataSourceProbeResultDB, DataSourceProbeRunDB
from tradingagents.dataflows.providers import build_default_registry
from tradingagents.dataflows.providers import support as provider_support
from tradingagents.dataflows.providers.capability_matrix import (
    BY_TOOL,
    CAPABILITIES,
    matrix_snapshot,
)
from tradingagents.dataflows.providers.errors import (
    SourceEmpty,
    SourceUnavailable,
    SourceUnsupported,
)
from tradingagents.dataflows.trade_calendar import (
    cn_today_str,
    is_cn_trading_day,
    previous_cn_trading_day,
)
from tradingagents.dataflows.vendor_text import (
    VENDOR_EMPTY_MARKERS,
    VENDOR_FAILURE_MARKERS,
    looks_like_empty_text,
    looks_like_failure_text,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 状态取值

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_TIMEOUT = "timeout"
STATUS_FAILED = "failed"
STATUS_UNSUPPORTED = "unsupported"

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"

#: 超过这个延迟就算"降级"——不是故障，但值得看见（免费源变慢通常是限流前兆）。
SLOW_LATENCY_MS = 4000

#: 单个探测目标的硬截止（秒）。没有它，一个挂死的源会拖住整轮探测。
DEFAULT_DEADLINE_SECONDS = 25.0

#: 探测用标的：贵州茅台（流动性最好、不会停牌退市）。
PROBE_SYMBOL = "600519.SH"
#: 探测用基准指数：沪深300（主线评分用的就是它）。
PROBE_BENCHMARK = "sh000300"
#: 板块类探测用行业名（新浪行业快照里有）。
PROBE_BOARD = "银行"
#: 探测取数的回看窗口（自然日），够拿到约 5 个交易日。
PROBE_LOOKBACK_DAYS = 12


@dataclass(frozen=True)
class ProbeContext:
    """一次探测轮次的公共参数（所有目标共用，保证结果可比）。"""

    symbol: str
    board: str
    day: str          # 最近一个**已收盘**交易日 YYYY-MM-DD
    start: str        # day 往前 PROBE_LOOKBACK_DAYS 天
    end: str          # = day
    benchmark: str = PROBE_BENCHMARK

    @classmethod
    def build(cls, *, today: str | None = None) -> "ProbeContext":
        today = today or cn_today_str()
        # 用"上一个交易日"而不是"今天"：09:00 探测时今日还没有数据，
        # 拿今天的空结果去判定源不可用是假故障。
        day = previous_cn_trading_day(today) or today
        start_dt = datetime.strptime(day, "%Y-%m-%d") - timedelta(days=PROBE_LOOKBACK_DAYS)
        return cls(
            symbol=PROBE_SYMBOL,
            board=PROBE_BOARD,
            day=day,
            start=start_dt.strftime("%Y-%m-%d"),
            end=day,
            benchmark=PROBE_BENCHMARK,
        )


@dataclass
class ProbeOutcome:
    source: str
    tool: str
    status: str
    latency_ms: int
    row_count: int | None = None
    #: `row_count` 的单位：`rows`（真实记录数）/ `lines`（文本的非空行数）。
    #: 不区分会让"N 行"被误读成"N 条记录"。
    row_unit: str | None = None
    version: str | None = None
    error_kind: str | None = None
    error_type: str | None = None
    error_message: str | None = None

    @property
    def label(self) -> str | None:
        cap = BY_TOOL.get(self.tool)
        return cap.label if cap else None

    @property
    def category(self) -> str | None:
        cap = BY_TOOL.get(self.tool)
        return cap.category if cap else None


# ------------------------------------------------------- 版本指纹（事故核心变量）

#: provider → 需要读版本的第三方包。项目内的直连 provider 标记为 builtin：
#: 它们的"版本"就是应用自身的代码，跟着部署走，没有独立版本号。
_SOURCE_PACKAGES: Mapping[str, str | None] = {
    "cn_akshare": "akshare",
    "cn_baostock": "baostock",
    "yfinance": "yfinance",
    "alpha_vantage": "alpha_vantage",
    "cn_sina_moneyflow": None,
    "cn_eastmoney_http": None,
    "cn_stub": None,
}


def source_version(source: str) -> str | None:
    """读该源依赖库的版本号（拿不到就返回 None，绝不抛）。"""
    package = _SOURCE_PACKAGES.get(source)
    if package is None:
        return "builtin" if source in _SOURCE_PACKAGES else None
    try:
        module = importlib.import_module(package)
    except Exception:  # 缺依赖本身是探测结论，不该在探测里再炸一次
        return None
    for attr in ("__version__", "version", "VERSION"):
        value = getattr(module, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# ------------------------------------------------------------- 最小真实调用

#: `row_count` 的单位标记
ROW_UNIT_ROWS = "rows"
ROW_UNIT_LINES = "lines"


def _rows_of(result: Any) -> tuple[int, str | None]:
    """把返回值折算成 `(规模, 单位)`。

    * DataFrame / list / tuple / set → `(长度, "rows")`：**真实记录数**
    * 文本产物（多数 tool 返回预格式化文本）→ `(非空行数, "lines")`：
      这是**载荷规模的代理指标**，足以区分"有数据"和"空壳成功"，
      但它**不是记录数**——所以必须带上单位，否则页面上的"N 行"
      会被读成"N 条记录"（表格里一行 ≠ 一条记录）。
    * None → `(0, None)`
    """
    if result is None:
        return 0, None
    if isinstance(result, (list, tuple, set)):
        return len(result), ROW_UNIT_ROWS
    if hasattr(result, "shape") and hasattr(result, "empty"):  # pandas DataFrame/Series
        try:
            return int(result.shape[0]), ROW_UNIT_ROWS
        except Exception:
            return 0, ROW_UNIT_ROWS
    text = str(result)
    if _looks_empty_text(text):
        return 0, ROW_UNIT_LINES
    return len([line for line in text.splitlines() if line.strip()]), ROW_UNIT_LINES


#: 「没数据」与「失败」的标记表**已收敛**到引擎层唯一权威处
#: `tradingagents/dataflows/vendor_text.py`。本层原先自存一份 `_EMPTY_MARKERS`，
#: 与工具层的表各认识一半文案，于是同一条返回在两层得到相反结论。现在只保留
#: 本层特有的**长度护栏**（见下），标记与判断都委托上去。
_EMPTY_MARKERS = VENDOR_EMPTY_MARKERS

#: provider **把异常吞成一句失败文案**再正常 return 的痕迹。
#: 这不是理论问题：`cn_akshare` 有 20+ 处这样写，例如
#:   return f"板块历史行情获取失败（无替代源）：{type(exc).__name__}: {exc}"
#: 这类返回值**不是异常**，探测如果按"非空文本 = 有数据"处理，就会把一个
#: **失败**记成"降级/慢"甚至"可用"——也就是"永不为空的成功"，最难发现的故障形态。
#: 实测 `get_board_cons` 报"降级、18611ms、1 行"，那 1 行正是失败文案。
_FAILURE_MARKERS = VENDOR_FAILURE_MARKERS

#: 失败文案都是**短句**；真正的数据载荷要长得多。
#: 加这个上限是为了避免把正文里恰好含"失败"二字的长报表误判成失败。
_FAILURE_TEXT_MAX_LEN = 500


def _looks_failed_text(text: str) -> bool:
    """识别「provider 把失败当成功返回」的文本。

    **长度护栏只属于本层**：探测拿到的可能是整篇报表，而工具层面对的始终是
    单次 vendor 返回。所以这里保留 500 字上限，判断本身委托权威实现。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > _FAILURE_TEXT_MAX_LEN:
        return False
    return looks_like_failure_text(stripped)


def _looks_empty_text(text: str) -> bool:
    """识别 provider 用"友好文案"表达的"没数据"。

    这类返回值**不是异常**，不识别就会被当成成功——"永不为空的成功"是最难发现的
    故障形态（本次事故里 Baostock 的失败就曾被记成"这个 provider 没实现"）。

    本层契约：**空文本也算空**（`_count_rows` 依赖它把空返回记成 0 行）。
    标记判断委托给 `vendor_text` 的唯一权威实现。
    """
    stripped = text.strip()
    if not stripped:
        return True
    return looks_like_empty_text(stripped)


def _call_daily_bars(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_daily_bars_df(ctx.symbol, ctx.start, ctx.end)


def _call_intraday_bars(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_intraday_bars(ctx.symbol, ctx.day, "5")


def _call_stock_data(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_stock_data(ctx.symbol, ctx.start, ctx.end)


def _call_indicators(provider: Any, ctx: ProbeContext) -> Any:
    # 30 天窗口足够验证指标链路；provider 内部会自行放大到 260 天以满足均线需要。
    return provider.get_indicators(ctx.symbol, "rsi", ctx.day, 30)


def _call_fundamentals(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_fundamentals(ctx.symbol, curr_date=ctx.day)


def _call_balance_sheet(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_balance_sheet(ctx.symbol, curr_date=ctx.day)


def _call_cashflow(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_cashflow(ctx.symbol, curr_date=ctx.day)


def _call_income_statement(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_income_statement(ctx.symbol, curr_date=ctx.day)


def _call_realtime_quotes(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_realtime_quotes([ctx.symbol])


def _call_news(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_news(ctx.symbol, ctx.start, ctx.end)


def _call_global_news(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_global_news(ctx.day, 3, 10)


def _call_insider(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_insider_transactions(ctx.symbol)


def _call_board_spot(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_board_spot("industry")


def _call_board_rank(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_board_rank("industry", "1d")


def _call_board_hist(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_board_hist(ctx.board, "industry")


def _call_board_cons(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_board_cons(ctx.board, "industry")


def _call_board_fund_flow(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_board_fund_flow()


def _call_individual_fund_flow(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_individual_fund_flow(ctx.symbol)


def _call_lhb(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_lhb_detail(ctx.symbol, ctx.day)


def _call_zt_pool(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_zt_pool(ctx.day)


def _call_hot_stocks(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_hot_stocks_xq()


def _call_chip(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_chip_distribution(ctx.symbol, ctx.day)


def _call_flow_frame(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_flow_frame(ctx.symbol, ctx.day)


def _call_peer_frames(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_peer_frames(ctx.symbol, ctx.day)


def _call_turnover(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_stock_turnover(ctx.symbol, ctx.day)


def _call_symbol_news(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_symbol_news(ctx.symbol, ctx.start, ctx.end)


def _call_index_daily_bars(provider: Any, ctx: ProbeContext) -> Any:
    return provider.get_index_daily_bars_df(ctx.benchmark, ctx.start, ctx.end)


#: 每个能力维度的最小真实调用。**新增能力维度时必须在这里补一条**，
#: 否则 `probe_targets()` 会因为找不到调用而拒绝探测（宁可少探，不要假探）。
PROBE_CALLS: Mapping[str, Callable[[Any, ProbeContext], Any]] = {
    "get_daily_bars_df": _call_daily_bars,
    "get_intraday_bars": _call_intraday_bars,
    "get_stock_data": _call_stock_data,
    "get_indicators": _call_indicators,
    "get_fundamentals": _call_fundamentals,
    "get_balance_sheet": _call_balance_sheet,
    "get_cashflow": _call_cashflow,
    "get_income_statement": _call_income_statement,
    "get_realtime_quotes": _call_realtime_quotes,
    "get_news": _call_news,
    "get_global_news": _call_global_news,
    "get_insider_transactions": _call_insider,
    "get_board_spot": _call_board_spot,
    "get_board_rank": _call_board_rank,
    "get_board_hist": _call_board_hist,
    "get_board_cons": _call_board_cons,
    "get_board_fund_flow": _call_board_fund_flow,
    "get_individual_fund_flow": _call_individual_fund_flow,
    "get_lhb_detail": _call_lhb,
    "get_zt_pool": _call_zt_pool,
    "get_hot_stocks_xq": _call_hot_stocks,
    "get_chip_distribution": _call_chip,
    "get_flow_frame": _call_flow_frame,
    "get_peer_frames": _call_peer_frames,
    "get_stock_turnover": _call_turnover,
    "get_symbol_news": _call_symbol_news,
    "get_index_daily_bars_df": _call_index_daily_bars,
}


def probe_targets() -> list[tuple[str, str]]:
    """要探测的 (source, tool) 列表——**唯一来源是能力矩阵**。

    早先这里在矩阵之外还硬编码了一份 `EXTRA_PROBE_TARGETS` 来补直连能力
    （基准指数日线），结果是"两个事实源"：矩阵改了、探测清单不跟着改。
    现在直连能力用 `routed=False` 登记进矩阵，这里统一遍历，不再另抄一份。

    矩阵里有、但没写探测调用的维度会被**明确告警并跳过**（宁可少探，不要假探）。
    """
    out: list[tuple[str, str]] = []
    for cap in CAPABILITIES:
        if cap.tool not in PROBE_CALLS:
            logger.warning(
                "[data-source-probe] 能力 %s 没有探测调用（PROBE_CALLS 缺项），已跳过",
                cap.tool,
            )
            continue
        for src in cap.sources:
            out.append((src.source, cap.tool))
    return out


# ------------------------------------------------------------------ 单点探测

def _classify_success(outcome: ProbeOutcome, started: float) -> None:
    outcome.latency_ms = int((time.monotonic() - started) * 1000)
    if outcome.status != STATUS_OK:
        return
    if not outcome.row_count:
        outcome.status = STATUS_DEGRADED
        outcome.error_kind = "empty"
        outcome.error_message = "调用成功但返回 0 行"
    elif outcome.latency_ms > SLOW_LATENCY_MS:
        outcome.status = STATUS_DEGRADED
        outcome.error_kind = "slow"
        outcome.error_message = f"延迟 {outcome.latency_ms}ms 超过阈值 {SLOW_LATENCY_MS}ms"


def probe_target(
    source: str,
    tool: str,
    ctx: ProbeContext,
    *,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    registry: Any = None,
) -> ProbeOutcome:
    """探测一个 (source, tool)：真实调一次，并归类结果。

    未实现的组合不会被调用（也不该出现在矩阵里）——但这里仍然防守一次：
    万一将来有人把方法改回占位，探测报告会**如实显示"不支持"**，而不是伪造成功。
    """
    outcome = ProbeOutcome(source=source, tool=tool, status=STATUS_OK, latency_ms=0)
    outcome.version = source_version(source)

    registry = registry or build_default_registry()
    provider = registry.get(source)
    if provider is None:
        outcome.status = STATUS_FAILED
        outcome.error_kind = "error"
        outcome.error_type = "NotRegistered"
        outcome.error_message = f"provider {source!r} 未注册"
        return outcome

    if tool not in provider_support.provider_implemented_methods(provider):
        outcome.status = STATUS_UNSUPPORTED
        outcome.error_kind = "unsupported"
        outcome.error_type = "SourceUnsupported"
        outcome.error_message = f"{source} 未实现 {tool}"
        return outcome

    call = PROBE_CALLS.get(tool)
    if call is None:
        outcome.status = STATUS_UNSUPPORTED
        outcome.error_kind = "no-probe-call"
        outcome.error_message = f"没有为 {tool} 定义探测调用"
        return outcome

    started = time.monotonic()
    # 硬截止：卡住的调用不能拖住整轮探测。注意这会**泄漏一个线程**
    # （与 `signal_service._run_with_deadline` 同样的取舍）——宁可泄漏，
    # 也不要让探测任务永远不返回。
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(call, provider, ctx)
        try:
            result = future.result(timeout=deadline_seconds)
        except FuturesTimeout:
            outcome.latency_ms = int((time.monotonic() - started) * 1000)
            # **超时单列一类，不算 failed**：一个"能用只是慢"的源和一个"彻底挂了"
            # 的源需要完全不同的处置（前者调窗口/加缓存，后者去修）。此前两者都是
            # 红色 failed，运维看到的信号是一样的。实测 `get_board_hist` 单个板块
            # 就要 30s+ —— 那是慢，不是坏。
            outcome.status = STATUS_TIMEOUT
            outcome.error_kind = "timeout"
            outcome.error_type = "Timeout"
            outcome.error_message = f"超过 {deadline_seconds:g}s 硬截止（线程已放弃）"
            return outcome
        outcome.row_count, outcome.row_unit = _rows_of(result)
        # 「把失败写成一句文案」必须按失败记账，不能按"有数据"。
        # 实测 get_board_cons 的 18611ms/1 行就是这个形态。
        if isinstance(result, str) and _looks_failed_text(result):
            outcome.status = STATUS_FAILED
            outcome.error_kind = "text-error"
            outcome.error_type = "SwallowedError"
            outcome.error_message = result.strip()[:500]
            outcome.row_count, outcome.row_unit = 0, ROW_UNIT_LINES
            return outcome
        _classify_success(outcome, started)
        return outcome
    except SourceUnsupported as exc:
        outcome.latency_ms = int((time.monotonic() - started) * 1000)
        outcome.status = STATUS_UNSUPPORTED
        outcome.error_kind = "unsupported"
        outcome.error_type = type(exc).__name__
        outcome.error_message = str(exc)
        return outcome
    except SourceUnavailable as exc:
        outcome.latency_ms = int((time.monotonic() - started) * 1000)
        outcome.status = STATUS_FAILED
        outcome.error_kind = "unavailable"
        outcome.error_type = type(exc).__name__
        outcome.error_message = str(exc)
        return outcome
    except SourceEmpty as exc:
        outcome.latency_ms = int((time.monotonic() - started) * 1000)
        outcome.status = STATUS_DEGRADED
        outcome.error_kind = "empty"
        outcome.error_type = type(exc).__name__
        outcome.error_message = str(exc)
        return outcome
    except Exception as exc:
        outcome.latency_ms = int((time.monotonic() - started) * 1000)
        outcome.status = STATUS_FAILED
        kind = getattr(exc, "kind", None)
        outcome.error_kind = kind if isinstance(kind, str) else "error"
        outcome.error_type = type(exc).__name__
        outcome.error_message = str(exc)[:500]
        return outcome
    finally:
        pool.shutdown(wait=False)


# -------------------------------------------------------------------- 整轮

@dataclass
class ProbeRunSummary:
    id: str
    started_at: datetime
    finished_at: datetime | None = None
    status: str = STATUS_OK
    trigger: str = TRIGGER_MANUAL
    trading_day: bool = True
    total_count: int = 0
    ok_count: int = 0
    degraded_count: int = 0
    timeout_count: int = 0
    failed_count: int = 0
    unsupported_count: int = 0
    duration_ms: int = 0
    note: str | None = None
    outcomes: list[ProbeOutcome] = field(default_factory=list)


def _summarize(outcomes: Sequence[ProbeOutcome]) -> dict[str, int]:
    counts = {
        STATUS_OK: 0,
        STATUS_DEGRADED: 0,
        STATUS_TIMEOUT: 0,
        STATUS_FAILED: 0,
        STATUS_UNSUPPORTED: 0,
    }
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    return counts


def _overall_status(counts: Mapping[str, int]) -> str:
    """整轮健康度：有失败即失败；否则超时/降级都算降级。

    注意 `unsupported` **不拉低**健康度：它是"这个源本来就没这能力"，
    属于配置事实，不是故障。`timeout` 则拉低到 degraded 而**不是** failed——
    它可能是"源很慢"而非"源坏了"，混为一谈会让告警失去意义。
    """
    if counts.get(STATUS_FAILED):
        return STATUS_FAILED
    if counts.get(STATUS_TIMEOUT) or counts.get(STATUS_DEGRADED):
        return STATUS_DEGRADED
    return STATUS_OK


def run_probe(
    db: Session,
    *,
    trigger: str = TRIGGER_MANUAL,
    ctx: ProbeContext | None = None,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    persist: bool = True,
    targets: Iterable[tuple[str, str]] | None = None,
    registry: Any = None,
) -> ProbeRunSummary:
    """跑一整轮探测，落库，并返回汇总。"""
    ctx = ctx or ProbeContext.build()
    started_wall = datetime.now(timezone.utc)
    started = time.monotonic()
    trading_day = is_cn_trading_day(cn_today_str())

    summary = ProbeRunSummary(
        id=str(uuid.uuid4()),
        started_at=started_wall,
        trigger=trigger,
        trading_day=trading_day,
    )

    target_list = list(targets if targets is not None else probe_targets())
    for source, tool in target_list:
        try:
            summary.outcomes.append(
                probe_target(
                    source,
                    tool,
                    ctx,
                    deadline_seconds=deadline_seconds,
                    registry=registry,
                )
            )
        except Exception as exc:  # 探测本身绝不允许打断整轮
            logger.exception("[data-source-probe] %s.%s 探测异常", source, tool)
            summary.outcomes.append(
                ProbeOutcome(
                    source=source,
                    tool=tool,
                    status=STATUS_FAILED,
                    latency_ms=0,
                    version=source_version(source),
                    error_kind="error",
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                )
            )

    counts = _summarize(summary.outcomes)
    summary.total_count = len(summary.outcomes)
    summary.ok_count = counts[STATUS_OK]
    summary.degraded_count = counts[STATUS_DEGRADED]
    summary.timeout_count = counts[STATUS_TIMEOUT]
    summary.failed_count = counts[STATUS_FAILED]
    summary.unsupported_count = counts[STATUS_UNSUPPORTED]
    summary.status = _overall_status(counts)
    summary.finished_at = datetime.now(timezone.utc)
    summary.duration_ms = int((time.monotonic() - started) * 1000)

    if persist:
        _persist(db, summary, ctx)
    return summary


def _persist(db: Session, summary: ProbeRunSummary, ctx: ProbeContext) -> None:
    db.add(
        DataSourceProbeRunDB(
            id=summary.id,
            started_at=summary.started_at,
            finished_at=summary.finished_at,
            status=summary.status,
            trigger=summary.trigger,
            trading_day=summary.trading_day,
            total_count=summary.total_count,
            ok_count=summary.ok_count,
            degraded_count=summary.degraded_count,
            timeout_count=summary.timeout_count,
            failed_count=summary.failed_count,
            unsupported_count=summary.unsupported_count,
            duration_ms=summary.duration_ms,
            note=summary.note,
        )
    )
    checked_at = summary.finished_at or summary.started_at
    for outcome in summary.outcomes:
        db.add(
            DataSourceProbeResultDB(
                id=str(uuid.uuid4()),
                run_id=summary.id,
                source=outcome.source,
                tool=outcome.tool,
                label=outcome.label,
                category=outcome.category,
                status=outcome.status,
                latency_ms=outcome.latency_ms,
                row_count=outcome.row_count,
                row_unit=outcome.row_unit,
                version=outcome.version,
                error_kind=outcome.error_kind,
                error_type=outcome.error_type,
                error_message=outcome.error_message,
                checked_at=checked_at,
            )
        )
    db.commit()


# ------------------------------------------------------------------- 读侧

def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def run_to_dict(row: DataSourceProbeRunDB) -> dict:
    return {
        "id": row.id,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "status": row.status,
        "trigger": row.trigger,
        "trading_day": bool(row.trading_day),
        "total_count": row.total_count or 0,
        "ok_count": row.ok_count or 0,
        "degraded_count": row.degraded_count or 0,
        #: 超时单列，**不计入 failed**——"慢"与"挂"要能分开看
        "timeout_count": row.timeout_count or 0,
        "failed_count": row.failed_count or 0,
        "unsupported_count": row.unsupported_count or 0,
        "duration_ms": row.duration_ms or 0,
        "note": row.note,
    }


def result_to_dict(row: DataSourceProbeResultDB) -> dict:
    return {
        "source": row.source,
        "tool": row.tool,
        "label": row.label,
        "category": row.category,
        "status": row.status,
        "latency_ms": row.latency_ms,
        "row_count": row.row_count,
        #: row_count 的单位（rows/lines），前端据此决定是否写"≈"
        "row_unit": row.row_unit,
        "version": row.version,
        "error_kind": row.error_kind,
        "error_type": row.error_type,
        "error_message": row.error_message,
        "checked_at": _iso(row.checked_at),
    }


def latest_run(db: Session) -> DataSourceProbeRunDB | None:
    return (
        db.query(DataSourceProbeRunDB)
        .order_by(desc(DataSourceProbeRunDB.started_at))
        .first()
    )


def recent_runs(db: Session, *, limit: int = 20) -> list[DataSourceProbeRunDB]:
    return (
        db.query(DataSourceProbeRunDB)
        .order_by(desc(DataSourceProbeRunDB.started_at))
        .limit(max(1, min(limit, 200)))
        .all()
    )


def latest_results(db: Session) -> list[DataSourceProbeResultDB]:
    """最新一轮的明细。没有探测记录时返回空列表（页面显示"还没探测过"）。"""
    run = latest_run(db)
    if run is None:
        return []
    return (
        db.query(DataSourceProbeResultDB)
        .filter(DataSourceProbeResultDB.run_id == run.id)
        .all()
    )


def history_points(
    db: Session,
    *,
    days: int = 30,
    source: str | None = None,
    tool: str | None = None,
    limit: int = 2000,
) -> list[DataSourceProbeResultDB]:
    """时间序列（画趋势用）。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 365)))
    query = db.query(DataSourceProbeResultDB).filter(
        DataSourceProbeResultDB.checked_at >= cutoff
    )
    if source:
        query = query.filter(DataSourceProbeResultDB.source == source)
    if tool:
        query = query.filter(DataSourceProbeResultDB.tool == tool)
    return (
        query.order_by(desc(DataSourceProbeResultDB.checked_at))
        .limit(max(1, min(limit, 10000)))
        .all()
    )


def status_payload(db: Session) -> dict:
    """`GET /v1/data-sources/status` 的响应体。"""
    run = latest_run(db)
    return {
        "latest_run": run_to_dict(run) if run else None,
        "results": [result_to_dict(r) for r in latest_results(db)],
        "runs": [run_to_dict(r) for r in recent_runs(db, limit=20)],
    }


def matrix_payload() -> dict:
    """`GET /v1/data-sources/matrix` 的响应体。"""
    from tradingagents.dataflows.trade_calendar import now_cn

    return {
        "generated_at": now_cn().isoformat(),
        "items": list(matrix_snapshot()),
    }


# ------------------------------------------------------------------ 定时开关

def probe_enabled() -> bool:
    """是否启用交易日定时探测（`TA_DATA_SOURCE_PROBE_ENABLED=0` 关闭）。"""
    import os

    return os.getenv("TA_DATA_SOURCE_PROBE_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def probe_at() -> str:
    """定时探测时刻（`TA_DATA_SOURCE_PROBE_AT`，默认 09:00 开盘前）。"""
    import os

    raw = os.getenv("TA_DATA_SOURCE_PROBE_AT", "09:00").strip()
    return raw or "09:00"


def probe_interval_seconds() -> float:
    """调度循环的巡检间隔（秒）。默认 120s：足够准时，又不至于空转。"""
    import os

    try:
        return max(30.0, float(os.getenv("TA_DATA_SOURCE_PROBE_INTERVAL_SEC", "120")))
    except ValueError:
        return 120.0


#: A 股时区（北京时间）。与 `api/main.py` 的调度循环保持一致用 ZoneInfo：
#: 中国自 1991 年起没有夏令时，固定 +08:00 等价，但统一来源能少一类"这里跟那里
#: 差一小时"的问题。
from zoneinfo import ZoneInfo

TZ_CN = ZoneInfo("Asia/Shanghai")


def _normalize_hhmm(value: str) -> str:
    """把 `9:00` / `09:00` / `09:00:00` 统一成可比较的 `HH:MM`。"""
    parts = (value or "").strip().split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return "09:00"
    hour = min(max(hour, 0), 23)
    minute = min(max(minute, 0), 59)
    return f"{hour:02d}:{minute:02d}"


def _cn_date_of(value: datetime | None) -> str | None:
    """把库里存的时间戳折算成**北京时间的日期**。

    存的是 UTC（与项目其它表一致），而"今天跑没跑过"必须按北京时间判断——
    否则 09:00 的探测在 UTC 视角下会落到前一天，调度循环会重复触发。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(TZ_CN).strftime("%Y-%m-%d")


def should_run_scheduled_probe(
    db: Session, *, now: datetime | None = None
) -> tuple[bool, str]:
    """判断这一刻该不该跑定时探测，返回 `(是否该跑, 原因)`。

    抽成纯函数（不 sleep、不起线程）是为了**可测试**：调度逻辑最难验证的部分
    就是"该跑的时候跑、不该跑的时候别跑"，把它塞进 `while True` 里就只能靠等。

    规则（按顺序）：
      1. 开关关闭 → 不跑
      2. 非交易日 → 不跑（非交易日的空数据会污染报告）
      3. 还没到探测时刻 → 不跑
      4. 今天已经跑过 → 不跑（这正是需要"北京时间的日期"的原因）
    """
    if not probe_enabled():
        return False, "disabled"

    now = now or datetime.now(TZ_CN)
    today = now.strftime("%Y-%m-%d")

    if not is_cn_trading_day(today):
        return False, "non-trading-day"

    if now.strftime("%H:%M") < _normalize_hhmm(probe_at()):
        return False, "before-scheduled-time"

    run = latest_run(db)
    if run is not None and _cn_date_of(run.started_at) == today:
        return False, "already-ran-today"

    return True, "due"


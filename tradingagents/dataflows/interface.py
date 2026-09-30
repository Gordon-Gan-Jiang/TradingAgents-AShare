import os

from .alpha_vantage_common import AlphaVantageRateLimitError
from .config import get_config
from .providers import build_default_registry
from .providers.errors import SourceEmpty, SourceUnsupported
from .vendor_text import classify_vendor_text

# Tools organized by category
TOOLS_CATEGORIES = {
    "core_stock_apis": {
        "description": "OHLCV stock price data",
        "tools": ["get_stock_data"],
    },
    "technical_indicators": {
        "description": "Technical analysis indicators",
        "tools": ["get_indicators"],
    },
    "fundamental_data": {
        "description": "Company fundamentals",
        "tools": [
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement",
        ],
    },
    "news_data": {
        "description": "News and insider data",
        "tools": [
            "get_news",
            "get_global_news",
            "get_insider_transactions",
        ],
    },
    "realtime_data": {
        "description": "Real-time market quotes",
        "tools": ["get_realtime_quotes"],
    },
    "cn_market_data": {
        "description": "China A-share market sentiment and fund flow data",
        "tools": [
            "get_board_fund_flow",
            "get_individual_fund_flow",
            "get_lhb_detail",
            "get_zt_pool",
            "get_hot_stocks_xq",
        ],
    },
    "cn_research_data": {
        "description": (
            "A 股研究类数据：结构化日线、分时、筹码分布、资金流、板块基准、个股新闻。"
            "对话式研判依赖这些源；放在同一类别下使用户配置一处即可调整全部。"
        ),
        "tools": [
            "get_daily_bars_df",
            "get_intraday_bars",
            "get_chip_distribution",
            "get_flow_frame",
            "get_peer_frames",
            "get_stock_turnover",
            "get_symbol_news",
        ],
    },
    "cn_board_data": {
        "description": "China A-share sector/board data: spot rankings, fund flow, history, constituents (mainline analysis)",
        "tools": [
            "get_board_spot",
            "get_board_rank",
            "get_board_hist",
            "get_board_cons",
        ],
    },
}

_registry = build_default_registry()

VENDOR_LIST = _registry.list_names()


def _is_trace_enabled() -> bool:
    env_value = os.getenv("TA_TRACE")
    if env_value is not None:
        return env_value.strip().lower() in ("1", "true", "yes", "on")

    config = get_config()
    return bool(config.get("provider_trace", True))


def _trace(msg: str) -> None:
    if _is_trace_enabled():
        print(f"[provider-trace] {msg}", flush=True)


def get_category_for_method(method: str) -> str:
    """Get the category that contains the specified method."""
    for category, info in TOOLS_CATEGORIES.items():
        if method in info["tools"]:
            return category
    raise ValueError(f"Method '{method}' not found in any category")


def get_vendor(category: str, method: str = None) -> str:
    """Get configured vendor for category or tool method."""
    config = get_config()

    if method:
        tool_vendors = config.get("tool_vendors", {})
        if method in tool_vendors:
            return tool_vendors[method]

    return config.get("data_vendors", {}).get(category, "yfinance")


def _resolve_vendor_chain(method: str, configured_vendor: str) -> list[str]:
    configured = [v.strip() for v in configured_vendor.split(",") if v.strip()]
    fallback = configured.copy()

    for provider_name in _registry.list_names():
        if provider_name not in fallback:
            fallback.append(provider_name)

    return fallback


def _label(exc: BaseException) -> str:
    """失败原因的短标签：带上 `kind`（unavailable / empty / throttled），
    因为"连不上"与"没实现"曾经在日志里完全同形。"""
    kind = getattr(exc, "kind", None)
    return f"{kind}:{type(exc).__name__}" if kind else type(exc).__name__


def route_to_vendor(method: str, *args, **kwargs):
    """Route method calls to provider implementations with fallback support."""
    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)
    fallback_vendors = _resolve_vendor_chain(method, vendor_config)
    last_exc = None
    last_empty = None
    # 链上**每一个**源失败的原因都要留下。原实现只报 `last_exc`，于是真实的
    # "所有源都连不上"会被链尾那个占位 provider 的 `SourceUnsupported` 顶掉，
    # 报错写成 `Last error: SourceUnsupported: Provider 'cn_stub' is a placeholder`
    # —— **把根因指错了**。实测（2026-09-29）`get_stock_data` 就是这样报的，
    # 而真实情况是 cn_akshare / cn_baostock 两个真实源都 unavailable。
    failures: list[tuple[str, str]] = []
    # "取到了、只是没有"单独记：它是**正常结果**，不是源故障。
    empties: list[str] = []
    # provider 用**文案**表达的失败/没数据（不是异常）。链上全部如此时，仍要返回
    # 文案而不是新抛异常：`tools/data.py:950` 的调用点没有 try/except，它依赖
    # 「拿不到时拿到一句失败文案」这一形状产出降级说明。若这里改成抛异常，
    # 工具面会从「降级」变成「报错」——把修复做成新 bug。
    returned_notices: list[tuple[str, str, str]] = []
    _trace(
        f"method={method} category={category} configured='{vendor_config}' "
        f"chain={fallback_vendors}"
    )

    for vendor in fallback_vendors:
        provider = _registry.get(vendor)
        if provider is None:
            _trace(f"method={method} vendor={vendor} status=skip reason=not-registered")
            continue

        impl_func = getattr(provider, method, None)
        if impl_func is None:
            _trace(f"method={method} vendor={vendor} status=skip reason=not-implemented")
            continue

        try:
            result = impl_func(*args, **kwargs)
            # provider **把失败/没数据吞成一句文案再正常 return** 是常态
            # （`cn_akshare` 有 20+ 处这样写，例如
            # `return f"板块历史行情获取失败（无替代源）：{type(exc).__name__}: {exc}"`）。
            #
            # 旧实现只要没抛异常就记 `status=hit` 并**立即返回**，于是：
            #   * 运维看到一条"该源正常"的假信号；
            #   * 一个"客气地返回错误文本"的源会把后面**本来可用**的源一起挡掉
            #     ——`tests/test_data_stability_hardening.py` 把这条记为事故 4。
            # **「客气地返回错误文本」不该比「抛异常」更有杀伤力**，所以这里先分类。
            #
            # 只看字符串：`[]`、空 DataFrame 都是**合法结果形状**，行数由消费方判断；
            # 若在这里把非字符串判成失败，一个返回空列表的 provider 会把整条链拖成
            # 异常（`tests/test_vendor_routing.py` 已依赖"空列表照样返回"）。
            if isinstance(result, str):
                kind = classify_vendor_text(result)
                if kind != "data":
                    returned_notices.append((vendor, kind, result))
                    _trace(
                        f"method={method} vendor={vendor} "
                        f"status={'empty' if kind == 'empty' else 'fallback'} "
                        f"reason=returned-{kind}-text"
                    )
                    continue
            _trace(f"method={method} vendor={vendor} status=hit")
            return result
        except SourceEmpty as exc:
            # 停牌 / 非交易日 / 未上市：这个源是**好的**，只是这天没有数据。
            # 必须与"源坏了"分开——否则非交易日会把整条链的每个源都刷成坏源，
            # 让探测报告与降级统计一起失真。
            last_empty = exc
            empties.append(vendor)
            _trace(f"method={method} vendor={vendor} status=empty")
            continue
        except SourceUnsupported as exc:
            # 结构性不支持：这个源本就不该被排在这条链上。这是**配置问题**，
            # 不是运行时故障——单独打一条 skip，方便一眼看出链里有死成员。
            last_exc = exc
            failures.append((vendor, _label(exc)))
            _trace(
                f"method={method} vendor={vendor} status=skip reason=unsupported"
            )
            continue
        except (AlphaVantageRateLimitError, NotImplementedError) as exc:
            last_exc = exc
            failures.append((vendor, _label(exc)))
            # Try next provider for transient/routing issues or placeholder providers.
            _trace(
                f"method={method} vendor={vendor} status=fallback "
                f"reason={type(exc).__name__}"
            )
            continue
        except Exception as exc:
            # Provider-specific runtime/parsing errors (e.g., schema changes, KeyError)
            # should not terminate the full chain; fall through to next vendor.
            last_exc = exc
            failures.append((vendor, _label(exc)))
            _trace(f"method={method} vendor={vendor} status=fallback reason={_label(exc)}")
            continue

    detail = "; ".join(f"{v}={r}" for v, r in failures) or "none"

    # 整条链都只是"空"：这是**确实没有数据**，不是源坏了。分开抛，让调用方
    # 能区分"今天没数据"与"数据源故障"（这正是 errors.py 三类语义的用意）。
    if not failures and empties and not returned_notices:
        _trace(f"method={method} status=empty reason=all-sources-empty")
        raise SourceEmpty(
            f"all sources returned empty for method '{method}': {empties}"
        ) from last_empty

    # 链上**有源返回了一句"失败/没数据"文案**：把**这句文案**还给调用方，
    # 而不是抛异常。这是本仓库既有的对外契约（`tests/test_board_spot_provider.py`
    # 的「every source fails 时返回诊断而非空」、`test_data_stability_hardening`
    # 的事故 4 都依赖它）：`tools/data.py:950` 的调用点没有 try/except，它靠
    # 「拿不到时拿到一句可读文案」产出降级说明；若这里改成抛异常，工具面会从
    # 「降级」变成「报错」——把修复做成新 bug。
    #
    # 取**第一个**：与旧实现在"首个返回文案的源"处给用户的字符串一致。
    # 修复的目标是「别让这句文案挡住后继源」，不是换掉这句话。
    if returned_notices:
        first_vendor, first_kind, first_text = returned_notices[0]
        _trace(
            f"method={method} status={'empty' if first_kind == 'empty' else 'failed'} "
            f"reason=all-sources-exhausted first-notice-vendor={first_vendor} "
            f"failures={detail}"
        )
        return first_text

    _trace(f"method={method} status=failed reason=no-available-vendor")
    if last_exc is not None:
        raise RuntimeError(
            f"No available vendor for method '{method}'. "
            f"Configured chain: {fallback_vendors}. "
            f"Attempts: {detail}. "
            f"Last error: {type(last_exc).__name__}: {last_exc}"
        ) from last_exc
    raise RuntimeError(
        f"No available vendor for method '{method}'. "
        f"Configured chain: {fallback_vendors}"
    )

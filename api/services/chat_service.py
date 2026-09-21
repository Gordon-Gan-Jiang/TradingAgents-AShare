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
    from tradingagents.tools.evidence import default_market_fetchers

    def lazy_ports(symbol: str, as_of: str) -> Any:
        return build_ports_factory(user_id)(symbol, as_of)

    def lazy_news(symbol: str, start: str, end: str) -> Any:
        return build_news_fetcher()(symbol, start, end)

    def _history_port() -> Any:
        """判定历史端口：**user_id 在闭包里绑定**，不经过模型参数。"""
        from api.services import signal_service

        class _Port:
            def recent_verdicts(self, *, limit: int, symbol: str | None = None):
                if not user_id:
                    return []
                payload = signal_service.list_verdicts(
                    user_id=user_id, symbol=symbol, limit=limit
                )
                # 键名必须与 `list_verdicts` 的实际返回一致（它返回 items/count）。
                # 事故记录：这里曾写 `payload.get("verdicts")`——键不存在 → 永远拿到
                # 空列表 → `verdict_history_read` 一直报「尚未落库的判定记录」，
                # 而该用户其实**有**判定。这正是本平台最不能接受的失败形态：
                # **「读不到」被说成「没有」**，而且完全静默。
                return payload.get("items") or []

        return _Port()

    def _weight_port() -> Any:
        """权重版本端口：只读，无用户维度（权重是全局的）。"""
        from api.services import signal_weight_service

        class _Port:
            def active_version(self):
                return signal_weight_service.active_version()

            def list_versions(self):
                rows = signal_weight_service.list_versions() or []
                return [
                    row if isinstance(row, dict)
                    else {"version": getattr(row, "version", None),
                          "activated_at": getattr(row, "activated_at", None),
                          "source": getattr(row, "source", None) or getattr(row, "reason", None)}
                    for row in rows
                ]

        return _Port()

    def _holdings_port() -> Any:
        """持仓端口：**user_id 在闭包里绑定**，不经过模型参数。"""
        from api.database import ImportedPortfolioPositionDB, get_db_ctx

        class _Port:
            def list_holdings(self, *, limit: int):
                if not user_id:
                    return []
                with get_db_ctx() as db:
                    rows = (
                        db.query(ImportedPortfolioPositionDB)
                        .filter(ImportedPortfolioPositionDB.user_id == user_id)
                        .order_by(
                            ImportedPortfolioPositionDB.market_value.desc(),
                            ImportedPortfolioPositionDB.symbol,
                        )
                        .limit(limit)
                        .all()
                    )
                    return [
                        {
                            "symbol": row.symbol,
                            "security_name": row.security_name,
                            "current_position": row.current_position,
                            "available_position": row.available_position,
                            "average_cost": row.average_cost,
                            "market_value": row.market_value,
                            "current_position_pct": row.current_position_pct,
                        }
                        for row in rows
                    ]

        return _Port()

    def _mainline_port() -> Any:
        """主线端口：读平台已生成的主线报告（含系统级共享报告的回退）。"""
        from api.database import get_db_ctx

        class _Port:
            def list_runs(self, *, limit: int):
                if not user_id:
                    return []
                from api.services import mainline_service

                with get_db_ctx() as db:
                    return mainline_service.list_runs(db, user_id=user_id, limit=limit)

        return _Port()

    def _task_port() -> Any:
        """异步任务端口：**user_id 在闭包里绑定**；只暴露提交与查询。"""
        from api.services import backtest_service

        class _Port:
            def submit_backtest(self, *, symbol, start_date, end_date, hold_days, sample_interval):
                return backtest_service.submit(
                    symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    # 回测要跑分析师管道，选一个最轻的分析师组合即可；
                    # 具体分析师的取舍属于产品决策，这里取最小可用集。
                    selected_analysts=["market"],
                    hold_days=hold_days,
                    sample_interval=sample_interval,
                    config={},
                    user_id=user_id,
                )

            def job_status(self, *, job_id):
                return backtest_service.get_job(job_id)

        return _Port()

    def lazy_screener() -> Any:
        from api.services.market_screen_ports import build_market_screener

        return build_market_screener()

    def _watchlist_port() -> Any:
        """自选清单端口（L2 证据工具）：**user_id 在闭包里绑定**。

        缺 user_id 时**抛错而不是返回空列表**：空列表会被读成「你的自选是空的」，
        而事实是「这次装配没有用户维度」。把「读不到」说成「没有」正是本平台
        最不能接受的失败形态；工具会把它降级为「自选清单未覆盖」。
        """
        from api.database import get_db_ctx
        from api.services import watchlist_service

        class _Port:
            def list_items(self, *, limit: int):
                if not user_id:
                    raise LookupError("watchlist port requires a bound user")
                with get_db_ctx() as db:
                    rows = watchlist_service.list_watchlist(db, user_id)
                return list(rows)[:limit]

        return _Port()

    def _freshness_port() -> Any:
        """数据时效探针：只报**本地能确证**的截止日，不触网。

        「这数据什么时候的」必须由可确证的事实回答，所以这里只报本地日线库的
        最新日期（离线唯一能确证的源）。其余源**不返回该键** = 截止日未知，由工具
        声明为「时效未知」——刻意不用今天日期兜底，那会把「未验证」伪装成
        「已确认最新」，比不填更糟。
        """
        from api.services import daily_bar_store

        class _Probe:
            def source_facts(self, *, source_keys, symbol=None):
                facts: dict[str, dict[str, Any]] = {}
                if symbol and "stock_data" in tuple(source_keys):
                    latest = daily_bar_store.latest_date(symbol)
                    if latest:
                        facts["stock_data"] = {
                            "cutoff": str(latest)[:10],
                            "origin": "本地日线库",
                        }
                return facts

        return _Probe()

    # ── 第二批 L1 决策工具的端口 ─────────────────────────────────────────
    #
    # 五个工具都读**平台已有的结论**（判定兑现 / 报告结论 / 平台推荐 / 跟踪看板 /
    # 模型评测榜）。它们共三条纪律：
    #   1. user_id 在闭包里绑定，不经过模型参数（给模型一个 user_id 参数就等于
    #      给它越权的可能）；
    #   2. 缺 user_id 时抛错而不是返回空——空会被读成「你没有持仓/没有推荐」，
    #      而事实是「这次装配没有用户维度」，把"读不到"说成"没有"正是本平台最
    #      不能接受的失败形态（工具会把它降级为「未覆盖」）；
    #   3. 只读：不发起扫描、不触发回填、不写库。

    def _require_user(port_name: str) -> str:
        if not user_id:
            raise LookupError(f"{port_name} port requires a bound user")
        return user_id

    def _outcome_port() -> Any:
        """判定兑现端口（L1）：读已落库的 T+3/T+5 兑现。

        取数落在 `signal_outcome_service.list_recorded_outcomes`——**按兑现行分页**。
        曾经的写法是「取最近 N 条判定，再从中筛兑现」：最近的判定还没成熟时会得到
        空结果，而更早的判定**其实有兑现记录**，于是工具把"有记录"说成"没有"
        （实测 limit=10 空、limit=30 有 2 条）。分页主体改成兑现行后该假否定消失：
        未成熟的判定不进结果集，也不占配额。

        与 `_history_port` 的分工：本端口给**兑现**，`_history_port` 给**判定**；
        持有期口径共用 `signal_outcome_service.DEFAULT_HORIZONS` 一份。
        """
        from api.services import signal_outcome_service

        class _Port:
            def recent_outcomes(self, *, limit: int, symbol: str | None = None):
                uid = _require_user("verdict_outcome")
                return signal_outcome_service.list_recorded_outcomes(
                    user_id=uid, symbol=symbol, limit=limit
                )

        return _Port()

    def _report_port() -> Any:
        """报告结论端口（L1）：读该用户的历史报告结论。

        只取 `REPORT_SUMMARY_COLUMNS` 里的结论字段（方向/置信度/目标/止损/状态），
        不搬报告正文——正文是 L2 证据，按需检索；结论字段是平台的确定性产出。
        """
        from api.database import get_db_ctx
        from api.services import report_service

        class _Port:
            def recent_reports(self, *, limit: int, symbol: str | None = None):
                uid = _require_user("report_conclusion")
                with get_db_ctx() as db:
                    rows = report_service.get_reports_by_user(
                        db, user_id=uid, symbol=symbol, limit=max(1, int(limit))
                    )
                    return [
                        {
                            "report_id": row.id,
                            "symbol": row.symbol,
                            "trade_date": row.trade_date,
                            "status": row.status,
                            "decision": row.decision,
                            "direction": row.direction,
                            "confidence": row.confidence,
                            "target_price": row.target_price,
                            "stop_loss_price": row.stop_loss_price,
                            "horizon": row.horizon,
                            "freshness_status": row.freshness_status,
                            "created_at": row.created_at.isoformat() if row.created_at else None,
                        }
                        for row in rows
                    ]

        return _Port()

    def _recommendation_port() -> Any:
        """推荐端口（L1）：读平台**已生成**的推荐结果。

        首选 `recommendation_feedback_service.list_scan_results`：那是推荐端点算完
        就落库的那批结果，自带 T+N 兑现回填（`feedback_status` /
        `realized_return_pct`），是真正意义上的「平台已生成的推荐」。

        该用户还没有落库结果时才退回复用 `recommendation_service.recommend_for_user`
        （`source_mode="user_pool"`：只用跟踪池与自选池，不触发全市场扫描）。
        它是平台**确定性**的选股管道，不是让模型临场生成推荐。
        """
        from api.database import get_db_ctx
        from api.services import recommendation_feedback_service, recommendation_service

        class _Port:
            def list_recommendations(self, *, limit: int):
                uid = _require_user("recommendation")
                capped = max(1, int(limit))
                with get_db_ctx() as db:
                    rows = recommendation_feedback_service.list_scan_results(
                        db, user_id=uid, limit=capped
                    )
                    if rows:
                        return {
                            "items": rows,
                            "source": "persisted_scan_results",
                            "score_profile": rows[0].get("score_profile"),
                            "generated_at": rows[0].get("created_at"),
                        }
                    payload = recommendation_service.recommend_for_user(
                        db,
                        user_id=uid,
                        top_k=min(capped, 20),
                        source_mode="user_pool",
                        include_tracking=True,
                        include_watchlist=True,
                    )
                scoring = payload.get("scoring_model")
                scoring = scoring if isinstance(scoring, dict) else {}
                return {
                    "items": list(payload.get("items") or []),
                    "source": "live_user_pool",
                    "score_profile": scoring.get("profile"),
                    "generated_at": None,
                }

        return _Port()

    def _tracking_board_port() -> Any:
        """跟踪看板端口（L1）：持仓 + 实时行情 + 最新分析结论，同一份快照。"""
        from api.database import get_db_ctx
        from api.services import tracking_board_service

        class _Port:
            def tracking_board(self):
                uid = _require_user("tracking_board")
                with get_db_ctx() as db:
                    return tracking_board_service.get_tracking_board(db, uid)

        return _Port()

    def _arena_port() -> Any:
        """模型评测榜端口（L1）：读平台落库的评测产出（已剔除日期漂移样本）。

        口径由工具传入（`all` 默认 / `portfolio`），不再这里写死：实测真实用户
        在 `portfolio` 口径下只有 16 个样本、榜上 0 行，而 `all` 有 2238 个样本 ——
        把口径固定成 `portfolio` 会让「哪个模型更准」几乎总是"没有样本"。
        """
        from api.database import get_db_ctx
        from api.services import model_arena_service

        class _Port:
            def leaderboard(self, *, limit: int, days: int, scope: str):
                uid = _require_user("model_arena")
                with get_db_ctx() as db:
                    payload = model_arena_service.build_model_leaderboard(
                        db, user_id=uid, days=int(days), scope=scope
                    )
                rows = list(payload.get("leaderboard") or [])[: max(1, int(limit))]
                return {**payload, "leaderboard": rows}

        return _Port()

    # 扫描器也惰性构造：注册表可能在 import 期（MCP 挂载）构建，而真实扫描
    # 依赖应用层配置与网络。缺实现时不注册该工具（`build_registry` 既有语义），
    # 于是这里必须传一个真实对象而不是 None。
    return _build(
        ports_factory=lazy_ports,
        news_fetcher=lazy_news,
        market_screener=lazy_screener(),
        history_port=_history_port(),
        weight_port=_weight_port(),
        holdings_port=_holdings_port(),
        mainline_port=_mainline_port(),
        task_port=_task_port(),
        # 第二批 L1 决策工具：五个都只读平台已有结论，user_id 在闭包里绑定。
        outcome_port=_outcome_port(),
        report_port=_report_port(),
        recommendation_port=_recommendation_port(),
        tracking_port=_tracking_board_port(),
        arena_port=_arena_port(),
        # L2 证据工具的依赖：组合根显式装配，「未注入就不注册」由 build_registry 保证。
        #
        # `default_market_fetchers()` 指向**既有**取数实现（cn_research_data），
        # 不在这里另写一套；换实现（例如先库后网）只需改这一处。
        evidence_fetchers=default_market_fetchers(),
        watchlist_port=_watchlist_port(),
        freshness_port=_freshness_port(),
        # announcements_port 刻意不传：仓库当前**没有**个股公告的取数实现
        # （news_service 只有 akshare 新闻；main.py 的 announcements.json 是应用
        # 发布说明，不是标的公告）。缺实现就不注册——不拿新闻冒充公告。
    )


def build_planner(*, user_id: str | None = None) -> Callable[[str], Any] | None:
    """按灰度开关构造规划器；**未启用时返回 None**（= 今天的行为）。

    规划器与回答模型分开配置：规划每轮都跑、要便宜快，回答要质量（见
    `tradingagents/agent/planner_config.py`）。
    """
    from tradingagents.agent.planner_config import llm_planner_enabled, planner_target

    if not llm_planner_enabled():
        return None

    from tradingagents.agent.routing import LlmPlanner, RuleTierClassifier

    profile_id, model_name = planner_target()
    try:
        completer = build_llm_completer(user_id=user_id, model_profile_id=profile_id)
    except Exception as exc:  # noqa: BLE001 — 规划器造不出来就退回规则层
        logger.warning("planner completer unavailable, planner disabled: %s", exc)
        return None

    registry = build_registry(user_id)
    digest_lines = []
    for name in registry.names():
        spec = registry.get(name)
        description = str(getattr(spec, "description", "") or "").splitlines()
        digest_lines.append(f"- {name}：{description[0][:60] if description else ''}")
    digest = "\n".join(digest_lines)

    planner = LlmPlanner(
        inner=RuleTierClassifier(_Resolver()),
        complete=lambda prompt: _completion_text(completer(prompt)),
        known_tools=registry.names(),
        tool_digest=digest,
    )

    def _plan(text: str):
        return planner.plan(text)

    return _plan


def _completion_text(result: Any) -> str:
    """补全结果 → 文本。补全器返回 `Completion`（带用量），这里只取正文。"""
    return str(getattr(result, "text", result) or "")


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


def build_turn_request(
    body: ChatRequest, *, user_id: str, planner: Callable[[str], Any] | None = None
) -> TurnRequest | None:
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
    # ── 规划层：规则定不了标的时才启用 ──────────────────────────────────
    # `planner` 由装配处注入（默认 None = 今天的行为，灰度关闭时不改变任何东西）。
    # 位置很关键：放在"规则已经解析出标的"之后——那类高频问句不该付规划成本；
    # 也放在 400 追问之前——那正是自然语言失败的场景，是规划层的用武之地。
    if symbol is None and planner is not None:
        try:
            plan = planner(text)
        except Exception as exc:  # noqa: BLE001 — 规划失败必须降级，不能中断回合
            logger.warning("planner failed, falling back to rules: %s", exc)
            plan = None
        if plan is not None:
            from tradingagents.dataflows.trade_calendar import cn_today_str

            return TurnRequest(
                text=text,
                user_id=user_id,
                tier=body.tier or plan.tier,
                symbol=plan.symbol,
                symbols=tuple(plan.symbols),
                screen=bool(plan.screen),
                market_level=bool(plan.market_level),
                plan_source="llm",
                intent=plan.intent,
                planned_steps=tuple(
                    {"tool": step.tool, "arguments": dict(step.arguments), "reason": step.reason}
                    for step in plan.steps
                ),
                as_of=body.as_of or cn_today_str(),
                conversation_id=body.conversation_id,
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

"""信号服务：装配端口、执行探针、落库判定、汇总校准指标。

## 分层位置

* 端口实现（`signal_ports.py`）负责「从真实数据源取数」
* 探针（`tradingagents.signals`）负责「算出判定」，纯函数
* 本模块负责「把它们接起来 + 落库 + 统计」

因此本模块是唯一同时知道数据库与探针的地方。若把落库塞进探针，信号层就
不再纯净（架构边界测试会失败）；若把取数塞进路由，路由会变成第二个 main.py。

## 可测试性

`build_ports_factory` 是模块级函数，测试直接替换它就能在没有网络的条件下
跑通整条链路——这比 mock 十几个 provider 方法可靠得多。
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Callable, Mapping, Sequence

from tradingagents.signals import DEFAULT_WEIGHTS, SIGNAL_VERSION, WashoutWeights, evaluate
from tradingagents.signals.types import ProbeResult

logger = logging.getLogger(__name__)

# 判定状态
STATE_WASHOUT = "washout"
STATE_DISTRIBUTION = "distribution"
STATE_TREND_BREAK = "trend_break"
STATE_INSUFFICIENT = "insufficient_evidence"
DIRECTIONAL_STATES = (STATE_WASHOUT, STATE_DISTRIBUTION, STATE_TREND_BREAK)

# 判定兑现的操作性定义：判定后 T+5 内创出高于判定日高点的**新高**
SUCCESS_HORIZON_DAYS = 5
EVALUATION_HORIZONS = (3, 5)


class SignalEvaluationError(RuntimeError):
    """探针无法完成评估（数据不足等）。由路由转成 422，而不是 500。"""


def build_ports_factory(user_id: str | None = None) -> Callable[[str, str], Any]:
    """装配真实端口。

    延迟导入 `signal_ports`：它依赖 pandas 与 provider 栈，导入较重；
    测试替换本函数时不应被迫加载它们。
    """
    from api.services.daily_bar_store import wrap_daily_port
    from api.services.signal_ports import (
        ProviderCapitalFlowPort,
        ProviderChipPort,
        ProviderDailyBarsPort,
        ProviderIntradayPort,
        ProviderPeerPort,
        build_signal_ports,
    )
    from tradingagents.dataflows.providers.registry import build_default_registry

    provider = build_default_registry().get("cn_akshare") or build_default_registry().get("cn_eastmoney_http")

    def factory(symbol: str, as_of: str) -> Any:
        if provider is None:
            raise SignalEvaluationError("no market data provider is registered")

        # 先库后网：日线是不可变的历史，已经取到过的就别再打一次外网——每次判定都
        # 实时取数会持续把 IP 推向封禁（东财系已实测被封）。取数口径不变，只是外面
        # 多了一层本地库；库内不够用时行为与改造前完全一致，并把结果顺手入库。
        daily = wrap_daily_port(ProviderDailyBarsPort(provider=provider))
        intraday = ProviderIntradayPort(fetcher=_intraday_fetcher)
        chips = ProviderChipPort(fetcher=_chips_fetcher)
        capital = ProviderCapitalFlowPort(fetcher=_capital_fetcher)
        peer = ProviderPeerPort(
            loader=_peer_loader,
            turnover_lookup=_stock_turnover_lookup,
        )
        return build_signal_ports(daily=daily, intraday=intraday, chips=chips, capital=capital, peer=peer)

    return factory


# 取数统一经 vendor 路由：只有这样才能共享既有三级降级链与失败冷却。
# 直连 akshare 会让「一个源抖动」变成「该维度不可得」，而东财直连明明可用。
def _intraday_fetcher(symbol: str, day: str, freq: str):
    from tradingagents.dataflows.providers.cn_research_data import fetch_intraday_via_router

    return fetch_intraday_via_router(symbol, day, freq)


def _chips_fetcher(symbol: str, day: str):
    from tradingagents.dataflows.providers.cn_research_data import fetch_chips_via_router

    return fetch_chips_via_router(symbol, day)


def _capital_fetcher(symbol: str, day: str):
    from tradingagents.dataflows.providers.cn_research_data import fetch_flow_via_router

    return fetch_flow_via_router(symbol, day)


def _peer_loader(symbol: str, day: str):
    from tradingagents.dataflows.providers.cn_research_data import fetch_peer_via_router

    return fetch_peer_via_router(symbol, day)


def _stock_turnover_lookup(symbol: str, day: str) -> float | None:
    from tradingagents.dataflows.providers.cn_research_data import fetch_turnover_via_router

    return fetch_turnover_via_router(symbol, day)


def _contribution_of(component: Mapping[str, Any]) -> float:
    """从存档的组件重算带符号贡献（与 Component.contribution 同规则）。"""
    if not component.get("available") or not component.get("side"):
        return 0.0
    sign = 1.0 if component.get("side") == "washout" else -1.0
    return sign * float(component.get("weight") or 0.0) * float(component.get("magnitude") or 0.0)


def _dimension_power_from_records(verdicts: Sequence[Any], outcomes: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """由已落库的判定与兑现结果计算各维度判别力。

    复用离线回放模块的 `dimension_power`：判别力的算法只应有一份，否则
    在线统计与回放统计会给出不同结论，而没人能判断该信哪个。
    """
    from tradingagents.signals.replay import ReplaySample, dimension_power

    outcome_by_verdict: dict[str, Any] = {}
    for outcome in outcomes:
        # 同一判定有多个持有期时取最长窗口：判别力关心的是「最终兑现了吗」
        existing = outcome_by_verdict.get(outcome.verdict_id)
        if existing is None or int(outcome.horizon_days or 0) > int(existing.horizon_days or 0):
            outcome_by_verdict[outcome.verdict_id] = outcome

    samples: list[ReplaySample] = []
    for verdict in verdicts:
        outcome = outcome_by_verdict.get(verdict.id)
        components = ((verdict.contributions or {}).get("components")) or []
        samples.append(
            ReplaySample(
                symbol=verdict.symbol,
                as_of=verdict.as_of,
                state=verdict.state,
                margin=float(verdict.margin or 0.0),
                confidence=int(verdict.confidence or 0),
                evaluable=bool(getattr(outcome, "evaluable", False)),
                threshold_hit=bool(getattr(outcome, "threshold_hit", False)),
                contributions={
                    str(c.get("key")): _contribution_of(c)
                    for c in components
                    if isinstance(c, Mapping)
                },
            )
        )

    try:
        return dict(dimension_power(samples))
    except Exception as exc:  # noqa: BLE001 — 统计失败不得让端点整体失败
        logger.warning("dimension power computation failed: %s", exc)
        return {}


def verdict_payload(result: ProbeResult, *, persisted_id: str | None = None) -> dict[str, Any]:
    """把探针结果转成对外载荷（结构化，不含散文）。"""
    return {
        "probe": result.probe,
        "symbol": result.symbol,
        "as_of": result.as_of,
        "state": result.state,
        "state_label": _STATE_LABELS.get(result.state, result.state),
        "confidence": int(result.confidence),
        "margin": round(float(result.margin), 6),
        "scores": {
            "washout": round(float(result.score_washout), 6),
            "distribution": round(float(result.score_distribution), 6),
            "trend_break": round(float(result.score_trend_break), 6),
        },
        "high_confidence": bool(result.high_confidence),
        "daily_only": bool(result.daily_only),
        "signal_version": result.signal_version,
        "applied_gates": list(result.applied_gates),
        "unavailable": list(result.unavailable),
        "falsifiers": [
            {
                "observable": f.observable,
                "threshold": f.threshold,
                "window_days": f.window_days,
                "description": f.description,
                "derived_from": f.derived_from,
            }
            for f in result.falsifiers
        ],
        "components": [
            {
                "key": c.key,
                "available": c.available,
                "side": c.side,
                "magnitude": round(float(c.magnitude), 6),
                "weight": float(c.weight),
                "raw": dict(c.raw),
                "unavailable_reason": c.unavailable_reason,
            }
            for c in result.components
        ],
        "modulator": (
            {
                "key": result.modulators[0].key,
                "factor": round(float(result.modulators[0].factor), 6),
                "raw": dict(result.modulators[0].raw),
            }
            if result.modulators
            else None
        ),
        "verdict_id": persisted_id,
    }


_STATE_LABELS = {
    STATE_WASHOUT: "洗盘（震仓）",
    STATE_DISTRIBUTION: "派发（出货）",
    STATE_TREND_BREAK: "趋势破坏",
    STATE_INSUFFICIENT: "证据不足",
}


# 判定端到端截止时间（秒）。实测一次判定在真实环境里卡过 10 分钟没返回——
# 取数链路的每个源都没有超时，akshare 内部还有重试。对投研产品而言
# **"没有结论"必须是一个能按时给出的结论**：挂在半路比"数据不足"糟得多。
VERDICT_DEADLINE_SECONDS = 60.0


def _evaluate_core(
    *,
    factory: Callable[[str, str], Any],
    symbol: str,
    as_of: str,
    window_days: int,
    weights: Any,
) -> Any:
    """真正执行评估（取数 + 信号计算）。单独成函数以便施加截止时间。"""
    ports = factory(symbol, as_of)
    return evaluate(
        ports=ports, symbol=symbol, as_of=as_of, window_days=window_days, weights=weights
    )


def _run_with_deadline(fn: Callable[[], Any], *, seconds: float, symbol: str) -> Any:
    """在截止时间内跑完 `fn`；超时抛可读错误。

    超时后**不等待**工作线程（`shutdown(wait=False)`）：线程卡在某个网络调用上，
    等它等于把超时变成装饰。这与 `ToolRegistry.invoke` 的取舍一致——
    宁可漏一个后台线程，也不让请求无限挂住。
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="verdict")
    future = pool.submit(fn)
    try:
        result = future.result(timeout=seconds)
    except FuturesTimeout:
        future.cancel()
        raise SignalEvaluationError(
            f"判定超时：{symbol} 的评估超过 {seconds:g} 秒仍未完成（数据源响应过慢）。"
            "已中止本次判定；可稍后重试，或在数据源恢复后再问。"
        ) from None
    finally:
        pool.shutdown(wait=False)
    return result


def evaluate_symbol(
    *,
    symbol: str,
    as_of: str | None,
    window_days: int,
    user_id: str | None,
    persist: bool = False,
    ports_factory: Callable[[str, str], Any] | None = None,
    weights: WashoutWeights | None = None,
) -> dict[str, Any]:
    """执行一次判定。失败抛 `SignalEvaluationError`（路由转 422）。"""
    from tradingagents.dataflows.trade_calendar import cn_today_str

    resolved_as_of = as_of or cn_today_str()
    factory = ports_factory or build_ports_factory(user_id)
    # 权重来自激活版本（无激活时回落内置先验）。这一步让「权重被数据校准」
    # 真正影响评估——否则激活记录只是一个没人读的表。
    if weights is None:
        from api.services.signal_weight_service import active_weights

        weights = active_weights(user_id)

    try:
        result = _run_with_deadline(
            lambda: _evaluate_core(
                factory=factory,
                symbol=symbol,
                as_of=resolved_as_of,
                window_days=window_days,
                weights=weights,
            ),
            seconds=VERDICT_DEADLINE_SECONDS,
            symbol=symbol,
        )
    except SignalEvaluationError:
        raise
    except Exception as exc:  # noqa: BLE001 — 统一转成可读的 422
        logger.info("signal evaluation failed for %s: %s", symbol, exc)
        raise SignalEvaluationError(f"无法完成对 {symbol} 的评估：{exc}") from exc

    persisted_id = None
    if persist and user_id:
        persisted_id = save_verdict(result, user_id=user_id)

    payload = verdict_payload(result, persisted_id=persisted_id)

    # 持仓上下文：判定卡片据此显示「你的成本 / 可卖数量」，
    # 或在未提供时明确显示「未提供持仓」。未知不会被静默忽略。
    if user_id:
        try:
            from api.services.position_context_service import resolve_position

            payload["position_context"] = resolve_position(
                user_id=user_id, symbol=symbol
            ).as_dict()
        except Exception as exc:  # noqa: BLE001
            logger.info("position resolution failed for %s: %s", symbol, exc)
            payload["position_context"] = None

    return payload


def save_verdict(result: ProbeResult, *, user_id: str, conversation_id: str | None = None) -> str:
    """落库判定。含**全量权重与观测**，供后续按维度统计判别力与重放校验。"""
    from api.database import SignalVerdictDB, get_db_ctx
    from tradingagents.agent.repository import new_id

    verdict_id = new_id()
    with get_db_ctx() as db:
        db.add(
            SignalVerdictDB(
                id=verdict_id,
                user_id=user_id,
                symbol=result.symbol,
                as_of=result.as_of,
                probe=result.probe,
                state=result.state,
                confidence=int(result.confidence),
                margin=float(result.margin),
                contributions={
                    "components": [asdict(c) for c in result.components],
                    "scores": {
                        "washout": float(result.score_washout),
                        "distribution": float(result.score_distribution),
                        "trend_break": float(result.score_trend_break),
                    },
                },
                # 必须存**完整**调制器（含 factor）。只存 raw 观测会让存档
                # 无法重放：重算时缺因子 → 得分与原始判定不一致，而校准统计
                # 会因此建立在一个与历史判定不同的模型上。
                modulator=(
                    {
                        "key": result.modulators[0].key,
                        "factor": float(result.modulators[0].factor),
                        "raw": dict(result.modulators[0].raw),
                    }
                    if result.modulators
                    else {}
                ),
                falsifiers=[asdict(f) for f in result.falsifiers],
                unavailable=list(result.unavailable),
                daily_only=bool(result.daily_only),
                high_confidence=bool(result.high_confidence),
                signal_version=result.signal_version or SIGNAL_VERSION,
                conversation_id=conversation_id,
            )
        )
        db.commit()
    return verdict_id


def list_verdicts(*, user_id: str, symbol: str | None = None, limit: int = 50) -> dict[str, Any]:
    from api.database import SignalVerdictDB, SignalVerdictOutcomeDB, get_db_ctx

    with get_db_ctx() as db:
        query = db.query(SignalVerdictDB).filter(SignalVerdictDB.user_id == user_id)
        if symbol:
            query = query.filter(SignalVerdictDB.symbol == symbol)
        rows = query.order_by(SignalVerdictDB.created_at.desc()).limit(limit).all()

        items = []
        for row in rows:
            outcomes = (
                db.query(SignalVerdictOutcomeDB)
                .filter(SignalVerdictOutcomeDB.verdict_id == row.id)
                .all()
            )
            items.append(
                {
                    "verdict_id": row.id,
                    "symbol": row.symbol,
                    "as_of": row.as_of,
                    "probe": row.probe,
                    "state": row.state,
                    "state_label": _STATE_LABELS.get(row.state, row.state),
                    "confidence": int(row.confidence or 0),
                    "margin": float(row.margin or 0.0),
                    "daily_only": bool(row.daily_only),
                    "high_confidence": bool(row.high_confidence),
                    "signal_version": row.signal_version,
                    "falsifiers": row.falsifiers or [],
                    "outcomes": [
                        {
                            "horizon_days": o.horizon_days,
                            "ref_date": o.ref_date,
                            "ret_pct": o.ret_pct,
                            "threshold_hit": bool(o.threshold_hit),
                            "evaluable": bool(o.evaluable),
                            "reason": o.reason,
                        }
                        for o in sorted(outcomes, key=lambda x: x.horizon_days)
                    ],
                }
            )
    return {"items": items, "count": len(items)}


def calibration_report(*, user_id: str, symbol: str | None = None) -> dict[str, Any]:
    """校准报告。

    **两类指标必须分列**：`guaranteed_by_construction` 是构造保证的（未溯源
    数值断言率恒为 0），`monitored` 是靠监控校准的。混在一起会退化成
    「我们加了校验，应该没问题」。
    """
    from api.database import SignalVerdictDB, SignalVerdictOutcomeDB, get_db_ctx

    with get_db_ctx() as db:
        query = db.query(SignalVerdictDB).filter(SignalVerdictDB.user_id == user_id)
        if symbol:
            query = query.filter(SignalVerdictDB.symbol == symbol)
        rows = query.all()
        # 按 (symbol, as_of, probe, signal_version) 去重：判定是这四个量的确定性
        # 函数，不同用户对同一标的同一天的判定是**同一个样本**。若按行统计，
        # 多用户部署下同一样本会被重复计数，命中率随之失真。
        verdicts = list({(v.symbol, v.as_of, v.probe, v.signal_version): v for v in rows}.values())
        outcomes = (
            db.query(SignalVerdictOutcomeDB)
            .filter(SignalVerdictOutcomeDB.verdict_id.in_([v.id for v in verdicts] or [""]))
            .all()
        )

    by_state: dict[str, int] = {s: 0 for s in (STATE_WASHOUT, STATE_DISTRIBUTION, STATE_TREND_BREAK, STATE_INSUFFICIENT)}
    for v in verdicts:
        by_state[v.state] = by_state.get(v.state, 0) + 1

    total = len(verdicts)
    abstained = by_state.get(STATE_INSUFFICIENT, 0)
    directional = total - abstained

    evaluated = [o for o in outcomes if o.evaluable]
    hits = [o for o in evaluated if o.threshold_hit]

    state_of = {v.id: v.state for v in verdicts}

    # 按维度判别力（7.7 的原始诉求）：此前它只在离线回放模块里，校准端点不输出，
    # 而该端点是校准闭环的对外窗口——看不到就没人会用
    dimension_power = _dimension_power_from_records(verdicts, outcomes)

    per_horizon: dict[int, dict[str, Any]] = {}
    for horizon in EVALUATION_HORIZONS:
        rows = [o for o in evaluated if o.horizon_days == horizon]
        per_state: dict[str, dict[str, int]] = {}
        for state in DIRECTIONAL_STATES:
            state_rows = [o for o in rows if state_of.get(o.verdict_id) == state]
            per_state[state] = {
                "sample": len(state_rows),
                "hit": sum(1 for o in state_rows if o.threshold_hit),
            }
        per_horizon[horizon] = {
            "sample": len(rows),
            "hit": sum(1 for o in rows if o.threshold_hit),
            # 分状态样本量必须显式：合并统计会掩盖「洗盘样本只有 3 个」这类
            # 样本饥饿问题，而分状态才是权重校准真正需要的最小样本单位
            "by_state": per_state,
        }

    return {
        "signal_version": verdicts[0].signal_version if verdicts else SIGNAL_VERSION,
        "sample": {
            "total": total,
            "directional": directional,
            "abstained": abstained,
            "by_state": by_state,
        },
        # ── 由构造保证 ────────────────────────────────────────────────
        "guaranteed_by_construction": {
            "unsourced_numeric_assertion_rate": 0.0,
            "note": "回答层的机械校验器保证：未溯源的数值断言无法输出，因此该比率恒为 0，不依赖监控发现。",
        },
        # ── 由监控校准 ────────────────────────────────────────────────
        "monitored": {
            "abstention_rate": round(abstained / total, 4) if total else 0.0,
            "abstention_rate_healthy_range": [0.30, 0.50],
            "evaluated_sample": len(evaluated),
            "hit_rate": round(len(hits) / len(evaluated), 4) if evaluated else None,
            "per_horizon": per_horizon,
            "dimension_power": dimension_power,
            "note": (
                "样本量低于权重激活闸门（≥100 前瞻判定，且每个待改维度 ≥30 个可用样本）时"
                "不给出权重建议——本平台有真实的样本饥饿前科。"
            ),
        },
        "weight_activation_gate": {
            "min_forward_verdicts": 100,
            "min_per_dimension_samples": 30,
            "satisfied": len(evaluated) >= 100,
            "offline_replay_may_only": "reject_dimensions",
        },
    }


def save_verdict_payload(
    payload: Mapping[str, Any], *, user_id: str, conversation_id: str | None = None
) -> str | None:
    """从判定载荷落库。

    载荷里已经包含校准所需的全部字段（八维观测 + 权重 + 调制器 + 失效条件 +
    信号版本），因此不需要把 `ProbeResult` 一路传到应用层——避免为了落库
    而让编排层持有领域对象。

    同一 `(symbol, as_of, probe, signal_version)` 重复落库时**复用已有记录**：
    判定不可变，重跑同一个基准日不应产生第二行，否则校准样本会被重复计数。
    """
    from api.database import SignalVerdictDB, get_db_ctx
    from tradingagents.agent.repository import new_id

    symbol = str(payload.get("symbol") or "")
    as_of = str(payload.get("as_of") or "")
    probe = str(payload.get("probe") or "washout")
    version = str(payload.get("signal_version") or SIGNAL_VERSION)
    if not symbol or not as_of:
        return None

    with get_db_ctx() as db:
        existing = (
            db.query(SignalVerdictDB)
            .filter(
                SignalVerdictDB.symbol == symbol,
                SignalVerdictDB.as_of == as_of,
                SignalVerdictDB.probe == probe,
                SignalVerdictDB.signal_version == version,
            )
            .first()
        )
        if existing is not None:
            return existing.id

        verdict_id = new_id()
        db.add(
            SignalVerdictDB(
                id=verdict_id,
                user_id=user_id,
                symbol=symbol,
                as_of=as_of,
                probe=probe,
                state=str(payload.get("state") or ""),
                confidence=int(payload.get("confidence") or 0),
                margin=float(payload.get("margin") or 0.0),
                contributions={
                    "components": payload.get("components") or [],
                    "scores": payload.get("scores") or {},
                },
                modulator=payload.get("modulator") or {},
                falsifiers=payload.get("falsifiers") or [],
                unavailable=payload.get("unavailable") or [],
                daily_only=bool(payload.get("daily_only", False)),
                high_confidence=bool(payload.get("high_confidence", False)),
                signal_version=version,
                conversation_id=conversation_id,
            )
        )
        db.commit()
    return verdict_id


def latest_market_states(*, user_id: str, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
    """取每个标的最新一条判定，供退出引擎作为附加触发信号使用。

    只取**最新一条**：退出评估关心的是「现在市场侧怎么看」，而不是历史轨迹。
    弃权（evidence_insufficient）同样返回——它必须被显式披露，不能被过滤掉
    （过滤会让用户以为「市场侧没问题」）。
    """
    from api.database import SignalVerdictDB, get_db_ctx

    cleaned = [str(s) for s in symbols if s]
    if not cleaned:
        return {}

    with get_db_ctx() as db:
        rows = (
            db.query(SignalVerdictDB)
            .filter(SignalVerdictDB.user_id == user_id, SignalVerdictDB.symbol.in_(cleaned))
            .order_by(SignalVerdictDB.as_of.desc(), SignalVerdictDB.created_at.desc())
            .all()
        )

    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.symbol in latest:
            continue
        latest[row.symbol] = {
            "state": row.state,
            "confidence": int(row.confidence or 0),
            "symbol": row.symbol,
            "as_of": row.as_of,
            "signal_version": row.signal_version,
            "daily_only": bool(row.daily_only),
            "falsifiers": row.falsifiers or [],
            "unavailable": row.unavailable or [],
        }
    return latest

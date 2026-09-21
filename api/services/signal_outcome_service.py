"""T+3 / T+5 回填的编排。

## 三段职责分离

* `select_matured` —— **纯函数**：按「判定日 + N 个交易日 ≤ 今天」筛出可以回填的
  判定。成熟度判断错了会导致「用还没走完的窗口去判对错」，那是根本性的数据错误，
  因此它必须能被穷举单测，而不是埋在 SQL 里。
* `evaluate_and_record` —— 取数 + 纯评估 + 落库
* `backfill` —— 批量调度与计数

## 幂等与不可变性

规格要求回填**不得产生重复行、不得改写已记录的结果**。因此已存在结果的
`(verdict_id, horizon)` 直接跳过——包括 `evaluable=False` 的那些：如果第一次
因停牌记了「不可评估」，那也是当时的真实状态，事后重算等于用未来信息修正历史。

## 取数失败不等于判定错误

loader 抛异常时记 `evaluable=False` 并写明原因，**不**记成未命中。把取数失败
计入命中率会让统计被基础设施问题污染。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd

from tradingagents.signals.outcome import (
    DEFAULT_OUTCOME_PARAMS,
    DIRECTIONAL_STATES,
    OutcomeParams,
    add_trading_days,
    evaluate_outcome,
)

logger = logging.getLogger(__name__)

# 落库的持有期（交易日）
DEFAULT_HORIZONS = (3, 5)

BarsLoader = Callable[[str, str, str], pd.DataFrame]
"""loader(symbol, start, end) → 含 date/open/high/low/close 的 DataFrame（升序）。"""


@dataclass(frozen=True)
class MaturedVerdict:
    """一个已成熟、可以回填的判定。"""

    verdict_id: str
    user_id: str
    symbol: str
    as_of: str
    state: str
    target_date: str


def select_matured(
    verdicts: Iterable[Mapping[str, Any]],
    *,
    horizon_days: int,
    today: str,
    next_day: Callable[[str], str] | None = None,
    recorded: Iterable[tuple[str, int]] = (),
) -> list[MaturedVerdict]:
    """筛选可以回填的判定。

    成熟条件：`as_of + horizon 交易日 <= today`。窗口没走完就回填，等于用
    不完整的数据判对错——这是最隐蔽也最严重的一类统计错误。
    同时排除已有结果的 `(verdict_id, horizon)`（幂等 + 不可变）。
    """
    already = {(vid, h) for vid, h in recorded}
    out: list[MaturedVerdict] = []
    for row in verdicts:
        state = str(row.get("state") or "")
        if state not in DIRECTIONAL_STATES:
            # 弃权没有可检验的预测，不进回填队列
            continue
        verdict_id = str(row.get("verdict_id") or row.get("id") or "")
        if not verdict_id:
            continue
        if (verdict_id, horizon_days) in already:
            continue
        as_of = str(row.get("as_of") or "")
        if not as_of:
            continue
        target = add_trading_days(as_of, horizon_days, next_day=next_day)
        if target > today:
            continue
        out.append(
            MaturedVerdict(
                verdict_id=verdict_id,
                user_id=str(row.get("user_id") or ""),
                symbol=str(row.get("symbol") or ""),
                as_of=as_of,
                state=state,
                target_date=target,
            )
        )
    return out


def bars_window(loader: BarsLoader, symbol: str, as_of: str, horizon_days: int,
                *, next_day: Callable[[str], str] | None = None) -> tuple[float, float, pd.DataFrame]:
    """取回「判定日 + 其后 horizon 个交易日」的行情，并切成 (entry_high, entry_close, 后续窗口)。

    一次取数同时拿到判定日那根与之后的窗口：分两次取会引入两个时点的数据，
    在不复权/停牌边界上可能互相矛盾。
    """
    start = as_of
    end = add_trading_days(as_of, horizon_days, next_day=next_day)
    frame = loader(symbol, start, end)
    if frame is None or len(frame) == 0:
        raise ValueError(f"no bars for {symbol} in [{start}, {end}]")

    frame = frame.copy()
    frame["date"] = frame["date"].astype(str).str[:10]
    frame = frame.sort_values("date")

    entry_rows = frame[frame["date"] <= as_of]
    if entry_rows.empty:
        raise ValueError(f"no entry bar on or before {as_of}")
    entry = entry_rows.iloc[-1]

    window = frame[frame["date"] > as_of]
    return float(entry["high"]), float(entry["close"]), window.reset_index(drop=True)


def evaluate_and_record(
    matured: MaturedVerdict,
    *,
    loader: BarsLoader,
    horizon_days: int,
    params: OutcomeParams = DEFAULT_OUTCOME_PARAMS,
    next_day: Callable[[str], str] | None = None,
    recorder: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """评估单个判定并落库。取数失败记 `evaluable=False`，不计入命中率。"""
    try:
        entry_high, entry_close, window = bars_window(
            loader, matured.symbol, matured.as_of, horizon_days, next_day=next_day
        )
        result = evaluate_outcome(
            state=matured.state,
            entry_high=entry_high,
            entry_close=entry_close,
            bars=window,
            horizon_days=horizon_days,
            params=params,
        )
    except Exception as exc:  # noqa: BLE001 — 取数失败不等于判定错误
        logger.info("outcome backfill loader failed for %s: %s", matured.symbol, exc)
        result = None
        failure_reason = f"loader_failed: {type(exc).__name__}: {exc}"
    else:
        failure_reason = result.reason

    payload = {
        "verdict_id": matured.verdict_id,
        "horizon_days": horizon_days,
        "ref_date": matured.target_date,
        "max_high": getattr(result, "max_high", None),
        "close": getattr(result, "close", None),
        "ret_pct": getattr(result, "ret_pct", None),
        "threshold_hit": bool(getattr(result, "threshold_hit", False)),
        "evaluable": bool(getattr(result, "evaluable", False)),
        "reason": failure_reason,
    }
    if recorder is not None:
        recorder(**payload)
    return payload


def backfill(
    *,
    horizon_days: int,
    today: str,
    loader: BarsLoader,
    verdicts: Sequence[Mapping[str, Any]],
    recorded: Iterable[tuple[str, int]] = (),
    next_day: Callable[[str], str] | None = None,
    recorder: Callable[..., None] | None = None,
    params: OutcomeParams = DEFAULT_OUTCOME_PARAMS,
) -> dict[str, Any]:
    """批量回填。返回计数，便于调度与观测。"""
    matured = select_matured(
        verdicts,
        horizon_days=horizon_days,
        today=today,
        next_day=next_day,
        recorded=recorded,
    )

    recorded_rows: list[dict[str, Any]] = []
    for item in matured:
        recorded_rows.append(
            evaluate_and_record(
                item,
                loader=loader,
                horizon_days=horizon_days,
                params=params,
                next_day=next_day,
                recorder=recorder,
            )
        )

    evaluable = [r for r in recorded_rows if r["evaluable"]]
    return {
        "horizon_days": horizon_days,
        "candidates": len(matured),
        "recorded": len(recorded_rows),
        "evaluable": len(evaluable),
        "not_evaluable": len(recorded_rows) - len(evaluable),
        "hits": sum(1 for r in evaluated_hits(evaluable)),
        "rows": recorded_rows,
    }


def evaluated_hits(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [r for r in rows if r.get("threshold_hit")]


def run_backfill(
    *,
    today: str,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    user_id: str | None = None,
    loader: BarsLoader | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """从数据库读取待回填判定并执行回填（应用层接线）。

    取数与落库在这里发生；筛选与评估仍是纯函数，因此逻辑可被单测覆盖。
    """
    from api.database import SignalVerdictDB, SignalVerdictOutcomeDB, get_db_ctx

    loader = loader or _default_loader()

    with get_db_ctx() as db:
        query = db.query(SignalVerdictDB)
        if user_id:
            query = query.filter(SignalVerdictDB.user_id == user_id)
        verdict_rows = query.order_by(SignalVerdictDB.as_of.asc()).limit(limit).all()

        verdicts = [
            {
                "verdict_id": v.id,
                "user_id": v.user_id,
                "symbol": v.symbol,
                "as_of": v.as_of,
                "state": v.state,
            }
            for v in verdict_rows
        ]

        outcome_query = db.query(SignalVerdictOutcomeDB)
        if verdicts:
            outcome_query = outcome_query.filter(
                SignalVerdictOutcomeDB.verdict_id.in_([v["verdict_id"] for v in verdicts])
            )
        recorded = [(o.verdict_id, int(o.horizon_days)) for o in outcome_query.all()]

    reports: dict[str, Any] = {}
    for horizon in horizons:
        def _recorder(**payload: Any) -> None:
            _record_outcome(payload)

        reports[str(horizon)] = backfill(
            horizon_days=horizon,
            today=today,
            loader=loader,
            verdicts=verdicts,
            recorded=recorded,
            recorder=_recorder,
        )
    return reports


def _record_outcome(payload: Mapping[str, Any]) -> None:
    """写入一行结果。已存在则**不改写**（历史不可变）。"""
    from api.database import SignalVerdictOutcomeDB, get_db_ctx
    from tradingagents.agent.repository import new_id

    with get_db_ctx() as db:
        existing = (
            db.query(SignalVerdictOutcomeDB)
            .filter(
                SignalVerdictOutcomeDB.verdict_id == payload["verdict_id"],
                SignalVerdictOutcomeDB.horizon_days == payload["horizon_days"],
            )
            .first()
        )
        if existing is not None:
            return
        db.add(
            SignalVerdictOutcomeDB(
                id=new_id(),
                verdict_id=payload["verdict_id"],
                horizon_days=int(payload["horizon_days"]),
                ref_date=str(payload["ref_date"]),
                max_high=payload.get("max_high"),
                close=payload.get("close"),
                ret_pct=payload.get("ret_pct"),
                threshold_hit=bool(payload.get("threshold_hit")),
                evaluable=bool(payload.get("evaluable")),
                reason=payload.get("reason"),
            )
        )
        db.commit()


def _default_loader() -> BarsLoader:
    """默认取数：复用既有的 provider 结构化缝隙（不解析格式化字符串）。"""
    from tradingagents.dataflows.providers.registry import build_default_registry

    provider = build_default_registry().get("cn_akshare")

    def loader(symbol: str, start: str, end: str) -> pd.DataFrame:
        if provider is None:
            raise RuntimeError("no market data provider is registered")
        return provider.get_daily_bars_df(symbol, start, end)

    return loader

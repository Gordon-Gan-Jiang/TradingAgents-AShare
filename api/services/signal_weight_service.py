"""权重版本化激活（审计 D2-b 修复）。

## 修复前后的区别

**修复前**：只有 `check_activation_gate`（一个拒绝逻辑）与 `with_weight`（一个纯
函数）。没有任何机制产生并持久化新的 `signal_version`——于是「权重是被数据校准的」
这句话在产品上无处落地，也没有版本可对照。

**修复后**：激活是一次**显式、有记录、可追溯**的操作：

```
propose_activation(...)   → 检查闸门，不足则拒绝并报告差距
activate(...)             → 落库新版本（必填样本量与理由）+ 置为唯一 active
active_weights()          → 供评估使用；无激活记录时回落到内置先验
list_versions()           → 旧版本保留，用于同标的同日期的对照
```

## 分层

从数据库读权重必须发生在**应用层**：`signals/` 是纯函数层、不得依赖 DB
（由架构边界测试强制）。信号层只接收一个 `WashoutWeights` 实例——这也是
「权重靠参数注入」这条设计的直接好处：换权重源不需要改算法。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from uuid import uuid4

from tradingagents.signals.replay import MIN_FORWARD_VERDICTS, MIN_PER_DIMENSION_SAMPLES
from tradingagents.signals.weights import DEFAULT_WEIGHTS, SIGNAL_VERSION, WashoutWeights, replace

logger = logging.getLogger(__name__)

BASE_VERSION_PREFIX = "washout"


@dataclass(frozen=True)
class ActivationProposal:
    allowed: bool
    reason: str
    have_forward: int = 0
    needed_forward: int = MIN_FORWARD_VERDICTS
    blocking_dimensions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActivatedVersion:
    version: str
    sample_size: int
    rationale: str
    activated_by: str = ""
    weights: Mapping[str, Any] = field(default_factory=dict)


def propose_activation(
    *,
    forward_samples: int,
    per_dimension: Mapping[str, int],
    min_forward: int = MIN_FORWARD_VERDICTS,
    min_per_dimension: int = MIN_PER_DIMENSION_SAMPLES,
) -> ActivationProposal:
    """检查是否满足激活闸门。**不足时拒绝**并报告差距。"""
    blocking = tuple(sorted(k for k, v in per_dimension.items() if v < min_per_dimension))
    if forward_samples < min_forward:
        return ActivationProposal(
            allowed=False,
            reason=(
                f"前瞻样本 {forward_samples} < {min_forward}；"
                "离线回放只能否决维度，不能激活权重"
            ),
            have_forward=forward_samples,
            needed_forward=min_forward,
            blocking_dimensions=blocking,
        )
    if blocking:
        return ActivationProposal(
            allowed=False,
            reason=f"以下维度样本低于单维度下限（{min_per_dimension}）：{list(blocking)}",
            have_forward=forward_samples,
            needed_forward=min_forward,
            blocking_dimensions=blocking,
        )
    return ActivationProposal(
        allowed=True, reason="闸门已满足", have_forward=forward_samples, needed_forward=min_forward
    )


def _next_version() -> str:
    return f"{BASE_VERSION_PREFIX}-v{uuid4().hex[:8]}"


def activate(
    *,
    weights: Mapping[str, Any],
    rationale: str,
    forward_samples: int,
    per_dimension: Mapping[str, int],
    activated_by: str = "",
) -> ActivatedVersion:
    """激活一个新的权重版本。

    拒绝条件（抛 ValueError）：闸门未满足、理由为空。
    两者都是「不许悄悄改权重」的具体形式。
    """
    if not str(rationale or "").strip():
        raise ValueError("activation requires a non-empty rationale")

    proposal = propose_activation(forward_samples=forward_samples, per_dimension=per_dimension)
    if not proposal.allowed:
        raise ValueError(f"weight activation gate not satisfied: {proposal.reason}")

    from api.database import SignalWeightVersionDB, get_db_ctx

    version = _next_version()
    with get_db_ctx() as db:
        # 只允许一个 active：先清掉旧的，避免「同时有两个生效版本」这种
        # 让评估结果取决于查询顺序的状态
        db.query(SignalWeightVersionDB).filter(
            SignalWeightVersionDB.is_active.is_(True)
        ).update({"is_active": False})
        db.add(
            SignalWeightVersionDB(
                version=version,
                weights=dict(weights),
                rationale=str(rationale),
                sample_size=int(forward_samples),
                per_dimension=dict(per_dimension),
                activated_by=activated_by or "",
                is_active=True,
            )
        )
        db.commit()

    logger.info("activated signal weights version=%s samples=%s", version, forward_samples)
    return ActivatedVersion(
        version=version,
        sample_size=int(forward_samples),
        rationale=str(rationale),
        activated_by=activated_by or "",
        weights=dict(weights),
    )


def list_versions() -> list[ActivatedVersion]:
    from api.database import SignalWeightVersionDB, get_db_ctx

    with get_db_ctx() as db:
        rows = (
            db.query(SignalWeightVersionDB)
            .order_by(SignalWeightVersionDB.created_at.desc())
            .all()
        )
        return [
            ActivatedVersion(
                version=row.version,
                sample_size=int(row.sample_size or 0),
                rationale=row.rationale or "",
                activated_by=row.activated_by or "",
                weights=dict(row.weights or {}),
            )
            for row in rows
        ]


def get_version(version: str) -> ActivatedVersion | None:
    from api.database import SignalWeightVersionDB, get_db_ctx

    with get_db_ctx() as db:
        row = (
            db.query(SignalWeightVersionDB)
            .filter(SignalWeightVersionDB.version == version)
            .first()
        )
        if row is None:
            return None
        return ActivatedVersion(
            version=row.version,
            sample_size=int(row.sample_size or 0),
            rationale=row.rationale or "",
            activated_by=row.activated_by or "",
            weights=dict(row.weights or {}),
        )


def active_version() -> str | None:
    from api.database import SignalWeightVersionDB, get_db_ctx

    with get_db_ctx() as db:
        row = (
            db.query(SignalWeightVersionDB)
            .filter(SignalWeightVersionDB.is_active.is_(True))
            .first()
        )
        return row.version if row is not None else None


def active_weights(user_id: str | None = None) -> WashoutWeights:
    """供评估使用的权重。

    没有任何激活记录时回落到内置先验——**不得**返回空权重：那会让判定
    失去全部阈值。
    """
    del user_id  # 权重是全局激活的；保留参数以便未来按用户覆盖
    try:
        from api.database import SignalWeightVersionDB, get_db_ctx

        with get_db_ctx() as db:
            row = (
                db.query(SignalWeightVersionDB)
                .filter(SignalWeightVersionDB.is_active.is_(True))
                .first()
            )
            if row is None:
                return DEFAULT_WEIGHTS
            overrides = {
                key: value
                for key, value in dict(row.weights or {}).items()
                if key.startswith("weight_") or key in WashoutWeights.__dataclass_fields__
            }
            return replace(DEFAULT_WEIGHTS, version=row.version, **overrides)
    except Exception as exc:  # noqa: BLE001 — 读不到激活版本时用先验，不能因此不可用
        logger.warning("failed to load active weights, falling back to defaults: %s", exc)
        return DEFAULT_WEIGHTS

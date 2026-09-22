"""回合 trace：把"这一轮到底做了什么"落库，供排查与评测。

## 为什么必须落库，而不是只写日志

Phase 0 的目标是**让改进可被证明**。三件事都依赖 trace：

1. **排查**：用户说"这次答得不对"，得能回答"计划是什么、哪一步空了、模型看到几张证据"。
   日志会被轮转、会被 grep 淹没，且无法按问句聚合。
2. **评测**：黄金集跑分要按问句比对期望（`text` 字段），线上失败样本也要能进集合。
3. **成本归因**：按档位/工具看 tokens 与耗时，才知道预算该调哪里。

## 只存摘要，不存工具原始返回

单轮工具返回可达 10-50KB（见 `ChatMessageDB` 的同类取舍）。trace 存的是
`{tool, arguments, ok, elapsed_ms, evidence_count}` —— 这些足以回答"为什么慢、
哪步空、模型看到了多少"，而原始数据按需重算比存旧快照更正确。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from tradingagents.measurement import summarize_samples

# 单条 trace 的 JSON 字段上限：防止一个异常巨大的计划把行写爆
_MAX_JSON_CHARS = 20_000


def _clip(payload: Any) -> Any:
    """把 JSON 字段裁剪到合理体积，并显式标注被裁剪。"""
    if payload is None:
        return None
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"_unserializable": str(type(payload).__name__)}
    if len(text) <= _MAX_JSON_CHARS:
        return payload
    return {"_clipped": True, "_preview": text[:_MAX_JSON_CHARS]}


def save_turn_trace(
    *,
    conversation_id: str,
    user_id: str,
    text: str,
    tier: str = "",
    status: str = "",
    plan: Mapping[str, Any] | None = None,
    steps: Sequence[Mapping[str, Any]] | None = None,
    tool_calls: Sequence[str] = (),
    tokens: int = 0,
    model_calls: int = 0,
    model: str = "",
    latency_ms: int = 0,
    rejected_claims: int = 0,
    weak_verification: bool = False,
    verdict_id: str | None = None,
    unavailable: Sequence[str] = (),
) -> str | None:
    """写一轮 trace。没有会话 id 时跳过（无从归属）。"""
    from api.database import ChatTurnTraceDB, get_db_ctx
    from tradingagents.agent.repository import new_id

    if not conversation_id:
        return None

    trace_id = new_id()
    with get_db_ctx() as db:
        db.add(
            ChatTurnTraceDB(
                id=trace_id,
                conversation_id=conversation_id,
                user_id=user_id,
                text=text or "",
                tier=tier or "",
                status=status or "",
                plan=_clip(dict(plan) if plan else None),
                steps=_clip([dict(step) for step in (steps or [])]),
                tool_calls=(list(tool_calls) or None),
                tokens=int(tokens or 0),
                model_calls=int(model_calls or 0),
                model=model or "",
                latency_ms=int(latency_ms or 0),
                rejected_claims=int(rejected_claims or 0),
                weak_verification=bool(weak_verification),
                verdict_id=verdict_id,
                unavailable=_clip(list(unavailable)) if unavailable else None,
            )
        )
        db.commit()
    return trace_id


def list_turn_traces(
    *,
    user_id: str,
    conversation_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """按会话（或该用户全部）列出 trace，最新在前。"""
    from api.database import ChatTurnTraceDB, get_db_ctx

    limit = max(1, min(int(limit), 200))
    with get_db_ctx() as db:
        query = db.query(ChatTurnTraceDB).filter(ChatTurnTraceDB.user_id == user_id)
        if conversation_id:
            query = query.filter(ChatTurnTraceDB.conversation_id == conversation_id)
        rows = (
            query.order_by(ChatTurnTraceDB.created_at.desc(), ChatTurnTraceDB.id.desc())
            .limit(limit)
            .all()
        )
        return [_to_dict(row) for row in rows]


def get_turn_trace(*, user_id: str, trace_id: str) -> dict[str, Any] | None:
    from api.database import ChatTurnTraceDB, get_db_ctx

    with get_db_ctx() as db:
        row = (
            db.query(ChatTurnTraceDB)
            .filter(ChatTurnTraceDB.id == trace_id, ChatTurnTraceDB.user_id == user_id)
            .first()
        )
        return _to_dict(row) if row is not None else None


def _percentile(values: Sequence[float], q: float) -> float:
    """线性插值分位数。空序列返回 0.0。

    自己实现而不引第三方：只需要这一个函数，而依赖越少越好。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return float(ordered[low] * (1 - frac) + ordered[high] * frac)


def _stats(values: Sequence[float]) -> dict[str, float | int]:
    """一组数的分布摘要。

    **为什么必须给中位与 P90，而不是只给平均**：这条路径的方差极大——
    实测同一个问题、同一套配置重复三次，延迟是 8.4 s / 13.2 s / 19.8 s（差 2.4 倍）。
    均值会被长尾拉走，让人把"这一次特别慢"读成"整体变慢了"。
    中位 + P90 + 样本量才能支撑一次判断。
    """
    if not values:
        return {"count": 0, "median": 0.0, "p90": 0.0, "max": 0.0}
    return {
        "count": len(values),
        "median": round(_percentile(values, 0.5), 1),
        "p90": round(_percentile(values, 0.9), 1),
        "max": round(float(max(values)), 1),
    }


def summarize_turn_traces(
    *,
    user_id: str,
    limit: int = 500,
) -> dict[str, Any]:
    """按档位与工具归因延迟、token 与失败率。

    这是 Phase 4.2 要求的"成本与延迟看板"的后端。只读，不改任何行为——
    观测与行为分开，才能拿它当"改行为"的证据。

    归因口径说明：

    * **延迟按档位**：档位是预算的载体（`TierBudget`），调预算前要知道各档现状；
    * **工具耗时从 `steps` 里取**：`elapsed_ms` 只记工具本身，不含模型——
      这正是要分开看的原因（实测模型调用占回合延迟 93–98%，工具只占约 4.5%）；
    * **`tokens` 含推理**：实测正文 745 字符却计费 2340 token，所以这里是
      "计费量"而不是"用户看到的长度"。按它做延迟判断时要记得这一点。
    """
    return _summarize_rows(_load_rows(user_id=user_id, limit=limit))


def _load_rows(*, user_id: str, limit: int) -> list[Any]:
    """取该用户最近的 trace 行（IO 外壳，便于把聚合逻辑单测成纯函数）。"""
    from api.database import ChatTurnTraceDB, get_db_ctx

    limit = max(1, min(int(limit), 2000))
    with get_db_ctx() as db:
        return (
            db.query(ChatTurnTraceDB)
            .filter(ChatTurnTraceDB.user_id == user_id)
            .order_by(ChatTurnTraceDB.created_at.desc(), ChatTurnTraceDB.id.desc())
            .limit(limit)
            .all()
        )


def _summarize_rows(rows: Sequence[Any]) -> dict[str, Any]:
    """把 trace 行聚合成归因视图。**纯函数**：不碰数据库，便于单测。

    拆出来的理由：这个函数的正确性完全由数字口径决定（中位 vs 平均、
    工具耗时 vs 回合延迟），而那正是最容易写错、也最需要测试的部分。
    把它混在查询里就没法用构造数据测边界（空历史、全失败、长尾）。
    """
    by_tier: dict[str, dict[str, Any]] = {}
    by_tool: dict[str, dict[str, Any]] = {}
    status_counts: dict[str, int] = {}
    turn_latencies: list[float] = []

    for row in rows:
        tier = row.tier or "(未标注)"
        bucket = by_tier.setdefault(tier, {"latency_ms": [], "tokens": [], "model_calls": []})
        bucket["latency_ms"].append(float(row.latency_ms or 0))
        bucket["tokens"].append(float(row.tokens or 0))
        bucket["model_calls"].append(float(row.model_calls or 0))
        turn_latencies.append(float(row.latency_ms or 0))
        status_counts[row.status or "(空)"] = status_counts.get(row.status or "(空)", 0) + 1

        for step in row.steps or []:
            name = step.get("tool") if isinstance(step, Mapping) else None
            if not name:
                continue
            entry = by_tool.setdefault(
                name, {"elapsed_ms": [], "ok": 0, "failed": 0, "evidence_total": 0}
            )
            entry["elapsed_ms"].append(float(step.get("elapsed_ms") or 0))
            if step.get("ok"):
                entry["ok"] += 1
            else:
                entry["failed"] += 1
            entry["evidence_total"] += int(step.get("evidence_count") or 0)

    def _tier_view(bucket: Mapping[str, Sequence[float]]) -> dict[str, Any]:
        return {
            "turns": len(bucket["latency_ms"]),
            "latency_ms": _stats(bucket["latency_ms"]),
            "tokens": _stats(bucket["tokens"]),
            "model_calls_median": round(_percentile(bucket["model_calls"], 0.5), 1),
        }

    def _tool_view(entry: Mapping[str, Any]) -> dict[str, Any]:
        calls = entry["ok"] + entry["failed"]
        return {
            "calls": calls,
            "failed": entry["failed"],
            "failure_rate": round(entry["failed"] / calls, 3) if calls else 0.0,
            "elapsed_ms": _stats(entry["elapsed_ms"]),
            "evidence_total": entry["evidence_total"],
        }

    total = len(rows)
    degraded = status_counts.get("degraded", 0)
    return {
        "sample": {
            "turns": total,
            "status": status_counts,
            "degraded_rate": round(degraded / total, 3) if total else 0.0,
            # 样本量与方差警告随数据一起返回：这条路径的方差大到单次采样无意义，
            # 把警告写在数据旁边，比写在文档里更容易被读到。
            "warning": (
                "延迟方差极大（实测同一配置重复三次可差 2.4 倍）。"
                "样本量小的结论不可信；比较改动前后必须多次重复取中位。"
            ),
            # 上面那句是**文案**，人可能不读。这里给出**结构化**判定：
            # 延迟这一组到底稳不稳、够不够拿来下方向性结论。
            # 「单次采样不可信」因此变成 API 层面做不出来的事，而不是纪律问题。
            "latency_sufficiency": summarize_samples(turn_latencies, drop_non_positive=True),
        },
        "overall_latency_ms": _stats(turn_latencies),
        "by_tier": {tier: _tier_view(b) for tier, b in sorted(by_tier.items())},
        "by_tool": {
            name: _tool_view(e)
            for name, e in sorted(by_tool.items(), key=lambda kv: -len(kv[1]["elapsed_ms"]))
        },
    }


def _to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "conversation_id": row.conversation_id,
        "text": row.text,
        "tier": row.tier,
        "status": row.status,
        "plan": row.plan or {},
        "steps": row.steps or [],
        "tool_calls": list(row.tool_calls or []),
        "tokens": row.tokens,
        "model_calls": row.model_calls,
        "model": row.model,
        "latency_ms": row.latency_ms,
        "rejected_claims": row.rejected_claims,
        "weak_verification": bool(row.weak_verification),
        "verdict_id": row.verdict_id,
        "unavailable": list(row.unavailable or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }

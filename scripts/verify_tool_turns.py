#!/usr/bin/env python
"""逐工具**真实对话**验证：规划器选不选得中、证据引不引得进、取不到时认不认输。

## 为什么必须有这个脚本（而不是靠单测）

单测证明的是"工具在注入端口后能被调用、返回结构合规"。它证明不了三件产品级的事：

1. **规划器会不会选中它**——工具在册 ≠ 模型会挑它。真实问句要经过标的解析、档位
   判定、计划生成、参数填充四条链路，任何一环错位都表现为"这个工具从没被用过"。
2. **它的证据会不会进回答**——工具返回了 `EvidenceItem` ≠ 回答引用了它。引用要过
   引用校验（refs 必须存在、数字必须落在被引用的证据里）。
3. **取不到时是否如实弃权**——外部源被封、无 token 时，工具应当声明未覆盖/弃权，
   而不是返回 0 或编一个数。这条只能在真实网络状态下验。

所以本脚本走**完整回合**（`build_turn_request` → `run_turn_streaming`），产出的是
"这句话能不能被这句话答好"，而不是"这个函数有没有返回"。

## 用法

    .venv/bin/python scripts/verify_tool_turns.py                 # 跑全部
    .venv/bin/python scripts/verify_tool_turns.py --only signal_washout_probe
    .venv/bin/python scripts/verify_tool_turns.py --json out.json

退出码：有工具**既没被选中也没有如实说明原因**时为 1（"静默失败"是不可接受的
形态，必须让 CI 红）。仅仅"取不到数据但如实弃权"不算失败。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试账号：档案是**按用户**存的，不给 user_id 解析不到模型档案。
DEFAULT_USER_ID = os.getenv(
    "TA_VERIFY_USER_ID", "2f711d25-5be0-46ec-ac5c-109707b1b25b"
)
# 显式档案：该账号 5 个档案都没设默认，不指定会 AuthenticationError。
DEFAULT_PROFILE_ID = os.getenv("TA_VERIFY_PROFILE_ID", "c35aa366c7a24bfc81fed40e0e2ba180")


@dataclass(frozen=True)
class Case:
    """一条真实验证：问句 + 期望被选中的工具。"""

    tool: str
    question: str
    # 允许的替代工具：例如组合级问题可能被判成 batch 版
    alternates: tuple[str, ...] = ()
    # 该问句是否**本来就无解**（无数据源/无落库）。此时"如实弃权"即为通过。
    may_abstain: bool = False
    note: str = ""


CASES: tuple[Case, ...] = (
    # ── L1 决策：单标的判定（已走过真实对话，保留作回归锚点）────────────
    Case("signal_washout_probe", "600519 是不是在洗盘", note="核心场景，回归锚点"),
    # ── L1 决策：只读平台已有结论（这些是 ① 的重点：全部只做过端口级验证）──
    Case("portfolio_holdings_read", "我账户现在都持有哪些股票",
         alternates=("tracking_board_read",), note="组合级，无标的"),
    Case("portfolio_risk_review", "我的持仓哪只该清仓",
         alternates=("portfolio_holdings_read",), note="组合体检"),
    Case("trade_plan_read", "我当前的交易计划是什么",
         alternates=("trade_ledger_read",), note="纪律锚点"),
    Case("trade_ledger_read", "我这段时间交易胜率怎么样", note="交易归因"),
    Case("verdict_history_read", "我最近的洗盘判定是什么",
         alternates=("verdict_outcome_lookup",), note="判定历史"),
    Case("verdict_outcome_lookup", "上次那个判定后来兑现了吗",
         alternates=("verdict_history_read",), note="判定兑现"),
    Case("signal_weight_read", "当前判定用的是哪一版权重", note="权重版本"),
    Case("report_conclusion_lookup", "之前那份报告结论是什么", note="历史报告结论"),
    Case("report_consensus_read", "600519 最近这份报告的多视角共识怎么样", note="多视角共识"),
    Case("market_mainline_view", "最近哪个板块最强",
         alternates=("market_screen",), note="市场主线"),
    Case("recommendation_read", "平台推荐了什么票", note="平台推荐"),
    Case("tracking_board_read", "我的跟踪池现在怎么样", note="跟踪看板"),
    Case("model_arena_leaderboard", "哪个模型在这些标的上更准", note="模型榜单"),
    Case("t1_quality_read", "平台判定的 T+1 命中率是多少", note="自身质量"),
    Case("paper_portfolio_read", "模拟盘现在怎么样", note="模拟盘"),
    # ── L2 证据：本轮新增的三条真数据源 ────────────────────────────────
    Case("chip_distribution_read", "600519 最近筹码集中度怎么样", note="筹码"),
    Case("intraday_bars_read", "600519 今天分时承接怎么样",
         alternates=("capital_flow_read",), note="分时承接"),
    Case("capital_flow_read", "600519 今天主力净流入多少",
         may_abstain=True, note="东财封 IP 且无 token 时只应弃权"),
    Case("announcements_read", "600519 最近有什么公告", note="公告"),
    Case("index_quote_read", "今天上证指数多少点",
         alternates=("get_realtime_quotes",), note="指数快照"),
    Case("watchlist_read", "我的自选股有哪些", note="自选"),
    Case("data_freshness_check", "你的数据是新的吗", alternates=("index_quote_read",),
         note="时效自检"),
)


@dataclass
class Observation:
    tool: str
    question: str
    ok: bool = False
    planned: tuple[str, ...] = ()
    executed: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    status: str = ""
    tier: str = ""
    latency_ms: int = 0
    model_calls: int = 0
    rejected_claims: int = 0
    unavailable_declared: tuple[str, ...] = ()
    answer_head: str = ""
    detail: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


def _evidence_ids(result: Any) -> tuple[str, ...]:
    """从回合的逐步观测里收集证据编号。

    `TurnResult.steps` 里每步记了 `evidence_count`，但"被引用"要看编号本身。
    这里退一步取**实际产出的证据编号集合**：它是"证据进得来"的必要条件，
    "进得来"之后才轮到校验器判"引用对不对"（那部分由 `rejected_claims` 观测）。
    """
    ids: list[str] = []
    for step in getattr(result, "steps", ()) or ():
        for key in ("evidence_ids", "evidence"):
            raw = step.get(key) if isinstance(step, dict) else None
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    text = item.get("id") if isinstance(item, dict) else item
                    if text:
                        ids.append(str(text))
    return tuple(dict.fromkeys(ids))


def run_case(case: Case, *, user_id: str, profile_id: str, planner: Any) -> Observation:
    from api.services import chat_service
    from tradingagents.agent.routing import looks_like_market_screen

    obs = Observation(tool=case.tool, question=case.question)
    body = chat_service.ChatRequest(
        messages=[{"role": "user", "content": case.question}],
        model_profile_id=profile_id,
    )
    started = time.time()
    try:
        request = chat_service.build_turn_request(body, user_id=user_id, planner=planner)
    except Exception as exc:  # noqa: BLE001 — 观测脚本不该因单条异常中断
        obs.detail = f"build_turn_request raised: {type(exc).__name__}: {exc}"
        return obs

    if request is None:
        obs.status = "clarify(400)"
        obs.detail = "没构造出回合（追问）。若该工具本来不需要标的，这是失败。"
        obs.ok = False
        return obs

    obs.tier = request.tier
    obs.planned = tuple(
        str(raw.get("tool") or "")
        for raw in (request.planned_steps or ())
        if isinstance(raw, dict) and raw.get("tool")
    )

    events: "queue.Queue[Any]" = queue.Queue()
    try:
        result = chat_service.run_turn_streaming(
            request=request,
            user_id=user_id,
            body=body,
            events=events,
            is_cancelled=lambda: False,
            deps=None,
        )
    except Exception as exc:  # noqa: BLE001
        obs.detail = f"run_turn raised: {type(exc).__name__}: {exc}"
        obs.latency_ms = int((time.time() - started) * 1000)
        return obs

    obs.latency_ms = int(getattr(result, "latency_ms", 0) or (time.time() - started) * 1000)
    obs.status = str(getattr(result, "status", ""))
    obs.executed = tuple(getattr(result, "tool_calls", ()) or ())
    obs.evidence_ids = _evidence_ids(result)
    obs.model_calls = int(getattr(result, "model_calls", 0) or 0)
    obs.rejected_claims = int(getattr(result, "rejected_claims", 0) or 0)
    obs.unavailable_declared = tuple(getattr(result, "unavailable_declared", ()) or ())
    answer = str(getattr(result, "answer", "") or "")
    obs.answer_head = answer.replace("\n", " ")[:160]
    obs.extras = {
        "weak_verification": bool(getattr(result, "weak_verification", False)),
        "position_known": getattr(result, "position_known", None),
        "notices": list(getattr(result, "notices", ()) or ())[:3],
    }

    wanted = {case.tool, *case.alternates}
    if wanted & set(obs.executed):
        obs.ok = True
    elif case.may_abstain and (obs.unavailable_declared or "未覆盖" in answer or "取不到" in answer):
        # 取不到但如实说了：通过（这正是 ① 要验的"如实弃权"）
        obs.ok = True
        obs.detail = "数据取不到，但已如实声明未覆盖/弃权"
    else:
        obs.detail = (
            f"期望 {case.tool}（或 {list(case.alternates)}）出现在执行计划里，"
            f"实际执行 {list(obs.executed)}；规划步骤={list(obs.planned)}"
        )
        if looks_like_market_screen(case.question):
            obs.detail += "（该问句被判为全市场筛选）"
    return obs


def main() -> int:
    parser = argparse.ArgumentParser(description="逐工具真实对话验证")
    parser.add_argument("--only", action="append", default=None, help="只跑指定工具（可多次）")
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--profile-id", default=DEFAULT_PROFILE_ID)
    args = parser.parse_args()

    cases = [c for c in CASES if not args.only or c.tool in args.only]
    if not cases:
        print("没有匹配的用例", file=sys.stderr)
        return 2

    from api.services import chat_service

    planner = chat_service.build_planner(user_id=args.user_id)
    print(f"规划器：{'已启用' if planner else '未启用（走规则层）'}")
    print(f"用户：{args.user_id}｜模型档案：{args.profile_id}")
    print(f"用例：{len(cases)} 条\n")

    observations: list[Observation] = []
    for case in cases:
        obs = run_case(
            case, user_id=args.user_id, profile_id=args.profile_id, planner=planner
        )
        observations.append(obs)
        flag = "OK " if obs.ok else "FAIL"
        print(f"[{flag}] {obs.tool}")
        print(f"       问：{case.question}")
        print(f"       执行：{list(obs.executed) or '（无）'}｜证据 {len(obs.evidence_ids)} 条"
              f"｜{obs.status}｜{obs.latency_ms}ms")
        if obs.detail:
            print(f"       {obs.detail}")
        if obs.answer_head:
            print(f"       答：{obs.answer_head}")
        print()

    passed = sum(1 for o in observations if o.ok)
    print(f"通过 {passed}/{len(observations)}")
    failed = [o for o in observations if not o.ok]
    if failed:
        print("\n未通过：")
        for obs in failed:
            print(f"  - {obs.tool}：{obs.detail or obs.status}")

    payload = {
        "user_id": args.user_id,
        "profile_id": args.profile_id,
        "planner_enabled": planner is not None,
        "passed": passed,
        "total": len(observations),
        "observations": [o.__dict__ for o in observations],
    }
    if args.json:
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"JSON 已写入 {args.json}")

    return 0 if passed == len(observations) else 1


if __name__ == "__main__":
    raise SystemExit(main())

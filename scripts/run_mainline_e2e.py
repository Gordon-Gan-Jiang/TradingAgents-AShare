"""市场主线真实端到端一键运行（M1-M6 全链路）。

用法：
    python scripts/run_mainline_e2e.py [--date 2026-08-31] [--perspective short|medium] [--focus "只看 AI 相关"] [--no-save]

前置条件（.env 或环境变量）：
    TA_API_KEY       必填（LLM 密钥，OpenAI 兼容）
    TA_BASE_URL      可选（OpenAI 兼容代理地址，如 https://api.deepseek.com/v1）
    TA_LLM_DEEP      可选（默认 gpt-4o；注意空字符串不会回退默认值，请显式填写）
    TA_LLM_QUICK     可选（默认 gpt-4o-mini）

流程：真实板块数据（东财/同花顺/新浪多源兜底）→ 规则层 v2 评分/硬门槛/情绪 gating
      → LLM 主线识别（状态机携带昨日主线）→ 规则层候选池 → LLM 主线选股 → 控制台输出。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from tradingagents.dataflows.trade_calendar import cn_today_str  # noqa: E402


def _check_config() -> None:
    missing: list[str] = []
    if not os.getenv("TA_API_KEY"):
        missing.append("TA_API_KEY")
    if not os.getenv("TA_LLM_DEEP"):
        print("[warn] TA_LLM_DEEP 未设置或为空字符串（空字符串不会回退默认值），将使用空模型名 → 建议显式设置")
    if not os.getenv("TA_LLM_QUICK"):
        print("[warn] TA_LLM_QUICK 未设置或为空字符串，同上")
    if missing:
        print("=" * 60)
        print("缺少 LLM 配置：请在 .env 中设置：")
        print("  TA_API_KEY=sk-...        # OpenAI 兼容密钥")
        print("  TA_BASE_URL=https://api.openai.com/v1   # 或代理地址")
        print("  TA_LLM_DEEP=gpt-4o")
        print("  TA_LLM_QUICK=gpt-4o-mini")
        print("=" * 60)
        raise SystemExit(1)


def _fmt_pct(v) -> str:
    if v is None:
        return "-"
    return f"{v * 100:.2f}%"


def _print_result(result: dict) -> None:
    market = result.get("market") or {}
    emotion = market.get("emotion") or {}
    print("\n" + "=" * 66)
    print(f"市场主线报告  {result['trade_date']}  [{result['perspective']}]")
    print(f"情绪温度: {emotion.get('temperature')} ({emotion.get('regime')})  涨停{emotion.get('zt_count')}家")
    if emotion.get("gate_reason"):
        print(f"⚠ {emotion['gate_reason']}")
    warnings = result.get("warnings") or []
    for w in warnings[:6]:
        print(f"· 数据提示: {w[:90]}")
    print("=" * 66)

    mainlines = result.get("mainlines") or []
    if not mainlines:
        print("本次未识别出高置信主线（见原始报告）。")
    for i, m in enumerate(mainlines, 1):
        print(f"\n主线 {i}: {m.get('name')}  [{m.get('type')}] {m.get('phase') or ''} "
              f"置信度={m.get('confidence')} 状态={m.get('status_vs_yesterday') or '新发'}")
        print(f"  逻辑: {m.get('logic')}")
        if m.get("drivers"):
            print(f"  驱动: {', '.join(m['drivers'][:4])}")
        if m.get("representative_boards"):
            print(f"  代表板块: {', '.join(b.get('board', '') for b in m['representative_boards'][:3])}")
        if m.get("verify_conditions"):
            print(f"  验证条件: {'；'.join(m['verify_conditions'][:2])}")
        if m.get("risks"):
            print(f"  风险: {'；'.join(m['risks'][:3])}")
        if m.get("evidence"):
            print(f"  依据: {m['evidence'][:120]}")

    candidates = result.get("candidates") or []
    print(f"\n候选股（{len(candidates)} 只）:")
    for c in candidates:
        print(f"  {c.get('name')}({c.get('symbol')}) [{c.get('tier') or '未分层'}] score={c.get('score')} "
              f"介入: {c.get('entry_hint')}  风险: {c.get('risk')}")
    if result.get("gated_out"):
        print(f"\n置信度不足未选股的主线: {', '.join(result['gated_out'])}")
    print("=" * 66)


def main() -> None:
    parser = argparse.ArgumentParser(description="市场主线真实端到端")
    parser.add_argument("--date", default=cn_today_str(), help="交易日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--perspective", choices=["short", "medium"], default="short")
    parser.add_argument("--focus", default=None, help="关注方向，如：只看 AI 相关")
    parser.add_argument("--no-save", action="store_true", help="不写入数据库（仅控制台输出）")
    args = parser.parse_args()

    _check_config()

    from tradingagents.graph.mainline_graph import run_mainline_analysis

    if args.no_save:
        result = asyncio.run(
            run_mainline_analysis(
                args.date, args.perspective, user_focus=args.focus, include_breadth=False
            )
        )
        _print_result(result)
        return

    # 走 API 服务链路（落库 + job 事件），需要 DB 可用
    from api.database import get_db_ctx
    from api.services import mainline_service

    job_id = __import__("uuid").uuid4().hex
    with get_db_ctx() as db:
        mainline_service.create_run(
            db, user_id="cli", run_id=job_id, trade_date=args.date,
            perspective=args.perspective, job_id=job_id,
        )
    result = asyncio.run(
        mainline_service.run_mainline_job(
            job_id, "cli", args.date, args.perspective,
            user_focus=args.focus, set_job=lambda **kw: None, emit_event=lambda *a: None,
        )
    )
    _print_result(result)
    print(f"已落库 run_id={job_id}")


if __name__ == "__main__":
    main()

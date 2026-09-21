"""从项目数据库 model_profiles 捞取真实 LLM 配置，跑市场主线真实端到端。"""
import asyncio
import os
import sys
import uuid

sys.path.insert(0, "/Users/gankang/AlphaPilot-A-Share")
from dotenv import load_dotenv

load_dotenv("/Users/gankang/AlphaPilot-A-Share/.env")

from sqlalchemy import text  # noqa: E402

from api.database import get_db_ctx  # noqa: E402
from api.services import auth_service, mainline_service  # noqa: E402


def load_deepseek_config() -> dict:
    """从 model_profiles 挑一条 DeepSeek 配置（OpenAI 兼容）。"""
    with get_db_ctx() as db:
        cols = [r[1] for r in db.execute(text("PRAGMA table_info(model_profiles)")).fetchall()]
        rows = db.execute(
            text("SELECT * FROM model_profiles WHERE api_key_encrypted IS NOT NULL AND api_key_encrypted != ''")
        ).fetchall()
        profiles = [dict(zip(cols, r)) for r in rows]
    chosen = None
    for p in profiles:
        name = str(p.get("name") or "")
        deep = str(p.get("deep_think_llm") or "")
        if "deepseek" in name.lower() or "deepseek" in deep.lower():
            chosen = p
            break
    if chosen is None:
        chosen = profiles[0]
    key = auth_service.decrypt_secret_with_fallback(chosen.get("api_key_encrypted"))
    if not key:
        raise RuntimeError("model_profiles 中无可用 key（解密失败）")
    deep = chosen.get("deep_think_llm") or chosen.get("name") or "deepseek-chat"
    quick = chosen.get("quick_think_llm") or "deepseek-chat"
    print(f"[config] profile={chosen.get('name')} deep={deep} quick={quick} key={key[:6]}…(len={len(key)})")
    return {
        "llm_provider": "openai",
        "backend_url": "https://api.deepseek.com/v1",
        "deep_think_llm": str(deep),
        "quick_think_llm": str(quick),
        "api_key": key,
    }


def _fmt_pct(v) -> str:
    if v is None:
        return "-"
    return f"{v * 100:.2f}%"


def print_report(result: dict, tag: str) -> None:
    market = result.get("market") or {}
    emotion = market.get("emotion") or {}
    print(f"\n{'='*66}\n[{tag}] 市场主线报告 {result['trade_date']} [{result['perspective']}]")
    print(f"情绪温度: {emotion.get('temperature')} ({emotion.get('regime')}) 涨停{emotion.get('zt_count')}家 最高{emotion.get('max_lianban')}连板")
    if emotion.get("gate_reason"):
        print(f"⚠ {emotion['gate_reason']}")
    for w in (result.get("warnings") or [])[:5]:
        print(f"· 数据提示: {w[:90]}")
    print("=" * 66)
    mainlines = result.get("mainlines") or []
    if not mainlines:
        print("（未识别出高置信主线）")
    for i, m in enumerate(mainlines, 1):
        print(f"\n主线{i}: {m.get('name')} [{m.get('type')}] {m.get('phase') or ''} 置信={m.get('confidence')} 状态={m.get('status_vs_yesterday') or '新发'}")
        print(f"  逻辑: {m.get('logic')}")
        if m.get("drivers"):
            print(f"  驱动: {'、'.join(m['drivers'][:4])}")
        if m.get("representative_boards"):
            print(f"  代表板块: {', '.join(str(b.get('board','')) for b in m['representative_boards'][:3])}")
        if m.get("verify_conditions"):
            print(f"  验证: {'；'.join(m['verify_conditions'][:2])}")
        if m.get("risks"):
            print(f"  风险: {'；'.join(m['risks'][:3])}")
    candidates = result.get("candidates") or []
    print(f"\n候选股（{len(candidates)}）:")
    for c in candidates:
        print(f"  {c.get('name')}({c.get('symbol')}) [{c.get('tier') or '未分层'}] score={c.get('score')} 介入: {c.get('entry_hint')}")
    if result.get("gated_out"):
        print(f"\n未选股（置信度不足）: {', '.join(result['gated_out'])}")
    print("=" * 66)


async def run_once(config: dict, tag: str) -> dict:
    trade_date = "2026-08-31"
    job_id = uuid.uuid4().hex
    with get_db_ctx() as db:
        mainline_service.create_run(
            db, user_id="cli-live", run_id=job_id, trade_date=trade_date, perspective="short", job_id=job_id
        )
    result = await mainline_service.run_mainline_job(
        job_id, "cli-live", trade_date, "short",
        set_job=lambda *a, **kw: None, emit_event=lambda *a: None,
        config=config,
    )
    print_report(result, tag)
    return result


def main() -> int:
    base = load_deepseek_config()
    attempts = [
        ("deepseek-v4-pro/flash", dict(base)),
    ]
    # 回退候选：用 deepseek-reasoner / deepseek-chat 作为 deep
    fallback = dict(base)
    fallback["deep_think_llm"] = "deepseek-reasoner"
    fallback["quick_think_llm"] = "deepseek-v4-flash"
    attempts.append(("deepseek-reasoner/flash(fallback)", fallback))
    for tag, cfg in attempts:
        print(f"\n>>> 尝试 [{tag}] ...")
        try:
            asyncio.run(run_once(cfg, tag))
            return 0
        except Exception as exc:
            print(f"[{tag}] 失败: {type(exc).__name__}: {str(exc)[:200]}")
            # 该模型不存在或 key 无效 → 试下一个
            continue
    print("所有配置均失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())

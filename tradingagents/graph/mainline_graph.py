"""市场主线 Agent 链路（M3）：mainline_analyst → mainline_stock_selector。

独立小图（不混入个股主图），复用 create_llm_client / tracker 流式输出。
数据来自 MarketCollector（M2 多源稳定采集），规则层候选池由本模块构建：
- 涨停池（东财，稳定可用）为主候选池；
- 东财板块成分股为每主线增强（尽力而为，失败自动降级）。
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import pandas as pd
from langchain_core.messages import HumanMessage, SystemMessage

from tradingagents.agents.utils.agent_states import current_tracker_var
from tradingagents.agents.utils.mainline_structured import (
    extract_mainline_json,
    extract_selector_json,
    get_mainline_json_instruction,
    get_selector_json_instruction,
    normalize_candidates,
    normalize_mainlines,
)
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.dataflows.market_collector import MarketCollector, _df_to_rows
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients import create_llm_client
from tradingagents.methodology import get_mainline_ashare_methodology_block
from tradingagents.prompts import get_prompt

# 选股置信度门槛：低于该值的主线不输出候选股（弱主线强行选股 = 制造亏损）
DEFAULT_CONFIDENCE_THRESHOLD = 60


# ── LLM 客户端参数（与 trading_graph._get_provider_kwargs 同口径） ──


def _provider_kwargs(config: dict) -> dict:
    kwargs: dict = {}
    provider = str(config.get("llm_provider", "")).lower()
    llm_temperature = config.get("llm_temperature")
    if provider == "google":
        thinking_level = config.get("google_thinking_level")
        if thinking_level:
            kwargs["thinking_level"] = thinking_level
        if config.get("api_key"):
            kwargs["api_key"] = config["api_key"]
    elif provider == "openai":
        reasoning_effort = config.get("openai_reasoning_effort")
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if llm_temperature is not None:
            kwargs["temperature"] = llm_temperature
        if config.get("api_key"):
            kwargs["api_key"] = config["api_key"]
    elif provider in ("xai", "openrouter", "ollama"):
        if llm_temperature is not None:
            kwargs["temperature"] = llm_temperature
        if config.get("api_key"):
            kwargs["api_key"] = config["api_key"]
    elif provider == "anthropic":
        if config.get("api_key"):
            kwargs["api_key"] = config["api_key"]
    return kwargs


def validate_llm_config(config: dict) -> None:
    """LLM 运行时配置前置校验：缺失 api_key/模型时给出可读中文错误（而非裸 OpenAIError）。

    覆盖「系统默认模型」场景：.env 未配置 TA_API_KEY、或模型名为空字符串时，
    报错明确指引配置位置（.env 或 Web 设置/模型管理页）。
    """
    missing: list[str] = []
    if not config.get("api_key"):
        missing.append("API Key")
    if not config.get("deep_think_llm"):
        missing.append("深度模型")
    if not config.get("quick_think_llm"):
        missing.append("快速模型")
    if not missing:
        return
    raise ValueError(
        "未配置 LLM "
        + "、".join(missing)
        + "：请在【.env】设置 TA_API_KEY 并确认 TA_LLM_DEEP / TA_LLM_QUICK 有值，"
        "或到 Web 端【设置】/【模型管理】页配置 LLM 后重试"
    )


def _build_llm(config: dict, model_key: str):
    client = create_llm_client(
        provider=config["llm_provider"],
        model=config[model_key],
        base_url=config.get("backend_url"),
        **_provider_kwargs(config),
    )
    return client.get_llm()


# ── 市场池 → LLM 输入文本 ────────────────────────────────────────


def format_market_pool_for_llm(
    pool: dict,
    yesterday_mainlines: Optional[list] = None,
    user_focus: Optional[str] = None,
) -> str:
    """把 MarketCollector 市场池浓缩为 LLM 可读的文本（只含真实数据）。"""
    lines: list[str] = []
    emotion = pool.get("emotion") or {}
    if emotion.get("temperature") is not None:
        lines.append(
            f"【市场情绪温度】{emotion['temperature']}（{emotion.get('regime')}）"
            f"｜涨停{emotion.get('zt_count')}家｜最高{emotion.get('max_lianban')}连板"
        )
        if emotion.get("gate_reason"):
            lines.append(f"情绪提示：{emotion['gate_reason']}")
    breadth = pool.get("breadth") or {}
    if breadth.get("total"):
        approx = "（近似·THS行业聚合）" if breadth.get("approximate") else ""
        lines.append(
            f"【市场宽度{approx}】上涨{breadth.get('up')} / 下跌{breadth.get('down')} / "
            f"平盘{breadth.get('flat') or '—'}（共{breadth.get('total')}）"
        )
    bench = pool.get("benchmark") or {}
    if bench.get("chg_5d") is not None:
        lines.append(
            f"【基准指数】沪深300 当日{bench.get('chg_1d', 0):.2%}｜5日{bench['chg_5d']:.2%}"
        )

    boards = pool.get("mainline_candidates") or []
    if boards:
        lines.append("\n【候选板块特征表（规则层 v2 已打分，按 composite 降序）】")
        lines.append("| # | 类型 | 板块 | 当日% | RS20% | 均线多头 | RSI14 | 硬门槛 | 阶段预判 | heat | strength | composite | 净流入1d(亿) | 5日/10日资金持续 | 领涨股 |")
        lines.append("|---|------|------|-------|-------|---------|-------|--------|---------|------|-----------|-----------|-------------|------------------|--------|")
        for i, b in enumerate(boards[:25], 1):
            lines.append(
                f"| {i} | {'行业' if b.get('sector_type') == 'industry' else '概念'} | {b.get('name')} "
                f"| {_fmt_pct_value(b.get('chg_1d'))} | {_fmt_pct(b.get('rs20'))} "
                f"| {'是' if b.get('ma_bullish') else '否'} | {_fmt(b.get('rsi14'))} "
                f"| {'通过' if b.get('passes_gate') else '未过'} | {b.get('phase_hint') or '-'} "
                f"| {b.get('heat_score')} | {b.get('strength_score')} | {b.get('composite_score')} "
                f"| {_fmt(b.get('net_inflow_1d'))} "
                f"| {'是' if b.get('inflow_persistent_10') else ('缺失' if b.get('flow_data_missing') else '否')} "
                f"| {b.get('leader')} |"
            )
        lines.append("说明：硬门槛=RS20>0 且 MA20>MA60 且 广度≥0.5 且 RSI<85 且 资金双周期（数据缺失豁免）；"
                     "阶段预判为规则层提示（发酵/主升/高位分歧/退潮/脉冲），请据此做最终阶段确认。")

    zt_heat = pool.get("zt_heat") or []
    if zt_heat:
        lines.append(
            "\n【涨停行业热度】"
            + "；".join(
                f"{r.get('industry')}(涨停{r.get('zt_count')},最高{r.get('max_lianban')}板)"
                for r in zt_heat[:10]
            )
        )

    ind_spot = pool.get("industry_spot") or []
    if ind_spot:
        lines.append(
            "\n【行业板块涨幅榜 Top10】"
            + "、".join(f"{r.get('name')}{_fmt_pct_value(r.get('chg_1d'))}" for r in ind_spot[:10])
        )
    con_spot = pool.get("concept_spot")
    if con_spot:
        lines.append(
            "\n【概念板块涨幅榜 Top10】"
            + "、".join(f"{r.get('name')}{_fmt_pct_value(r.get('chg_1d'))}" for r in con_spot[:10])
        )
    elif (pool.get("sources") or {}).get("concept_spot") is None:
        lines.append("\n【概念板块涨幅榜】当前数据源不可用（东财接口受限），以涨停行业热度作为题材信号。")

    macro = pool.get("macro_brief") or ""
    if macro:
        lines.append(f"\n【宏观简报】\n{macro[:800]}")

    if yesterday_mainlines:
        lines.append("\n【昨日主线（状态机输入，判断延续/扩散/退潮/新发）】")
        for m in yesterday_mainlines:
            lines.append(
                f"- {m.get('name')}（confidence={m.get('confidence')}, phase={m.get('phase') or '未知'}）"
            )
    if user_focus:
        lines.append(f"\n【用户关注方向】{user_focus}")
    return "\n".join(lines)


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    try:
        f = float(v)
        return f"{f:,.1f}" if abs(f) >= 100 else f"{f:.1f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_pct(v: Any) -> str:
    """小数单位 → 百分数文本（如 -0.0436 → '-4.4%'）。"""
    if v is None:
        return "-"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(v)


def _fmt_pct_value(v: Any) -> str:
    """百分比单位 → 百分数文本（如 6.5 → '6.5%'，akshare 涨跌幅即为百分比单位）。"""
    if v is None:
        return "-"
    try:
        return f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return str(v)


# ── 候选股池（规则层） ───────────────────────────────────────────


def _filter_candidate_stocks(df: pd.DataFrame) -> pd.DataFrame:
    """候选股硬过滤：剔 ST/退市、北交所，剔停牌（价格/换手缺失）。"""
    if df is None or df.empty:
        return df
    d = df.copy()
    if "name" in d.columns:
        d = d[~d["name"].astype(str).str.contains(r"ST|退|\*", regex=True, na=False)]
    if "code" in d.columns:
        d = d[d["code"].astype(str).str.match(r"^(6|0|3)\d{5}$", na=False)]
    if "latest" in d.columns and "turnover" in d.columns:
        d = d[~(d["latest"].isna() & d["turnover"].isna())]
    return d.reset_index(drop=True)


def build_candidate_pool(
    mainlines: list[dict],
    pool: dict,
    ak_module=None,
    *,
    max_zt: int = 25,
    max_cons_per_mainline: int = 12,
) -> dict:
    """为选股师构建候选股池（规则层真实数据）。

    - 涨停池（东财，稳定）为主候选，含代码/名称/涨幅/连板/行业；
    - 东财板块成分股为每主线增强（尽力而为，失败自动降级，不阻塞）。
    返回 {"zt_pool": [rows], "by_mainline": {主线名: [rows]}, "warnings": [...]}
    """
    from tradingagents.dataflows.providers.cn_akshare_provider import (
        fetch_board_cons_df,
        fetch_zt_pool_rows_df,
    )
    from tradingagents.dataflows.trade_calendar import cn_today_str

    if ak_module is None:
        import akshare as ak  # type: ignore

        ak_module = ak
    result: dict[str, Any] = {"zt_pool": [], "by_mainline": {}, "warnings": []}
    trade_date = pool.get("trade_date") or cn_today_str()

    try:
        zt = fetch_zt_pool_rows_df(ak_module, trade_date)
        zt = _filter_candidate_stocks(zt)
        if not zt.empty:
            zt = zt.sort_values(["lianban", "chg_1d"], ascending=False).head(max_zt)
            result["zt_pool"] = _df_to_rows(zt)
    except Exception as exc:
        result["warnings"].append(f"涨停池候选不可用：{type(exc).__name__}: {str(exc)[:100]}")

    # 每主线成分股：并发拉取 + 每块超时降级（东财 push2 不稳时快速跳过，不阻塞）
    cons_jobs: list[tuple[str, str, str]] = []  # (mainline_name, board_name, sector_type)
    for m in mainlines:
        name = m.get("name")
        if not name:
            continue
        boards = (m.get("representative_boards") or [])[:1]
        for b in boards:
            board_name = b.get("board") if isinstance(b, dict) else str(b)
            if board_name:
                cons_jobs.append((name, board_name, m.get("type", "concept")))

    def _fetch_cons(job: tuple[str, str, str]):
        _ml, board_name, sector_type = job
        try:
            return fetch_board_cons_df(ak_module, board_name, sector_type)
        except Exception:
            return None

    if cons_jobs:
        with ThreadPoolExecutor(max_workers=min(3, len(cons_jobs))) as executor:
            futures = {executor.submit(_fetch_cons, job): job for job in cons_jobs}
            for future, job in futures.items():
                ml_name, board_name, _sector_type = job
                try:
                    cons = future.result(timeout=12)
                except Exception:
                    cons = None
                if cons is None or cons.empty:
                    continue
                cons = _filter_candidate_stocks(cons)
                if cons.empty:
                    continue
                cons = cons.sort_values("chg_1d", ascending=False).head(max_cons_per_mainline)
                rows = result["by_mainline"].setdefault(ml_name, [])
                for _, r in cons.iterrows():
                    rows.append(
                        {
                            "code": str(r.get("code", "")),
                            "name": str(r.get("name", "")),
                            "chg_1d": _opt(r.get("chg_1d")),
                            "turnover": _opt(r.get("turnover")),
                            "total_mv": _opt_mv_100m(r.get("total_mv")),  # 元 → 亿
                            "lianban": 0,
                            "industry": "",
                            "source": "cons",
                            "board": board_name,
                        }
                    )
    return result


def _opt(v: Any) -> Any:
    try:
        f = float(v)
        return None if pd.isna(f) else round(f, 3)
    except (TypeError, ValueError):
        return None


def _opt_mv_100m(v: Any) -> Any:
    """市值（元）→ 亿元。cons/zt 的总市值/流通市值原始单位是元，转亿避免 LLM/前端误读量级。"""
    f = _opt(v)
    if f is None:
        return None
    return round(f / 1e8, 2)


def format_selector_input(mainlines: list[dict], candidate_pool: dict) -> str:
    """候选股池 + 主线清单 → LLM 选股输入文本。"""
    lines: list[str] = []
    zt = candidate_pool.get("zt_pool") or []
    if zt:
        lines.append("【候选股池 · 涨停池（source=zt_pool）】")
        lines.append("| 代码 | 名称 | 涨跌幅% | 连板 | 所属行业 | 换手% |")
        lines.append("|---|---|---|---|---|---|")
        for r in zt:
            lines.append(
                f"| {r.get('code')} | {r.get('name')} | {_fmt(r.get('chg_1d'))} "
                f"| {r.get('lianban')} | {r.get('industry')} | {_fmt(r.get('turnover'))} |"
            )
    else:
        lines.append("【候选股池 · 涨停池】当前不可用（数据源受限）。")

    by_mainline = candidate_pool.get("by_mainline") or {}
    for m in mainlines:
        rows = by_mainline.get(m.get("name"), [])
        if rows:
            lines.append(f"\n【{m.get('name')} · 板块成分股候选（source=cons）】")
            lines.append("| 代码 | 名称 | 涨跌幅% | 换手% | 总市值(亿) |")
            lines.append("|---|---|---|---|---|")
            for r in rows:
                lines.append(
                    f"| {r.get('code')} | {r.get('name')} | {_fmt(r.get('chg_1d'))} "
                    f"| {_fmt(r.get('turnover'))} | {_fmt(r.get('total_mv'))} |"
                )

    lines.append("\n【待选股的主线（confidence<60 的不在此列）】")
    for m in mainlines:
        boards = "、".join(
            (b.get("board") if isinstance(b, dict) else str(b))
            for b in (m.get("representative_boards") or [])
        )
        lines.append(
            f"- {m.get('name')}（type={m.get('type')}, confidence={m.get('confidence')}, "
            f"phase={m.get('phase') or '未知'}, 代表板块={boards or '无'}）"
        )
    return "\n".join(lines)


# ── Agent 节点 ───────────────────────────────────────────────────


async def mainline_analyst(
    llm,
    market_pool: dict,
    *,
    yesterday_mainlines: Optional[list] = None,
    user_focus: Optional[str] = None,
    config: Optional[dict] = None,
    tracker=None,
) -> dict:
    """主线分析师：规则层候选特征 + 情绪/宽度/催化 → 1~3 条主线 JSON。"""
    cfg = config or get_config()
    system = get_prompt("mainline_system_message", config=cfg) or ""
    addon = get_mainline_ashare_methodology_block(cfg)
    if addon:
        system += "\n\n## A 股主线识别方法论（须对照执行）\n\n" + addon
    data_text = format_market_pool_for_llm(market_pool, yesterday_mainlines, user_focus)
    messages = [
        SystemMessage(content=system),
        HumanMessage(content=data_text + "\n\n" + get_mainline_json_instruction()),
    ]
    active_tracker = tracker or current_tracker_var.get()
    full = ""
    async for chunk in llm.astream(messages):
        content = chunk.content if hasattr(chunk, "content") else str(chunk)
        full += content
        if active_tracker is not None:
            active_tracker._emit_token("Mainline Analyst", "mainline_report", content)
    parsed = normalize_mainlines(extract_mainline_json(full))
    return {"report": full, "structured": parsed}


async def mainline_stock_selector(
    llm,
    mainlines: list[dict],
    candidate_pool: dict,
    *,
    config: Optional[dict] = None,
    tracker=None,
    confidence_threshold: int = DEFAULT_CONFIDENCE_THRESHOLD,
) -> dict:
    """主线选股师：候选股池 + 主线清单 → 分层候选股 JSON。

    - 置信度 < threshold 的主线强制不选股（gated_out）；
    - 候选池不足时输出空 + warning，不硬凑。
    """
    cfg = config or get_config()
    eligible = [m for m in mainlines if int(m.get("confidence") or 0) >= confidence_threshold]
    gated_out = [
        m.get("name")
        for m in mainlines
        if m.get("name") and int(m.get("confidence") or 0) < confidence_threshold
    ]
    if not eligible:
        return {
            "candidates": [],
            "gated_out": gated_out,
            "report": "",
            "warnings": [f"无 confidence>={confidence_threshold} 的主线，跳过选股"],
        }
    system = get_prompt("mainline_selector_system_message", config=cfg) or ""
    addon = get_mainline_ashare_methodology_block(cfg)
    if addon:
        system += "\n\n## 主线选股方法论（须对照执行）\n\n" + addon
    data_text = format_selector_input(eligible, candidate_pool)
    messages = [
        SystemMessage(content=system),
        HumanMessage(content=data_text + "\n\n" + get_selector_json_instruction()),
    ]
    active_tracker = tracker or current_tracker_var.get()
    full = ""
    async for chunk in llm.astream(messages):
        content = chunk.content if hasattr(chunk, "content") else str(chunk)
        full += content
        if active_tracker is not None:
            active_tracker._emit_token("Mainline Selector", "mainline_selector_report", content)
    candidates = normalize_candidates(extract_selector_json(full))
    return {"candidates": candidates, "gated_out": gated_out, "report": full, "warnings": []}


# ── 编排入口 ─────────────────────────────────────────────────────


async def run_mainline_analysis(
    trade_date: str,
    perspective: str = "short",
    *,
    yesterday_mainlines: Optional[list] = None,
    user_focus: Optional[str] = None,
    config: Optional[dict] = None,
    tracker=None,
    market_collector: Optional[MarketCollector] = None,
    include_breadth: bool = False,
    confidence_threshold: int = DEFAULT_CONFIDENCE_THRESHOLD,
) -> dict:
    """M3 端到端编排：采集市场池 → 主线识别 → 主线选股。

    返回结构化结果：market（市场池）、analyst_report、mainlines、candidates、
    gated_out、candidate_pool、warnings。
    """
    cfg = dict(config or DEFAULT_CONFIG)
    set_config(cfg)
    # LLM 配置前置校验（config=None 直接调用场景）：给出可读错误而非裸 OpenAIError
    validate_llm_config(cfg)
    collector = market_collector if market_collector is not None else MarketCollector()
    # 关键：collect 内部是同步 akshare 网络请求（30-70s），必须移到线程池，
    # 否则会阻塞 asyncio 事件循环 → 其他所有页面/请求全部卡住
    pool = await asyncio.to_thread(
        collector.collect, trade_date, perspective, include_breadth=include_breadth
    )

    deep_llm = _build_llm(cfg, "deep_think_llm")
    quick_llm = _build_llm(cfg, "quick_think_llm")

    analyst_out = await mainline_analyst(
        deep_llm,
        pool,
        yesterday_mainlines=yesterday_mainlines,
        user_focus=user_focus,
        config=cfg,
        tracker=tracker,
    )
    mainlines = analyst_out["structured"].get("mainlines", [])
    market_reading = analyst_out["structured"].get("market_reading", "")

    candidate_pool = await asyncio.to_thread(build_candidate_pool, mainlines, pool, None)
    selector_out = await mainline_stock_selector(
        quick_llm,
        mainlines,
        candidate_pool,
        config=cfg,
        tracker=tracker,
        confidence_threshold=confidence_threshold,
    )

    warnings = list(pool.get("warnings") or [])
    warnings.extend(candidate_pool.get("warnings") or [])
    warnings.extend(selector_out.get("warnings") or [])

    return {
        "trade_date": trade_date,
        "perspective": perspective,
        "market": pool,
        "analyst_report": analyst_out["report"],
        "selector_report": selector_out.get("report", ""),
        "market_reading": market_reading,
        "mainlines": mainlines,
        "candidates": selector_out["candidates"],
        "gated_out": selector_out.get("gated_out", []),
        "candidate_pool": candidate_pool,
        "warnings": warnings,
    }

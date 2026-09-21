"""
A 股方法论加载器：内置 Markdown + 按类型可选外部片段（如 FinSkills China-market）。

环境变量（均为「设为 0/false 关闭」各类方法论注入）：
  TA_METHODOLOGY_ASHARE_FUNDAMENTALS / _NEWS / _MACRO
  TA_METHODOLOGY_EXTRA / _NEWS / _MACRO   显式 .md 或目录（优先于 FinSkills 默认）
  FINSKILLS_ROOT 或 TA_FINSKILLS_ROOT     本地克隆的 finskills 仓库根目录；未设 EXTRA 时自动拼接
    China-market/*/references（见 finskills_defaults.py）
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

_MAX_EXTRA_CHARS = 12_000

_PKG_DIR = Path(__file__).resolve().parent

_KIND_FILES: dict[str, str] = {
    "fundamentals": "ashare_fundamentals.md",
    "news_events": "ashare_events.md",
    "sector_macro": "ashare_sector_macro.md",
    "mainline": "ashare_mainline.md",
}

_CONFIG_FLAG: dict[str, str] = {
    "fundamentals": "methodology_ashare_fundamentals",
    "news_events": "methodology_ashare_news_events",
    "sector_macro": "methodology_ashare_sector_macro",
    "mainline": "methodology_ashare_mainline",
}

_ENV_FLAG: dict[str, str] = {
    "fundamentals": "TA_METHODOLOGY_ASHARE_FUNDAMENTALS",
    "news_events": "TA_METHODOLOGY_ASHARE_NEWS",
    "sector_macro": "TA_METHODOLOGY_ASHARE_MACRO",
    "mainline": "TA_METHODOLOGY_ASHARE_MAINLINE",
}

_CONFIG_EXTRA: dict[str, str] = {
    "fundamentals": "methodology_extra_path",
    "news_events": "methodology_extra_path_news",
    "sector_macro": "methodology_extra_path_macro",
    "mainline": "methodology_extra_path_mainline",
}

_ENV_EXTRA: dict[str, str] = {
    "fundamentals": "TA_METHODOLOGY_EXTRA",
    "news_events": "TA_METHODOLOGY_EXTRA_NEWS",
    "sector_macro": "TA_METHODOLOGY_EXTRA_MACRO",
    "mainline": "TA_METHODOLOGY_EXTRA_MAINLINE",
}

_EXTRA_SECTION_TITLE: dict[str, str] = {
    "fundamentals": "外部追加方法论（FinSkills 等）",
    "news_events": "外部追加（事件/舆情）",
    "sector_macro": "外部追加（行业/宏观）",
    "mainline": "外部追加（主线/题材）",
}


def _read_file(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _load_extra_from_path(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    p = Path(raw).expanduser()
    if not p.exists():
        return ""
    chunks: list[str] = []
    if p.is_file():
        if p.suffix.lower() in (".md", ".markdown", ".txt"):
            chunks.append(_read_file(p))
    elif p.is_dir():
        for f in sorted(p.glob("*.md")):
            try:
                chunks.append(_read_file(f))
            except OSError:
                continue
    text = "\n\n".join(c for c in chunks if c.strip())
    if len(text) > _MAX_EXTRA_CHARS:
        text = text[:_MAX_EXTRA_CHARS] + "\n\n…（外部方法论已截断至长度上限）"
    return text


def _kind_enabled(kind: str, config: dict | None) -> bool:
    cfg = config or {}
    key = _CONFIG_FLAG.get(kind, "")
    if key and key in cfg:
        return bool(cfg[key])
    env_name = _ENV_FLAG.get(kind, "TA_METHODOLOGY_ASHARE_FUNDAMENTALS")
    return os.getenv(env_name, "1").strip().lower() not in ("0", "false", "no", "off")


def _extra_path(kind: str, config: dict | None) -> str:
    cfg = config or {}
    ck = _CONFIG_EXTRA.get(kind, "methodology_extra_path")
    ev = _ENV_EXTRA.get(kind, "TA_METHODOLOGY_EXTRA")
    explicit = (cfg.get(ck) or os.getenv(ev, "") or "").strip()
    if explicit:
        return explicit
    from tradingagents.methodology.finskills_defaults import default_finskills_references_path

    fs = default_finskills_references_path(kind, cfg)
    if fs:
        return fs
    return _default_stock_analysis_team_references_path(cfg)


def _default_stock_analysis_team_references_path(config: dict | None = None) -> str:
    """
    兜底支持本仓库 `skills/stock-analysis-team/references`：
    优先读取 env `STOCK_ANALYSIS_TEAM_ROOT` / `TA_STOCK_ANALYSIS_TEAM_ROOT`。
    """
    cfg = config or {}
    raw = (
        cfg.get("stock_analysis_team_root")
        or os.getenv("STOCK_ANALYSIS_TEAM_ROOT")
        or os.getenv("TA_STOCK_ANALYSIS_TEAM_ROOT")
        or ""
    ).strip()
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw).expanduser())

    # 默认按当前文件相对仓库根目录回退：.../TradingAgents-AShare/skills/stock-analysis-team
    repo_guess = _PKG_DIR.parent.parent / "skills" / "stock-analysis-team"
    candidates.append(repo_guess)

    for root in candidates:
        ref = root / "references"
        if ref.is_dir():
            return str(ref)
    return ""


def _stock_team_enabled(config: dict | None = None) -> bool:
    cfg = config or {}
    if "methodology_stock_analysis_team" in cfg:
        return bool(cfg.get("methodology_stock_analysis_team"))
    return os.getenv("TA_METHODOLOGY_STOCK_TEAM", "1").strip().lower() not in ("0", "false", "no", "off")


def get_stock_team_reference_block(
    filename: str,
    *,
    heading: str | None = None,
    config: dict | None = None,
) -> str:
    """
    读取 stock-analysis-team/references 中的单个参考文档并包装为注入块。
    """
    if not _stock_team_enabled(config):
        return ""
    refs = _default_stock_analysis_team_references_path(config)
    if not refs:
        return ""
    path = Path(refs) / filename
    if not path.is_file():
        return ""
    try:
        text = _read_file(path).strip()
    except OSError:
        return ""
    if not text:
        return ""
    title = heading or f"stock-analysis-team/{filename}"
    if len(text) > _MAX_EXTRA_CHARS:
        text = text[:_MAX_EXTRA_CHARS] + "\n\n…（方法论已截断至长度上限）"
    return f"【{title}】\n{text}"


def get_stock_team_analysis_framework_block(config: dict | None = None) -> str:
    return get_stock_team_reference_block("analysis-framework.md", heading="分析框架", config=config)


def get_stock_team_market_strategy_block(config: dict | None = None) -> str:
    return get_stock_team_reference_block("market-strategy.md", heading="市场策略", config=config)


def get_stock_team_risk_scoring_block(config: dict | None = None) -> str:
    return get_stock_team_reference_block("risk-scoring-criteria.md", heading="风险评分标准", config=config)


def get_ashare_methodology_block(kind: str, config: dict | None = None) -> str:
    """
    kind: fundamentals | news_events | sector_macro
    返回注入对应 Agent 系统提示的方法论文本；禁用或缺失文件时返回空字符串。
    """
    if kind not in _KIND_FILES:
        return ""
    if not _kind_enabled(kind, config):
        return ""
    builtin_path = _PKG_DIR / _KIND_FILES[kind]
    try:
        base = _read_file(builtin_path)
    except OSError:
        return ""

    extra = _load_extra_from_path(_extra_path(kind, config))
    if not extra:
        return base

    title = _EXTRA_SECTION_TITLE.get(kind, "外部追加")
    return base + f"\n\n---\n\n## {title}\n\n" + extra


def methodology_ashare_fundamentals_enabled(config: dict | None) -> bool:
    return _kind_enabled("fundamentals", config)


def get_fundamentals_ashare_methodology_block(config: dict | None = None) -> str:
    """兼容旧接口：等价于 get_ashare_methodology_block("fundamentals", config)。"""
    return get_ashare_methodology_block("fundamentals", config)


def get_news_events_ashare_methodology_block(config: dict | None = None) -> str:
    return get_ashare_methodology_block("news_events", config)


def get_sector_macro_ashare_methodology_block(config: dict | None = None) -> str:
    return get_ashare_methodology_block("sector_macro", config)


def get_mainline_ashare_methodology_block(config: dict | None = None) -> str:
    """市场主线方法论（mainline_analyst / mainline_stock_selector 注入）。"""
    return get_ashare_methodology_block("mainline", config)


def get_methodology_snapshot(config: dict | None = None) -> dict[str, object]:
    """Compact fingerprint of enabled built-in / stock-team methodology for report provenance."""

    cfg = config or {}
    parts: list[str] = []
    for kind in sorted(_KIND_FILES.keys()):
        en = _kind_enabled(kind, cfg)
        parts.append(f"{kind}={'1' if en else '0'}")
        if en:
            try:
                blob = get_ashare_methodology_block(kind, cfg) or ""
            except OSError:
                blob = ""
            parts.append(str(len(blob)))
    st = _stock_team_enabled(cfg)
    parts.append(f"stock_team={'1' if st else '0'}")
    raw = "|".join(parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return {
        "digest": digest,
        "ashare_blocks": {k: _kind_enabled(k, cfg) for k in _KIND_FILES},
        "stock_analysis_team": st,
    }

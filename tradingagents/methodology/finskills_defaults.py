"""
本地 FinSkills 仓库路径解析：在未设置 TA_METHODOLOGY_EXTRA_* 时，
从 FINSKILLS_ROOT 拼接 China-market 下与各类 Agent 对齐的 references 目录。

FinSkills 版权归原项目所有，使用须遵守其 LICENSE：
https://github.com/Geeksfino/finskills
"""
from __future__ import annotations

import os
from pathlib import Path

# 与 tradingagents.methodology.loader 中 kind 一致
_FINSKILLS_CHINA_REF_SUBDIR: dict[str, str] = {
    # 财务报表深度分析
    "fundamentals": "China-market/financial-statement-analyzer/references",
    # 事件驱动
    "news_events": "China-market/event-driven-detector/references",
    # 行业轮动 / 宏观框架
    "sector_macro": "China-market/sector-rotation-detector/references",
}


def finskills_root_from_config(config: dict | None) -> Path | None:
    cfg = config or {}
    raw = (cfg.get("finskills_root") or os.getenv("FINSKILLS_ROOT") or os.getenv("TA_FINSKILLS_ROOT") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    return p if p.is_dir() else None


def default_finskills_references_path(kind: str, config: dict | None = None) -> str:
    """
    若本地存在对应目录，返回绝对路径字符串；否则返回空字符串。
    显式 TA_METHODOLOGY_EXTRA_* 优先于本函数（在 loader 中处理）。
    """
    rel = _FINSKILLS_CHINA_REF_SUBDIR.get(kind)
    if not rel:
        return ""
    root = finskills_root_from_config(config)
    if root is None:
        return ""
    target = root / rel
    return str(target) if target.is_dir() else ""

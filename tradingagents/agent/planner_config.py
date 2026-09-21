"""规划层的开关与模型配置（纯函数，便于单测）。

## 为什么规划层的模型必须与回答模型分开配

两件事的成本与要求完全不同：
* **规划**：一次很短的调用（输入问句 + 工具摘要，输出一小段 JSON），要的是**便宜、
  快、稳定**——它每轮都跑；
* **回答**：要的是**表达与引用质量**，可以用更贵的模型。

用同一个模型跑两件事，要么规划太贵（每轮都付高价模型的价），要么回答太弱。
因此留出独立配置项，默认跟随回答模型（不改变今天行为）。

## 为什么必须有关闭开关

新规划层是**行为变更**（它改变了"什么问句能进来"）。灰度开关是本平台的既有做法
（每一处接入都"新增而不替换"）：关闭即回到规则层行为，出问题能立刻回退，
而不是回滚代码。
"""

from __future__ import annotations

import os

# 开关：只有显式打开才启用 LLM 规划层。默认关闭 = 今天的行为。
PLANNER_ENABLED_ENV = "TA_LLM_PLANNER"
# 规划器专用模型（可选）。留空 = 跟随回答模型。
PLANNER_MODEL_ENV = "TA_PLANNER_MODEL"
# 规划器专用模型档案（可选，优先级高于上面的模型名）。
PLANNER_PROFILE_ENV = "TA_PLANNER_MODEL_PROFILE_ID"

_TRUTHY = ("1", "true", "yes", "on", "enabled")


def _clean(name: str) -> str:
    return (os.getenv(name) or "").strip()


def llm_planner_enabled() -> bool:
    """LLM 规划层是否启用。**默认关闭**——灰度期间行为必须与今天一致。"""
    return _clean(PLANNER_ENABLED_ENV).lower() in _TRUTHY


def planner_target() -> tuple[str | None, str | None]:
    """规划器的 (model_profile_id, model_name)。

    两者都留空时返回 (None, None) → 调用方跟随回答模型，行为与今天一致。
    """
    return _clean(PLANNER_PROFILE_ENV) or None, _clean(PLANNER_MODEL_ENV) or None


def describe_planner() -> dict[str, object]:
    """配置摘要，供日志/诊断——"为什么这轮没走规划"要能一句话答出来。"""
    profile, model = planner_target()
    return {
        "enabled": llm_planner_enabled(),
        "profile_id": profile,
        "model": model,
        "follows_answer_model": profile is None and model is None,
    }

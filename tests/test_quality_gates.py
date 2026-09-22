"""M2/M3/M4/M6：**闸门必须守住它声称要守的东西**。

## 缺陷（M6 的由来）

`golden_report.py --with-planner` 曾在规划器构造失败时**静默退回规则层**，
而报告标题固定写死"Phase 0"，于是"规划层报告"里装的是规则路径的数字，
文件里没有一行说明。当时的输出是这样的：

    [LLM Client] Init openai (gpt-4o) at https://api.openai.com/v1
    WARNING: planner failed, falling back to rules: 规划器认证失败
    → 报告照常产出，口径标错

这类"**度量工具测的不是它声称测的东西**"是本轮主线二的核心：
CI 全绿 ≠ 生产可用。以下测试把它钉住。

## 覆盖

* **M6**：`--with-planner` 但规划器构造不出来 → **必须拒绝出报告**（已修，补测试）
* **M2**：规划层口径与规则口径**必须分开**标注，不能共用一个标题
* **M3/M4**：`check.sh` 的各档位必须**自报口径**，且 35 例验收套件要能接进来
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK_SH = ROOT / "scripts" / "check.sh"
GOLDEN = ROOT / "scripts" / "golden_report.py"


# ----------------------------------------------------- M6：拒绝而不是标错口径

def test_with_planner_refuses_when_planner_cannot_be_built(monkeypatch):
    """**核心回归**：要规划层却构造不出规划器 → 抛 SystemExit 拒绝出报告。

    修复前：静默退回规则层却照常产出报告，标题写死"Phase 0"，
    于是"规划层报告"里装的是规则路径的数字，文件里没有一行说明。

    这里 monkeypatch 掉 `build_planner` 让它返回 None（等价于没给
    `TA_PLANNER_USER_ID`），并断言**拒绝**而不是产出。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import golden_report  # type: ignore

    from api.services import chat_service

    monkeypatch.setattr(chat_service, "build_planner", lambda user_id=None: None)

    with pytest.raises(SystemExit) as ei:
        golden_report.run(verbose=False, with_planner=True)

    assert ei.value.code != 0
    assert "规划层" in str(ei.value), "拒绝原因没说明口径风险"


def test_rules_path_still_runs_without_planner(monkeypatch):
    """反向护栏：**不**要规划层时不得因为构造不出规划器而拒绝。

    规则路径是离线的、无模型依赖的，必须随时能跑——否则本地开发就被堵死。
    这条防止把拒绝逻辑写得过宽。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    import golden_report  # type: ignore

    payload = golden_report.run(verbose=False, with_planner=False)
    assert payload is not None
    assert payload["metrics"]["total"] > 0


def test_golden_report_source_refuses_on_missing_planner():
    """回归护栏：源码里必须保留"要规划层但没构造出来就 SystemExit"的分支。

    用源码断言而不是跑真实 CLI，是因为真实 CLI 要 50 次 LLM 调用。
    这条护栏防的是"以后有人图省事把拒绝分支删掉"。
    """
    src = GOLDEN.read_text(encoding="utf-8")
    assert "SystemExit" in src, "拒绝分支不见了：口径又会标错"
    assert "with_planner" in src
    # 报告里必须落盘口径，否则事后无法判断这份数字是哪条路径跑的
    assert '"with_planner"' in src or "'with_planner'" in src
    assert "planner_model" in src


def test_golden_report_stamps_the_mode():
    """报告标题必须带上口径，不能写死成某个阶段名。"""
    src = GOLDEN.read_text(encoding="utf-8")
    assert "规则路径" in src and "规划层" in src
    # 两种口径必须写到**不同**文件，否则跑一次规划层就把规则基线覆盖了
    assert "golden-with-planner" in src, "两种口径共用输出路径 → 会互相覆盖"


def test_printed_metrics_block_declares_its_mode():
    """终端打印的指标块标题必须带口径。

    此前是写死的"黄金集基线"，于是"规划层"的数字与"规则路径"的数字
    在终端上长得一模一样——与报告标题写死"Phase 0"是同一类问题。
    """
    src = GOLDEN.read_text(encoding="utf-8")
    assert '_mode_label' in src, "打印标题没有按口径区分"
    # 断言**打印语句**本身带口径，而不是断言"全文不含旧字符串"——后者会误伤
    # 描述这次修复的注释（注释里合法地引用了旧写法）。
    printer_lines = [
        ln for ln in src.splitlines()
        if "print(" in ln and "黄金集" in ln
    ]
    assert printer_lines, "找不到打印黄金集标题的语句"
    for ln in printer_lines:
        assert "_mode_label" in ln, f"打印标题没带口径：{ln.strip()}"


def test_offline_and_planner_have_separate_thresholds():
    """两个口径基线不同，阈值必须跟着口径走。"""
    src = CHECK_SH.read_text(encoding="utf-8")
    assert "GOLDEN_MIN_PLANNING" in src
    assert "GOLDEN_MIN_RECALL" in src


# --------------------------------------------- M2/M3/M4：闸门要自报口径

def test_check_sh_exists_and_is_syntactically_valid():
    """闸门脚本必须语法正确——一个语法错误会让整条闸门形同虚设。

    这不是假想问题：该脚本历史上就出现过"中文全角括号首字节被当成变量名"，
    导致"检测到回归"那条分支永远只输出一个语法错误而不给结论。
    """
    out = subprocess.run(
        ["bash", "-n", str(CHECK_SH)], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, f"check.sh 语法错误：{out.stderr}"


def test_check_sh_declares_which_path_each_gate_guards():
    """每一档位都要说清它守的是**哪个口径**，否则会误以为守住了生产。"""
    src = CHECK_SH.read_text(encoding="utf-8")
    assert "规则路径" in src or "离线" in src, "离线档没有标注口径"
    assert "规划" in src, "没有规划层档位 → 生产路径无人守"


def test_check_sh_mentions_the_acceptance_suite():
    """35 例端到端验收套件要能接进闸门（M4）。"""
    src = CHECK_SH.read_text(encoding="utf-8")
    assert "verify_tool_turns" in src, "端到端验收套件没有接入闸门"


def test_check_sh_planner_tier_refuses_without_user():
    """规划层档位在没给 `TA_PLANNER_USER_ID` 时必须**拒绝跑**。

    否则会退化成"用规则路径的数字冒充规划层"——正是 M6 那个坑。
    这条与 `verify_tool_turns.py` 的 `--allow-rules-only` 是同一个设计：
    默认拒绝，要跑得显式声明。
    """
    src = CHECK_SH.read_text(encoding="utf-8")
    assert "TA_PLANNER_USER_ID" in src, "规划层档位没有要求用户上下文 → 会静默退规则层"


def test_check_sh_offline_gate_runs_by_default():
    """离线黄金集闸门成本低（不调模型），应当**默认就跑**，不是选入。

    修复前：默认 `check.sh` 只跑 pytest+tsc+vitest，**没有任何质量指标**——
    套件全绿但规划准确率可以悄悄掉一半。

    断言的是"**旧的 opt-in 守卫已经不存在**"，而不是去解析脚本的控制流：
    后者很脆（注释里出现同样的字面量就会让切分错位）。
    """
    src = CHECK_SH.read_text(encoding="utf-8")
    # 旧的选入写法必须消失：`if [ "${1:-}" = "--golden" ]; then`
    assert '= "--golden" ]' not in src, "离线闸门仍是选入的 → 默认 CI 不看质量指标"
    # 而黄金集调用必须在（无条件地）出现
    assert "golden_report.py --no-write" in src
    # 且不能被任何档位守卫包着：调用行所在位置之前不得有未闭合的 tier 判断
    offender = [
        ln for ln in src.splitlines()
        if "golden_report.py --no-write" in ln and "--with-planner" not in ln
    ]
    assert offender, "找不到无条件的离线黄金集调用"

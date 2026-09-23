"""规划层看到的**工具面**必须与执行层一致，且描述不许被截断。

## 为什么这两条必须有测试

④ 的收口动作是"先看规划器实际多调了什么"。要回答这个问题，跑分必须先**可信**。
本轮排查发现跑分链路有两处系统性偏差，都会让"规划器乱调工具"这个结论失真：

1. **规划器与执行层看到的不是同一份工具面。**
   `build_planner()` 用生产注册表（当时 50 个工具；本轮退役 8 个后为 42 个）构造提示词，而黄金集 harness 用哑
   依赖装配了 26 个。规划器选中一个只在生产注册表里的 L1 工具（例如
   `report_consensus_read`）时，`_plan_tools` 会因为"未在册"把它**丢掉**。
   于是观测到的工具集合 ≠ 规划器的真实选择——精度与召回都测在一个被截断的
   集合上。这正是本仓库反复踩的"静默丢掉"形态，只是这次发生在度量里。

2. **工具摘要被按 60 字截断，恰好砍掉唯一的用法提示。**
   注册表里 25 个工具的 `description` 带"用于回答「…」"，其中 14 个的这段
   落在第 60 字之后。规划器只看到第一行前 60 字，等于看着 23 条几乎同义的
   "既有分析管道使用的数据工具 get_xxx"去猜该调哪个——多调噪声工具的根因
   在这里，而不在模型的判断力。

两处都不是风格问题：一个是度量有效性，一个是提示词有效性。
"""

from __future__ import annotations

import pytest

from api.services import chat_service
from tests.golden.harness import warm_registry


def test_golden_registry_matches_planner_tool_surface():
    """跑分用的注册表必须与规划器构造提示词时用的是**同一份**。

    只要两边存在差集，跑分观测到的工具集合就不等于规划器的选择，精度/召回
    全部失真。这条断言是"④ 工具精确率收口"结论有效性的前提。
    """
    harness_names = set(warm_registry().names())
    planner_names = set(chat_service.build_registry("golden").names())

    assert harness_names == planner_names, (
        "跑分注册表与规划器工具面不一致："
        f"缺失={sorted(planner_names - harness_names)} "
        f"多余={sorted(harness_names - planner_names)}"
    )


def test_planned_step_for_port_only_tool_is_not_dropped():
    """规划器选中的 L1 工具必须真的进入工具计划，不能被静默丢弃。

    用 `report_consensus_read` 作样本：它只存在于"注入了 report_port"的注册表里，
    正是旧 harness 缺的那一类。静默丢弃会让回答看起来"查过了"而实际没有——本仓库
    最不能接受的失败形态。
    """
    from tradingagents.agent.loop import TurnRequest, _plan_tools

    request = TurnRequest(
        text="600519 的多视角共识怎么样",
        user_id="golden",
        symbol="600519.SH",
        planned_steps=(
            {"tool": "report_consensus_read", "arguments": {}, "reason": "planned"},
        ),
    )
    planned = _plan_tools(request, registry=warm_registry())

    assert [call.tool for call in planned] == ["report_consensus_read"], (
        "规划器选中的工具被丢弃了：规划层与实际执行的工具面不一致"
    )


def test_every_planned_tool_in_golden_set_is_executable():
    """黄金集里出现的每个 `required_tools`/`optional_tools` 都必须在跑分注册表里在册。

    否则那一条永远不可能被满足——失败被记成"规划器没选"，实际是"根本不可选"。
    """
    from tests.golden.harness import load_golden

    names = set(warm_registry().names())
    missing: dict[str, list[str]] = {}
    for item in load_golden():
        gap = sorted((set(item.required_tools) | set(item.optional_tools)) - names)
        if gap:
            missing[item.id] = gap

    assert not missing, f"黄金集引用了不在册的工具：{missing}"


@pytest.mark.parametrize(
    "tool_name",
    [
        "portfolio_holdings_read",
        "portfolio_risk_review",
        "report_consensus_read",
        "verdict_history_read",
        "signal_washout_probe_batch",
    ],
)
def test_digest_keeps_usage_hint_for_tools_that_have_one(tool_name: str):
    """带"用于回答「…」"的工具，这段提示必须出现在给规划器的摘要里。

    摘要的作用就是让模型区分"该调哪个"。截断到第一行前 60 字会把 25 个工具里
    14 个的用法提示砍掉，剩下的信息量不足以做选择。
    """
    digest = chat_service.build_tool_digest(chat_service.build_registry("golden"))
    spec = chat_service.build_registry("golden").get(tool_name)
    assert spec is not None

    line = next(
        (row for row in digest.splitlines() if row.startswith(f"- {tool_name}")), None
    )
    assert line is not None, f"{tool_name} 不在摘要里"
    assert "用于回答" in line, f"{tool_name} 的用法提示被截断掉了：{line}"


def test_digest_labels_every_tool_with_its_layer():
    """摘要必须标出工具面分层。

    L1 是"一个工具回答一个判断问题"，L2 只是取数原语。不标分层，模型无从知道
    `signal_washout_probe` 与 `get_balance_sheet` 不在一个抽象层级上，就会同一个问题
    既调判定又调它底下的原始数据——多调工具的典型形态。
    （这里原先是拿 `get_indicators` 举例；它**已从工具面退役**，例子换成一个仍在册的 L2 工具。）
    """
    registry = chat_service.build_registry("golden")
    digest = chat_service.build_tool_digest(registry)
    rows = [row for row in digest.splitlines() if row.startswith("- ")]

    assert len(rows) == len(registry.names()), "摘要条数与注册表不符"
    for row in rows:
        assert "[L1]" in row or "[L2]" in row or "[L3]" in row, f"缺分层标注：{row}"


def test_digest_puts_decision_tools_before_raw_data_tools():
    """判断类工具排在取数原语之前。

    模型对清单是有位置偏好的：把"能直接回答问题"的 L1 放前面、把 23 条同义的
    vendor 取数工具压到最后，选择会明显更稳。顺序不是装饰，是提示词的一部分。
    """
    digest = chat_service.build_tool_digest(chat_service.build_registry("golden"))
    rows = [row for row in digest.splitlines() if row.startswith("- ")]
    layers = [
        ("L1" if "[L1]" in row else "L3" if "[L3]" in row else "L2") for row in rows
    ]

    assert layers == sorted(layers, key=["L1", "L3", "L2"].index), (
        f"分层顺序不对：{[f'{a}' for a in layers]}"
    )


# ── 参数清单：规划器不能靠猜参数名 ────────────────────────────────────────
#
# 摘要只给"名字 + 用法提示"时，模型知道该调哪个工具，却不知道那个工具要什么参数。
# 实测代价（2026-09-21～22 的 41 个回合）：规划器按语感拼参数名——
# `get_board_spot` 给 `board_name`（真名 `sector_type`）、`index_quote_read` 给
# `symbols`（真名 `codes`）、`get_board_hist` 给 `period`（真名 `board_name`）——
# 每次都被注册表拒掉，用户看到的是「本轮无可用证据」。
#
# 注册表后来学会了"丢弃不相干的外来键、只拒绝像拼写错误的键"，这让那些调用
# **不再失败**，但代价是参数被静默丢弃、工具退回默认值（`get_board_spot` 于是
# 永远按 `sector_type="industry"` 取数）。**容错是止血，不是治疗**——治疗是让
# 模型一开始就看见真名。


def _digest_rows() -> dict[str, str]:
    """把摘要解析成 {工具名: 该行}，供参数契约断言使用。"""
    digest = chat_service.build_tool_digest(chat_service.build_registry("golden"))
    rows: dict[str, str] = {}
    for row in digest.splitlines():
        if row.startswith("- "):
            rows[row[2:].split(" ", 1)[0]] = row
    return rows


def _argument_segment(row: str) -> str:
    """取摘要行里的参数清单段；没有这一段就是缺契约。"""
    assert "参数：" in row, f"摘要行里没有参数清单：{row}"
    return row.split("参数：", 1)[1]


def test_digest_renders_argument_name_for_every_declared_parameter():
    """每个已声明参数的**真名**都必须出现在摘要里。

    真名是模型唯一能用来拼调用的凭据。给不出真名，模型只能按语感猜；而猜错的
    后果不是报错（注册表会容错丢弃），而是**静默退回默认值**——一个不报错、
    也不留痕迹的错结果。
    """
    registry = chat_service.build_registry("golden")
    rows = _digest_rows()

    missing: list[str] = []
    for name in registry.names():
        schema = getattr(registry.get(name), "input_schema", None) or {}
        segment = _argument_segment(rows[name])
        for arg in (schema.get("properties") or {}):
            if arg not in segment:
                missing.append(f"{name}.{arg}")

    assert not missing, f"摘要没给出这些参数的真名：{missing[:12]}"


def test_digest_marks_required_arguments_as_required():
    """必填与可选必须区分开。

    把 `as_of`（有默认值）当可选没问题；把 `symbol` 当可选就会得到
    `missing required arguments` 而让整次调用失败——两者在摘要里长得一样时，
    模型没有依据做这个区分。
    """
    registry = chat_service.build_registry("golden")
    rows = _digest_rows()

    wrong: list[str] = []
    for name in registry.names():
        schema = getattr(registry.get(name), "input_schema", None) or {}
        required = set(schema.get("required") or [])
        segment = _argument_segment(rows[name])
        for arg in (schema.get("properties") or {}):
            marked = f"{arg}(必填)" in segment
            if arg in required and not marked:
                wrong.append(f"{name}.{arg} 是必填却没标")
            elif arg not in required and marked:
                wrong.append(f"{name}.{arg} 不是必填却标了必填")

    assert not wrong, f"必填标记与 schema 不符：{wrong[:12]}"


def test_digest_says_explicitly_when_a_tool_takes_no_argument():
    """无参数工具必须写明"无"。

    `get_board_fund_flow` / `get_hot_stocks_xq` 不接受任何参数，而它们恰好是
    当前最稳的两个板块类工具。不写"无"，模型会按"板块类工具总得有个板块名"的
    语感给它们编一个参数出来。
    """
    registry = chat_service.build_registry("golden")
    rows = _digest_rows()
    no_argument = [
        name
        for name in registry.names()
        if not (
            (getattr(registry.get(name), "input_schema", None) or {}).get("properties")
            or {}
        )
    ]

    assert no_argument, "工具面里应当存在无参数工具，否则这条契约没有意义"
    for name in no_argument:
        segment = _argument_segment(rows[name]).strip()
        assert segment.startswith("无"), f"{name} 不接收参数却没写明：{segment}"


# ── 摘要的预算与断句 ──────────────────────────────────────────────────────
#
# 摘要是**每轮都要付的成本**：42 条工具清单随每次规划调用进模型。它涨一截，
# 成本与延迟就涨一截（实测中位回合延迟 26.7s，已在 15s 验收线之外）。参数清单
# 让摘要从 5540 涨到 7074 字符（+28%）——这是有理由的涨价，但**下一次涨价应当
# 是一个显式决定**，而不是没人察觉的漂移。
#
# 断句是另一头：摘要被截断时若停在字中间（实测 22 条里有 7 条停在句中、15 条
# 停在词中，例如 `…（vendor 类别：board_data）。返回 v`），读到的就是一个残句。
# 残句比短句更糟——它看上去像完整的语义，而实际上被切掉了限定条件。


def _digest_row(digest: str, name: str) -> str | None:
    return next(
        (row for row in digest.splitlines() if row.startswith(f"- {name} ")), None
    )


def test_digest_stays_within_its_declared_budget():
    """摘要体积必须有一个**显式声明**的上限。

    没有上限时，"顺手多渲染一点"累积到某一天才会以"延迟为什么涨了"的形式
    暴露，而那时已经查不出是哪次改动涨的。上限是棘轮：放宽它可以，但要改这个
    常量、并在评审里说明理由。
    """
    digest = chat_service.build_tool_digest(chat_service.build_registry("golden"))

    assert len(digest) <= chat_service._DIGEST_MAX_CHARS, (
        f"摘要 {len(digest)} 字符，超出声明上限 {chat_service._DIGEST_MAX_CHARS}"
    )
    # 棘轮必须咬得住：把上限改成任意大数等于没有上限。
    assert chat_service._DIGEST_MAX_CHARS <= 12000, "上限宽到失去约束意义"


def test_digest_never_truncates_text_mid_clause():
    """被截断的摘要行必须收在句读上，不许断在字中间。

    判据只作用于**真的被截断**的行：源文本没超过窗口的行不适用（它们本来就
    完整）。断在字中间会让规划器读到一个残句——"自述**；窗口内没有样本时会
    显式声明。scope 决" 这种结尾，看上去像句子，实际被砍掉了限定条件。
    """
    registry = chat_service.build_registry("golden")
    digest = chat_service.build_tool_digest(registry)
    boundaries = ("。", "！", "？", "；", "」", "）")

    offenders: list[str] = []
    for name in registry.names():
        row = _digest_row(digest, name)
        assert row is not None, f"{name} 不在摘要里"
        head = ((registry.get(name).description or "").splitlines() or [""])[0]
        marker = "用于回答"
        if marker in head:
            source, cap = head[head.index(marker) :], chat_service._DIGEST_USAGE_CHARS
        else:
            source, cap = head, chat_service._DIGEST_TITLE_CHARS
        if len(source) <= cap:
            continue  # 没被截断
        if not row.split("｜参数：")[0].rstrip().endswith(boundaries):
            offenders.append(f"{name}（结尾：…{row.split('｜参数：')[0][-18:]}）")

    assert not offenders, "这些行的摘要断在字中间：\n  " + "\n  ".join(offenders)


# ── 规划提示词的选择纪律 ──────────────────────────────────────────────────
#
# 工具摘要只解决"看不看得清"，提示词还要解决"该不该多选"。实测规划器在
# "这只票要不要止损"上同时给了判定工具与一个板块取数工具——多出来的那个既不
# 是必需也不产生可引用证据，纯粹是噪声与成本。纪律必须写进提示词，而不是
# 指望模型自己悟。


def _prompt() -> str:
    from tradingagents.agent.routing import LlmPlanner, RuleTierClassifier

    planner = LlmPlanner(
        inner=RuleTierClassifier(chat_service._Resolver()),
        complete=lambda _p: "{}",
        known_tools=(),
        tool_digest="- signal_washout_probe [L1]：判定洗盘/派发",
    )
    return planner.build_prompt("这只票要不要止损")


def test_prompt_demands_the_fewest_necessary_tools():
    prompt = _prompt()
    assert "宁少勿多" in prompt or "最少" in prompt, "提示词缺少「少调优先」的纪律"
    assert "必需" in prompt, "提示词没有说清「只选回答该问题必需的工具」"


def test_prompt_warns_against_mixing_layers():
    """同问题不要既调判定工具又调它底下的原始数据工具。"""
    prompt = _prompt()
    assert "L1" in prompt and "L2" in prompt, "提示词没解释分层含义"
    assert "不要" in prompt or "禁止" in prompt, "提示词没有给出禁止性约束"


def test_prompt_shows_the_exact_output_shape():
    """提示词要给形状样例，不能只列键名。

    只列键名时模型会把 `steps` 写成工具名数组、把 `confidence` 写成 `"high"`。
    解析层现在读得懂这两种写法（不丢信息），但每次读懂都是一次侥幸；样例摆在
    眼前更省事，也更容易被审计。
    """
    prompt = _prompt()
    assert '"tool"' in prompt and '"arguments"' in prompt, "没给 steps 的项形状"
    assert "数字" in prompt, "没说 confidence 是数字"
    assert '"intent"' in prompt, "没给完整 JSON 样例"
    assert "0.8" in prompt, "样例里没有可模仿的 confidence 数值"


# ── 度量必须同时记录"规划器的选择"与"活下来的工具" ────────────────────────


def test_probe_records_raw_plan_steps():
    """跑分要能回答"规划器到底选了什么"，而不只是"最后执行了什么"。

    只记后者时，被注册表挡掉的步骤在数据里**完全不存在**——本轮就是靠掐指头
    才发现规划器与执行层工具面不一致。观测项必须把两边都留下，差异自己会说话。

    注意规划层只在**规则定不出标的**时才被调用（见 `build_turn_request`），
    所以这里用的问句必须没有可解析标的，否则测的是规则路径。
    """
    from tests.golden import harness
    from tradingagents.agent.routing import Plan, PlanStep

    def _fake_plan(_text: str) -> Plan:
        return Plan(
            intent="single_symbol_judgement",
            tier="medium",
            symbol="600519.SH",
            steps=(
                PlanStep(tool="signal_washout_probe", reason="判定"),
                PlanStep(tool="report_consensus_read", reason="共识"),
            ),
            confidence=0.9,
            source="llm",
        )

    probe_ = harness.probe("它是不是在洗盘", planner=_fake_plan)

    assert probe_.planned_steps == ("signal_washout_probe", "report_consensus_read")
    assert "signal_washout_probe" in probe_.tools
    assert probe_.plan_source == "llm"


def test_probe_marks_dropped_planned_steps():
    """规划器选了、执行层却丢掉的步骤必须能被识别出来（不是静默消失）。"""
    from tests.golden import harness
    from tradingagents.agent.routing import Plan, PlanStep

    def _fake_plan(_text: str) -> Plan:
        return Plan(
            intent="single_symbol_judgement",
            tier="medium",
            symbol="600519.SH",
            steps=(PlanStep(tool="__not_a_tool__", reason="x"),),
            confidence=0.9,
            source="llm",
        )

    probe_ = harness.probe("它是不是在洗盘", planner=_fake_plan)

    assert "__not_a_tool__" in probe_.planned_steps
    assert "__not_a_tool__" not in probe_.tools


# ── 规划层说"不需要工具"时，执行层不许自作主张调一个 ──────────────────────


def _plan_request(**kw):
    from tradingagents.agent.loop import TurnRequest

    base = dict(text="什么是洗盘", user_id="u1", tier="light")
    base.update(kw)
    return TurnRequest(**base)


def test_llm_empty_plan_means_no_tools():
    """`plan_source="llm"` + 空步骤 = 规划层的明确决定，不是"没有计划"。

    实测：50 条黄金集里有 8 条（「什么是洗盘」「帮我记一下这笔交易」「我持仓里哪只
    最危险」这一类的无标的问题）规划器明确给了空步骤，执行层却因为它落进轻档的
    机械兜底，替用户调了一次 `get_board_spot`。用户问概念，平台去查板块行情——
    8 次全是纯噪声，工具精确率因此从 60.9% 掉到 52.0%。

    "算出来了却在边界丢掉"是本仓库的老毛病；这一次是它的镜像：**决定做出来了，
    却在边界被推翻**。
    """
    from tradingagents.agent.loop import _plan_tools

    request = _plan_request(plan_source="llm", intent="concept_qa", planned_steps=())
    assert _plan_tools(request, registry=warm_registry()) == [], (
        "规划层说了不需要工具，执行层不该兜底调一个"
    )


def test_rules_turns_still_get_the_mechanical_fallback():
    """对照组：**规则层**的回合并未被规划器表态，机械兜底必须保留。

    否则「现在多少钱」这类最高频的事实问句会退回"无可用证据"——那正是之前修过的
    老 bug（轻档计划空 → 永远答无证据）。
    """
    from tradingagents.agent.loop import _plan_tools

    request = _plan_request(text="今天涨停板有多少只", plan_source="rules", tier="light")
    planned = [call.tool for call in _plan_tools(request, registry=warm_registry())]
    assert planned, "规则层回合仍要有兜底工具"


def test_llm_screen_turn_is_not_treated_as_an_empty_plan():
    """筛选回合即使步骤为空也要走筛选：`screen=True` 本身就是规划层的决定。"""
    from tradingagents.agent.loop import _plan_tools

    request = _plan_request(
        text="放量票找十个给我", plan_source="llm", screen=True, planned_steps=()
    )
    planned = [call.tool for call in _plan_tools(request, registry=warm_registry())]
    assert planned == ["market_screen"]


def test_planned_step_without_arguments_gets_them_backfilled():
    """规划层只给工具名时，参数要按**工具自己的 schema** 从回合上下文补上。

    实测：模型会把 steps 写成工具名数组。真的照空参数去调，注册表在 handler 之前
    就按 schema 拒了——用户看到的是"查过了但没数据"，而标的一直就在手上
    （回合里已经解析出来了）。
    """
    from tradingagents.agent.loop import _plan_tools

    request = _plan_request(
        text="600519 最近筹码集中度怎么样",
        plan_source="llm",
        tier="light",
        symbol="600519.SH",
        as_of="2026-09-21",
        planned_steps=({"tool": "chip_distribution_read", "arguments": {}},),
    )
    planned = _plan_tools(request, registry=warm_registry())

    assert [call.tool for call in planned] == ["chip_distribution_read"]
    assert planned[0].arguments.get("symbol") == "600519.SH", "标的没回填"
    assert planned[0].arguments.get("as_of") == "2026-09-21", "基准日没回填"


def test_planned_step_that_cannot_be_completed_is_skipped_with_a_warning(caplog):
    """补不满必填项的步骤要**跳过并留痕**，而不是发一次注定失败的调用。"""
    from tradingagents.agent.loop import _plan_tools

    request = _plan_request(
        text="这只票的筹码怎么样",
        plan_source="llm",
        tier="light",
        symbol=None,  # 没有标的 → symbol 补不出来
        as_of="2026-09-21",
        planned_steps=({"tool": "chip_distribution_read", "arguments": {}},),
    )
    with caplog.at_level("WARNING"):
        planned = _plan_tools(request, registry=warm_registry())

    assert planned == [], "补不满必填项就不该发车"
    assert "cannot be derived" in caplog.text, "跳过必须留痕，不能静默"


def test_planner_supplied_arguments_win_over_backfill():
    """规划层明确给了参数就以它为准，回填只补空位，不覆盖。"""
    from tradingagents.agent.loop import _plan_tools

    request = _plan_request(
        text="600519 最近筹码集中度怎么样",
        plan_source="llm",
        tier="light",
        symbol="000001.SZ",
        as_of="2026-09-21",
        planned_steps=(
            {"tool": "chip_distribution_read", "arguments": {"symbol": "600519.SH"}},
        ),
    )
    planned = _plan_tools(request, registry=warm_registry())

    assert planned[0].arguments["symbol"] == "600519.SH", "规划层的显式选择被覆盖了"


def test_probe_clarify_flag_mirrors_the_real_prequel_condition():
    """跑分的"是否追问"必须与执行层的追问条件**同口径**。

    `_prequel` 的追问条件比"有候选"宽：没有标的、没有多标的、不是筛选、不是市场级
    事实、也没有规划步骤时，它会追问。旧口径只认"有候选"，于是「它是不是在洗盘」
    这种**实际会追问**的问句在跑分里被记成"没追问"——尺子和产品说的不是一件事，
    澄清正确率就永远是错的。
    """
    from tests.golden import harness
    from tradingagents.agent.routing import Plan

    def _fake_plan(_text: str) -> Plan:
        # 规划器判断这是研判类问题，但问句里没有可解析的标的 → symbol 留空、不猜。
        return Plan(intent="single_symbol_judgement", tier="medium", confidence=0.8,
                    need_clarify=True, clarify_question="是哪一只？", source="llm")

    probe_ = harness.probe("它是不是在洗盘", planner=_fake_plan)

    assert probe_.servable is True, "追问是可服务回合，不是 400"
    assert probe_.symbols == (), "不该猜一个标的出来"
    assert probe_.clarifying is True, "无标的、无指代、无计划 → 执行层一定会追问"


def test_planner_can_say_no_clarification_is_needed():
    """规划层说了"不用追问"，执行层就不许再要代码。

    反过来的那半同样致命：实测「什么是洗盘」「帮我记一下这笔交易」这类**根本不涉及
    标的**的问句会走进澄清分支，回一句「请告诉我具体的股票代码（如 600519.SH）」。
    用户问概念，平台问代码——追问过度和答不出一样让人烦。
    """
    from tests.golden import harness
    from tradingagents.agent.routing import Plan

    def _fake_plan(_text: str) -> Plan:
        return Plan(intent="concept_qa", tier="light", confidence=0.9,
                    need_clarify=False, source="llm")

    probe_ = harness.probe("什么是洗盘", planner=_fake_plan)

    assert probe_.servable is True
    assert probe_.clarifying is False, "概念问答不该被要代码"


def test_rules_turns_keep_clarifying_when_no_symbol_is_found():
    """没有规划层表态时，保持"没标的就追问"的既有行为（默认不变）。"""
    from tests.golden import harness

    probe_ = harness.probe("它是不是在洗盘")
    assert probe_.clarifying is True

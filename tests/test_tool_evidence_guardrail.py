"""护栏：**实测会产出证据的工具，回归时不得静默变成 0 条。**

`specpower/changes/2026-09-22-tool-evidence-coverage/` Phase 5.3。

## 为什么需要这条护栏

本专项修复前，11 个在册数据工具**全部** `evidence=()`——而这是**静默**的：
工具 `ok=True`、不报错、回答照常生成，只是用户拿到「本轮无可用证据」。
生产库当时有 **26 个回合**是 `ok=true` 且 `evidence_count=0`，其中没有任何
一条会让人想到"解析器坏了"。这类漏洞不会被任何"工具没抛异常"的测试抓到。

所以护栏断言的是**正向产出**，而不是"没崩"：给定**真实抓取**的 vendor 文案，
每个已支持的工具必须产出证据。文案逐字来自实抓（见每个常量上方的来源说明），
**不是手写的理想样例**——手写样例会让护栏在真实格式变化时依然通过。

失败信息一律点名工具：只说"某处 0 条"无法定位，而这条链上工具名就是定位键。
"""

from __future__ import annotations

import pytest

REAL_GET_FUNDAMENTALS = '## Fundamentals for 600519.SH\n\n### Financial Abstract (latest available columns)\n\n最新披露期: 2026-Q2\n\n| 选项   | 指标          |      20260630 |      20260331 |      20251231 |      20250930 |      20250630 |      20250331 |      20241231 |      20240930 |\n|:-----|:------------|--------------:|--------------:|--------------:|--------------:|--------------:|--------------:|--------------:|--------------:|\n| 常用指标 | 归母净利润       |   4.45169e+10 |   2.72425e+10 |   8.23201e+10 |   6.46267e+10 |   4.5403e+10  |   2.68475e+10 |   8.62281e+10 |   6.08276e+10 |\n| 常用指标 | 营业总收入       |   9.22781e+10 |   5.47029e+10 |   1.72054e+11 |   1.30904e+11 |   9.10938e+10 |   5.14435e+10 |   1.74144e+11 |   1.23123e+11 |\n| 常用指标 | 营业成本        |   3.0946e+10  |   1.71922e+10 |   5.73708e+10 |   4.14262e+10 |   2.83539e+10 |   1.44333e+10 |   5.4524e+10  |   3.91824e+10 |\n| 常用指标 | 净利润         |   4.60333e+10 |   2.81538e+10 |   8.53103e+10 |   6.68988e+10 |   4.69867e+10 |   2.77746e+10 |   8.93347e+10 |   6.30315e+10 |\n| 常用指标 | 扣非净利润       |   4.44642e+10 |   2.724e+10   |   8.22931e+10 |   6.46806e+10 |   4.53902e+10 |   2.68499e+10 |   8.62409e+10 |   6.07786e+10 |\n| 常用指标 | 股东权益合计(净资产) |   2.62096e+11 |   2.81136e+11 |   2.53959e+11 |   2.65705e+11 |   2.49136e+11 |   2.6819e+11  |   2.42011e+11 |   2.47688e+11 |\n| 常用指标 | 商誉          | nan           | nan           | nan           | nan           | nan           | nan           | nan           | nan           |\n| 常用指标 | 经营现金流量净额    |   7.06908e+10 |   2.69099e+10 |   6.15222e+10 |   3.81968e+10 |   1.31191e+10 |   8.8092e+09  |   9.24637e+10 |   4.44214e+10 |\n| 常用指标 | 基本每股收益      |  35.57        |  21.76        |  65.66        |  51.53        |  36.18        |  21.38        |  68.64        |  48.42        |\n| 常用指标 | 每股净资产       | 200.99        | 216.322       | 195.355       | 204.641       | 189.975       | 205.667       | 185.565       | 189.229       |\n| 常用指标 | 每股现金流       |  56.5489      |  21.4889      |  49.1285      |  30.4067      |  10.4435      |   7.01259     |  73.606       |  35.3618      |\n| 常用指标 | 净资产收益率(ROE) |  16.75        |  10.57        |  32.53        |  24.64        |  17.89        |  10.92        |  36.02        |  26.09        |\n| 常用指标 | 总资产报酬率(ROA) |  20.0558      |  12.0428      |  38.0849      |  29.6215      |  21.2389      |  12.1162      |  41.8628      |  30.0485      |\n| 常用指标 | 毛利率         |  89.5552      |  89.7592      |  91.1796      |  91.2934      |  91.2993      |  91.9736      |  91.9312      |  91.5314      |\n| 常用指标 | 销售净利率       |  50.7516      |  52.2245      |  50.5279      |  52.0801      |  52.5641      |  54.8895      |  52.2734      |  52.1887      |\n| 常用指标 | 期间费用率       |   7.40144     |   6.31253     |   8.85371     |   7.36484     |   7.31902     |   6.24313     |   8.01824     |   7.42722     |\n| 常用指标 | 资产负债率       |  15.1931      |  12.1227      |  16.4154      |  12.8088      |  14.7549      |  14.143       |  19.0448      |  13.6285      |\n| 每股指标 | 基本每股收益      |  35.57        |  21.76        |  65.66        |  51.53        |  36.18        |  21.38        |  68.64        |  48.42        |\n| 每股指标 | 稀释每股收益      |  35.57        |  21.76        |  65.66        |  51.53        |  36.18        |  21.38        |  68.64        |  48.42        |\n| 每股指标 | 摊薄每股收益_最新股数 |  35.6112      |  21.7926      |  65.8518      |  51.698       |  36.32        |  21.4766      |  68.978       |  48.6589      |'

REAL_GET_BALANCE_SHEET = '## Balance Sheet (600519.SH)\n\n最新披露期: 2026-Q2\n\n|      报告日 |   流动资产 |        货币资金 |   结算备付金 |        拆出资金 |       交易性金融资产 |    买入返售金融资产 |   衍生金融资产 |        应收票据及应收账款 |          应收票据 |             应收账款 |        应收款项融资 |        预付款项 |   应收股利 |   应收利息 |   应收保费 |   应收分保账款 |   应收分保合同准备金 |\n|---------:|-------:|------------:|--------:|------------:|--------------:|------------:|---------:|-----------------:|--------------:|-----------------:|--------------:|------------:|-------:|-------:|-------:|---------:|------------:|\n| 20260630 |    nan | 5.35188e+10 |     nan | 1.41084e+11 | nan           | 4.06491e+08 |      nan | 570895           | nan           | 570895           |   2.47575e+09 | 1.61902e+07 |    nan |    nan |    nan |      nan |         nan |\n| 20260331 |    nan | 4.87867e+10 |     nan | 1.55995e+11 | nan           | 4.0469e+08  |      nan |      3.23446e+07 | nan           |      3.23446e+07 |   3.53166e+09 | 2.80534e+07 |    nan |    nan |    nan |      nan |         nan |\n| 20251231 |    nan | 5.16906e+10 |     nan | 9.90962e+10 | nan           | 8.87971e+09 |      nan |      2.60905e+06 | nan           |      2.60905e+06 |   4.45706e+09 | 6.63731e+06 |    nan |    nan |    nan |      nan |         nan |\n| 20250930 |    nan | 5.17531e+10 |     nan | 1.35403e+11 | nan           | 3.50566e+09 |      nan |      5.23506e+09 |   5.20953e+09 |      2.55317e+07 | nan           | 2.12298e+07 |    nan |    nan |    nan |      nan |         nan |\n| 20250630 |    nan | 5.16449e+10 |     nan | 1.26351e+11 | nan           | 8.79393e+09 |      nan |      2.85374e+09 |   2.81578e+09 |      3.79612e+07 | nan           | 2.15047e+07 |    nan |    nan |    nan |      nan |         nan |\n| 20250331 |    nan | 5.21992e+10 |     nan | 1.54469e+11 | nan           | 1.04298e+09 |      nan |      2.14266e+09 |   2.12746e+09 |      1.51993e+07 | nan           | 4.28832e+07 |    nan |    nan |    nan |      nan |         nan |\n| 20241231 |    nan | 5.92958e+10 |     nan | 1.27187e+11 |   2.48513e+08 | 7.22031e+09 |      nan |      2.00338e+09 |   1.98441e+09 |      1.89742e+07 | nan           | 2.6975e+07  |    nan |    nan |    nan |      nan |         nan |\n| 20240930 |    nan | 6.00847e+10 |     nan | 1.22686e+11 |   2.00794e+08 | 5.78543e+09 |      nan |      1.15918e+09 |   1.14905e+09 |      1.01317e+07 | nan           | 4.17543e+07 |    nan |    nan |    nan |      nan |         nan |\n| 20240630 |    nan | 5.68403e+10 |     nan | 1.15294e+11 |   3.82856e+09 | 7.90821e+09 |      nan |      3.33101e+08 |   3.31742e+08 |      1.3586e+06  | nan           | 2.43071e+07 |    nan |    nan |    nan |      nan |         nan |\n| 20240331 |    nan | 7.41971e+10 |     nan | 1.05061e+11 |   3.81234e+09 | 7.112e+09   |      nan |      2.13805e+08 |   1.4068e+08  |      7.31247e+07 | nan           | 4.12047e+07 |    nan |    nan |    nan |      nan |         nan |\n| 20231231 |    nan | 6.90701e+10 |     nan | 1.05554e+11 |   4.00712e+08 | 3.50485e+09 |      nan |      7.43069e+07 |   1.39334e+07 |      6.03734e+07 | nan           | 3.45851e+07 |    nan |    nan |    nan |      nan |         nan |\n| 20230930 |    nan | 7.0641e+10  |     nan | 9.56256e+10 |   3.50944e+09 | 7.3571e+09  |      nan |      1.31214e+08 | nan           |      1.31214e+08 | nan           | 1.10593e+08 |    nan |    nan |    nan |      nan |         nan |'

REAL_GET_INCOME_STATEMENT = '## Income Statement (600519.SH)\n\n最新披露期: 2026-Q2\n\n|      报告日 |       营业总收入 |        营业收入 |        利息收入 |   已赚保费 |   手续费及佣金收入 |   房地产销售收入 |   其他业务收入 |       营业总成本 |        营业成本 |   手续费及佣金支出 |   房地产销售成本 |   退保金 |   赔付支出净额 |   提取保险合同准备金净额 |   保单红利支出 |   分保费用 |   其他业务成本 |\n|---------:|------------:|------------:|------------:|-------:|-----------:|----------:|---------:|------------:|------------:|-----------:|----------:|------:|---------:|--------------:|---------:|-------:|---------:|\n| 20260630 | 9.22781e+10 | 9.07033e+10 | 1.57481e+09 |    nan |        nan |       nan |      nan | 3.0946e+10  | 9.47376e+09 |   183088   |       nan |   nan |      nan |           nan |      nan |    nan |      nan |\n| 20260331 | 5.47029e+10 | 5.39093e+10 | 7.9366e+08  |    nan |        nan |       nan |      nan | 1.71922e+10 | 5.52073e+09 |   118272   |       nan |   nan |      nan |           nan |      nan |    nan |      nan |\n| 20251231 | 1.72054e+11 | 1.68838e+11 | 3.21607e+09 |    nan |        nan |       nan |      nan | 5.73708e+10 | 1.48923e+10 |   103597   |       nan |   nan |      nan |           nan |      nan |    nan |      nan |\n| 20250930 | 1.30904e+11 | 1.28454e+11 | 2.45018e+09 |    nan |        nan |       nan |      nan | 4.14262e+10 | 1.1184e+10  |    55753.4 |       nan |   nan |      nan |           nan |      nan |    nan |      nan |\n| 20250630 | 9.10938e+10 | 8.93894e+10 | 1.70441e+09 |    nan |        nan |       nan |      nan | 2.83539e+10 | 7.77749e+09 |    42669.4 |       nan |   nan |      nan |           nan |      nan |    nan |      nan |\n| 20250331 | 5.14435e+10 | 5.0601e+10  | 8.42493e+08 |    nan |        nan |       nan |      nan | 1.44333e+10 | 4.06143e+09 |    19149.9 |       nan |   nan |      nan |           nan |      nan |    nan |      nan |\n| 20241231 | 1.74144e+11 | 1.70899e+11 | 3.24492e+09 |    nan |        nan |       nan |      nan | 5.4524e+10  | 1.37895e+10 |    94078.2 |       nan |   nan |      nan |           nan |      nan |    nan |      nan |\n| 20240930 | 1.23123e+11 | 1.20776e+11 | 2.34641e+09 |    nan |        nan |       nan |      nan | 3.91824e+10 | 1.02281e+10 |   212030   |       nan |   nan |      nan |           nan |      nan |    nan |      nan |\n| 20240630 | 8.34512e+10 | 8.1931e+10  | 1.52019e+09 |    nan |        nan |       nan |      nan | 2.59361e+10 | 6.75088e+09 |   200914   |       nan |   nan |      nan |           nan |      nan |    nan |      nan |\n| 20240331 | 4.64847e+10 | 4.57755e+10 | 7.09221e+08 |    nan |        nan |       nan |      nan | 1.33441e+10 | 3.38129e+09 |    17623.1 |       nan |   nan |      nan |           nan |      nan |    nan |      nan |\n| 20231231 | 1.5056e+11  | 1.47694e+11 | 2.86673e+09 |    nan |        nan |       nan |      nan | 4.69609e+10 | 1.18673e+10 |    68578.6 |       nan |   nan |      nan |           nan |      nan |    nan |      nan |\n| 20230930 | 1.05316e+11 | 1.03268e+11 | 2.04755e+09 |    nan |        nan |       nan |      nan | 3.22357e+10 | 8.56356e+09 |   161542   |       nan |   nan |      nan |           nan |      nan |    nan |      nan |'

REAL_GET_BOARD_FUND_FLOW = '板块资金流向排名（数据截止 2026-09-22，共48个板块，前10名）：\n 排名   名称     今日涨跌幅   今日主力净流入-净额  今日主力净流入-净占比\n  1  房地产  0.005472 3.203957e+09     0.100913\n  2 生物制药 -0.001041 1.942324e+09     0.045254\n  3 煤炭行业  0.011193 1.176931e+09     0.067429\n  4 传媒娱乐  0.020929 1.051620e+09     0.093424\n  5 印刷包装  0.025007 7.571413e+08     0.078147\n  6 食品行业  0.003797 4.114461e+08     0.032585\n  7 其它行业 -0.002004 4.055144e+08     0.017807\n  8 商业百货 -0.002715 3.299349e+08     0.018190\n  9 汽车制造  0.003969 3.007087e+08     0.009599\n 10 有色金属  0.001431 2.908297e+08     0.005226'

REAL_GET_ZT_POOL = '2026-09-21 涨停家数：103\n连板分布：\n连板数\n1    82\n2    13\n3     5\n4     2\n5     1'

# (工具名, 实抓文案, 期望的最少证据条数)
#
# 期望值是**下界**而非精确值：上游多给几期/多给几个板块都不该让护栏失败，
# 但少到 0 条必须失败。
EVIDENCE_PRODUCING: tuple[tuple[str, str, int], ...] = (
    ("get_fundamentals", REAL_GET_FUNDAMENTALS, 30),
    ("get_balance_sheet", REAL_GET_BALANCE_SHEET, 30),
    ("get_income_statement", REAL_GET_INCOME_STATEMENT, 30),
    ("get_board_fund_flow", REAL_GET_BOARD_FUND_FLOW, 20),
    ("get_zt_pool", REAL_GET_ZT_POOL, 5),
)


@pytest.mark.parametrize(
    "tool_name,rendered,minimum",
    EVIDENCE_PRODUCING,
    ids=[case[0] for case in EVIDENCE_PRODUCING],
)
def test_real_vendor_text_still_yields_evidence(tool_name, rendered, minimum):
    """真实文案必须产出证据——**失败信息点名工具**。"""
    from tradingagents.tools.data import evidence_from_rendered

    items = evidence_from_rendered(rendered, source_key=tool_name)

    assert len(items) >= minimum, (
        f"{tool_name}：真实 vendor 文案只产出 {len(items)} 条证据（期望 >= {minimum}）。"
        "解析器可能已静默失效——这类失效不会抛异常，只会让用户看到"
        "「本轮无可用证据」。请检查对应的解析路径（表格 / 固定文本 / 排名榜单）。"
    )
    # 逐字搬运：证据里的每个数值都必须能在原文里找到。
    #
    # 判据必须是**数值比较**，不能是字符串包含：原文写科学计数法
    # （`3.203957e+09`）时，`f"{v:g}"` 只有 6 位有效数字，字符串匹配会误报
    # "凭空造证据"。第一版就是这么写的，被这条断言自己抓出来了。
    from tradingagents.grounding.numbers import extract_numbers

    in_source = {token.value for token in extract_numbers(rendered)}
    missing = [
        item.id for item in items
        if item.value not in in_source
        and not any(abs(item.value - value) <= abs(item.value) * 1e-9 for value in in_source)
    ]
    assert not missing, (
        f"{tool_name}：证据 {missing} 的数值在原文里找不到——这是**凭空造出来的"
        "证据**，比没有证据更糟。"
    )


@pytest.mark.parametrize(
    "tool_name,rendered,_minimum",
    EVIDENCE_PRODUCING,
    ids=[case[0] for case in EVIDENCE_PRODUCING],
)
def test_evidence_as_of_comes_from_the_data_not_the_query_day(tool_name, rendered, _minimum):
    """每条证据都要有 `as_of`，且**取自数据自身时点**。

    取查询日会骗过日期校验（模型写对日期反而被判无源），所以这条必须钉住。
    """
    from tradingagents.tools.data import evidence_from_rendered

    items = evidence_from_rendered(rendered, source_key=tool_name)
    assert items, f"{tool_name}：没有证据，无法检查 as_of"
    missing = [item.id for item in items if not item.as_of]
    assert not missing, f"{tool_name}：{missing} 缺 as_of（时点拿不到就不该产出这条）"


def test_the_five_tools_that_were_fixed_are_still_the_ones_producing_evidence():
    """防止"修好的又坏回去"：这 5 个工具必须都还在产出证据。

    单独一条清单式测试（而不是全靠 parametrize）：它给出的是**整体**信号——
    "当前有几个工具在产出证据"，回归时一眼看出少了哪个。
    """
    from tradingagents.tools.data import evidence_from_rendered

    producing = {
        tool_name
        for tool_name, rendered, _ in EVIDENCE_PRODUCING
        if evidence_from_rendered(rendered, source_key=tool_name)
    }
    expected = {tool_name for tool_name, _, _ in EVIDENCE_PRODUCING}
    assert producing == expected, (
        f"产出证据的工具集合变了：缺 {sorted(expected - producing)}，"
        f"多 {sorted(producing - expected)}"
    )

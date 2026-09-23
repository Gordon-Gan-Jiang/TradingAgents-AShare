# 黄金集报告 · 规则路径（离线）

> 由 `scripts/golden_report.py` 生成，**不要手工编辑**。
> 这份报告是后续所有改动的对照基线：没有它就说不清"更好了"。

| 口径 | 值 |
|---|---|
| 规划层 | 未启用（规则路径） |
| 规划器模型 | （未启用） |
| 黄金集 | 50 条 |

## 总体

| 指标 | 值 | 含义 |
|---|---|---|
| 覆盖率 | **58.0%** | 能构造出可服务回合（而非 400 追问）的比例 |
| 规划准确率 | **46.0%** | 意图与标的集合都判对的比例 |
| 工具召回率 | **64.1%** | 必需工具被计划到的比例 |
| 工具精确率 | **89.3%** | 计划到的工具中不属噪声的比例 |
| 澄清正确率 | **66.0%** | 该追问的追问了、不该追问的没问 |
| 样本数 | 50 | — |
| 名称表规模 | 32222 | 冷表会让名称类用例全废，分数不可比 |

## 按意图拆解

| 意图 | 样本 | 可服务 | 意图判对 | 工具满足 |
|---|---|---|---|---|
| `action_request` | 3 | 2 | 0 | 3 |
| `concept_qa` | 3 | 0 | 0 | 3 |
| `factual_lookup` | 8 | 8 | 7 | 8 |
| `history_lookup` | 4 | 1 | 0 | 1 |
| `market_screen` | 6 | 6 | 6 | 6 |
| `market_view` | 5 | 0 | 0 | 0 |
| `multi_symbol_compare` | 5 | 4 | 3 | 5 |
| `portfolio_review` | 6 | 0 | 0 | 1 |
| `single_symbol_judgement` | 10 | 8 | 7 | 9 |

## 失败明细（前 25 条）

| id | 问句 | 原因 | 详情 |
|---|---|---|---|
| s05 | 宁德时代是不是主力在出货 | planning mismatch | `{"expected_intent": "single_symbol_judgement", "observed_intent": "factual_lookup", "expected_symbols": ["300750.SZ"], "observed_symbols": ["300750.SZ"]}` |
| s05 | 宁德时代是不是主力在出货 | missing required tools | `{"missing": ["signal_washout_probe"], "observed": ["capital_flow_read"]}` |
| s07 | 它是不是在洗盘 | planning mismatch | `{"expected_intent": "single_symbol_judgement", "observed_intent": null, "expected_symbols": [], "observed_symbols": []}` |
| s08 | 这只票要不要止损 | planning mismatch | `{"expected_intent": "single_symbol_judgement", "observed_intent": null, "expected_symbols": [], "observed_symbols": []}` |
| f08 | 今天涨停板有多少只 | planning mismatch | `{"expected_intent": "factual_lookup", "observed_intent": null, "expected_symbols": [], "observed_symbols": []}` |
| p01 | 我持仓里哪只最危险 | planning mismatch | `{"expected_intent": "portfolio_review", "observed_intent": null, "expected_symbols": [], "observed_symbols": []}` |
| p01 | 我持仓里哪只最危险 | missing required tools | `{"missing": ["portfolio_risk_review"], "observed": []}` |
| p01 | 我持仓里哪只最危险 | clarify expectation mismatch | `{"expected": false, "observed": true}` |
| p02 | 帮我看看自选股里有没有要止损的 | planning mismatch | `{"expected_intent": "portfolio_review", "observed_intent": null, "expected_symbols": [], "observed_symbols": []}` |
| p02 | 帮我看看自选股里有没有要止损的 | missing required tools | `{"missing": ["signal_washout_probe_batch", "watchlist_read"], "observed": []}` |
| p02 | 帮我看看自选股里有没有要止损的 | clarify expectation mismatch | `{"expected": false, "observed": true}` |
| p03 | 我那 30 只持仓整体风险如何 | planning mismatch | `{"expected_intent": "portfolio_review", "observed_intent": null, "expected_symbols": [], "observed_symbols": []}` |
| p03 | 我那 30 只持仓整体风险如何 | missing required tools | `{"missing": ["portfolio_risk_review"], "observed": []}` |
| p03 | 我那 30 只持仓整体风险如何 | clarify expectation mismatch | `{"expected": false, "observed": true}` |
| p04 | 跟踪池今天表现怎么样 | planning mismatch | `{"expected_intent": "portfolio_review", "observed_intent": null, "expected_symbols": [], "observed_symbols": []}` |
| p04 | 跟踪池今天表现怎么样 | missing required tools | `{"missing": ["tracking_board_read"], "observed": []}` |
| p04 | 跟踪池今天表现怎么样 | clarify expectation mismatch | `{"expected": false, "observed": true}` |
| p05 | 我账户现在仓位多少 | planning mismatch | `{"expected_intent": "portfolio_review", "observed_intent": null, "expected_symbols": [], "observed_symbols": []}` |
| p05 | 我账户现在仓位多少 | missing required tools | `{"missing": ["portfolio_holdings_read"], "observed": []}` |
| p05 | 我账户现在仓位多少 | clarify expectation mismatch | `{"expected": false, "observed": true}` |
| p06 | 帮我看看还能不能拿 | planning mismatch | `{"expected_intent": "portfolio_review", "observed_intent": null, "expected_symbols": [], "observed_symbols": []}` |
| c03 | 茅台和宁德时代哪个好 | planning mismatch | `{"expected_intent": "multi_symbol_compare", "observed_intent": "factual_lookup", "expected_symbols": [], "observed_symbols": []}` |
| c04 | 这两个哪个更值得拿 | planning mismatch | `{"expected_intent": "multi_symbol_compare", "observed_intent": null, "expected_symbols": [], "observed_symbols": []}` |
| v01 | 最近哪个板块最强 | planning mismatch | `{"expected_intent": "market_view", "observed_intent": null, "expected_symbols": [], "observed_symbols": []}` |
| v01 | 最近哪个板块最强 | missing required tools | `{"missing": ["market_mainline_view"], "observed": []}` |

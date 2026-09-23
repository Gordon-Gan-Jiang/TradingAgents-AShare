# 黄金集报告 · 规划层启用（生产口径）

> 由 `scripts/golden_report.py` 生成，**不要手工编辑**。
> 这份报告是后续所有改动的对照基线：没有它就说不清"更好了"。

| 口径 | 值 |
|---|---|
| 规划层 | 启用 |
| 规划器模型 | c35aa366c7a24bfc81fed40e0e2ba180 |
| 黄金集 | 50 条 |

## 总体

| 指标 | 值 | 含义 |
|---|---|---|
| 覆盖率 | **100.0%** | 能构造出可服务回合（而非 400 追问）的比例 |
| 规划准确率 | **88.0%** | 意图与标的集合都判对的比例 |
| 工具召回率 | **96.2%** | 必需工具被计划到的比例 |
| 工具精确率 | **92.9%** | 计划到的工具中不属噪声的比例 |
| 澄清正确率 | **98.0%** | 该追问的追问了、不该追问的没问 |
| 样本数 | 50 | — |
| 名称表规模 | 32222 | 冷表会让名称类用例全废，分数不可比 |

## 按意图拆解

| 意图 | 样本 | 可服务 | 意图判对 | 工具满足 |
|---|---|---|---|---|
| `action_request` | 3 | 3 | 1 | 3 |
| `concept_qa` | 3 | 3 | 3 | 3 |
| `factual_lookup` | 8 | 8 | 8 | 8 |
| `history_lookup` | 4 | 4 | 2 | 4 |
| `market_screen` | 6 | 6 | 6 | 6 |
| `market_view` | 5 | 5 | 5 | 5 |
| `multi_symbol_compare` | 5 | 5 | 4 | 5 |
| `portfolio_review` | 6 | 6 | 6 | 5 |
| `single_symbol_judgement` | 10 | 10 | 9 | 9 |

## 失败明细（前 25 条）

| id | 问句 | 原因 | 详情 |
|---|---|---|---|
| s05 | 宁德时代是不是主力在出货 | planning mismatch | `{"expected_intent": "single_symbol_judgement", "observed_intent": "factual_lookup", "expected_symbols": ["300750.SZ"], "observed_symbols": ["300750.SZ"]}` |
| s05 | 宁德时代是不是主力在出货 | missing required tools | `{"missing": ["signal_washout_probe"], "observed": ["capital_flow_read"]}` |
| p02 | 帮我看看自选股里有没有要止损的 | missing required tools | `{"missing": ["signal_washout_probe_batch"], "observed": ["watchlist_read"]}` |
| c03 | 茅台和宁德时代哪个好 | planning mismatch | `{"expected_intent": "multi_symbol_compare", "observed_intent": "factual_lookup", "expected_symbols": [], "observed_symbols": []}` |
| h03 | 帮我找一份贵州茅台的研报结论 | planning mismatch | `{"expected_intent": "history_lookup", "observed_intent": "factual_lookup", "expected_symbols": ["600519.SH"], "observed_symbols": ["600519.SH"]}` |
| h04 | 当前洗盘判定的权重是哪一版 | planning mismatch | `{"expected_intent": "history_lookup", "observed_intent": "factual_lookup", "expected_symbols": [], "observed_symbols": []}` |
| a01 | 把 600519 加入自选 | planning mismatch | `{"expected_intent": "action_request", "observed_intent": "factual_lookup", "expected_symbols": ["600519.SH"], "observed_symbols": ["600519.SH"]}` |
| a02 | 帮我记一下这笔交易 | clarify expectation mismatch | `{"expected": false, "observed": true}` |
| a03 | 开始全面分析一下 600519 | planning mismatch | `{"expected_intent": "action_request", "observed_intent": "factual_lookup", "expected_symbols": ["600519.SH"], "observed_symbols": ["600519.SH"]}` |

# tasks — 工具证据覆盖率

> 依据 `proposal.md` 的实测证据。阶段按"独立可交付且风险递增"排序：
> Phase 1 是纯收益的诚实性修复（不改证据抽取），后面才是新增解析能力。

## Phase 1 — 补齐失败识别（先做，独立、低风险）

> **状态：已完成（2026-09-22）。** 实施中发现并修掉了两个计划外的真实缺陷，
> 记在下面 1.2 / 1.3 的"实测新增"里。


现状：`data.py:484` 的 `_VENDOR_FAILURE_MARKERS` 漏掉英文错误与限流措辞，于是
`get_global_news` 的 `Error fetching global news: Too Many Requests.` 被当成正常数据，
最终用户看到「本轮无可用证据」而不是「数据源被限流」。

| # | 任务 | 验收 |
|---|---|---|
| 1.1 | `_VENDOR_FAILURE_MARKERS` 增补：`error fetching`、`rate limited`、`too many requests`、`timeout`、`connection` 类措辞；每条都附一行注释说明是实测漏网还是预判 | 用实探到的 `get_global_news` 原文做 fixture，`_looks_like_vendor_failure` 必须为 True |
| 1.2 | 复核增补后**没有把正常数据误判为失败**：对 11 个在册数据工具各跑一次真实调用，确认 `degraded=False` 仍成立 | 11/11 正常数据不被误判；把这次实测结果写进测试注释 |
| 1.3 | 失败时用户可见：确认 `degraded=True` 会让回答显式声明"数据源故障"，而不是"无可用证据" | 端到端：构造一个失败源，断言回答里出现故障声明 |

### 实测新增（计划外，但属同一 Phase 的诚实性口径）

**A. 空 JSON 载荷被当成有效数据。** `_looks_like_vendor_failure` 早就挡住了空字符串
（注释写着"空返回同样是拿不到"），但 `'{}'` 是长度 2 的非空字符串，穿了过去。
实测 `get_realtime_quotes(symbols='600519.SH')` 就返回 `'{}'`，于是

```
degraded=False                      （不是失败）
available=('core_stock_apis',)      （声称该源**可用**）
unavailable=()                      （声称**没有**未覆盖项）
证据 0 条
```

用户看到"数据源可用、没有未覆盖项、但没有数据"。判据已收窄为"**能解析成 JSON 且是空
dict/list**"——不碰散文，不存在把正常文案误判的路径；`{"quotes":{}}` 这类**非空**载荷
仍判为数据。

**B. 真正的诊断被算完就丢。** 失败时 `coverage.unavailable` 只声明 `(decl.category,)`，
于是用户看到的是「未覆盖：news_data」——一个内部标识符。而人话诊断写在
`ToolResult.degraded_reason` 里，**全仓库无任何代码读它**。现已按本仓库既有范式
（`trade_ledger（没有已录入的交易流水）`）改为"类别名（数据源故障：<诊断>）"。

**1.2 的实测结论**（11 个在册数据工具各真实调用一次）：6 个正常数据**无一被误判**，
5 个判为失败的**全是真故障**（`get_global_news` 限流、`get_board_spot/rank/hist`
连接被拒、`get_hot_stocks_xq` JSONDecodeError）。**误判 0 条。**
（首轮扫描曾把 3 个判失败误以为是误判，实为我参数传错——`sector_type` 的合法值是
`industry`/`1d`，默认即英文。）

**1.3 的端到端实测**（问题「最近全球市场有什么大事」）：

```
本轮无可用证据，且全球新闻数据源故障，无法对最近全球市场大事给出有依据的判断。
**数据未覆盖**：news_data（数据源故障：Error fetching global news: Too Many Requests. Rate limited.）
```

修复前用户只看到「本轮无可用证据」——故障被伪装成了"没有数据"。

**风险**：误判正常数据为失败会让本来能答的问句变成降级。1.2 就是为此设的闸门，必须真跑。

## Phase 2 — 表格 → 证据解析器（本提案的主体）

**前置**：本 Phase 推翻 `data.py:314` 的一条既有决定（"表格不做解析"）。动手前必须先按
`proposal.md` 的「⚠️ 与既有决定冲突」一节确认护栏设计被接受；否则不要开工。

`get_fundamentals` / `get_balance_sheet` / `get_income_statement` 返回 Markdown 表格，
`get_zt_pool` 返回半结构化文本。它们目前一律 `evidence=()`。

护栏（逐条都要有对应测试，任一条不满足就**整张表放弃**）：

| 编号 | 规则 |
|---|---|
| G1 | 必须含分隔行的 Markdown 表格，且所有行**列数一致**；否则不产出任何证据 |
| G2 | `nan` / 空单元格不产出证据（`nan` 不是事实） |
| G3 | 字段名与数值只做**原样搬运**，不换算、不推断 |
| G4 | 财务数据 `as_of` 取**报告期**（表头 `20260630`）；取不到合法日期则不产出该行 |
| G5 | 受 `_MAX_EVIDENCE_ITEMS` 约束，单表只取语义最关键的部分 |

| # | 任务 | 验收 |
|---|---|---|
| 2.1 | 新增 `_evidence_items_from_table(rendered, source_key, as_of)`，实现 G1–G5 | 用真实 vendor 文案做 fixture；G1/G2 各有一条"必须放弃"的否证测试 |
| 2.2 | 在 vendor handler 里接入：JSON → 表格 → 文本，按序尝试；全部不匹配才保持空证据 | 三个财务报表工具的 `evidence` 非空 |
| 2.3 | **数值规范化**：科学计数法（`4.45169e+10`）要落到可比较的形态，否则模型写「445.17 亿」仍会被判 `unsourced_number` | 端到端：模型引用财务数值时通过校验 |
| 2.4 | **条数上限**：财务报表可能几十列 × 多期，全量转证据会撑爆提示词并拖慢作答（延迟正是要压的目标） | 单次调用证据条数 ≤ 上限；提示词 token 增量经实测记录 |
| 2.5 | `as_of` 正确性（G4）：证据必须能通过 `date_assertion_ok` | 写一条测试钉住"报告期 ≠ 查询日" |

**风险**：表格解析错了会产出"看似有出处实则错位"的证据——比没有证据更糟，而且校验器
**不会**拦住它（数字确实在所引证据里）。因此 G1/G2 的否证测试是本 Phase 的核心交付物，
不是可选项。

## Phase 3 — 半结构化文本解析器

| # | 任务 | 验收 |
|---|---|---|
| 3.1 | `get_zt_pool` 形态（`2026-09-21 涨停家数：103` + 连板分布列表）写定向解析 | 真实文案 → 至少产出 `涨停家数` 一条数值证据，`as_of` 取文本里的日期 |
| 3.2 | 逐工具评估其余文本型工具（`get_board_spot` / `get_board_rank` / `get_board_fund_flow` / `get_hot_stocks_xq`）能否定向解析；**不能的明确记录为"保持空证据"并说明理由** | 每个工具都有结论，不留"待定" |

## Phase 4 — 空表工具的口径

`trade_ledger_read` 读 `trade_ledger`（0 行）、`verdict_outcome_lookup` 读
`signal_verdict_outcomes`（0 行），结构性必然返回空。

| # | 任务 | 验收 |
|---|---|---|
| 4.1 | 按 `proposal.md` Open Question 1 的决策落地（显式声明"尚未开始记录" or 从工具面摘掉） | 二者其一，且回答里不再出现"查了但没有"的假象 |
| 4.2 | `announcements_read` 空结果复核：确认"该标的确实无公告"与"接口失败"能被区分开 | 两种情形在回答里措辞不同 |

## Phase 5 — 验收与护栏

| # | 任务 | 验收 |
|---|---|---|
| 5.1 | 重跑 `scripts/verify_tool_turns.py`，对比本提案前后的**零证据率**与**空转弃权率** | 零证据回合显著下降；剩余零证据全部可归因 |
| 5.2 | 用生产库 `chat_turn_traces.steps` 复算：`ok=true 且 evidence_count=0` 的次数 | 从 26 降到接近 0（`announcements_read` 这类真无数据的除外） |
| 5.3 | 补护栏测试：表格型工具在真实文案下不得产出空证据 | 回归时必然失败，且失败信息点名工具 |
| 5.4 | 全量后端测试：失败集必须 ⊆ 基线 | `no_proxy='*' .venv/bin/python -m pytest tests/ -q -p no:randomly` |

## 依赖与阻塞

- **Phase 4.1 的一部分取决于用户侧数据**：`trade_ledger` 是否有数据，取决于成交流水/券商导出
  是否接入（当前阻塞项）。在此之前 `trade_ledger_read` 只能走"显式声明尚未记录"。
- Phase 1 与本文件之外的在途改动（工具面退役 50 → 42）**无耦合**，可并行。

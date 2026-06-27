## Context

AlphaPilot A-Share 深度分析链路：`DataCollector` 并行拉取十余类市场数据 → Analyst Agent 生成章节报告 → `ReportDB` 持久化 → `ReportViewer` / 邮件展示。

当前新鲜度处理散点式：`cn_eastmoney_http_provider` 有文本滞后提示，`cn_akshare_provider` 无等价校验；`trade_calendar` 与 `MarketContext.analysis_mode` 未与数据截止日绑定；报告无结构化 `freshness_summary`。

用户诉求：**按数据类型定义可判定的过时依据，并在报告上打标**。需「数据集 → 章节 → 报告」三层聚合 + 生成时刻不可变快照。

## Goals / Non-Goals

**Goals:**
- 四类 taxonomy + YAML 契约，覆盖 `data_collector` 全部数据源
- 采集输出 `freshness_meta`，报告聚合 `freshness_summary`（含 `overall_status`）
- 列表/详情/章节三级 UI；stale 时硬封顶 confidence + LLM 注入
- **分析交易日相对锚点**：历史复盘 vs 当日分析使用不同 expected 规则
- 盘后 grace 窗口：`warning`（缓冲期内）vs `stale`（超出缓冲）
- 生成时刻快照不可变；区分数据源过时 vs 报告年龄
- **拉取异常可标识**：fetch 失败与数据滞后区分，均写入报告 `freshness_summary`

**Non-Goals:**
- V1 不重算历史报告 freshness（无 backfill 强制要求）
- V1 不做新闻 NLP 相关性评分
- V1 不覆盖美股完整契约（YAML 预留 `market: CN`）
- V1 不因 stale 硬阻断报告生成

## Design Decisions

### D1: 三层新鲜度模型（数据集 → 章节 → 报告）

**Options considered:**
- A. Provider 文本提示 — 无法结构化聚合
- B. 报告级单一 `is_stale` — 无法定位根因
- C. meta → section_impacts → overall — 可解释、可测试

**Chosen:** C

**Rationale:** 列表看整体，详情看 `blocking_sources`，章节头看局部影响。

```
DataCollector → freshness_meta[source_key]
             → freshness_pool
             → freshness_summary (persisted)
                  ├─ overall_status (fresh|warning|stale|error)
                  ├─ blocking_sources[] (含 issue_type: lag|fetch_error)
                  ├─ fetch_errors[] (异常源摘要，便于 UI 展示)
                  ├─ datasets[] (摘要)
                  └─ section_impacts{}
```

### D2: 分析交易日相对锚点（refine 新增，解决 plan 隐含假设）

**Options considered:**
- A. 始终用「当前时刻 latest_settled_td」— 历史复盘会误判 stale
- B. 始终用 `analysis_trade_date` — 盘中当日分析会误判
- C. **双模式锚点** — 按 `analysis_mode` 分支

**Chosen:** C

**Normative 规则：**

| analysis_mode | expected 锚点 |
|---------------|---------------|
| `historical`（trade_date < today） | `expected = analysis_trade_date` |
| `t_plus_1`（trade_date = 上一交易日） | `expected = analysis_trade_date` |
| `intraday` / `pre_market`（trade_date = today，未收盘） | `expected = previous_cn_trading_day(today)` |
| `post_market`（trade_date = today，已收盘） | `expected = today`（受 grace 约束） |
| 非交易日 | `expected = previous_cn_trading_day(today)` |

**Rationale:** 用户指定 6/20 复盘时，日K/资金流 cutoff 必须 ≥ 6/20；用户指定今日盘中分析时，日级数据只认上一完整交易日。

### D3: 四类数据判定规则（最终契约）

| category | 代表源 | 判定逻辑 | 影响 overall |
|----------|--------|----------|--------------|
| `calendar_anchored` | stock_data, fund_flow, zt_pool, board_fund_flow, realtime_quotes* | `cutoff_date >= expected` → fresh；grace 内滞后 → warning；否则 stale | 按 criticality |
| `disclosure_period` | fundamentals, 三表 | `disclosed_period` 为该股在 analysis_date 前最新已披露期 → fresh；落后一期 → warning；落后两期+ → stale | important |
| `conditional_daily` | lhb_detail | 有数据且 cutoff 达标 → fresh；empty + empty_ok → empty_ok；应有数据但 cutoff 滞后 → stale | critical（仅当该源有数据时） |
| `event_driven` | news, global_news, hot_stocks_xq | `latest_publish` 在窗口内 → fresh；超窗口 → warning；无数据 → not_applicable | 不单独拉高 overall |

\* `realtime_quotes`：仅 `intraday`/`pre_market` 参与 freshness；`post_market` 及以后以日K为准。

### D4: Grace 窗口（refine 决议，替代 Open Q1）

**Chosen:** 按源配置 `grace_minutes`，仅作用于 `calendar_anchored` 且 `post_market` 模式。

| source_key | grace_minutes | 说明 |
|------------|---------------|------|
| stock_data | 30 | 日K通常 15:30–16:00 可用 |
| individual_fund_flow | 120 | 资金流常 17:00 后完整 |
| board_fund_flow | 90 | 板块汇总略早于个股 |
| zt_pool | 60 | 涨停池较快 |
| indicators (derived) | 0 | 随 stock_data，不单独 grace |

Grace 内：`cutoff < expected` → `warning`（非 stale），不计入 `blocking_sources`。Grace 外 → `stale`。

### D5: overall_status 聚合（criticality 加权 + 拉取异常优先）

**Chosen:** 四级 overall + 源级 `error` 与 `stale` 语义分离

| 源级 status | 含义 | issue_type |
|-------------|------|------------|
| `error` | 拉取/解析/超时失败，无有效数据 | `fetch_error` |
| `stale` | 拉取成功但 cutoff 滞后 | `lag` |
| `warning` | grace 缓冲内滞后 | `lag` |

**聚合算法（normative，error 优先于 stale）：**

```
if any(critical|important status==error):  overall = error
elif any(critical stale):                 overall = stale
elif any(important stale):                overall = warning
elif any(critical|important warning):     overall = warning
else:                                     overall = fresh
```

`empty_ok`、`fresh`、`not_applicable` 不抬高 overall。`informational` 的 error/stale 仅写入 `datasets`，不单独拉高 overall。

**fetch_errors[]：** 从 `blocking_sources` 中筛出 `issue_type=fetch_error` 的条目，冗余写入 summary 便于 UI 单独渲染「数据异常」区块。

### D6: 双维度「过时」（数据源 vs 报告年龄）

**Chosen:**
- **主标识** `overall_status`：仅反映生成时数据源状态
- **次要** `report_age_note`：打开报告时若 `now - created_at > 3 trading days`，UI 灰字提示，**不改变** `overall_status`

### D7: 存储与 API

**Chosen:** `ReportDB.freshness_status` 列（`fresh|warning|stale|error|null`）+ `result_data.freshness_summary` 完整 JSON。

**最终 schema：**

```json
{
  "overall_status": "error",
  "overall_label": "关键数据拉取异常",
  "evaluated_at": "2026-06-24T17:30:00+08:00",
  "analysis_trade_date": "2026-06-24",
  "analysis_mode": "post_market",
  "expected_anchor": "2026-06-24",
  "blocking_sources": [{
    "source_key": "individual_fund_flow",
    "category": "calendar_anchored",
    "criticality": "critical",
    "status": "error",
    "issue_type": "fetch_error",
    "error_code": "provider_timeout",
    "error_message": "东方财富接口超时",
    "lag_note": "主力资金拉取失败，本章结论可信度下降"
  }],
  "fetch_errors": [{
    "source_key": "individual_fund_flow",
    "error_code": "provider_timeout",
    "error_message": "东方财富接口超时"
  }],
  "datasets": [],
  "section_impacts": {
    "smart_money_report": "stale",
    "market_report": "fresh"
  },
  "confidence_cap": 40,
  "report_age_days": 0,
  "report_age_note": null
}
```

### D8: Confidence 硬封顶（refine 决议，替代 Open Q3）

**Options considered:**
- A. 仅 prompt 建议 — LLM 可能忽略
- B. 仅代码 clamp — 模型仍输出高置信文案
- C. **双轨** — 写库 clamp + prompt 约束

**Chosen:** C

| overall_status | confidence 写库上限 | prompt 约束 |
|----------------|---------------------|-------------|
| fresh | 100（不变） | 无 |
| warning | 70 | 注明部分数据在 grace 缓冲 |
| stale | 50 | 禁止基于滞后 blocking_sources 做高置信结论 |
| error | 40 | 禁止基于 fetch_errors 中缺失数据做方向性结论 |

实现点：`report_service` persist 前 `confidence = min(confidence, cap)`。

### D9: UI 标识（refine 决议，替代 Open Q2）

**Options considered:**
- A. 默认隐藏 stale 报告 — 用户可能错过重要复盘
- B. 默认全部展示 + stale 排序靠后 + 可选筛选
- C. 默认展示 + 仅 warning/stale 显示徽章（fresh 无徽章）

**Chosen:** C + B 的排序：`error` > `stale` > `warning` > `null`（历史）> `fresh`，Reports 页筛选器「仅数据异常/过时」（含 error、warning、stale）。

| overall | 列表 | 详情顶栏 |
|---------|------|----------|
| fresh | 无徽章 | 无 |
| warning | 琥珀「部分滞后」 | 可折叠数据源列表 |
| stale | 红色「数据过时」 | 强制 blocking_sources 横幅（issue_type=lag） |
| error | 红色「数据异常」 | 强制 fetch_errors 横幅 + 异常源 error_message |

章节级：`section_impacts != fresh` 时在 `ReportViewer` 标题旁显示 ⚠/✕，tooltip 显示 `lag_note`。

### D10: 模块结构

**Chosen:** `tradingagents/dataflows/freshness/`

| 模块 | 职责 |
|------|------|
| `contract.yaml` | 源注册表（category, criticality, grace, window_hours） |
| `section_map.yaml` | section → sources |
| `anchor.py` | `resolve_expected_anchor(trade_date, analysis_mode, now)` |
| `validator.py` | `evaluate(source_key, parsed, ctx) -> FreshnessMeta` |
| `aggregator.py` | `build_freshness_summary(pool, trade_date, analysis_mode)` |
| `types.py` | dataclass / TypedDict |

集成点：Provider 返回前 attach meta；`DataCollector` 汇总 pool；graph state 携带 pool；`report_service` 调用 aggregator。

### D11: 完整数据源注册表（V1 范围）

| source_key | category | criticality | grace_min | 备注 |
|------------|----------|-------------|-----------|------|
| stock_data | calendar_anchored | critical | 30 | last bar date |
| indicators | calendar_anchored | critical | 0 | 派生自 stock_data |
| individual_fund_flow | calendar_anchored | critical | 120 | last_row_date |
| board_fund_flow | calendar_anchored | important | 90 | 「今日」口径 |
| zt_pool | calendar_anchored | important | 60 | request date |
| lhb_detail | conditional_daily | critical | 0 | empty_ok |
| fundamentals | disclosure_period | important | — | report_period |
| balance_sheet | disclosure_period | important | — | |
| cashflow | disclosure_period | important | — | |
| income_statement | disclosure_period | important | — | |
| news | event_driven | informational | — | window 72h |
| global_news | event_driven | informational | — | window 72h |
| hot_stocks_xq | event_driven | informational | — | window 6h |
| insider_transactions | event_driven | informational | — | window 30d |
| realtime_quotes | calendar_anchored | important | 0 | 仅 intraday |

### D12: section ↔ source 映射

| section_key | primary sources |
|-------------|-----------------|
| market_report | stock_data, indicators |
| volume_price_report | stock_data |
| smart_money_report | individual_fund_flow, lhb_detail |
| macro_report | board_fund_flow, global_news |
| game_theory_report | zt_pool, lhb_detail, hot_stocks_xq |
| fundamentals_report | fundamentals, balance_sheet, cashflow, income_statement |
| news_report | news, global_news |
| sentiment_report | hot_stocks_xq, news |
| investment_plan / final_trade_decision | 继承全部 critical+important 最差状态 |

### D13: 拉取异常与数据滞后分离（refine 补充）

**Options considered:**
- A. 异常归入 `stale` — 实现简单，用户无法区分「没拉到」与「拉到了但旧」
- B. 异常归入 `unknown` — 不参与 overall，报告无异常标识
- C. **独立 `error` status + `issue_type`** — 语义清晰，UI/LLM 可分别处理

**Chosen:** C

**Normative 规则：**

| 触发条件 | 源级 status | issue_type | error_code 示例 |
|----------|-------------|------------|-----------------|
| Provider 抛异常 | `error` | `fetch_error` | `provider_exception` |
| HTTP 超时 / akshare slot timeout | `error` | `fetch_error` | `provider_timeout` |
| 返回空且非 empty_ok | `error` | `fetch_error` | `empty_response` |
| 解析失败（CSV/JSON） | `error` | `fetch_error` | `parse_error` |
| 拉取成功但 cutoff 滞后 | `stale` | `lag` | — |

**DataCollector 职责：** `_fetch_all` 每个 task 必须 try/except；异常时仍写入 `freshness_pool[source_key]`，**禁止静默吞掉**（当前部分路径返回「调用失败」文本但无结构化 meta）。

**LLM 注入差异：**
- `stale`：「数据已过期，截至 {anchor_actual}」
- `error`：「数据拉取失败（{error_code}），该维度结论缺失，勿臆造」

## Risks / Trade-offs

| 风险 | 缓解 |
|------|------|
| 历史复盘锚点与当日分析逻辑分叉 | `anchor.py` 单测覆盖 4 种 analysis_mode |
| Grace 窗口仍误判 | grace 内标 warning 而非 stale，用户可见缓冲状态 |
| Section 映射遗漏 | data_collector 变更 checklist + section_map 同 PR |
| LLM 忽略 freshness | 硬 clamp confidence + structured blocking_sources |
| 异常与滞后混淆 | 独立 `error` status + `issue_type` + UI 分文案 |
| JSON 膨胀 | datasets 存摘要；debug 模式才存完整 meta |
| 定时任务 16:00 跑批 | grace 规则自然覆盖；16:00 前 fund_flow 可能 warning 非 stale |

## Migration Plan

1. 部署 `freshness/` 包 + YAML
2. `ReportDB.freshness_status` 可空列 migration
3. 新报告自动写入；旧报告 null → UI 无徽章
4. API 扩展可选字段；前端 fallback

## Open Questions

None — refine 已决议 grace、UI 筛选、confidence 双轨、历史锚点。

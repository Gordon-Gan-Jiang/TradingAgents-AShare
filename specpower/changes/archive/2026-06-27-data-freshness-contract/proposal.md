## Why

深度分析依赖多源市场数据，但当前系统缺少统一的「数据新鲜度」契约：新闻类与量价/资金类被同等对待，且拉取失败与数据滞后未区分，导致 LLM 可能基于缺失或滞后数据做出高置信研判。需要按数据类型建立可验证的新鲜度标准，在采集、分析、展示全链路显式标注 `fresh / warning / stale / error`，并在报告上持久化不可变快照。

## What Changes

- 引入 **Freshness Contract**：四类 taxonomy（事件型 / 日历锚定型 / 披露周期型 / 条件日度型），YAML 注册全部 `data_collector` 数据源
- 实现 **分析交易日相对锚点**：历史复盘、T+1、盘中、盘后四种 `analysis_mode` 使用不同 `expected_anchor`（refine 决议，替代「始终用 latest_settled_td」）
- **Grace 缓冲窗口**：盘后各源独立 `grace_minutes`；缓冲期内滞后标 `warning`，超出标 `stale`
- Provider + DataCollector 输出 `freshness_meta`；报告聚合 `freshness_summary`（overall / blocking_sources / section_impacts）
- **拉取异常独立标识**：Provider 异常、超时、解析失败 → 源级 `status: error`（与 `stale` 滞后区分）；critical/important 源 error 时报告 `overall_status: error`
- **Confidence 双轨封顶**：error → ≤40，stale → ≤50，warning → ≤70（写库硬 clamp + LLM prompt）
- 前端：warning/stale/error 徽章（「数据异常」vs「数据过时」）、详情横幅、章节级图标；Reports 列表支持「仅数据异常/过时」筛选；fresh 不显示徽章
- 区分 **数据源过时**（主标识）与 **报告年龄过久**（次要灰字，不改变 overall）
- 推广 Eastmoney 主力资金滞后检测到全部 CN Provider

## Capabilities

### New Capabilities

- `freshness-contract`: taxonomy、YAML 注册表、分析模式相对锚点、grace 窗口、expected_rule 枚举
- `freshness-validator`: Provider/DataCollector 校验、`freshness_meta`/`freshness_pool`、criticality 与 grace 判定
- `freshness-disclosure`: LLM 注入、报告持久化、confidence clamp、UI/邮件/企微披露

### Modified Capabilities

<!-- greenfield — 无 baseline specs -->

## Impact

- **数据层**：`tradingagents/dataflows/freshness/`（contract.yaml, anchor, validator, aggregator）；`trade_calendar`；各 CN Provider；`data_collector`
- **分析层**：graph state 携带 `freshness_pool`；`context_utils` / 各 Analyst prompt 注入
- **持久化**：`ReportDB.freshness_status` + `result_data.freshness_summary`；`report_service` confidence clamp
- **前端**：`Report`/`ReportDetail` 类型；Dashboard/Reports 徽章与筛选；ReportViewer 横幅与章节图标
- **测试**：anchor 四模式、grace warning vs stale、历史复盘、LHB empty_ok、aggregator、API smoke
- **兼容**：新字段可选；旧报告 freshness_status=null 不展示徽章

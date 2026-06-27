<!-- Refined tasks (post-refine). Will be rewritten to writing-plans precision in /specpower:build Phase A. -->

# Data Freshness Contract — Refined Implementation Tasks

## 1. Freshness 契约、锚点与 Grace

- [ ] 1.1 创建 `tradingagents/dataflows/freshness/` 包（types, contract, anchor, validator, aggregator, section_map）
- [ ] 1.2 编写 `contract.yaml` 完整注册表（15 源 + category/criticality/grace/window_hours）
- [ ] 1.3 实现 `resolve_expected_anchor(trade_date, analysis_mode, now)` 四模式分支
- [ ] 1.4 实现 grace 判定：缓冲内 `warning`、超出 `stale`
- [ ] 1.5 单元测试：historical / intraday / post_market / 非交易日 / grace 边界

## 2. Validator 与 Provider 集成

- [ ] 2.1 实现 `evaluate(source_key, parsed_cutoff, ctx) -> FreshnessMeta` 含四类 category 分支
- [ ] 2.2 重构 `cn_eastmoney_http_provider` 主力资金校验为共享 validator
- [ ] 2.3 为 `cn_akshare_provider` 接入 fund_flow / stock_data / zt_pool / lhb / board_fund_flow
- [ ] 2.4 indicators 继承 stock_data freshness，不单独 grace
- [ ] 2.5 `data_collector._fetch_all` 每个 source try/except，异常写入 `freshness_meta.status=error`（禁止静默吞掉）
- [ ] 2.6 `data_collector._fetch_all` 汇总 `freshness_pool` 并传入 graph state
- [ ] 2.7 集成测试：fetch timeout→error、parse_error、stale fund flow、grace warning、LHB empty_ok

## 3. 报告聚合、Confidence 与持久化

- [ ] 3.1 编写 `section_map.yaml` 及 `section_impacts` 聚合（含 final_trade_decision 继承规则）
- [ ] 3.2 实现 `build_freshness_summary(pool, trade_date, analysis_mode)` 含 blocking_sources 与 fetch_errors
- [ ] 3.3 `report_service` 持久化前写入 `result_data.freshness_summary` + `freshness_status` 列
- [ ] 3.4 实现 confidence 硬封顶：error≤40、stale≤50、warning≤70
- [ ] 3.5 `ReportDB.freshness_status` migration（可空，含 error 枚举值）
- [ ] 3.6 aggregator 测试：critical error→error；critical stale→stale；仅 informational stale→fresh

## 4. 分析层 Prompt 注入

- [ ] 4.1 扩展 `MarketContext` / graph state 携带 `freshness_pool` 与摘要文本
- [ ] 4.2 smart_money / market / fundamentals analyst 注入 blocking_sources 与 grace warning
- [ ] 4.3 final decision stage 注入 overall_status 与 confidence_cap 说明

## 5. API 与前端过时标识

- [ ] 5.1 reports API 透出 `freshness_status` 与 `freshness_summary`
- [ ] 5.2 更新 `frontend/src/types` Report / ReportDetail
- [ ] 5.3 Reports 列表：error「数据异常」/ stale「数据过时」/ warning 徽章；「仅数据异常/过时」筛选
- [ ] 5.4 ReportViewer：error 顶栏（fetch_errors）与 stale/warning 顶栏（lag）分开展示
- [ ] 5.5 历史报告 freshness_status=null 时不展示徽章；report_age_note 灰字（>3 交易日）
- [ ] 5.6 Dashboard 列表同步 freshness 徽章

## 6. 通知与端到端验证

- [ ] 6.1 email / wecom 决策摘要前插入 freshness 一行警告
- [ ] 6.2 API smoke：mock fetch error → `freshness_status: error` + confidence≤40；mock stale → stale + ≤50
- [ ] 6.3 `specpower validate` 三份 delta spec 与实现行为对齐

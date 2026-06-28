# Tasks: snap-retro-implementation

> 事后补档 — 所有任务标记为已完成 `[x]`

## 1. 数据新鲜度（spec 已独立归档，实现存在于工作区）

- [x] 1.1 `tradingagents/dataflows/freshness/` 模块与 contract.yaml
- [x] 1.2 DataCollector / graph state / API 持久化集成
- [x] 1.3 前端 FreshnessBadge、FreshnessStatusCell、Reports 独立列、筛选
- [x] 1.4 akshare 板块流 API 回退 + intraday_only 评估修复
- [x] 1.5 披露期 YYYYMMDD 解析修复 + `最新披露期` 锚点
- [x] 1.6 测试：freshness 41 项 + disclosure/board 回归

## 2. 推荐与推送

- [x] 2.1 `recommendation_service` 用户池推荐
- [x] 2.2 定时/手动推送（WeCom + WPS）
- [x] 2.3 `/v1/recommendations` API 与 Recommendations 页

## 3. T+1 洞察

- [x] 3.1 `insights_t1_service` 趋势与明细 API
- [x] 3.2 QualityInsights / RecommendationInsights 前端页
- [x] 3.3 多模型共识 T+1 指标

## 4. 模型档案与竞技场

- [x] 4.1 `model_profile_service` CRUD + warmup
- [x] 4.2 `model_arena_service` 跑数、排行榜、drift
- [x] 4.3 ModelProfiles 前端页

## 5. 模拟盘

- [x] 5.1 `paper_trading_service` 组合/成交/复盘
- [x] 5.2 PaperTrading 前端页与 API

## 6. 共识与报告质量

- [x] 6.1 `consensus_service` + ConsensusCard
- [x] 6.2 `report_quality_service` 质量后处理
- [x] 6.3 `decision_critic` 决策校验节点

## 7. Prompt 模板与每日产品

- [x] 7.1 `prompt_template_service` CRUD
- [x] 7.2 `market_scanner_service` + daily-product run API

## 8. 分析师结构化输出

- [x] 8.1 `analyst_structured` JSON 块解析
- [x] 8.2 各 analyst 接入 structured instruction

## 9. 基础设施

- [x] 9.1 CN Eastmoney/Sina providers
- [x] 9.2 WPS 通知、`portfolio_import` 增强
- [x] 9.3 README、API smoke、312 项测试套件

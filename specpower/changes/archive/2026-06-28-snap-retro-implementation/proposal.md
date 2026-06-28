## Why

本地工作区含大量已实现、未单独走 SpecPower 流水线的功能（**78 个已跟踪文件** + **50+ 新增模块**）。部分能力已通过独立 change **先行归档**（freshness 主线、报告列表列、akshare/fix），其余产品能力需本 snap **一次性补档**，便于审计与后续 `specpower change archive` 合并进 `specpower/specs/`。

## What Changes

本 snap 从 `git diff HEAD` + untracked 文件推断，覆盖下列**已落地**能力：

| 域 | 代表路径 | 说明 |
|----|----------|------|
| **数据新鲜度（实现）** | `tradingagents/dataflows/freshness/` | spec 已在 main；含 disclosure/akshare 修复代码 |
| **报告列表数据状态列** | `FreshnessStatusCell`, `Reports.tsx` | spec 已归档 `reports-list-freshness-column` |
| **推荐与推送** | `recommendation_*`, `daily_stock_analysis` | 用户池扫描、定时/手动推送、WeCom/WPS |
| **T+1 洞察** | `insights_t1_service`, Quality/Recommendation Insights | 准确率趋势、多模型共识 |
| **模型档案** | `model_profile_service`, ModelProfiles | CRUD、warmup、任务绑定 |
| **模型竞技场** | `model_arena_service` | 并行跑数、排行榜、drift |
| **模拟盘** | `paper_trading_service`, PaperTrading | 组合、成交、日终复盘 |
| **共识摘要** | `consensus_service`, ConsensusCard | 多分析师方向加权 |
| **报告质量** | `report_quality_service`, `decision_critic` | trace 合并、verdict reconcile |
| **Prompt 模板** | `prompt_template_service` | 用户级 CRUD |
| **每日产品/扫描** | `market_scanner_service` | 策略扫描 run |
| **结构化分析师** | `analyst_structured` | JSON 块 + 各 analyst 接入 |
| **通知/数据源** | WPS、Eastmoney/Sina providers | 扩展 CN 数据路径 |
| **前端产品页** | 10+ 新页面/组件 | Dashboard、Reports、Portfolio 等对接 |

### 已独立归档（本 snap **不含** delta，避免重复）

| Change | 状态 |
|--------|------|
| `data-freshness-contract` | main specs ✅ `fda01dd` |
| `reports-list-freshness-column` | main specs ✅ `5660b32` |
| `fix-akshare-board-fund-flow-realtime-freshness` | main specs ✅ `67b2b49` |
| `fix-disclosure-period-parser` | archive 待 commit ⏳ |

## Capabilities

### New Capabilities（本 snap delta specs）

- `product-recommendations` — 用户池推荐与推送
- `insights-t1` — T+1 准确率与共识洞察
- `model-profiles` — 模型档案
- `model-arena` — 模型竞技场
- `paper-trading` — 模拟盘
- `consensus-summary` — 报告共识摘要
- `report-quality` — 报告质量后处理
- `prompt-templates` — Prompt 模板
- `daily-product` — 每日扫描产品
- `analyst-structured` — 结构化分析师输出

### Modified Capabilities

- **无** — freshness 三件套已在 main baseline；UI 列与 fix 已独立归档。

## Impact

- **代码**：`api/`, `tradingagents/`, `frontend/`, `tests/`（+11635 / -1939 行，相对 HEAD）
- **依赖**：PyYAML、前端 vitest、package 升级
- **数据库**：`api/database.py` 多表扩展
- **测试**：全量 **312** 项 collected；freshness 相关 **41** passed
- **归档后**：main specs 预计从 3 个 freshness 能力扩展至 **13** 个产品能力域

## Evidence

```bash
git diff --stat HEAD                    # 78 files changed
uv run pytest tests/ -q --co            # 312 tests collected
uv run pytest tests/test_freshness_contract.py \
  tests/test_disclosure_period_parser.py \
  tests/test_board_fund_flow_provider.py -q   # 41 passed
```

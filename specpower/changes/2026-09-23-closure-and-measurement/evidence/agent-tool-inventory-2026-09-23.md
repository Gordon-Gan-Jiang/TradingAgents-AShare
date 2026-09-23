# 工具清单盘点（Agent 能力原子化 · Phase 0 交付物 0.1）

> 生成方式：端点与工具规格由脚本从 `api/main.py` 与工具注册表**自动提取**，
> 分层与缺口判断为人工分类。数字随代码变动，重新跑一遍提取即可核对。

## 一、总览：能力在哪，工具面在哪

| 项 | 数量 | 说明 |
|---|---|---|
| HTTP 端点 | **133** | 分布在 30 个前缀下 |
| 服务模块（`api/services/`） | **41** | 平台真正的业务能力在这里 |
| 注册进工具面的 `ToolSpec` | **42** | 供对话 Agent / MCP / 规划器共用（L1 决策常驻 / L2 证据按需 / L3 任务异步）。本轮从 50 退役 8 个 vendor 工具，见下方「本轮退役」 |
| 其中**决策型（L1）** | **18** | 判定、兑现、权重、持仓、组合体检、计划、报告结论、共识、推荐、看板、评测榜、质量、模拟盘、台账 |
| 其中**证据型（L2）** | **20** | `get_*` 系列（11）+ 新闻/筹码/分时/资金流/自选/公告/指数/时效 + `poll_async_job` |
| 其中**任务型（L3）** | **4** | `submit_backtest_job`、`submit_deep_analysis_job`、`submit_daily_review_job`、`submit_model_arena_job`（L3 必须异步，`ToolSpec` 构造时强制） |

**本轮退役的 8 个（工具面 50 → 42，L2 28 → 20）**：`get_stock_data`、`get_indicators`、
`get_cashflow`、`get_news`、`get_insider_transactions`、`get_board_cons`、
`get_individual_fund_flow`、`get_lhb_detail`。

退役理由只有一条，且是可执行的：**它们返回预格式化中文散文、`evidence=()`**，取到的
数字无法被答案层的机械校验器引用——「没有证据就没有回答」，留着它们只会制造一个
永远出不了结论的落点。41 轮真实对话里规划层一次也没有选中过它们。

**边界（重要）**：退役只针对**工具面**。这 8 个 `@tool` 仍然存在，分析图
（`tradingagents/graph/*`、`tradingagents/agents/analysts/*`）照旧在用它们；它们的
声明以 `RETIRED_DATA_TOOL_DECLS` 保留，只喂 `data_tool_impl`。删掉会让分析图在调用时
抛 `KeyError`。判据见 `tests/test_data_tools_migration.py` 与 `tests/test_tool_registry.py`
（豁免名单恰好是这 8 个）。

**结论（Phase 2 收口后）**：盘点时平台独有能力"Agent 一个都调不到"；
现在 9 个 L1 缺口全部补齐，L2 补齐 6/6（另加 `index_quote_read`），**L3 补齐 4/4**。**剩余缺口不是"没做"，
而是平台没有可复用的服务入口**——逐条依据见第四节，一律不硬造。

## 二、盘点时的 23 个工具（Phase 0 基线快照，自动提取）

> ⚠️ **这是 Phase 0 盘点当时的快照，不是当前工具面**。当前工具面见文末附表
> （42 个）。保留本节是为了让"当时有什么、后来补了什么"可被核对。
>
> 表中标 **【已退役】** 的 8 个已从工具面摘除（见第一节「本轮退役」）。它们在**本表
> 的语境里仍然有效**——那时它们是工具面的成员；但读到这里的人不应当以为它们今天还在。

| 工具 | cost | 超时(ms) | freshness 源 | 层次 |
|---|---|---|---|---|
| `signal_washout_probe` | medium | 15000 | stock_data, intraday_bars, chip_distribution, individual_fund_flow, board_spot | **L1** |
| `signal_washout_probe_batch` | medium | 60000 | 同上 | **L1** |
| `market_screen` | medium | 120000 | realtime_quotes | **L1** |
| `data_symbol_news` | cheap | 8000 | symbol_news | L2 |
| `get_realtime_quotes` | cheap | 30000 | core_stock_apis | L2 |
| ~~`get_stock_data`~~【已退役】 | cheap | 30000 | core_stock_apis | L2 |
| ~~`get_indicators`~~【已退役】 | cheap | 30000 | technical_indicators | L2 |
| `get_fundamentals` / `get_balance_sheet` / `get_income_statement` | cheap | 30000 | fundamental_data | L2 |
| ~~`get_cashflow`~~【已退役】 | cheap | 30000 | fundamental_data | L2 |
| ~~`get_individual_fund_flow`~~【已退役】 / ~~`get_lhb_detail`~~【已退役】 / `get_zt_pool` / `get_hot_stocks_xq` / `get_board_*`(5) | cheap | 30000 | board_data | L2 |
| ~~`get_news`~~【已退役】 / `get_global_news` / ~~`get_insider_transactions`~~【已退役】 | cheap | 30000 | news_data | L2 |

观察：**L2 的工具超时与成本高度同质**（全部 cheap/30s），说明它们本就是同一层
的"取数原语"，适合整体按需检索注入；而 L1 三个各自有独特预算（15s/60s/120s），
正是"决策粒度"的特征——判定要等数据，扫描要等全市场。

**本轮的一个教训正好印证了这层观察**：L2 里那批"同质"的 vendor 工具，彼此在
**可引用性**上并不等价——8 个只给散文、8 个中的一个（`get_board_cons`）连板块成分
都只能给文本。同层同成本 ≠ 同可用性；退役判断的依据是 `evidence=()`，不是 cost。

## 三、端点分组（自动提取，按端点数量降序）

| 前缀 | 端点数 | 平台独有能力？ | 目标层 |
|---|---|---|---|
| `v1/mainline` | 21 | ✅ 市场主线/周期/自动挖掘 | **L1**（`market_mainline_view`） |
| `v1/scheduled` | 9 | 定时任务管理 | —（配置类，不进工具面） |
| `v1/model-arena` | 8 | ✅ 模型对比/漂移 | L1（只读：`model_arena_leaderboard`） |
| `v1/recommendations` | 8 | ✅ 平台推荐结果 + 反馈评分 | **L1**（`recommendation_read`：读结果，不生成） |
| `v1/reports` | 8 | ✅ 历史报告与质量 | **L1**（`report_conclusion_lookup`） |
| `v1/insights` | 7 | ✅ T+1 质量/多模型共识 | L1（只读） |
| `v1/portfolio` | 7 | ✅ 组合与导入持仓 | **L1**（`portfolio_risk_review`） |
| `v1/trade-ledger` | 6 | ✅ 交易流水 | L1（只读）+ 动作类（需确认） |
| `v1/daily-product` | 5 | ✅ 每日产品化运行 | L3（异步任务） |
| `v1/model-profiles` | 5 | 模型配置管理 | —（配置类） |
| `v1/paper-portfolio` | 5 | ✅ 模拟盘 | L1（只读）+ 动作类 |
| `v1/backtest` | 4 | ✅ 回测 | **L3**（异步任务） |
| `v1/watchlist` | 4 | ✅ 自选 | L2（读）+ 动作类（写） |
| `v1/market` | 3 | 行情检索/搜索 | L2 |
| `v1/jobs` | 3 | 任务状态 | 支撑 L3 的 poll |
| `v1/trade-plans` | 2 | ✅ 交易计划 | L1（只读） |
| `v1/exit-advice` | 1 | ✅ 离场建议 | L1 |
| `v1/announcements` | 1 | 公告 | L2 |
| `v1/dashboard` | 1 | 跟踪看板 | L1（`tracking_board_read`） |
| `v1/analyze` / `v1/chat` | 2 | 入口本身 | — |
| 其他（auth/tokens/config/feedbacks/prompt-templates/healthz 等） | 13 | 治理类 | —（不进工具面） |

## 四、缺口清单 → **收口状态**（逐条，据实）

### L1 决策工具（9/9 ✅ 全部补齐）

| 目标工具 | 状态 | 复用服务 | 关键纪律 |
|---|---|---|---|
| `portfolio_risk_review` | ✅ | `exit_engine_service`（+ 已落库判定喂 `market_states`） | 排序与风险口径由确定性引擎给出；「未导入持仓」与「有持仓但评估不出来」分开声明 |
| `verdict_outcome_lookup` | ✅ | `signal_outcome_service.list_recorded_outcomes`（本次新增读入口） | **按兑现行分页**：先取最近 N 条判定再筛兑现会把"有记录"说成"没有" |
| `verdict_history_read` | ✅ | `signal_service.list_verdicts` | 读完键名必须与返回一致（曾读成 `verdicts` → 永远空） |
| `signal_weight_read` | ✅ | `signal_weight_service` | 「正在用先验权重」是事实，不是未覆盖 |
| `report_conclusion_lookup` | ✅ | `report_service.get_reports_by_user` | 报告未完成 → 「结论尚未生成」，不得当中性 |
| `market_mainline_view` | ✅ | `mainline_service` | 板块强弱名次与涨跌幅必须可引用 |
| `recommendation_read` | ✅ | `recommendation_feedback_service`（无落库时才回退 `recommendation_service`） | 只读已生成结果；待评估 ≠ 已验证有效 |
| `tracking_board_read` | ✅ | `tracking_board_service` | 逐条声明「无分析结论」，缺结论 ≠ 中性 |
| `model_arena_leaderboard` | ✅ | `model_arena_service` | 准确率必须带样本量与评测窗口（`scope` 默认 `all`：`portfolio` 实测常年 0 行） |

**额外补齐的 L1（端点表里标了 L1 但不在编号缺口里）**：`trade_plan_read`（纪律锚点）、
`report_consensus_read`（多视角共识，含解析失败人数）、`t1_quality_read`（命中率 +
置信区间 + 所需样本量）、`paper_portfolio_read`、`trade_ledger_read`（含归因覆盖率）。

### L2 证据工具（7/7 ✅ 全部补齐）

* ✅ 筹码分布 `chip_distribution_read`、分时承接 `intraday_bars_read`、板块资金流 `capital_flow_read`
* ✅ `watchlist_read`、`data_freshness_check`
* ✅ **`announcements_read`（已补）**：先把数据源补上，再封工具——东财
  `np-anotice-stock/api/security/ann`（按个股 + 日期区间，实测 1.1–3.4s，**带公告类型**）
  优先，巨潮资讯 `hisAnnouncement/query`（实测 0.4–3.2s，权威披露源）兜底；实现在
  `tradingagents/dataflows/providers/cn_announcement_index_data.py`。一条源都没拿到 →
  声明未覆盖，**不拿新闻冒充公告**。注：akshare 的 `stock_notice_report` 只能**全市场
  按日**拉（实测 1385 行 / 36.9s），`stock_zh_a_disclosure_report_cninfo` 实测
  `JSONDecodeError`（akshare 用 query string 且不带 UA/Referer，巨潮回了非 JSON）——
  所以走直连，而不是硬套 akshare。
* ✅ **`index_quote_read`（已补）**：新浪直连行情 `hq.sinajs.cn`（实测 0.6s，**带快照日期**）
  → 新浪 akshare 快照（7.8s，无时间戳）→ 东财 akshare「沪深重要指数」（38.8s，无时间戳）。
  直连优先是因为**只有它带快照时间戳**：没有时间戳，指数点位最容易被错贴到某个历史交易日。

### L3 任务工具（4/4 ✅）

| 能力 | 状态 | 依据 |
|---|---|---|
| 回测 `submit_backtest_job` + `poll_async_job` | ✅ | `backtest_service.submit/get_job` 是可复用服务入口 |
| 全文研报生成 `submit_deep_analysis_job` | ✅ | 提交入口 `analysis_job_service.submit_deep_analysis`（`/v1/analyze` 路由改调它） |
| 全市场复盘 `submit_daily_review_job` | ✅ | 提交入口 `analysis_job_service.submit_daily_review`（原 `main.py::_start_daily_product_run` 整体迁入） |
| 模型对比运行 `submit_model_arena_job` | ✅ | 提交入口 `analysis_job_service.submit_model_arena_run`（`/v1/model-arena/runs` 路由改调它） |

> **这一层是怎么补上的**：先做前置工作——把"建任务记录 → 推 `job.created` →
> 投递后台任务"三步从 `api/main.py` 的路由内联代码抽到
> `api/services/analysis_job_service.py`（一份实现，路由与工具共用），再照 L3
> 异步协议（submit → poll）封工具。三个能力都**不依赖 HTTP 请求上下文**，只需要
> `user_id`（路由从 `current_user` 取、工具由端口闭包绑定），这是它们能被封成工具的前提。

### 动作类工具（1/4 可执行）

| 动作 | 状态 | 依据 |
|---|---|---|
| `watchlist_add`（= `watchlist_write`） | ✅ 已接通 | `watchlist_service.add_watchlist_item`；动作执行器此前**从未在生产装配里传入**，已修（`chat_service.build_action_executor`） |
| 价格阈值预警 `price_alert` | ❌ 无法执行 | 平台**没有**用户自定义阈值预警的落库与服务：`tracking_price_alert_service` 是按**离场计划**轮询的通知器。执行器如实拒绝并说明原因 |
| 深度分析 `deep_analysis` | ⚠️ 仍无法执行 | 现在有可复用的提交入口了（见上表），但**动作执行器**（"确认后执行"那条路）没有接它：动作语义是"改状态"，而深度分析是"起一个分钟级任务"。要走通应改走 `submit_deep_analysis_job` 工具，而不是给动作执行器塞一个异步任务入口 |
| `trade_ledger_write` / `paper_trade` / `scheduled_*` | ⏸ 未开始 | 涉及资金与长期承诺，需产品先定「确认文案 + 可撤销性」，不适合在补工具阶段顺手加 |


## 五、不进工具面的东西（明确排除，避免"什么都封成工具"）

* **配置类**（`model-profiles`、`config` 写、`prompt-templates`）：改配置该在设置页做，
  对话里做会让"一句话改错全局配置"成为可能。
* **治理类**（`auth`、`tokens`、`feedbacks`、`healthz`）：与投研无关。
* **定时任务管理**（`scheduled` 的增删改）：同上，且属于长期承诺，需专门界面确认。

## 六、给 Phase 1/2 的三条判据

1. **按决策问题封装，不按数据表封装**：一个 L1 工具回答一个判断问题（含结论+依据+弃权）；
   数据表类接口一律落 L2，由检索注入。
2. **分钟级能力不许进同步工具面**：一律 L3 异步（否则 MCP 超时重试、对话循环卡死、
   预算记账失真——今天 7 分析师管道无法被对话调用就是这个原因）。
3. **工具 handler 只做转换**：业务逻辑留在服务层（沿用 `tools/signals.py` 的既有做法），
   否则会出现第二份实现并逐渐漂移。


---

## 附：当前工具面快照（自动核对，勿手工改数字）

会话开始时 19 个 ToolSpec、**全是数据型**；Phase 2 收口后为 **50** 个；
**本轮（退役 8 个 vendor 工具）后为 42 个**（实测 `chat_service.build_registry()`，
命令见本节末）：

| 层 | 数量 | 成员 |
|---|---|---|
| **L1 决策** | 18 | signal_washout_probe、signal_washout_probe_batch、market_screen、verdict_history_read、signal_weight_read、portfolio_holdings_read、market_mainline_view、verdict_outcome_lookup、report_conclusion_lookup、recommendation_read、tracking_board_read、model_arena_leaderboard、**portfolio_risk_review、trade_plan_read、t1_quality_read、paper_portfolio_read、trade_ledger_read、report_consensus_read** |
| **L2 证据** | 20 | get_realtime_quotes、get_fundamentals、get_balance_sheet、get_income_statement、get_global_news、get_board_spot、get_board_rank、get_board_hist、get_board_fund_flow、get_zt_pool、get_hot_stocks_xq、data_symbol_news、poll_async_job、chip_distribution_read、intraday_bars_read、capital_flow_read、watchlist_read、**announcements_read、index_quote_read**、data_freshness_check |
| **L3 任务** | 4 | submit_backtest_job、submit_deep_analysis_job、submit_daily_review_job、submit_model_arena_job（均 `asynchronous=True`） |

**本轮退役的 8 个（L2 28 → 20，总数 50 → 42）**：`get_stock_data`、`get_indicators`、
`get_cashflow`、`get_news`、`get_insider_transactions`、`get_board_cons`、
`get_individual_fund_flow`、`get_lhb_detail`。

核对方式（**不要手工改上面的数字**，重跑这条即可）：

```
no_proxy='*' .venv/bin/python -c "
from api.services import chat_service
r = chat_service.build_registry('u')
for L in ('L1','L2','L3'):
    print(L, len(r.names_by_layer(L)))
print('TOTAL', len(r.names()))"
```

规则层引用的工具名必须都在这一份注册表里，已由
`tests/test_routing_references_real_tools.py` 强制。

### 缺口清单的执行情况（对应上文「四、缺口清单」）

| 目标 | 状态 |
|---|---|
| L1 缺 9 个 | ✅ **已补 9 个**（verdict_history_read / signal_weight_read / portfolio_holdings_read / market_mainline_view / verdict_outcome_lookup / report_conclusion_lookup / recommendation_read / tracking_board_read / model_arena_leaderboard）；**另有 6 个端点表里标 L1 的也已补**（portfolio_risk_review / trade_plan_read / t1_quality_read / paper_portfolio_read / trade_ledger_read / report_consensus_read） |
| L2 缺 6 个 | ✅ **已补 6 个**（chip_distribution_read / intraday_bars_read / capital_flow_read / watchlist_read / data_freshness_check / **announcements_read**）+ 本轮新增的 **index_quote_read**（第 7 个）。后两个此前"跳过"的根因是**没有数据源**：本轮先把取数补进 `cn_announcement_index_data.py`（实测表见上文 L2 小节），再封工具；组合根不注入时仍然不注册 |
| L3 缺 4 个 | ✅ **已补 4/4**：回测 `submit_backtest_job`、全文研报 `submit_deep_analysis_job`、每日复盘 `submit_daily_review_job`、模型对比 `submit_model_arena_job`。三项此前的"结构性阻塞"（提交逻辑内联在路由里）已修：`api/services/analysis_job_service.py` 现在是**唯一**的提交实现，`/v1/analyze`、`/v1/daily-product/run`、`/v1/model-arena/runs` 三个路由改调服务函数，工具端口（`chat_service.build_registry` 的 `_task_port`，`user_id` 闭包绑定）也调同一批函数 |

### 两条来自实测的诚实提醒

1. **`capital_flow_read` 在本机只会弃权**：东财 IP 封禁 + 无 `TUSHARE_TOKEN`，三通道全失败。它**不会**返回 0 冒充"无流入"。配好 token 或代理后才会真正生效。
2. **筹码维度实测永远走自算估计路径**（东财原始筹码拿不到）。工具证据里逐字写明「模型估计，非交易所原始数据」——这是刻意的，不是遗漏。

### 本轮新增的诚实提醒（四条）

1. **`index_quote_read` 已补，但它只给实时快照**：三条源（新浪直连/新浪 akshare/东财
   akshare）拿到的都是**当前**报价，历史上某一天的收盘点位它给不了。因此证据的 `as_of`
   记的是**快照日期**（不是请求基准日），基准日与快照日期不一致时 `data.note` 会写明；
   源不含时间戳时时效状态是 `unknown`，**不声明 fresh**。
2. **公告证据的「类型」依赖东财那条源**：巨潮在未按类别查询时 `announcementTypeName`
   是 `null`，此时该字段留空（不编一个类型出来）。东财优先正是为了拿到它。
3. **两个新源键未登记进时效契约**（`contract.yaml`）：`symbol_announcements` 沿用此前
   状态，`index_quotes` 同样。后果是 `data_freshness_check` 会把它们标成「未登记在时效
   契约」，且按默认分类（`calendar_anchored`/`informational`）判定；工具自身的
   `ToolResult.freshness` 不受影响。登记会改变全局 `freshness` 池的源集合，
   故本轮没有顺手动它。
4. **动作类只接通了自选**：`chat_service.build_action_executor` 现在真的被传进
   `run_turn`（此前从未传过，「确认」在生产里只会回「未配置执行器」）。自选可执行；
   价格阈值预警与深度分析**如实拒绝并说明原因**——平台没有对应服务入口，硬造会创建
   一张没人消费的预警表。
5. **L3 的三个提交入口已抽出**（`analysis_job_service.submit_deep_analysis /
   submit_daily_review / submit_model_arena_run`）：原先内联在
   `main.py::_start_daily_product_run`、`/v1/analyze`、`/v1/model-arena/runs` 里的
   "建任务记录 → 推 `job.created` → 投递后台任务"三步现在只有一份实现，HTTP 路由与
   对话工具共用。**边界据实说明**：内存任务表（`api.main._jobs`）、事件队列、后台任务
   保活与真正的分析管道 `_run_job` 仍留在应用层，通过注入的 `JobRuntime` 进来——
   它们绑在 FastAPI 应用的生命周期上，搬进服务层只会制造一份假的独立实现。

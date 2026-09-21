## Why

平台已经是一台成熟的「买方研究机器」，但**还不是一个能回答问题的助手**。用户问「某某股票是不是在洗盘」，今天得到的是一行任务回执，不是一个回答。

三条实证：

1. **对话入口是单档任务派发器，不产出回答。** `POST /v1/chat/completions`（`api/main.py:7295`）的行为链是：LLM 抽取股票代码/日期/周期（`tradingagents/graph/intent_parser.py`）→ **无条件**启动 7 分析师完整管道（`api/main.py:7396`）→ 响应 content 只有 `"已启动分析任务：{job_id}"`（`api/main.py:7495`）。没有回答文本、没有结论、没有证据。且「今天主力净流入多少」（1 次数据调用）与「全面分析一下」（7 分析师 + 2 轮辩论 + 三方风控 + 裁判）走**同一条链路**，都是分钟级、同量级 token 成本。

2. **洗盘判别的领域知识已经存在，但以自然语言形式沉睡在提示词里。** `tradingagents/prompts/zh.py:414-420` 已写明「主力净流出 + 急跌 + 缩量 = 洗盘信号（可能是假摔）」，量价分析师提示词包含完整威科夫体系（`prompts/zh.py:429+`，威科夫三大定律见 `:438`）。但它是提示词文本——**不可单测、不可回测、不可被工具调用、不可跨运行校准**，只能靠 LLM 每次都临场重新解释一遍。同一份数据两次运行可能得出不同结论。

3. **工具层存在但不可复用。** `tradingagents/agents/utils/*_tools.py` 有 19 个 `@tool`，但（a）返回值是**预格式化的中文字符串表格**（如 `core_stock_tools.py:19`），外部消费者无法做数值推理；（b）通过 `ToolNode` 硬绑定给固定分析角色（`tradingagents/graph/setup.py:172`），不是通用工具面。若直接把这 19 个工具包成 MCP server，外部 Agent 拿到的是一堆「别人嚼过的中文表格」，而仓库里会出现**第二份工具定义**——这正是本项目路线图已经记录过的失败模式（`analysis_lessons` 110 行无读者、`recommendation_eval_items` 0 行、`/v1/backtest` 无前端调用者）。

同时存在三个**必须诚实承认**的数据缺口，它们决定洗盘判定的真实质量上限：

| 缺口 | 证据 | 后果 |
|---|---|---|
| 无分时/逐笔数据 | `cn_akshare_provider._fetch_hist_df` 全部 `period="daily"`；全仓库无 `stock_zh_a_hist_min_em` | 无法观测「下跌过程中的承接强度」——而这才是洗盘最直接的证据 |
| 无筹码分布 | 全仓库无 `cyq` / 筹码 / 成本分布检索命中 | 缺失获利盘比例、平均成本、筹码集中度——判别洗盘/派发最有效的变量之一 |
| 无板块相对换手 | 有 `board_spot` / `zt_pool` 但未用于换手对比 | 「缩量」歧义无法消解：既可能是主力锁仓震仓，也可能是**流动性枯竭无人问津的真跌** |

**为什么是现在**：平台已具备把投研经验固化为确定性代码所需的全部前置件——19 个数据工具、三级降级链、freshness 契约（`dataflows/freshness/`）、机读契约块与「解析失败强制 reject / 无契约显式弃权」的成熟护栏文化（`graph/signal_processing.py` 注释直书弃权率指标）、持仓感知与退出引擎（`services/exit_engine_service.py`、`trade_plan_service.py`）、以及完整的 API Token 鉴权设施（`_require_api_user = RequireUser(allow_api_token=True)`，`api/main.py:2961`）。**缺的只是中间那一层**。再往上堆提示词只会加深幻觉，不会提升能力。

## What Changes

- **新增 L1 确定性信号层** `tradingagents/signals/`：纯函数、零 LLM、可单测可回测。覆盖量能、价格行为、位置、相对强度、资金、筹码、分时承接七个维度
- **补齐三个数据缺口**：分时（1/5/15 分钟）、筹码分布（`stock_cyq_em`）、板块相对换手（基于已有 `board_spot` / `zt_pool` 计算中位数）
- **新增 L2 统一工具注册表** `tradingagents/tools/`：`ToolSpec` / `ToolResult` 单一来源；19 个既有 `@tool` 登记并迁移；LangChain 与 MCP 两种适配器从同一份注册表生成
- **新增 L3 意图三档路由**：轻（数据问答，<5s）/ 中（研判问答，10-30s）/ 重（深度分析，沿用现有管道）。不可靠时降级到**中档而非重档**
- **新增对话 Agent 与会话持久化**：tool-calling loop（工具轮数上限 ≤4）+ `POST /v1/chat` **SSE 流式回答**（回答文本 + 工具轨迹 + 判定卡片事件）；新增 `chat_conversations` / `chat_messages` 表
- **新增领域探针 `signal_washout_probe`**：输出**四态判定**（洗盘 / 派发 / 趋势破坏 / 证据不足）+ 置信度 + 逐项证据分解 + **证伪条件**；LLM 只做表达，**不得改动 verdict 与 confidence**
- **新增 `signal_hold_or_exit_probe`**：结合已有持仓（`trade_ledger` / `exit_engine_service`）回答「还能不能拿」
- **新增 MCP 出口 `/mcp`**：`streamable-http` transport，复用现有 `UserTokenDB` API Token 鉴权与用户隔离；只暴露 `signal_*` / `data_*` / `plan_*`，**不暴露 `pipeline_*`**（分钟级任务不适合同步工具）
- **新增判定回填与权重校准闭环**：每次判定落库（含 `score_breakdown` 全量权重）→ T+3 / T+5 回填实际走势 → 统计各维度判别力 → 校准权重。判定成功的操作性定义：**判定后 5 个交易日内创出判定日新高**（洗盘侧）
- **前端**：`ChatCopilotPanel` 从「提交任务」改造为「对话 + 结构化判定卡片」；新增 `VerdictCard` 组件（四态徽章、证据来源表、证伪条件、后续实际走势回看）

## Capabilities

### New Capabilities

- `signal-layer`: 确定性信号计算层——七维度纯函数指标、位置交互调制因子、相对强度与相对换手、信号版本号
- `tool-registry`: `ToolSpec` / `ToolResult` 契约、注册表、LangChain 适配器、MCP 适配器、「无未注册工具」不变量
- `intent-routing`: 轻/中/重三档意图路由、成本与延迟预算、降级策略、显式弃权
- `conversational-agent`: tool-calling loop、SSE 流式回答协议、会话与消息持久化、多轮上下文（持仓/偏好/历史判定）
- `washout-probe`: 洗盘/派发判别矩阵、四态判定、位置交互调制、证伪条件生成、证据锚点
- `mcp-egress`: MCP server 挂载、工具白名单、API Token 鉴权与用户隔离、审计
- `signal-calibration`: 判定落库、T+3/T+5 回填、维度判别力统计、权重校准与版本切换（含离线回放 / 前瞻积累的权限分离与最小样本闸门）
- `architecture-guardrails`: 分层依赖方向、信号层纯函数性、显式组合与显式注入、adapter 隔离传输、Protocol 扩展点、新路由不进入口模块、**「新增功能不修改旧文件」的可验证判据**

### Modified Capabilities

<!-- greenfield — 本变更不修改任何 baseline 需求。
     既有 freshness-contract / freshness-validator 被复用（ToolResult 携带 freshness），
     但其 REQUIREMENTS 不变；consensus-summary / report-quality 的 verdict 口径不变。 -->

无。本变更为纯新增能力；既有 `freshness-contract`、`freshness-validator`、`consensus-summary`、`report-quality` 仅被**复用**（信号层工具输出携带 freshness、判定复用机读契约块风格），其既有需求不变。

## Impact

- **新增包**：`tradingagents/signals/`、`tradingagents/tools/`、`tradingagents/agent/`、`api/routers/`（既有 `api/main.py` 零 `APIRouter`，本变更首次引入路由分层，新路由不再进 `main.py`）
- **新增服务**：`api/services/conversation_service.py`、`api/services/agent_service.py`、`api/services/intent_router.py`、`api/services/signal_verdict_service.py`、`api/mcp_server.py`
- **数据层**：`cn_akshare_provider` 新增分时（`stock_zh_a_hist_min_em`）与筹码（`stock_cyq_em`）取数；`dataflows/interface.py` 新增 vendor 路由项；`freshness/contract.yaml` 新增源注册（intraday 用 `event_driven` / 新增分时锚点规则，chips 用 `calendar_anchored`）
- **持久化**：新增 4 张表 `chat_conversations`、`chat_messages`、`signal_verdicts`、`signal_verdict_outcomes`（`api/database.py`）
- **API**：新增 `POST /v1/chat`（SSE）、`GET /v1/conversations`、`GET /v1/conversations/{id}`、`GET /v1/signals/washout`、`GET /v1/signals/verdicts`、`GET /v1/signals/calibration`、`POST /mcp`；`POST /v1/chat/completions` **保留为兼容入口**并转为走三档路由
- **既有代码改动**：`agents/utils/*_tools.py` 迁移为 registry 注册（保留 thin wrapper 以免破坏现有 graph）；`intent_parser` 复用其股票名解析能力
- **依赖**：新增 `mcp`（官方 Python SDK / FastMCP）；`langchain-mcp-adapters` 仅 MCP Client 阶段（P2）引入
- **前端**：`ChatCopilotPanel.tsx` 流式化改造、新增 `VerdictCard.tsx`、`types/index.ts` 类型扩展、`services/api.ts` SSE 消费
- **测试**：新增信号层单测、`washout_probe` golden set 回归、registry 不变量、路由分层、会话隔离、MCP 鉴权与用户隔离、校准回填
- **合规**：洗盘/派发是对他人意图的推断，所有输出必须概率化表述 + 免责声明（复用既有机制），禁止输出指令式买卖措辞
- **兼容**：新表、新路由、新字段均为增量；`/v1/chat/completions` 语义不变（仍返回 job 回执）以避免破坏既有调用方

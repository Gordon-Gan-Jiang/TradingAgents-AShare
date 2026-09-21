## 1. 数据缺口补齐（P0 · 硬前置）

- [ ] 1.1 `cn_akshare_provider` 新增分时取数：`stock_zh_a_hist_min_em`（1 / 5 / 15 分钟），复用 `AKSHARE_CALL_LOCK` 与信号量
- [ ] 1.2 `cn_akshare_provider` 新增筹码分布取数：`stock_cyq_em`（获利盘比例、平均成本、集中度）
- [ ] 1.3 `dataflows/interface.py` 新增 vendor 路由项 `get_intraday_bars` / `get_chip_distribution`，接入三级降级链
- [ ] 1.4 `freshness/contract.yaml` 新增 `intraday` 与 `chips` 源注册（category / criticality / anchor / grace）
- [ ] 1.5 板块中位数换手计算：基于已有 `board_spot` / `zt_pool`，输出板块中位换手与个股相对换手
- [ ] 1.6 单测：分时/筹码取数的降级路径、空数据、解析失败、freshness 判定、并发锁未回归
- [ ] 1.7 测试：相对换手计算在板块数据缺失时返回 `insufficient_evidence`，不静默回退自身比较
- [ ] 1.8 **实测分时与筹码的历史深度**（本设计唯一未验证项：规划期网络受限，`stock_cyq_em` 实测 `ConnectionError`）。结论决定 D15 的校准路径：有历史 → 完整回放；无历史 → 二者仅前瞻积累，日线路径承担回放
- [ ] 1.9 板块双基准（D16）：行业中位数 primary（复用 `board_spot` 的 `industry`）+ 主线代表板块 secondary（仅当该股在 `mainline_candidates` 中）；实现四类背离组合的判定分支

## 2. 信号层（P0 · 纯新增，可独立合入）

- [ ] 2.1 定义 `signals/types.py`：`SignalResult` / `ScoreComponent` / `Evidence` / `Falsifier` / `Modulator`
- [ ] 2.2 `signals/volume.py`：缩量程度、下跌日量能占比、**反抽量能恢复率**
- [ ] 2.3 `signals/price_action.py`：跌幅速度、下影线占比、日内收复率、均线结构（5/10/20/60）
- [ ] 2.4 `signals/position.py`：累计涨幅、距前高、上涨天数 → `modulator_factor ∈ [0.5, 1.5]`
- [ ] 2.5 `signals/relative.py`：板块/大盘相对强度、相对换手分类（`broad_contraction` / `relative_strength_contraction`）
- [ ] 2.6 `signals/capital.py`：主力净流入、超大单占比、龙虎榜机构席位方向
- [ ] 2.7 `signals/chips.py`：获利盘比例、平均成本、集中度
- [ ] 2.8 `signals/intraday.py`：分时承接强度（下影、尾盘资金方向、跌幅回收率）
- [ ] 2.9 `signals/calibration.py`：权重加载（自库）+ `signal_version` 生成
- [ ] 2.10 单测：纯函数性（同输入同输出、无网络无时钟）、最小窗口不足返回 `insufficient_evidence`、证据锚点完整性
- [ ] 2.11 单测：**无证据的贡献必须被拒绝**（malformed result 拒绝路径）

## 3. 洗盘探针（P0 · 领域核心）

- [ ] 3.1 `signals/washout.py`：八维组合 + 位置调制（乘法，非加法）+ 四态判定
- [ ] 3.2 实现 D5 的调制公式：`score_washout = modulator × (缩量 + 承接 + 相对强度 + 筹码)`，`score_distribution = (1/modulator) × (放量 + 超大单流出 + 破位)`
- [ ] 3.3 实现 D6：`broad_contraction` 禁止为正贡献；板块中位数不可得时降为 `insufficient_evidence`
- [ ] 3.4 高置信标签用**结构条件**：分时承接可得 且 筹码可得 且 margin ≥ 0.30；不满足则不打高置信标签并披露缺失项；仅日线时标注「日线近似」且**保持可校准**
- [ ] 3.5 证伪条件生成：≥2 条，每条含可观测量 + 阈值 + 时间窗，阈值可追溯到已计算观测
- [ ] 3.6 弃权用**结构定义**（D14）：① 四个核心维度（量能/价格行为/位置/相对强度）任一不可得；② `margin = |S_w − S_d| / (S_w + S_d) < 0.15`。**禁止使用绝对分数阈值**
- [ ] 3.6b 置信度用三点锚定映射 `margin → confidence`（`0.15→40`、`0.30→70`、`0.45→85`），并在输出中暴露原始 margin 以便检验映射
- [ ] 3.7 golden set 回归：用历史案例标注（判定后 5 日创新高 = 洗盘成立）建立 `tests/golden/washout_cases.*`
- [ ] 3.8 单测：**同一形态在不同位置必须给出不同判定**（位置交互项的核心断言）
- [ ] 3.9 单测：`broad_contraction` 情形下不得输出 `washout`
- [ ] 3.10 单测：证伪条件数量与可追溯性；`insufficient_evidence` 不携带方向断言

## 4. 工具注册表（P0）

- [ ] 4.1 `tools/spec.py`：`ToolSpec` / `ToolResult` / `Evidence` 契约
- [ ] 4.2 `tools/registry.py`：注册 / 枚举 / JSON Schema 导出 / 调用前 schema 校验 / 同步工具筛选
- [ ] 4.3 `tools/signals.py`：`signal_washout_probe`（**宽工具**：一次返回八维 + peer_context + position_context，中档主路径只需 1 次调用）。`signal_hold_or_exit_probe` **不在 P0**，见第 11 组
- [ ] 4.4 `tools/data.py`：包裹既有数据能力，**新增结构化变体**，`str` 形态保留
- [ ] 4.5 `tools/plan.py`：包裹 `exit_engine_service` / `trade_plan_service`，声明 `requires=("position",)`
- [ ] 4.6 `tools/pipeline.py`：登记 `analyze` 为 `expensive`，标记不进同步面
- [ ] 4.7 将既有 19 个 `@tool` 登记入册，原函数改为 **thin wrapper 委托**（不删、不改行为）
- [ ] 4.8 `tools/adapters/langchain.py`：`ToolSpec` → `StructuredTool`，输出与注册表一致
- [ ] 4.9 测试：**扫描全仓库 `@tool`，断言不存在未注册工具**（除文档化豁免集）
- [ ] 4.10 测试：LangChain 与 MCP 适配器的 name / description / schema 完全一致
- [ ] 4.11 回归：现有 7 分析师管道运行后工具名与输出等价（迁移不改变图行为）

## 5. 意图路由（P0）

- [ ] 5.1 `api/services/intent_router.py`：三档分类 + 实体解析（复用 `intent_parser` 的股票名解析）
- [ ] 5.2 实现 normative 降级：分类失败/超时/低置信 → `medium`；相邻档模糊 → 取更低成本档
- [ ] 5.3 实现追问路径：无标的 → 追问；多候选 → 列出候选待选
- [ ] 5.4 路由记录落库（tier / 置信度 / 解析实体 / 降级原因）
- [ ] 5.5 单测：数据问答不定为 `heavy`；**仅有可解析标的不足以定为 `heavy`**
- [ ] 5.6 单测：分类器异常 → 必为 `medium`，绝不 `heavy`
- [ ] 5.7 测试：tier 分布可查询（用于发现 `heavy` 失控）

## 6. 对话 Agent 与流式回答（P0）

- [ ] 6.1 `api/database.py` 新增 4 张表：`chat_conversations` / `chat_messages` / `signal_verdicts` / `signal_verdict_outcomes`
- [ ] 6.2 `signal_verdicts` 建 `(symbol, as_of, signal_version)` 唯一索引；结构存摘要而非全量原始数据
- [ ] 6.3 `api/services/conversation_service.py`：会话/消息 CRUD + 强制 `user_id` 过滤 + 跨用户拒绝不披露存在性
- [ ] 6.4 `api/services/agent_service.py`：tool-calling loop，硬性上限 **3 轮 / 5 次调用**，与 token 预算
- [ ] 6.4b **middleware 链**（D19）：`BudgetGuard` / `ExpensiveToolGuard` / `NoProgressGuard`（第 2 轮后无新证据即终止）/ `VerdictGuard` / `DisclaimerInjector` / `TelemetrySink`，每个独立可测；**loop 本体不得内联这些逻辑**
- [ ] 6.4c `EventSink` 抽象：`SseEventSink` / `BufferingEventSink`（测试）/ `CliEventSink`；`llm` 与 `clock` 作为参数注入（**不读全局 config**）
- [ ] 6.5 SSE 事件协议：`answer.delta` / `answer.done` / `tool.start` / `tool.done` / `verdict` / `notice` / `error` / `end`
- [ ] 6.6 循环内**拒绝调用 `expensive` 工具**，改为提示用户显式确认启动重档
- [ ] 6.7 超预算/超轮数：返回已获证据 + 明示截断，**不静默升级档位**
- [ ] 6.8 **判定护栏**：出站校验 state 与置信度一致性，违规拒绝或修复
- [ ] 6.9 多轮上下文：解析持仓（`trade_ledger` / 导入持仓）、偏好（`model_profile` / 风险偏好）、历史判定；解析不到按 `unknown`，不假设默认持仓
- [ ] 6.10 撤销：客户端断开 → 停止循环、释放模型调用、按 `cancelled` 记账（不落为 completed）
- [ ] 6.11 免责声明注入 + 禁止指令式措辞的出站检查
- [ ] 6.12 轮次记账：tier / model / 工具名 / token / 延迟落库，可按 tier 归因成本
- [ ] 6.13 **引入路由分层**：新增 `api/routers/chat.py`、`api/routers/signals.py`、`api/routers/conversations.py`；`api/main.py` 只 `include_router`（**新端点不得写进 main.py**）；`POST /v1/chat/completions` 改为经路由但**响应结构保持不变**
- [ ] 6.14 测试：流式响应**不得只返回任务回执**；终止事件在成功/失败/取消下都必须发出
- [ ] 6.15 测试：模型试图改判定 → 被拦截；置信度不得被夸大
- [ ] 6.16 测试：会话跨用户隔离、消息顺序稳定、取消记账

## 7. 判定持久化与校准闭环（P0 落库 / P1 统计）

- [ ] 7.1 `api/services/signal_verdict_service.py`：判定落库（含全量权重快照与 `signal_version`）
- [ ] 7.2 测试：**权重快照可重放**——用存下的观测 + 权重重算得分与存档一致
- [ ] 7.3 回填任务挂载到 `scheduled_service`：T+3 / T+5（**按交易日计数**）
- [ ] 7.4 停牌/退市/无数据 → `evaluable=false` + 原因，**不计命中/未命中**
- [ ] 7.5 回填幂等：重复运行不产生重复行、不覆盖既有结果
- [ ] 7.6 操作性定义落地：`washout` / `distribution` / `trend_break` 三套兑现判定；弃权排除出方向准确率
- [ ] 7.7 维度判别力统计：样本数、各态命中率、贡献与结果的关联度
- [ ] 7.8 样本低于门槛 → 报告 `insufficient sample`，**不给点估计**
- [ ] 7.8b **离线回放 harness**（D15）：对历史 `(symbol, as_of)` 逐点跑探针（**只用 as_of 之前的数据**，杜绝前视偏差），落库为 replay 样本；日线经 `route_to_vendor("get_stock_data", ...)` 实取（`market_daily_prices` 仅 488 只 / 6 个月，不够）
- [ ] 7.8c 回放样本目标 **≥500**，覆盖 ≥100 只股票 × 上涨/震荡/下跌三种市场状态
- [ ] 7.8d **弃权率**作为一级指标接入监控，目标区间 **30%–50%**；< 20% 报警（"在硬凑结论"），> 70% 报警（"门控过严"）
- [ ] 7.8e **标定协议**：扫 margin 阈值 → hit-rate vs 弃权率曲线 → 取平台区拐点 → ±20% 扰动稳定性检验；**无平台区则判定维度集无判别力，不得进入调参**
- [ ] 7.9 **最小样本闸门**（D15）：**≥100 个已评估的前瞻判定**，且本次改动的每个维度各自 **≥30** 个可用样本；不满足则拒绝改权重并报告样本量 vs 门槛
- [ ] 7.9b **时间切分**：样本按时间排序，前 70% 调参、后 30% 报告；**断言不使用随机切分**（金融数据自相关会泄漏）
- [ ] 7.9c **离线/前瞻权限分离**：离线回放样本**只能否决维度**；**激活权重必须由前瞻样本支持**（同一份代码既选样本又调参 = 过拟合）
- [ ] 7.10 权重版本化激活：新 `signal_version` + 记录样本量与变更理由；旧版本保留可对照
- [ ] 7.11 测试：历史判定与历史结果**永不重写**；定义变更只对新评估生效
- [ ] 7.12 `GET /v1/signals/calibration` 输出版本 / 样本量 / 弃权率 / 各水平准确率 / 维度统计
- [ ] 7.13 文档与产品面**禁止呈现未经实测的数字**（与仓库既有的据实记录文化一致）

## 8. MCP 出口（P1）

- [ ] 8.1 引入官方 `mcp` Python SDK（FastMCP）
- [ ] 8.2 `api/mcp_server.py`：`streamable-http` endpoint + **与 FastAPI lifespan 合并**（禁止裸 `app.mount`）
- [ ] 8.3 鉴权桥接：复用 `_require_api_user` / `UserTokenDB`；无 Token 拒绝、吊销 Token 拒绝、无匿名读路径
- [ ] 8.4 白名单：只暴露同步只读工具；`pipeline_*` 与写操作**不可达**
- [ ] 8.5 直接按名调用被排除工具 → 显式 not-exposed 错误
- [ ] 8.6 `tools/adapters/mcp.py`：注册表名即 MCP 名，无 sanitize 映射；断言命名仅含 `[a-zA-Z0-9_-]`
- [ ] 8.7 延迟预算与失败语义：超时返回结构化错误并指明慢源；上游不可用返回结构化错误/降级结果，**不得静默空成功**
- [ ] 8.8 结果携带 freshness 与免责声明
- [ ] 8.9 调用审计日志（工具名 / 用户 / 时长 / 结果，**不记录凭证**）+ 聚合用量可查
- [ ] 8.10 测试：用户隔离（持仓类工具只解析本用户、判定历史只返回本人、跨用户标识被拒且不披露存在性）
- [ ] 8.11 测试：**既有 HTTP 路由在挂载后行为不变**
- [ ] 8.12 端到端：在真实 MCP 客户端（Claude Desktop / Cursor）跑通「帮我看看 600519 是不是在洗盘」

## 9. 前端（P1）

- [ ] 9.1 `frontend/src/types/index.ts` 扩展：会话、消息、SSE 事件、判定卡片类型
- [ ] 9.2 `frontend/src/services/api.ts`：`/v1/chat` SSE 消费（含中断与重连）
- [ ] 9.3 新增 `VerdictCard.tsx`：四态徽章、置信度、八维证据表（含来源与 `as_of`）、**证伪条件**、日线近似/缺失数据提示
- [ ] 9.4 `ChatCopilotPanel.tsx` 流式化：逐字回答 + 工具轨迹 + 判定卡片；保留「启动深度分析」为显式动作，旧任务事件流复用为重档视图
- [ ] 9.5 判定后续走势回看（T+3 / T+5 结果，含 `evaluable=false` 的据实标注）
- [ ] 9.6 免责声明在判定卡片与对话视图可见
- [ ] 9.7 `npx tsc --noEmit` 通过（exit 0）

## 10. 验收与回归

- [ ] 10.1 端到端验收：问「600519 是不是在洗盘」→ **15 秒内**返回结构化判定 + 证据 + 证伪条件
- [ ] 10.2 验收：单次中档成本 ≤ 重档的 1/10（用 6.12 的记账数据实测，**不得估算**）
- [ ] 10.3 验收：LLM 无法篡改 `verdict` / `confidence`（护栏测试为证）
- [ ] 10.4 验收：判定记录可在前端回看后续实际走势
- [ ] 10.5 全套 pytest 回归；新增失败必须逐一核实来源，**不得以改测试掩盖产品问题**
- [ ] 10.6 既有已知失败（`test_api_smoke.py` 3 个、`test_intent_parser.py` 3 个、`test_report_recovery.py` 2 个）确认未被本变更扩大
- [ ] 10.7 数据库重启后端验证新建表生效（同 `trade_plans` / `trade_ledger` 的既有注意项）
- [ ] 10.8 文档：`docs/` 设计说明与 `README.md` 能力清单同步；`specpower validate` 通过

## 11. 代码架构与解耦护栏（贯穿 P0 · D19）

- [ ] 11.1 **端口层**：`signals/ports.py` 用 `Protocol` 定义窄端口（`DailyBarsPort` / `IntradayBarsPort` / `ChipPort` / `CapitalFlowPort` / `PeerPort`），每个只声明消费方需要的方法；**不得扩展 `BaseMarketDataProvider`**、不得给它加 abstractmethod
- [ ] 11.2 **显式注入**：权重 / 端口 / 时钟全部作为参数传入；新代码**不得读写 `dataflows/config.py` 的全局 config**
- [ ] 11.3 **维度显式有序元组** `DIMENSIONS: tuple[Dimension, ...]`，**不用全局可变注册表**（pytest 并发会互相污染）
- [ ] 11.4 **探针协议** `Probe` + 统一 `ProbeResult`；持久化/回填/校准/前端卡片全部只认 `ProbeResult`
- [ ] 11.5 **adapter 隔离**：`tools/adapters/langchain.py` 与 `mcp.py` 互不 import，都只认 `ToolSpec`；`signals/` **不得引用 `ToolSpec`**（转换只在 `tools/signals.py`）
- [ ] 11.6 **组合根** `tradingagents/tools/__init__.py::build_registry(ports_factory)`：显式组装，**不用装饰器自动注册**
- [ ] 11.7 **路由降级用包装器** `DegradingClassifier`，不散落 try/except；分类器可组合（LLM / 规则 / 降级）
- [ ] 11.8 `tests/test_architecture_boundaries.py::test_signals_has_no_io_or_llm_imports`（C3：禁 requests/akshare/baostock/openai/anthropic/langchain_core/datetime.now/time.time）
- [ ] 11.9 `test_tradingagents_never_imports_api`（C2：锁死既有干净边界）
- [ ] 11.10 `test_new_routes_not_added_to_main`（C4）+ `test_every_langchain_tool_is_registered`（D1）
- [ ] 11.11 `test_dimension_order_declared_in_one_place`：新增维度只需改一处声明
- [ ] 11.12 测试：同输入两次评估结果**逐字节相同**（纯函数性），逐维度断言
- [ ] 11.13 测试：并发两个不同 `signal_version` + 不同模型配置的评估**互不干扰**

## 12. 持仓数据前置（P0.5 · D17）

- [ ] 12.1 **现状确认**：`trade_ledger` 实测 **0 行**，持仓仅 `imported_portfolio_positions` 390 行静态快照（无成交流水、成本不随加减仓更新、T+1 可卖数量不跟踪）
- [ ] 12.2 解决持仓数据来源：台账录入入口（前端）与/或导入增强，使成本与可卖数量可维护
- [ ] 12.3 仅在此之后，把 `washout` verdict 作为 **`exit_engine_service` 的新触发信号**接入（**不新建 `hold_or_exit_probe`**，复用其既有 102 个测试）
- [ ] 12.4 端到端：verdict → 退出决策 → 推送；复用 `test_exit_lifecycle_integration.py` 的链路断言
- [ ] 12.5 在 12.2 完成前，P0 阶段所有判定按 `unknown` 持仓处理，**不得在界面或文案上暗示持仓感知已通**

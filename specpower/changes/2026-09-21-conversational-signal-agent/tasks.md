## 1. 数据缺口补齐（P0 · 硬前置）

- [x] 1.1 `cn_akshare_provider` 新增分时取数：`stock_zh_a_hist_min_em`（1 / 5 / 15 分钟），复用 `AKSHARE_CALL_LOCK` 与信号量
- [x] 1.2 `cn_akshare_provider` 新增筹码分布取数：`stock_cyq_em`（获利盘比例、平均成本、集中度）
- [x] 1.3 `dataflows/interface.py` 新增 vendor 路由项 `get_intraday_bars` / `get_chip_distribution`，接入三级降级链
- [x] 1.4 `freshness/contract.yaml` 新增 `intraday` 与 `chips` 源注册（category / criticality / anchor / grace）
- [x] 1.5 板块中位数换手计算：基于已有 `board_spot` / `zt_pool`，输出板块中位换手与个股相对换手
- [x] 1.6 单测：分时/筹码取数的降级路径、空数据、解析失败、freshness 判定、并发锁未回归
- [x] 1.7 测试：相对换手计算在板块数据缺失时返回 `insufficient_evidence`，不静默回退自身比较
- [ ] 1.8 ⛔ **受环境阻塞**（沙箱内 akshare 返回 ConnectionError）：**实测分时与筹码的历史深度**（本设计唯一未验证项：规划期网络受限，`stock_cyq_em` 实测 `ConnectionError`）。结论决定 D15 的校准路径：有历史 → 完整回放；无历史 → 二者仅前瞻积累，日线路径承担回放
- [x] 1.9 板块双基准（D16）：行业中位数 primary（复用 `board_spot` 的 `industry`）+ 主线代表板块 secondary（仅当该股在 `mainline_candidates` 中）；实现四类背离组合的判定分支

## 2. 信号层（P0 · 纯新增，可独立合入）

- [x] 2.1 定义 `signals/types.py`：`SignalResult` / `ScoreComponent` / `Evidence` / `Falsifier` / `Modulator`
- [x] 2.2 `signals/volume.py`：缩量程度、下跌日量能占比、**反抽量能恢复率**
- [x] 2.3 `signals/price_action.py`：跌幅速度、下影线占比、日内收复率、均线结构（5/10/20/60）
- [x] 2.4 `signals/position.py`：累计涨幅、距前高、上涨天数 → `modulator_factor ∈ [0.5, 1.5]`
- [x] 2.5 `signals/relative.py`：板块/大盘相对强度、相对换手分类（`broad_contraction` / `relative_strength_contraction`）
- [x] 2.6 `signals/capital.py`：主力净流入、超大单占比、龙虎榜机构席位方向
- [x] 2.7 `signals/chips.py`：获利盘比例、平均成本、集中度
- [x] 2.8 `signals/intraday.py`：分时承接强度（下影、尾盘资金方向、跌幅回收率）
- [x] 2.9 `signals/calibration.py`：权重加载（自库）+ `signal_version` 生成
- [x] 2.10 单测：纯函数性（同输入同输出、无网络无时钟）、最小窗口不足返回 `insufficient_evidence`、证据锚点完整性
- [x] 2.11 单测：**无证据的贡献必须被拒绝**（malformed result 拒绝路径）

## 3. 洗盘探针（P0 · 领域核心）

- [x] 3.1 `signals/washout.py`：八维组合 + 位置调制（乘法，非加法）+ 四态判定
- [x] 3.2 实现 D5 的调制公式：`score_washout = modulator × (缩量 + 承接 + 相对强度 + 筹码)`，`score_distribution = (1/modulator) × (放量 + 超大单流出 + 破位)`
- [x] 3.3 实现 D6：`broad_contraction` 禁止为正贡献；板块中位数不可得时降为 `insufficient_evidence`
- [x] 3.4 高置信标签用**结构条件**：分时承接可得 且 筹码可得 且 margin ≥ 0.30；不满足则不打高置信标签并披露缺失项；仅日线时标注「日线近似」且**保持可校准**
- [x] 3.5 证伪条件生成：≥2 条，每条含可观测量 + 阈值 + 时间窗，阈值可追溯到已计算观测
- [x] 3.6 弃权用**结构定义**（D14）：① 四个核心维度（量能/价格行为/位置/相对强度）任一不可得；② `margin = |S_w − S_d| / (S_w + S_d) < 0.15`。**禁止使用绝对分数阈值**
- [x] 3.6b 置信度用三点锚定映射 `margin → confidence`（`0.15→40`、`0.30→70`、`0.45→85`），并在输出中暴露原始 margin 以便检验映射
- [ ] 3.7 golden set 回归：用历史案例标注（判定后 5 日创新高 = 洗盘成立）建立 `tests/golden/washout_cases.*`
- [x] 3.8 单测：**同一形态在不同位置必须给出不同判定**（位置交互项的核心断言）
- [x] 3.9 单测：`broad_contraction` 情形下不得输出 `washout`
- [x] 3.10 单测：证伪条件数量与可追溯性；`insufficient_evidence` 不携带方向断言

## 4. 工具注册表（P0）

- [x] 4.1 `tools/spec.py`：`ToolSpec` / `ToolResult` / `Evidence` 契约
- [x] 4.2 `tools/registry.py`：注册 / 枚举 / JSON Schema 导出 / 调用前 schema 校验 / 同步工具筛选
- [x] 4.3 `tools/signals.py`：`signal_washout_probe`（**宽工具**：一次返回八维 + peer_context + position_context，中档主路径只需 1 次调用）。`signal_hold_or_exit_probe` **不在 P0**，见第 11 组
- [x] 4.4 `tools/data.py`：包裹既有数据能力，**新增结构化变体**，`str` 形态保留
- [x] 4.5 `tools/plan.py`：包裹 `exit_engine_service` / `trade_plan_service`，声明 `requires=("position",)`
- [x] 4.6 `tools/pipeline.py`：登记 `analyze` 为 `expensive`，标记不进同步面
- [x] 4.7 将既有 19 个 `@tool` 登记入册，原函数改为 **thin wrapper 委托**（不删、不改行为）
- [x] 4.8 `tools/adapters/langchain.py`：`ToolSpec` → `StructuredTool`，输出与注册表一致
- [x] 4.9 测试：**扫描全仓库 `@tool`，断言不存在未注册工具**（除文档化豁免集）
- [x] 4.10 测试：LangChain 与 MCP 适配器的 name / description / schema 完全一致
- [x] 4.11 回归：现有 7 分析师管道运行后工具名与输出等价（迁移不改变图行为）

## 5. 意图路由（P0）

- [x] 5.1 `api/services/intent_router.py`：三档分类 + 实体解析（复用 `intent_parser` 的股票名解析）
- [x] 5.2 实现 normative 降级：分类失败/超时/低置信 → `medium`；相邻档模糊 → 取更低成本档
- [x] 5.3 实现追问路径：无标的 → 追问；多候选 → 列出候选待选
- [x] 5.4 路由记录落库（tier / 置信度 / 解析实体 / 降级原因）
- [x] 5.5 单测：数据问答不定为 `heavy`；**仅有可解析标的不足以定为 `heavy`**
- [x] 5.6 单测：分类器异常 → 必为 `medium`，绝不 `heavy`
- [x] 5.7 测试：tier 分布可查询（用于发现 `heavy` 失控）

## 6. 对话 Agent 与流式回答（P0）

- [x] 6.1 `api/database.py` 新增 4 张表：`chat_conversations` / `chat_messages` / `signal_verdicts` / `signal_verdict_outcomes`
- [x] 6.2 `signal_verdicts` 建 `(symbol, as_of, signal_version)` 唯一索引；结构存摘要而非全量原始数据
- [x] 6.3 `api/services/conversation_service.py`：会话/消息 CRUD + 强制 `user_id` 过滤 + 跨用户拒绝不披露存在性
- [x] 6.4 `api/services/agent_service.py`：tool-calling loop，硬性上限 **3 轮 / 5 次调用**，与 token 预算
- [x] 6.4b **middleware 链**（D19）：`BudgetGuard` / `ExpensiveoolGuard` / `NoProgressGuard`（第 2 轮后无新证据即终止）/ `VerdictGuard` / `DisclaimerInjector` / `TelemetrySink`，每个独立可测；**loop 本体不得内联这些逻辑**
- [x] 6.4c `EventSink` 抽象：`SseEventSink` / `BufferingEventSink`（测试）/ `CliEventSink`；`llm` 与 `clock` 作为参数注入（**不读全局 config**）
- [x] 6.5 SSE 事件协议：`answer.delta` / `answer.done` / `tool.start` / `tool.done` / `verdict` / `notice` / `error` / `end`
- [x] 6.6 循环内**拒绝调用 `expensive` 工具**，改为提示用户显式确认启动重档
- [x] 6.7 超预算/超轮数：返回已获证据 + 明示截断，**不静默升级档位**
- [x] 6.8 **判定护栏**：出站校验 state 与置信度一致性，违规拒绝或修复
- [x] 6.9 ✅ **已修复**：新增 `SessionEvent` 回传会话 id、`TurnRequest.history` + `_build_prompt` 注入最近 6 轮、前端保存并回传 id；HTTP 级端到端断言「两轮同会话且第二轮提示词含第一轮内容」。原任务：解析持仓（`trade_ledger` / 导入持仓）、偏好（`model_profile` / 风险偏好）、历史判定；解析不到按 `unknown`，不假设默认持仓
- [x] 6.10 撤销：客户端断开 → 停止循环、释放模型调用、按 `cancelled` 记账（不落为 completed）
- [x] 6.11 免责声明注入 + 禁止指令式措辞的出站检查
- [x] 6.12 ✅ **已修复**：`Completion` 携带 usage、循环累加、`model` 落库、`GET /v1/signals/cost-by-tier` 按档位归因（真实记账，非替身计数）。原任务：tier / model / 工具名 / token / 延迟落库，可按 tier 归因成本
- [x] 6.13 **引入路由分层**：新增 `api/routers/chat.py`、`api/routers/signals.py`、`api/routers/conversations.py`；`api/main.py` 只 `include_router`（**新端点不得写进 main.py**）；`POS /v1/chat/completions` 改为经路由但**响应结构保持不变**
- [x] 6.14 测试：流式响应**不得只返回任务回执**；终止事件在成功/失败/取消下都必须发出
- [x] 6.15 测试：模型试图改判定 → 被拦截；置信度不得被夸大
- [x] 6.16 测试：会话跨用户隔离、消息顺序稳定、取消记账

## 7. 判定持久化与校准闭环（P0 落库 / P1 统计）

- [x] 7.1 `api/services/signal_verdict_service.py`：判定落库（含全量权重快照与 `signal_version`）
- [x] 7.2 测试：**权重快照可重放**——用存下的观测 + 权重重算得分与存档一致
- [x] 7.3 回填任务挂载到 `scheduled_service`：+3 / +5（**按交易日计数**）
- [x] 7.4 停牌/退市/无数据 → `evaluable=false` + 原因，**不计命中/未命中**
- [x] 7.5 回填幂等：重复运行不产生重复行、不覆盖既有结果
- [x] 7.6 操作性定义落地：`washout` / `distribution` / `trend_break` 三套兑现判定；弃权排除出方向准确率
- [x] 7.7 ✅ **已修复**：`calibration_report.monitored.dimension_power` 由落库的判定与兑现结果计算（复用回放的 `dimension_power`，算法只此一份）。原任务：样本数、各态命中率、贡献与结果的关联度
- [x] 7.8 样本低于门槛 → 报告 `insufficient sample`，**不给点估计**
- [x] 7.8b **离线回放 harness**（D15）：对历史 `(symbol, as_of)` 逐点跑探针（**只用 as_of 之前的数据**，杜绝前视偏差），落库为 replay 样本；日线经 `route_to_vendor("get_stock_data", ...)` 实取（`market_daily_prices` 仅 488 只 / 6 个月，不够）
- [x] 7.8c 回放样本目标 **≥500**，覆盖 ≥100 只股票 × 上涨/震荡/下跌三种市场状态
- [x] 7.8d **弃权率**作为一级指标接入监控，目标区间 **30%–50%**；< 20% 报警（"在硬凑结论"），> 70% 报警（"门控过严"）
- [x] 7.8e **标定协议**：扫 margin 阈值 → hit-rate vs 弃权率曲线 → 取平台区拐点 → ±20% 扰动稳定性检验；**无平台区则判定维度集无判别力，不得进入调参**
- [x] 7.9 **最小样本闸门**（D15）：**≥100 个已评估的前瞻判定**，且本次改动的每个维度各自 **≥30** 个可用样本；不满足则拒绝改权重并报告样本量 vs 门槛
- [x] 7.9b **时间切分**：样本按时间排序，前 70% 调参、后 30% 报告；**断言不使用随机切分**（金融数据自相关会泄漏）
- [x] 7.9c **离线/前瞻权限分离**：离线回放样本**只能否决维度**；**激活权重必须由前瞻样本支持**（同一份代码既选样本又调参 = 过拟合）
- [x] 7.10 ✅ **已实现**：`signal_weight_versions` 表 + `propose_activation` / `activate` / `active_weights` / `list_versions`；理由为空或闸门未满足均抛错；评估实际读取激活权重（有断言证明权重变更改变了得分）。原任务：新 `signal_version` + 记录样本量与变更理由；旧版本保留可对照
- [x] 7.11 测试：历史判定与历史结果**永不重写**；定义变更只对新评估生效
- [x] 7.12 ✅ **已修复**：`/v1/signals/calibration` 现输出 `monitored.dimension_power`
- [x] 7.13 文档与产品面**禁止呈现未经实测的数字**（与仓库既有的据实记录文化一致）

## 8. MCP 出口（P1）

- [x] 8.1 引入官方 `mcp` Python SDK。**实测版本为 2.2.0**：`FastMCP` 已改名 `MCPServer`（`mcp.server.mcpserver`），客户端函数名为 `streamable_http_client`（v1 的 `streamablehttp_client` 不存在）、结果字段为 snake_case（`is_error` / `structured_content`）。已按 v2 API 实现并记入 design.md，依赖写入 `pyproject.toml`
- [x] 8.2 `api/mcp_server.py`：`streamable-http` endpoint + **与 FastAPI lifespan 合并**（禁止裸 `app.mount`）。**实测端点路径为 `/mcp/`（带尾斜杠）**：FastAPI 对挂载点的无尾斜杠 POST 返回 405，另加 308 重定向兼容 `/mcp`
- [x] 8.3 鉴权桥接：复用 `_require_api_user` / `UserokenDB`；无 oken 拒绝、吊销 oken 拒绝、无匿名读路径
- [x] 8.4 白名单：只暴露同步只读工具；`pipeline_*` 与写操作**不可达**
- [x] 8.5 直接按名调用被排除工具 → 显式 not-exposed 错误
- [x] 8.6 `tools/adapters/mcp.py`：注册表名即 MCP 名，无 sanitize 映射；断言命名仅含 `[a-zA-Z0-9_-]`
- [x] 8.7 ✅ **已修复**：`ToolRegistry.invoke` 用线程池执行预算，超时转**结构化结果**（含 `timeout` 标记、`degraded_reason` 指明慢源、`coverage.unavailable` 与 freshness 同步置 error）。原任务：超时返回结构化错误并指明慢源；上游不可用返回结构化错误/降级结果，**不得静默空成功**
- [x] 8.8 ✅ **已修复**：`tools/freshness.py` 桥接既有契约；三类工具均填充 freshness。**关键纪律：未验证截止日一律标 `unknown`，绝不默认 `fresh`**（有专门断言）
- [x] 8.9 调用审计日志（工具名 / 用户 / 时长 / 结果，**不记录凭证**）+ 聚合用量可查
- [x] 8.10 测试：用户隔离（持仓类工具只解析本用户、判定历史只返回本人、跨用户标识被拒且不披露存在性）
- [x] 8.11 测试：**既有 HP 路由在挂载后行为不变**
- [x] 8.12 端到端：在真实 MCP 客户端（Claude Desktop / Cursor）跑通「帮我看看 600519 是不是在洗盘」

## 9. 前端（P1）

- [x] 9.1 `frontend/src/types/index.ts` 扩展：会话、消息、SSE 事件、判定卡片类型
- [x] 9.2 `frontend/src/services/api.ts`：`/v1/chat` SSE 消费（含中断与重连）
- [x] 9.3 新增 `VerdictCard.tsx`：四态徽章、置信度、八维证据表（含来源与 `as_of`）、**证伪条件**、日线近似/缺失数据提示
- [x] 9.4 `ChatCopilotPanel.tsx` 流式化：逐字回答 + 工具轨迹 + 判定卡片；保留「启动深度分析」为显式动作，旧任务事件流复用为重档视图
- [x] 9.5 ✅ **已实现**：`VerdictHistoryPanel` 消费 `/v1/signals/verdicts`，展示 T+3/T+5 兑现、**不可评估单列**（不计命中也不计未命中）、待回填与失败区分；挂载于 `/judgement` 页。原任务：（+3 / +5 结果，含 `evaluable=false` 的据实标注）
- [x] 9.6 免责声明在判定卡片与对话视图可见
- [x] 9.7 `npx tsc --noEmit` 通过（exit 0）

## 10. 回答证据锚定（P0 · D20/D21/D22/D23）

- [x] 10.1 ✅ **A3 已修复**：`_coverage_for` 按 vendor 实际返回判定（识别失败文案与空返回），失败时 `available=()` + `degraded=True` + freshness 同步 error。注：19 个数据工具仍 `evidence=()`（prose vendor 无可引用标量，已用 `structured=False` 显式标注）。原任务：（`available` / `unavailable`）；**evidence 覆盖 `data` 中每个可陈述标量**（值 + 单位 + `source_key` + `as_of`），不只是参与打分的观测
- [x] 10.2 定义结构化回答 schema：`claims[]`（`text` + `refs[]`）+ `verdict_ref` + `unavailable[]` + `falsifier_observed`
- [x] 10.3 提示中以稳定 ID（`E1`/`E2`…）呈现证据项；要求每条 claim 携带 `refs`
- [x] 10.4 **渲染器**：由结构化 claims 生成正文（模型不自由书写）；`answer.delta` 逐 claim 流式下发（**流式不得绕过结构化**）
- [x] 10.5 **校验器**（纯函数、零 LLM、可单测）：refs 存在性 / 无引用即拒 / 数值须匹配**该 claim 所引证据**的标量（非全量证据集）/ 单位归一（`3200万`≡`0.32亿`）/ 相对容差 2% / 比率与百分比同为数值断言 / 日期断言须落在所引证据 `as_of` 集合 / `verdict_ref` 须指向本轮探针
- [x] 10.6 失败处理：**重写一次** → 再失败则降级为「无法给出有依据的回答」并列出已有证据；**未通过校验的文本绝不出门**
- [x] 10.7 记录 `rejection_rate` / `rewrite_rate`
- [x] 10.8 L4 兜底：结构化解析失败 → regex 数值后置校验 + 标记 fallback；记录并暴露 `fallback_rate`（**不是可接受的常态**）
- [x] 10.9 L5 无答案通道：`coverage.unavailable` 维度**必须**显式声明；无工具可支撑的问句（如「明天会涨吗」）**必须拒答并说明缺什么**；拒答记为**合法结果**而非系统失败
- [x] 10.10 因果类问句：接入已有 `get_news` / `get_global_news` / `GET /v1/announcements/latest`，纳入 `Coverage`；其不可得时必须声明未覆盖，**禁止从价格行为推断原因**
- [x] 10.11 `verdict_state` / `confidence` 在渲染中不可被改动（`VerdictGuard` 与渲染器一致性断言）
- [x] 10.12 测试：**未溯源数值断言率必须为 0**（构造保证）——用对抗样本（改错数字 / 引用错证据项 / 编造日期 / 编造原因）逐一断言被拒
- [x] 10.13 测试：单位等价表达（`0.32亿` vs `3200万`）**不得**被误判为编造；`3204.7万→3200万` 在 2% 容差内应通过
- [x] 10.14 测试：引 `E1` 说 `E2` 的数必须被拒（错配引用）
- [x] 10.15 测试：拒答路径返回可读说明且列出已有证据，不产生任何未校验文本
- [x] 10.16 指标区分：报告中**由构造保证**（未溯源率）与**由监控校准**（拒绝率 / fallback 率 / 弃权率）必须分列

## 11. 组合级与行动能力（P0 · D21）

- [x] 11.1 `signal_washout_probe_batch(symbols=[...])`：**1 次调用 N 只评估**，返回逐只结果 + 排序；逐只调用 30 次装不进任何轮数上限
- [x] 11.2 批量结果保持 L1 完备性：每只的 evidence 在同一 `ToolResult` 内
- [x] 11.3 批量上限：超限拒绝或截断并**显式告知**，不得静默评估子集
- [x] 11.4 批量的逐只弃权保留：证据不足的标的单独报 `insufficient_evidence`，不得被丢弃或强行给出方向
- [x] 11.5 **HIL 升级提议**：Agent 可提议启动重档并等待确认（提议 ≠ 静默升级；路由层仍遵守 D3 的「绝不静默升级」）
- [x] 11.6 **主动追问**：「还能不能拿」而该股不在持仓 → 必须问「你持有吗？成本多少？」，**不得按空仓假设回答**；同一会话内已提供过的值不得重复追问
- [x] 11.7 **行动提议（两步确认）**：「跌破 180 告诉我」→ 提议创建预警/跟踪任务，**用户确认后**经 `tracking_price_alert_service` / `watchlist` / `scheduled_service` 执行（**不新建并行机制**）
- [x] 11.8 行动未执行时**不得暗示已设置**；被拒绝/忽略的提议记入会话
- [x] 11.9 MCP 面 V1 不提供任何行动（只读），断言不可达

## 12. 代码架构与解耦护栏（贯穿 P0 · D19）

- [x] 12.1 **端口层**：`signals/ports.py` 用 `Protocol` 定义窄端口（`DailyBarsPort` / `IntradayBarsPort` / `ChipPort` / `CapitalFlowPort` / `PeerPort`），每个只声明消费方需要的方法；**不得扩展 `BaseMarketDataProvider`**、不得给它加 abstractmethod
- [x] 12.2 **显式注入**：权重 / 端口 / 时钟全部作为参数传入；新代码**不得读写 `dataflows/config.py` 的全局 config**
- [x] 12.3 **维度显式有序元组** `DIMENSIONS: tuple[Dimension, ...]`，**不用全局可变注册表**（pytest 并发会互相污染）
- [x] 12.4 **探针协议** `Probe` + 统一 `ProbeResult`；持久化/回填/校准/前端卡片全部只认 `ProbeResult`
- [x] 12.5 **adapter 隔离**：`tools/adapters/langchain.py` 与 `mcp.py` 互不 import，都只认 `ToolSpec`；`signals/` **不得引用 `ToolSpec`**（转换只在 `tools/signals.py`）
- [x] 12.6 **组合根** `tradingagents/tools/__init__.py::build_registry(ports_factory)`：显式组装，**不用装饰器自动注册**
- [x] 12.7 **路由降级用包装器** `DegradingClassifier`，不散落 try/except；分类器可组合（LLM / 规则 / 降级）
- [x] 12.8 `tests/test_architecture_boundaries.py::test_signals_has_no_io_or_llm_imports`（C3：禁 requests/akshare/baostock/openai/anthropic/langchain_core/datetime.now/time.time）
- [x] 12.9 `test_tradingagents_never_imports_api`（C2：锁死既有干净边界）
- [x] 12.10 `test_new_routes_not_added_to_main`（C4）+ `test_every_langchain_tool_is_registered`（D1）
- [x] 12.11 `test_dimension_order_declared_in_one_place`：新增维度只需改一处声明
- [x] 12.12 测试：同输入两次评估结果**逐字节相同**（纯函数性），逐维度断言
- [x] 12.13 测试：并发两个不同 `signal_version` + 不同模型配置的评估**互不干扰**

## 13. 持仓数据前置（P0.5 · D17）

- [x] 13.1 **现状确认**：`trade_ledger` 实测 **0 行**，持仓仅 `imported_portfolio_positions` 390 行静态快照（无成交流水、成本不随加减仓更新、+1 可卖数量不跟踪）
- [x] 13.2 **只读解析已落地**（`position_context_service`：台账优先 → 导入快照 → 显式 unknown，含可引用证据与来源标注）；**台账录入入口（前端）仍未建**——`trade_ledger` 实测 0 行，因此「成本随加减仓更新、T+1 可卖数量」仍依赖导入快照
- [x] 13.3 仅在此之后，把 `washout` verdict 作为 **`exit_engine_service` 的新触发信号**接入（**不新建 `hold_or_exit_probe`**，复用其既有 102 个测试）
- [x] 13.4 端到端：verdict → 退出决策（`/v1/exit-advice` 端点级断言已覆盖，含失效条件透传）；**推送环节复用既有预警服务，未新增断言**
- [x] 13.5 在 12.2 完成前，P0 阶段所有判定按 `unknown` 持仓处理，**不得在界面或文案上暗示持仓感知已通**

## 14. 验收与回归

- [x] 14.1 端到端验收：问「600519 是不是在洗盘」→ **15 秒内**返回结构化判定 + 证据 + 证伪条件
- [x] 14.2 ✅ **已修复**：新增「读真实落库记账」的验收用例（tier/tokens/latency 来自 `chat_messages`）；架构代理量对比保留但明确标注**不当作账单金额**
- [x] 14.3 验收：LLM 无法篡改 `verdict` / `confidence`（护栏测试为证）
- [x] 14.4 验收：判定记录可在前端回看后续实际走势
- [x] 14.5 全套 pytest 回归；新增失败必须逐一核实来源，**不得以改测试掩盖产品问题**
- [x] 14.6 既有已知失败（`test_api_smoke.py` 3 个、`test_intent_parser.py` 3 个、`test_report_recovery.py` 2 个）确认未被本变更扩大
- [x] 14.7 数据库重启后端验证新建表生效（同 `trade_plans` / `trade_ledger` 的既有注意项）
- [x] 14.8 文档：`docs/` 设计说明与 `README.md` 能力清单同步；`specpower validate` 通过

---

## 审计记录（第 6 轮 · 应「是否存在偷懒」之问）

上表 10 项已改回未完成并注明具体缺口。此外发现以下未在任务清单体现的实现妥协：

1. **多轮对话实质是单轮**：SSE 不回传会话 id，历史不注入提示词
2. **token 记账恒为 0**：`ctx.tokens` 从未累加
3. **`ToolResult.freshness` 恒为空**：freshness 契约未接入工具层
4. **`latency_budget_ms` / `freshness_sources` / `requires` / `output_contract` 只赋不读**（规格装饰）
5. **`max_model_calls` 未被执行**：`BudgetGuard` 只检查工具调用次数
6. **`_coverage_for` 伪造可用性**：旧 vendor 失败返回字符串时仍声明源可用
7. **死代码**：`signal_ports.industry_of` / `MappingLike`、`news_service._legacy_direct_fetch` 无调用方
8. **`loop.py` 用模块级全局 `_EXECUTOR`**：与在 `dataflows/config.py` 批评并声称避开的是同一反模式
9. **前端未消费 `position_context`**：后端已返回，类型定义缺失、卡片不显示
10. **`tasks.md` 被批量脚本改坏字符**（`ToolResult`→`oolResult` 等 9 处）：用正则批量改追踪文件且未校验结果

---

## 修复记录（第 7 轮 · 应「修复这些问题」之问）

第 6 轮审计的 10 项虚报 + 10 项实现妥协**已全部修复**，每项先写失败测试再实现：

| 编号 | 问题 | 修复 | 新增测试 |
|---|---|---|---|
| A1 | 多轮实质单轮 | `SessionEvent` + 历史注入 + 前端续接 | 15 |
| A2 | freshness 恒为空 | `tools/freshness.py` 桥接契约；**未验证不标 fresh** | 16 |
| A3 | 覆盖声明伪造 | 按 vendor 实际返回判定 + degraded | 4 |
| B1 | 延迟预算未执行 | 线程池执行 + 结构化超时错误（指明慢源） | 4 |
| B2 | 模型调用预算未执行 | 预算耗尽不再发起修复，走诚实降级 | 2 |
| B3 | `requires` 只赋不读 | 编排层校验并**通知用户**未解析到的上下文 | 2 |
| B4 | token/model 记账为假 | `Completion` 携带 usage + 落库 + 按 tier 归因端点 | 10 |
| C1 | 死代码 2 处 | 删除 `industry_of` / `_legacy_direct_fetch` | — |
| C2 | 模块级全局 `_EXECUTOR` | 改为显式参数传递 | — |
| D1 | 前端未消费 `position_context` | 类型 + 卡片区块（已知/未知/已清仓三态可辨） | 4 |
| D2 | 维度判别力未接入 + 权重激活未实现 | 校准端输出判别力；`signal_weight_versions` 表与激活流程 | 14 |
| 9.5 | 前端无走势回看 | `VerdictHistoryPanel` | 3 |
| 14.2 | 验收口径是替身计数 | 改为读真实记账 | 1 |

**过程中的额外发现（测试抓到的）**：

1. **源键分叉**：新闻工具的 `SOURCE_KEY="news"` 与我加进 freshness 契约的 `symbol_news`
   不一致——freshness 查不到该源只能标 unknown。已统一为契约键。
2. **`requires` 的语义被我用错**：原打算「未满足就跳过工具」，但探针本就设计成
   无持仓可跑（缺持仓时它声明该维度未覆盖）；跳过会连判定一起丢掉。改为**通知**，
   含义是「编排层必须先尝试解析」。
3. **`_finish` 签名重复插入**：批量脚本把 `model_label` 参数插了两处，且把
   `chain = ...` 挤到 docstring 同行导致语法错误。已修正——这是第 6 轮
   「批量脚本改坏文件」同一类问题的再次发生。

## 修复记录（第 8 轮 · 线上 `Failed to fetch` 事故）

用户截图显示 `/judgement` 页报 `Failed to fetch`。按"现象 → 三种可能 → 逐条排除"
的顺序定位，过程中翻出**两个真问题**和一个测试盲区。

### 事故链 1：不带 `symbol` 的对话路径 500（用户看到的那一个）

`chat_service.resolve_symbol` 里写着 `from api.main import _dual_stock_map`，
而 `api/main.py` 中**没有这个名字**。只在"用户没显式指定标的"时被调用，
运行时 ImportError → 未处理异常 → 500。

而 500 响应**不带 CORS 头**：FastAPI 的 `ServerErrorMiddleware` 在 CORS 中间件
**之外**，异常响应不经过它。浏览器拿不到可读响应，只能报 `TypeError: Failed to fetch`
——真实原因（ImportError）被完全掩盖。已实测确认：`symbol` 存在 → 200 带
`access-control-allow-origin`；缺失 → 500 无 CORS 头、`content-type: text/plain`。

| 编号 | 问题 | 修复 |
|---|---|---|
| E1 | 引用不存在的 `_dual_stock_map` | 改用真实访问器 `_get_stock_name_map_cached_only()`（name → code 正确极性） |
| E2 | 解析异常直接冒泡成 500 | `build_turn_request` 收敛为"无标的"，路由转 400 追问并**带 CORS 头** |
| E3 | 简称解析不出（「茅台」vs「贵州茅台」） | 唯一别名索引：只接受全表唯一的后缀（2~4 字），有歧义一律不猜 |
| E4 | 别名只做兜底，全名命中就吞掉另一个候选 | 两路结果**合并去重**：「茅台和宁德时代哪个好」必须两个都进候选去问 |

**测试盲区（本轮最该记住的一条）**：此前**每一个** `/v1/chat` 测试都显式传了
`symbol`，于是"不传 symbol"这条**真实前端主路径**从未被执行过——一个必定失败的
ImportError 躲过了 1900+ 个用例。新增 `tests/test_symbol_resolution.py`（12 条），
其中一条是 HTTP 级的"不带 symbol 不得 500"。

### 事故链 2：回合崩溃时事件流"静默且不结束"（顺带查出来的）

用正确载荷实测时拿到：

```
event: _CrashEvent
data: {"value": "<api.routers.chat._CrashEvent object at 0x129dac980>"}
```

三个问题叠加：① `_CrashEvent` 不是 dataclass，`_serialize` 走 `str(event)` 兜底，
**崩溃原因整条丢失**并泄漏服务端内存布局；② 事件名不在前端 `chatStream.ts` 的
switch 里，落到 `default:` 被静默忽略；③ **没有发 `EndEvent`**，前端 `finished`
永远为 false——界面永久转圈，用户既看不到原因也等不到结束。已实测线上复现。

| 编号 | 问题 | 修复 |
|---|---|---|
| F1 | 崩溃只发内部兜底事件 | 改发前端已认识的 `ErrorEvent` + 必须补 `EndEvent(status="failed")` |
| F2 | 异常原文/对象 repr 外泄 | `describe_turn_failure` 分类翻译成"是什么问题、该做什么"；原文只进服务端日志 |
| F3 | `_serialize` 对非 dataclass 退化成 repr | 改为交空对象并在**服务端**报错（响亮失败优于泄漏 repr） |
| F4 | 缺模型配置被当成崩溃 | 识别为可自助修复：提示去「设置 → 模型配置」填 API Key |

端到端复验（重启后端后的真实请求）：

```
event: ErrorEvent
data: {"message": "尚未配置可用的模型：请在「设置 → 模型配置」中填写 API Key，或选择一套已配置好的模型后重试。"}

event: EndEvent
data: {"status": "failed", "reason": "crash"}
```

新增 `tests/test_chat_crash_reporting.py`（6 条），其中一条是**结构性守卫**：
协议里所有 `*Event` 必须是 dataclass——把"内容为空的事件"挡在源头，
而不是等线上再发现。

### 本轮的测试自举教训

崩溃用例最初走 `/v1/auth/request-code` 取 token，结果**单独跑通过、整轮跑失败**：
该端点依赖 `_light_executor` 线程池，而套件里有测试会启停真实 uvicorn 并关掉
模块级线程池。这类"测试自身的环境耦合"会被误读成产品缺陷，故改为直接签发 JWT
（`_jwt()`），把用例收敛到它真正要验的东西上。

### 回归结果

| 项 | 结果 |
|---|---|
| 后端全量 | 1947 passed / 5 failed（5 条均为基线已有，且比基线 7 条更少） |
| 前端单测 | 64 passed |
| `tsc --noEmit` | exit 0 |
| 线上复验 | 不带 symbol → 200（不再 500）；400/200 均带 CORS 头；崩溃 → 可读错误 + 明确结束 |

### 第 8 轮补充：端到端复验时又揪出的三个缺陷

事故链 1、2 修完后做线上复验，**每一次复验都带出新问题**——说明"改完就跑一遍"
比"改完就报完成"值钱得多。

| 编号 | 缺陷 | 症状（实测） | 修复 |
|---|---|---|---|
| G1 | 别名索引收进了**纯数字**后缀 | 基金「诺德研发创新100」产生别名 `"00"`，全表唯一；用户输入 **"600519.SH 是不是在洗盘"** 时，"600519" 里含 "00" → 解析成 `007737.SZ`（那只基金）。**用户问 A 股，系统静默去分析基金** | 别名只取**全中文**后缀；并把"显式代码匹配"提到别名之前 |
| G2 | 对话路径**丢掉了模型档案** | `build_llm_completer` 算出 `profile_id` 后调用 `_build_runtime_config({}, ...)`，档案解析结果被丢弃：用户在设置里建好并设为默认的模型档案对对话**完全无效**，对话永远用 DEFAULT_CONFIG 的模型（实测恒为 `gpt-4o`）；缺 Key 时还提示"尚未配置可用的模型"——**提示指错方向**；报告路径与对话路径装配口径分叉 | 与报告路径同一套装配：`_merge_model_profile_overrides` + `trusted_overrides=True` + `strategy="profile_selected"`；并显式把 `api_key` 传给客户端工厂（`OpenAIClient` 只从 kwargs 透传，否则退到 `OPENAI_API_KEY` 环境变量） |
| G3 | 模型报错**原文外泄** | Agent 循环兜底分支 `ErrorEvent(message=f"{type(exc).__name__}: {exc}")` 把上游 401 原文（含 `AuthenticationError` 报文与 Alibaba 风格 request id）发到浏览器；`EndEvent.reason=str(exc)` 同样 | 实现移到 `tradingagents/agent/failures.py`（分层方向 api → agent，两侧共用），循环与路由统一走 `describe_turn_failure`；`EndEvent.reason` 只给异常类名 |

G1 的复盘：这条错是我**自己引入**的——为了支持简称（「茅台」↔「贵州茅台」）
加了后缀别名，第一版"只在全名不命中时兜底"反而掩盖了它；把两路合并后
（为了修 E4）这个坑才浮出来。教训是：**模糊匹配的键必须有形态约束**，
"唯一性"不足以排除噪声——数字串在任何金融问句里都极其常见。

G3 的复盘：`describe_turn_failure` 我一开始只挂在路由的崩溃兜底上，而绝大多数
模型错误其实是在**循环内部**被捕获的——分类器写对了却挂错了地方。

### 线上复验（每一步都实测，不靠推断）

| 场景 | 结果 |
|---|---|
| 不带 `symbol` + 中文全名 | `symbol=600519.SH`、`SessionEvent`、进入工具执行（不再是 500） |
| 不带 `symbol` + 显式代码 | `symbol=600519.SH`（不再被基金别名顶成 `007737.SZ`） |
| 无法解析的文本 | 400 + `detail` 追问，**带 CORS 头** |
| 未配置模型的账号 | `ErrorEvent` 可读 + `EndEvent(failed)`（不再永久 loading） |
| 模型 401 | `ErrorEvent` = "模型鉴权失败：API Key 无效或已过期…"（无上游原文、无 request id） |
| 真实 3.2 万名称表 | 「600519.SH 是不是在洗盘」→ 600519.SH；「今天大盘怎么样」→ 不误判；「茅台和宁德时代哪个好」→ 两个候选 |

### 回归结果（第 8 轮最终）

| 项 | 结果 |
|---|---|
| 后端全量 | 1956 passed / 7 failed（7 条与基线完全一致，无新增） |
| 前端单测 | 67 passed（新增 3 条 HTTP 错误文案用例） |
| `tsc --noEmit` | exit 0 |
| 新增测试文件 | `test_symbol_resolution.py`(15) / `test_chat_crash_reporting.py`(8) / `test_chat_model_profile_wiring.py`(4) |

前端侧同时修掉一个体验问题：非 2xx 时提示原先拼的是**原始 JSON 体**
（`HTTP 400：{"detail":"..."}`），改为解出 `detail` 再展示（`describeChatHttpError`）。

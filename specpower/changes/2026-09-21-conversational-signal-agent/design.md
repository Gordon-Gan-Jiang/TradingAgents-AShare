## Context

**现有对话链路（实测）**：`POST /v1/chat/completions`（`api/main.py:7295`）→ `_extract_chat_text` 取最后一条用户消息 → `_resolve_effective_model_profile_id` / `_merge_model_profile_overrides` 解析用户模型配置 → `_ai_extract_symbol_and_date[_streaming]`（`api/main.py:7193+`，内部使用 `create_llm_client` 做 StockExtract）→ 得到 `symbol / trade_date / horizons / focus_areas / specific_questions` → `_compose_analysis_user_context` 合并导入持仓 → 构造 `AnalyzeRequest` → `_run_job(job_id, analyze_req, ...)` 启动完整 7 分析师管道。流式分支立即返回 `StreamingResponse(_stream_job_events(job_id))`（`api/main.py:7402`），**流的是任务进度事件，不是回答**；非流式分支返回 content 为 `"已启动分析任务：{job_id}"` 的 chat completion 对象（`api/main.py:7495`）。

**结论**：现有入口是**单档任务派发器**。它把「用户想得到一个判断」和「用户想要一份完整研报」当成同一件事，且不产出任何回答文本。

**现有资产**：

| 子系统 | 位置 | 状态 / 对本变更的意义 |
|---|---|---|
| 19 个 LangChain `@tool` | `tradingagents/agents/utils/*_tools.py` | 返回值均为 `str`（格式化中文表格），经 `ToolNode` 硬绑定分析角色（`graph/setup.py:172`） |
| 数据源链 | `tradingagents/dataflows/providers/`（akshare / 东财直连 / 新浪三级降级）、`interface.route_to_vendor` | 稳定，`AKSHARE_CALL_LOCK` + 信号量并发控制 + 僵尸线程回收 |
| Freshness 契约 | `tradingagents/dataflows/freshness/`（contract.yaml / anchor / validator / aggregator） | 四类 taxonomy、analysis_mode 相对锚点、grace 窗口、confidence 双轨封顶——**信号层工具直接复用** |
| 机读契约块与护栏 | `prompts/zh.py`（VERDICT / RISK_JUDGE / ANALYST_JSON / CRITIC_RESULT）、`agents/utils/direction.py`、`graph/signal_processing.py` | 「解析失败强制 reject」「无契约显式弃权并计入弃权率」——**本变更的护栏设计直接沿用这套哲学** |
| 领域知识（洗盘） | `prompts/zh.py:414-420`（主力净流出+急跌+缩量=洗盘）、`prompts/zh.py:429+` 与 `:438`（量价 / 威科夫三大定律） | 知识已存在，但**以自然语言沉睡**，不可单测、不可回测、不可被调用 |
| 持仓与退出 | `api/services/exit_engine_service.py`、`trade_plan_service.py`、`trade_ledger_service.py`、`TradePlanDB` / `TradeLedgerDB` | 持仓感知链路已通（`_compose_analysis_user_context`） |
| 判定结果回填 | `ReportT1OutcomeDB`、`T1DailyStatDB`、`RecommendationEvalRunDB` | 有 T+1 回填机制，但**口径限于 T+1 单日**（路线图 §11.3 已自陈这是归因质量上限） |
| API Token 鉴权 | `_require_api_user = RequireUser(allow_api_token=True)`（`api/main.py:2961`）、`UserTokenDB`、`/v1/tokens` | **MCP 出口可直接复用，零新增鉴权代码** |
| 规范流程 | `specpower/specs/`（13 个 baseline capability）、`tests/` 69 个测试文件 | 本变更按 specpower 流程产出 |

**三个数据缺口（决定质量上限）**：

1. `cn_akshare_provider._fetch_hist_df` 全部 `period="daily"`（`cn_akshare_provider.py:944+`），全仓库无 `stock_zh_a_hist_min_em` → 无分时/逐笔
2. 全仓库无 `cyq` / 筹码 / 成本分布命中 → 无获利盘比例、平均成本、集中度
3. 有 `board_spot` / `zt_pool` 但未用于换手对比 → 「缩量」歧义无法消解

**为什么现在**：把投研经验固化为代码所需的**全部前置件已就绪**（数据工具、降级链、freshness 契约、契约块文化、持仓链路、Token 鉴权）。缺的只是中间那层确定性信号。继续堆提示词只会放大同一份不确定性。

---

## Goals / Non-Goals

**Goals:**

1. 让「XX 是不是在洗盘」这类问题在 **15 秒内**得到**结构化判定 + 逐项证据 + 证伪条件**，而不是一行任务回执
2. 把 `prompts/zh.py:414-420` 的洗盘判别规则从提示词文本**迁移为可单测、可回测的确定性代码**，并补齐它必需的分时、筹码、相对换手三类数据
3. 建立**单一来源工具注册表**，使内部对话 Agent、现有 LangGraph 分析师、MCP 外部消费者共享同一份工具定义，从架构上排除「双份定义漂移」
4. 引入**三档意图路由**，使数据问答与研判问答不再付出重档成本（目标：中档成本 ≤ 重档的 1/10）
5. 把平台能力通过 **MCP 出口**变成生态可调用项（在 Claude / Cursor / DSH 中直接问股票），复用既有 Token 鉴权与用户隔离
6. 建立**判定 → T+3/T+5 回填 → 维度判别力统计 → 权重版本化**的校准闭环，使判别权重来自实测而非提示词拍板
7. 让「无法判断」成为一个**被显式报告的产品结果**（`insufficient_evidence`），而不是被 LLM 悄悄转成一个观点

**Non-Goals:**

- 不改造现有 7 分析师深度管道本身（`graph/setup.py` 的节点拓扑、辩论轮次、风控裁决逻辑保持不动）；重档继续由它服务
- 不重写既有 19 个工具的行为（迁移期间保留 thin wrapper，仅登记入册 + 新增结构化变体）
- V1 不引入向量库 / RAG；多轮上下文的来源是结构化状态（持仓、偏好、历史判定），不是语义检索
- V1 不实现 MCP Client 接入外部数据源（P2 范围，且需先验证 `langchain-mcp-adapters` 的动态 header 鉴权）
- V1 不通过 MCP 暴露任何**写操作**（自选、台账、配置）
- V1 不替换 T+1 口径的既有统计（`insights-t1` 的基线需求不变）；本变更只**新增** T+3/T+5 口径用于信号校准
- 不追求洗盘判定的「准确」——它是对不可观测意图的推断；目标是把推断过程变得可解释、可证伪、可校准

---

## Design Decisions

### D1: 能力分层与单一工具来源

**Options considered:**

- A. **直接写 MCP server，把现有 19 个 `@tool` 包出去** — 工作量最小
- B. **只在产品内做对话 Agent，不管 MCP** — 体验闭环，生态缺席
- C. **能力层单一来源（ToolRegistry），对话 Agent 与 MCP 都是它的消费者** — 多一层抽象

**Chosen:** C

**Rationale:** A 会让外部 Agent 拿到 19 个「预咀嚼的中文表格字符串」——无法做数值推理；更严重的是产生**第二份工具定义**。本项目路线图已经逐条记录了这种失败模式的代价（`analysis_lessons` 110 行无读者、`recommendation_eval_items` 0 行、`/v1/backtest` 无前端调用者、`quality_flags` 无读者）。B 则把能力锁死在自家 UI，放弃「在任意 Agent 里问股票」这一跃迁。

**分层与消费者矩阵（normative）：**

```
L4 接口  POST /v1/chat (SSE)  │  POST /mcp (streamable-http)  │  GET /v1/signals/*  │  MCP Client (P2)
                    └──────────┴──────────────┬───────────────┴──────────┘
                                               ▼
L3 编排  意图三档路由 → 对话 Agent(tool-calling loop, ≤N 轮) ｜ 重档 → 现有 7 分析师管道
                                               ▼
L2 工具  ★ ToolRegistry — ToolSpec 单一来源
         ├─ signal_*   确定性信号（纯函数）
         ├─ data_*     数据取用（包 providers + freshness）
         ├─ plan_*     持仓 / 计划 / 退出（复用 exit_engine_service）
         └─ pipeline_* 深度分析（expensive，不进同步面）
                                               ▼
L1 信号  ★★ tradingagents/signals/ — 纯函数、零 LLM、零网络、可回测
                                               ▼
L0 数据  providers + freshness 契约（不改）
```

| 层 | 对话 Agent | LangGraph 分析师 | MCP 出口 | 直连 HTTP |
|---|---|---|---|---|
| `signal_*` | ✅ | 后续可选注入 | ✅ | ✅ |
| `data_*` | ✅ | ✅（经 thin wrapper） | ✅ | ✅ |
| `plan_*` | ✅ | 经 `user_context` | ✅ | ✅ |
| `pipeline_*` | ❌ 仅显式确认 | — | ❌ | ✅（既有 `/v1/analyze`） |

---

### D2: 工具契约形态——结构化 `ToolResult` 取代 `str`

**Options considered:**

- A. 沿用 `str` 返回值，MCP 侧把字符串包成 JSON
- B. 全部改结构化，立即重写 19 个工具与所有分析角色
- C. **新增结构化契约 + 并行保留 thin wrapper**，分阶段迁移

**Chosen:** C

**Rationale:** A 让外部消费者无法做数值推理，且 `degraded` / `freshness` / `as_of` 无处安放。B 一次性改动 19 个工具 + 7 个分析角色，破坏面过大且无法与既有 712+ 用例并行验证。C 的迁移路径安全：先登记、再给结构化变体、最后切换分析图。

**契约：**

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str                       # "signal_washout_probe"（见 D10 命名约定）
    title: str
    description: str                # LLM/MCP 共用同一份
    input_schema: dict              # JSON Schema
    output_contract: str            # 机读块名，如 "WASHOUT_PROBE"
    handler: Callable[..., ToolResult]
    cost: Literal["cheap", "medium", "expensive"]
    latency_budget_ms: int
    freshness_sources: tuple[str, ...]
    requires: tuple[str, ...]       # ("position", "risk_profile")

@dataclass
class ToolResult:
    data: dict                      # 结构化，可直接参与算术
    evidence: list[Evidence]        # value + source_key + as_of
    freshness: dict                 # 复用 freshness 契约输出
    degraded: bool
    degraded_reason: str | None
    as_of: str
```

**渲染 ≠ 契约：** 给 LLM 看的文本由独立 renderer 产出，**不得**作为工具返回契约。

---

### D3: 三档意图路由与向下降级

**Options considered:**

- A. 保留单档，全部走深度管道（现状）
- B. 二档：快问快答 / 深度分析
- C. **三档：轻 / 中 / 重**

**Chosen:** C

**Rationale:** B 会把「今天换手多少」（1 次数据调用）和「是不是在洗盘」（需要多工具 + 1 次归纳）混为一档，成本差一个数量级。C 的中档正是本变更的核心新增价值。

| 档 | 典型问句 | 执行 | 目标延迟 | 相对成本 | 工具轮数上限 |
|---|---|---|---|---|---|
| 轻 | 「茅台现在多少钱」「今天主力净流入多少」 | 1 个 `data_*` + 1 次汇总 | < 5s | 1× | 2 |
| **中** | **「XX 是不是在洗盘」「还能拿吗」「该止损吗」** | 2–4 个 `signal_*` / `plan_*` → 确定性打分 → 1 次归纳 | < 30s | ~5× | 4 |
| 重 | 「全面分析一下」「出份研报」 | 现有 7 分析师管道 | 分钟级 | ~100× | — |

**Normative 降级规则：** 分类器失败 / 超时 / 低置信 → **降到中档，绝不升到重档**。相邻两档模糊 → 取**成本更低**者。无可用标的 → 追问，不启动任何分析。

**Rationale（为什么必须向下降级）：** 用户提交后长时间等待的代价高于得到一个「中等深度但立刻可读」的判断；且重档成本是 100×，误升级的代价不可逆。这条规则同时防止路由层成为新的成本黑洞。

---

### D4: 方向权在信号层，表达权在 LLM

**Options considered:**

- A. 把结构化观测喂给 LLM，由 LLM 直接给判定
- B. 信号层出判定，LLM 只做文字渲染（不许改方向）
- C. 信号层出判定 + LLM 可覆盖（附理由）

**Chosen:** B

**Rationale:** A 就是今天的现状——同一份数据两次运行可能得出不同结论，且无法回测（LLM 判定不落库、不可复现）。C 让 LLM 有最终否决权，等于把校准闭环打穿：权重再准也会被临场覆盖，T+5 回填统计的将不再是模型行为。

B 与仓库既有护栏**同构**：`RISK_JUDGE` 解析失败强制按 reject 处理（`agents/utils/debate_utils.py:39-44` 与 `:54+` 的系统说明）、`signal_processing.py` 无 VERDICT 块时返回 `None` 计入弃权率而非猜测。**方向权在确定性层，表达权在 LLM** 是同一哲学的延伸。

**护栏实现：** 出站校验——若模型文本断言的 state 与 probe 不一致，或置信度超出 probe 报告区间，则拒绝/修复该响应，保证 verdict 事件与正文一致。

---

### D5: 位置作为交互调制项，而非独立加权项

**Options considered:**

- A. 把「累计涨幅 / 距前高」作为第 9 个独立维度，线性加权
- B. **作为量价证据的调制因子（乘子/gate）**
- C. 作为前置过滤（late-stage 直接否决 washout）

**Chosen:** B（C 作为其边界情形）

**Rationale:** 同样一根「缩量阴线」，在启动初期是**中继洗盘**，在翻倍高位是**派发前兆**。线性加权会把这两种情况打成相近分数——这是玩具级模型的典型特征。C 过于刚性：高位也可能有真洗盘，直接否决会丢掉判别力。

**Normative：** 位置观测（`cum_gain_60d`、`distance_to_prior_high`、`advance_days`）产出 `modulator_factor ∈ [0.5, 1.5]`，**乘在量价侧得分上**；输出中必须**分别暴露**原始位置观测与调制因子，且不得同时作为加法项出现。

```
score_washout = modulator(position) × (volume_contraction + intraday_absorption + rel_strength + chip)
score_distribution = (1 / modulator(position)) × (volume_expansion + super_large_outflow + ma_break)
```

---

### D6: 用相对换手消解「缩量」歧义

**Options considered:**

- A. 仅用自身历史均量判断缩量
- B. 与板块中位数换手率对比
- C. 两者结合，分类输出

**Chosen:** C

**Rationale:** 「缩量」有致命歧义——既可能是主力锁仓震仓，也可能是**流动性枯竭无人问津的真跌**。后者恰恰是最危险的情形：模型会在最该警惕的时候给出「洗盘」结论。仅与自身历史比无法区分（两种情况都表现为缩量）。

**Normative 分类：**

| 自身换手 vs 5 日均 | vs 板块中位数 | 分类 | 对 washout 的贡献 |
|---|---|---|---|
| 低于 | 低于 | `broad_contraction` | **禁止为正**，计入 distribution/trend_break 侧 |
| 低于 | 等于或高于 | `relative_strength_contraction` | 允许为正 |
| 板块中位数不可得 | — | `insufficient_evidence` | 禁止回退到仅自身比较 |

板块中位数由已有 `board_spot` / `zt_pool` 数据计算，不新增数据源。

---

### D7: 四态判定 + 强制证伪条件

**Options considered:**

- A. 二元（是/不是洗盘）
- B. 三态（洗盘 / 派发 / 不确定）
- C. **四态（洗盘 / 派发 / 趋势破坏 / 证据不足）+ ≥2 条证伪条件**

**Chosen:** C

**Rationale:** A 是把对不可观测意图的推断伪装成事实，专业上不成立。B 的「不确定」是个垃圾桶，无法区分「证据弱」与「证据分裂」。C 把「趋势破坏」（下跌趋势启动）单列——它与「派发」在**交易含义**上不同：派发是主力主动出货（可能仍有余波），趋势破坏是结构已坏（任何持有理由都需重审）。

**证伪条件是分水岭。** 市面产品只给结论；给**失效条件**才叫研究，也才使判定可回测——没有可检验的预测，T+5 回填就无从判定对错。

**Normative：** 除 `insufficient_evidence` 外，每个判定必须携带 ≥2 条证伪条件，每条须含**可观测量 + 阈值 + 时间窗**，且阈值必须可由已计算的观测推导、可追溯。

---

### D8: 数据缺口补齐优先级——分时与筹码进 P0，而非 P2

**Options considered:**

- A. 先用日线做 V1，分时/筹码放后续
- B. **分时 + 筹码 + 相对换手全部进 P0**
- C. 只补筹码（号称「最直接」），分时延后

**Chosen:** B

**Rationale:** 这是本设计中最需要克制「先出个能跑的东西」冲动的地方。若缺分时，「下跌过程中是否有人在接」——**洗盘最直接的证据**——完全无法观测，模型只能靠日线形态猜；若缺筹码，获利盘与成本分布这两个判别力最强的变量缺失；若缺相对换手，D6 的歧义消解失效。三者缺失下产出的 `washout_probe` 是一个**看起来专业、实际会在无人接盘的阴跌里给出洗盘结论**的危险工具——比没有这个功能更糟。

**因此：** 规格要求 `washout` 达到高置信必须同时具备可用的分时承接证据与筹码证据（见 `washout-probe` 规格）；缺任一项则**封顶置信度**。这使数据补齐成为**功能的硬前置**而非可选项。

**数据源：** `stock_zh_a_hist_min_em`（1/5/15 分钟）、`stock_cyq_em`（获利盘 / 平均成本 / 集中度）。两者均走既有 provider 体系与 freshness 契约（新增 contract 条目）。

---

### D9: 会话持久化模型

**Options considered:**

- A. 不落库，前端持有历史（现状：无任何 conversation 表）
- B. 单表存 JSON 消息数组
- C. **会话表 + 消息表 + 判定表 + 判定结果表**

**Chosen:** C

**Rationale:** A 无法跨设备、无法审计、无法做「上次判定后来怎么走」的追问。B 无法按 symbol / 时间范围检索判定，而 D12 的校准闭环需要**按判定检索结果**——判定必须是一等实体，不能埋在对话 JSON 里。

**Schema（normative）：**

| 表 | 关键列 |
|---|---|
| `chat_conversations` | id, user_id, title, primary_symbol, created_at, updated_at |
| `chat_messages` | id, conversation_id, role, content, tool_activity(JSON), verdict_id(FK, nullable), tier, model, tokens, latency_ms, status(completed\|cancelled\|failed), created_at |
| `signal_verdicts` | id, user_id, symbol, as_of, probe, verdict_state, confidence, contributions(JSON), modulator(JSON), falsifiers(JSON), evidence(JSON), signal_version, created_at |
| `signal_verdict_outcomes` | id, verdict_id, horizon_days, ref_date, max_high, close, ret_pct, threshold_hit(bool), evaluable(bool), reason, evaluated_at |

**隔离：** 所有查询强制 `user_id` 过滤；跨用户访问拒绝且**不披露记录是否存在**。

---

### D10: MCP 出口范围、命名与鉴权

**Options considered:**

- A. 暴露全部工具（含 `pipeline_*` 与写操作）
- B. **只暴露同步只读工具，鉴权复用既有 API Token**
- C. 新建一套 MCP 专用凭证与只读子集

**Chosen:** B

**Rationale:**

- **排除 `pipeline_*`**：分钟级任务不适合同步工具语义，会在 MCP 客户端造成超时与重试风暴
- **V1 排除写操作**：外部 Agent 能改自选/台账/配置的爆炸半径过大，且缺乏审计与确认回路
- **复用 `UserTokenDB`**：`_require_api_user = RequireUser(allow_api_token=True)`（`api/main.py:2961`）与 `/v1/tokens` 已完整存在，**用户隔离天然成立、零新增鉴权代码**。C 会引入第二套凭证生命周期（轮换、吊销、审计），是纯负担

**挂载方式（normative）：** 使用官方 MCP Python SDK 的 `streamable-http` transport（SSE 已过时），并**将 MCP 的 lifespan 与 FastAPI 生命周期合并**——FastMCP 的 lifespan 与 `app.mount` 组合有已知陷阱，社区为此专门提过 PR 补齐（[python-sdk#1712](https://github.com/modelcontextprotocol/python-sdk/pull/1712)、[fastmcp#1176](https://github.com/PrefectHQ/fastmcp/issues/1176)）。**不得**以 `app.mount` 直接挂载了事。

**命名约定（normative）：** 工具名统一使用 `<domain>_<action>` 下划线形式（`signal_washout_probe`、`data_stock_kline`、`plan_exit_advice`），**不使用点号命名空间**。原因：MCP 客户端对工具名字符集有 `[a-zA-Z0-9_-]` 类约束，点号可能被拒；统一为下划线后**注册表名即 MCP 名，无需任何 sanitize 映射**——避免 D1 想排除的「双份定义」以命名映射的形式复活。

---

### D11: 中档成本护栏

**Options considered:**

- A. 不设限，靠提示词要求模型「不要调用太多工具」
- B. 硬性轮数上限 + token 预算，超限返回部分证据
- C. 按 token 成本动态选择模型（轻档用便宜模型）

**Chosen:** B + C

**Rationale:** A 是无效护栏（本项目已有先例：`confidence` 仅靠 prompt 建议时会被忽略，故 freshness 契约采用了「写库硬 clamp + prompt 约束」双轨，见 `freshness-disclosure` 的 `Confidence hard cap by overall status`）。B 的**超限行为必须是「返回已获证据 + 明示截断」，而不是静默升级到重档**——否则 D3 的降级规则被绕过。

C 作为补充：轻档与路由分类可用 `quick_thinking_llm`（`graph/trading_graph.py:96` 已有 quick/deep 双客户端模式），中档归纳用用户配置的模型，**沿用既有 `model_profile` 体系**。

---

### D12: 校准闭环与最小样本闸门

**Options considered:**

- A. 人工按经验设权重（提示词现状的代码化）
- B. 全自动按回填结果调权重
- C. **落库 → 回填 → 统计 → 人工审核 → 版本化激活，且设最小样本闸门**

**Chosen:** C

**Rationale:** A 就是「提示词拍板」换个地方写，没有任何改进。B 在冷启动阶段是灾难——本平台已有**真实的样本饥饿前科**（路线图记录「策略权重样本饥饿」是缺陷之一）。

**因此最小样本闸门是强制项**：样本不足时**拒绝改权重**并报告当前样本量 vs 门槛。这不是保守，而是防止用一个 30 样本的噪声去替换一个可用的先验。

**操作性定义（normative）：**

| 判定 | 兑现定义 |
|---|---|
| `washout` | 判定后 T+5 内创出高于判定日高点的**新高** |
| `distribution` | T+5 内未创新高，且收盘较判定日收盘跌幅超过阈值 |
| `trend_break` | 窗口内未收复被跌破的参考位 |
| `insufficient_evidence` | **不计入方向准确率**，单独统计为弃权率 |

**T+3 / T+5 计数交易日，非自然日**；停牌/退市记 `evaluable=false`，**不计为命中或未命中**（避免用不可评估样本污染统计）。

**版本化：** 权重变更须生成新 `signal_version` 并记录样本量与理由；旧版本保留用于同标的同日期区间的对照。**历史判定与历史结果记录永不重写**——操作性定义变更只对新评估生效，且跨定义区间的报告必须声明适用口径。

---

### D13: 模块结构

| 新增单元 | 职责 |
|---|---|
| `tradingagents/signals/types.py` | `SignalResult` / `ScoreComponent` / `Evidence` / `Falsifier` / `Modulator` |
| `tradingagents/signals/volume.py` | 缩量程度、下跌日量能占比、反抽量能恢复率 |
| `tradingagents/signals/price_action.py` | 跌幅速度、下影线占比、日内收复率、均线结构 |
| `tradingagents/signals/position.py` | 累计涨幅、距前高、上涨天数 → `modulator_factor` |
| `tradingagents/signals/relative.py` | 板块/大盘相对强度、**板块中位数换手** |
| `tradingagents/signals/capital.py` | 主力净流入、超大单占比、龙虎榜席位 |
| `tradingagents/signals/chips.py` | 获利盘、平均成本、集中度 |
| `tradingagents/signals/intraday.py` | 分时承接强度（下影、尾盘资金、跌幅回收） |
| `tradingagents/signals/washout.py` | 八维组合 + 位置调制 + 四态 + 证伪生成 |
| `tradingagents/signals/calibration.py` | 权重加载（来自库）+ 版本号 |
| `tradingagents/tools/spec.py` | `ToolSpec` / `ToolResult` |
| `tradingagents/tools/registry.py` | 注册表 + 枚举 + schema 导出 + 未注册扫描 |
| `tradingagents/tools/adapters/langchain.py` | `ToolSpec` → LangChain `StructuredTool` |
| `tradingagents/tools/adapters/mcp.py` | `ToolSpec` → FastMCP tool |
| `tradingagents/tools/{data,signals,plan,pipeline}.py` | 四类工具实现 |
| `api/services/intent_router.py` | 三档路由 + 降级 + 路由记录 |
| `api/services/agent_service.py` | tool-calling loop + SSE 事件协议 + verdict 护栏 |
| `api/services/conversation_service.py` | 会话/消息 CRUD + 用户隔离 |
| `api/services/signal_verdict_service.py` | 判定落库 + 回填调度 + 校准统计 |
| `api/mcp_server.py` | MCP 挂载 + lifespan 合并 + 鉴权桥接 + 白名单 |

**新增 HTTP 面：**

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/v1/chat` | SSE 流式对话（回答 / 工具活动 / verdict 卡片 / 终止事件） |
| GET | `/v1/conversations`、`/v1/conversations/{id}` | 会话列表与回放 |
| GET | `/v1/signals/washout` | 直取结构化判定（**无模型调用**，便于调试与前端卡片） |
| GET | `/v1/signals/verdicts` | 历史判定 + 后续走势 |
| GET | `/v1/signals/calibration` | 版本、样本量、弃权率、各维度判别力 |
| POST | `/mcp` | MCP 出口（streamable-http） |

`POST /v1/chat/completions` **保留且语义不变**（仍返回 job 回执），内部改为经三档路由，避免破坏既有调用方。

**SSE 事件协议：** `answer.delta` / `answer.done` / `tool.start` / `tool.done` / `verdict` / `notice`（预算或截断提示）/ `error` / `end`。

---

### D14: 阈值与弃权采用**结构定义**，而非分数阈值

**Options considered:**

- A. 在分数轴上设绝对阈值（如 score < 某值则弃权）
- B. **弃权 = 数据完整性门控 + 两侧证据 margin；置信度 = 三点锚定映射；「高置信」= 结构条件**
- C. 先拍一组阈值上线，后续再调

**Chosen:** B

**Rationale:** A 和 C 都会引入需要"拍"的魔法数字，且这个数字会掩盖真正的问题——若探针没有判别力，调阈值只是把噪声换个位置。B 让判定门槛**不需要标定**，同时保留可检验性。

**Normative（弃权）：**

```
insufficient_evidence ⟺ 满足任一：
  ① 四个核心维度（量能 / 价格行为 / 位置 / 相对强度）任一不可得
  ② margin = |S_washout − S_distribution| / (S_washout + S_distribution) < 0.15
```

理由：8 个维度各贡献 O(1)，margin < 15% 时该差距落在**单一数据源修正带来的噪声之内**。用相对差距而非绝对分数，天然满足「证据对立即弃权」。

**Normative（置信度）：** 三点锚定映射 `margin → confidence`，线性分段：`0.15 → 40`、`0.30 → 70`、`0.45 → 85`。三个数是**曲线形状参数**，不是判定门槛。

**Normative（高置信标签）：** `高置信 ⟺ 分时承接证据可得 且 筹码证据可得 且 margin ≥ 0.30`。纯结构条件，不含分数阈值。

**Normative（弃权率监控）：** 目标区间 **30%–50%**，与命中率并列为一级监控指标。

| 弃权率 | 诊断 |
|---|---|
| > 70% | 门控过严，探针无用 |
| **30%–50%** | 合理——洗盘/派发本就是少数情形 |
| < 20% | 🚩 在硬凑结论，多半某维度贡献被系统性放大 |

**Rationale（为何以弃权率为一级指标）：** 仓库路线图已自评「风控层 99.94% 通过率意味着这道闸门目前是装饰性的」。同一逻辑：**从不弃权的探针是装饰性的**。

**标定协议（normative，仅在需要调整 0.15/0.30/0.45 时执行）：**

```
① 离线回放产出 N 个 (判定, T+5 结果) 样本
② 扫 margin 阈值 → hit-rate vs 弃权率 曲线
③ 取"命中率提升最陡段之后趋于平坦"的拐点
④ 稳定性检验：阈值 ±20% 扰动下拐点不移动（须存在平台区）
⑤ 无平台区 → 不是阈值问题，是维度本身无判别力 → 回到维度设计
⑥ 样本【按时间】切分：前 70% 调参，后 30% 报告（随机切分会因自相关泄漏）
```

---

### D15: 校准样本来源与「离线/前瞻」用途分离

**Options considered:**

- A. 只靠上线后前瞻积累
- B. 只靠离线回放
- C. **离线回放用于「否决」无效维度；前瞻积累用于「确认」并激活权重**

**Chosen:** C

**Rationale:** A 的冷启动太慢（T+5 需要真实等待）。B 有同源偏差——同一份代码既选样本又调参，回放上"调好"的权重可能只是过拟合了那段行情。C 把两者职责切开：离线负责**杀维度**（否决一个无判别力的维度是稳健结论），前瞻负责**确认**（激活需要独立样本）。

**样本量（normative）：**

| 用途 | 最小样本 | 依据 |
|---|---|---|
| 估计整体命中率 ±10%（95% CI） | **96** | n = 1.96²·0.25/0.1² |
| 区分两个权重版本 | **~350 / 组** | 该量级才谈得上"A 优于 B" |
| 单维度判别力 | 该维度 **≥30 个可用样本** | 防的正是仓库记录过的「策略权重样本饥饿」 |
| **离线回放目标** | **≥500 个判定案例**（跨 ≥100 只股票 × 上涨/震荡/下跌三种状态） | 用于否决维度 |
| **权重激活闸门** | **≥100 个已评估的前瞻判定**，且本次改动的每个维度各自 ≥30 | 用于确认 |

**数据深度的现实处理（normative）：** `market_daily_prices` 仅 7,946 行 / 488 只 / 6 个月，**不足以回放**，回放 harness 必须直接经 `route_to_vendor("get_stock_data", ...)` 实取日线。分时与筹码的历史深度**尚未验证**（本环境网络受限，实测 `stock_cyq_em` 返回 `ConnectionError`）。若二者无足够历史，则：

- 日线路径**立即**可产出上千样本 → 校准即刻启动
- 分时/筹码随**前瞻**积累逐步解锁更高置信档

**这正是 D8「缺分时或筹码则封顶置信度」的真正价值**：它不只是免责，而是让系统在数据不完整时**依然可校准**——日线近似是一等公民模式，不是降级兜底。

---

### D16: 板块基准采用**双基准 + 背离信号**

**Options considered:**

- A. 仅行业板块中位数
- B. 仅概念/主线板块
- C. **行业中位数 primary + 主线代表板块 secondary（仅对主线股），背离本身作为信号**

**Chosen:** C

**Rationale:** A 会漏掉"主线整体退潮带动龙头下跌"（个股跌得比行业多，但并非自身问题）。B 有**内生性**问题——个股自身涨跌会推高所属概念板块，使相对强度失真，且概念板块成员漂移大、蹭概念普遍。C 同时规避两者，并把背离变成信息。

| 基准 | 角色 | 数据来源 |
|---|---|---|
| 东财**行业**板块中位数 | primary —— 判断"是否被错杀" | `board_spot` 已有 `industry`，零新增 |
| 所属**主线**代表板块（仅当该股在 `mainline_candidates` 中，296 行可用） | secondary —— 判断"主线是否退潮" | 复用 `MainlineCandidateDB` |

**Normative（背离处理）：**

| 行业 | 主线 | 解读 | 动作 |
|---|---|---|---|
| 稳 | 退潮 | 被主线情绪拖累，非自身问题 | distribution 侧 +2 档 |
| 跌 | 稳 | 被行业错杀 | washout 侧加分 |
| 同跌 | 同跌 | 系统性下跌 | 位置调制主导 |
| 同稳独跌 | — | **alpha 走弱，最危险** | distribution 强信号 |

**明确不做**：全量概念板块（内生性 + 成员漂移）。概念板块的价值在于"主线还在不在"，属 `mainline_*` 子系统职责——**职责分离**：个股探针测"是否被错杀"，主线子系统测"情绪周期位置"。

---

### D17: `hold_or_exit` 不新建探针，接进既有 `exit_engine_service`

**Options considered:**

- A. 新建第二个探针 `hold_or_exit_probe`，自己解析持仓
- B. **不新建——`washout` verdict 作为 `exit_engine_service` 的新触发信号**

**Chosen:** B

**Rationale:** `exit_engine_service` 已有 **102 个测试用例**，并已实现"研报文本 → 计划 → 落库 → 退出决策"完整链路（`test_exit_lifecycle_integration.py` 18 个端到端用例）。新探针会重复它，并让那 102 个测试的有效性打折。

**职责切分：**

```
washout_probe        →  【市场侧判定】这只股票现在处于什么状态
        ↓ 作为新触发信号
exit_engine_service  →  【持仓侧决策】我的持仓该怎么办（复用既有逻辑与测试）
```

**⚠️ 硬前置（实测发现）：** `trade_ledger` **当前 0 行**，`imported_portfolio_positions` 390 行（静态导入快照，无成交流水、成本不随加减仓更新、T+1 可卖数量不跟踪）。**在持仓数据来源打通之前，把 verdict 接进 `exit_engine_service` 只是把判定接到一个输入为空的引擎上。**

**因此分期调整：**

- **P0**：`washout_probe` 只做**市场侧判定**，不碰持仓（依赖 D9 的「解析不到持仓按 `unknown`，不假设默认持仓」保证不出错）
- **P0.5**：先解决持仓数据来源（台账录入入口 / 导入增强），**再**把 verdict 接进 `exit_engine_service`

---

### D18: MCP Client 用官方 SDK 自写 adapter；会话数据分级保留

**MCP Client — Options considered:**

- A. 引入 `langchain-mcp-adapters` 转成 LangChain tool 再塞进注册表
- B. **用 server 端已引入的官方 `mcp` SDK `ClientSession`，自写 `MCPToToolSpec` adapter**

**Chosen:** B

**Rationale:**（1）server 端已依赖 `mcp` SDK，client 复用同库是**零新增依赖**；（2）`langchain-mcp-adapters` 有动态 header 鉴权的历史痛点（[issue #172](https://github.com/langchain-ai/langchain-mcp-adapters/issues/172)）；（3）更关键——**本项目本来就要写 `ToolSpec` adapter**（D1 核心），让外部 MCP 工具直接落进**自己的注册表**，比落进 LangChain tool 再想办法塞进注册表干净。

```
外部 MCP server → mcp.ClientSession → MCPToToolSpec adapter → ToolRegistry
```

**会话保留 — Chosen:** 分级保留。

| 数据 | 保留 | 理由 |
|---|---|---|
| `signal_verdicts` / `signal_verdict_outcomes` | **永久** | 校准资产，删了就没有复利 |
| `chat_messages` 元数据（tier / model / tokens / verdict_id） | **永久** | 成本归因与弃权率统计 |
| `chat_messages.content` | **90 天**后压缩为摘要 | ~3KB/轮 |
| `chat_messages.tool_activity` | **只存摘要**（工具名 / 参数 hash / `as_of` / 耗时 / 证据条数） | ⚠️ 存全量返回单轮可达 10–50KB |

**容量估算：** 100 活跃用户 × 10 轮/天 ≈ 1000 轮/天 → 按上述分级约 **4 MB/天 ≈ 1.5 GB/年**（可接受）；**若存全量工具返回**约 18 GB/年（不可接受）。完整返回不存——结构化判定已在 `signal_verdicts`，原始数据**按需重算比存旧快照更正确**（可取到修正后数据）。

---

### D19: 代码架构与解耦约束

**Options considered:**

- A. 顺着 `api/main.py` 继续加代码（现状路径）
- B. **分层 + 单向依赖 + 端口/适配器 + 显式组合根，用测试锁死边界**
- C. 全面重构既有代码到新架构

**Chosen:** B

**Rationale:** A 会把 `api/main.py`（现 9591 行）推向不可维护。C 破坏面过大，且既有代码（19 个工具、74 个测试文件）是稳定资产。B 只约束**新增**代码，既有代码不动。

**现存证据（实测）：**

| 事实 | 含义 |
|---|---|
| `tradingagents/` **零反向依赖** `api/`；20 个 service 单向依赖 `tradingagents` | ✅ 干净边界，用测试锁死 |
| `api/main.py` **零** `APIRouter` / `include_router` | ⚠️ 新路由绝不再进 main.py |
| `BaseMarketDataProvider` 是 fat ABC（11 个 `abstractmethod`） | ⚠️ 反模式，不复制 |
| `dataflows/config.py` 用 `global _config` 可变模块单例 | ⚠️ 并发串味 + 测试隔离隐患，新层不碰 |
| `Protocol` 全仓库零使用；`api/services` 几乎全模块级函数（`trade_plan_service` 28 def / 0 class） | 新代码沿用**函数式 + 显式组合**，不引入类层次 |

**四条硬约束（normative）：**

```
C1  依赖单向：signals → tools → agent → api。同层内不成环。
C2  tradingagents/ 永不 import api/（加测试锁死）
C3  signals/ 内禁止任何 IO：requests / akshare / baostock / openai / anthropic /
    langchain_core / datetime.now / time.time
C4  新 HTTP 路由永不写进 api/main.py，只写 api/routers/*.py
```

**解耦机制：**

1. **信号层数据端口用 `Protocol`**（每个端口 1–2 个方法），权重与时钟**显式传参**——不用全局 config
2. **维度用显式有序元组**（`DIMENSIONS: tuple[Dimension, ...]`），**不用全局可变注册表**（pytest 并发下会互相污染；74 个测试文件的仓库里这是真实风险）
3. **探针统一 `Probe` 协议 + `ProbeResult`** —— 持久化、回填、校准统计、前端卡片全部只认 `ProbeResult`，新增探针零改动
4. **`ToolSpec` 注册表 + adapter 隔离传输**：两个 adapter 互不知道对方存在；`signals/` 完全不知道 `ToolSpec` 存在（转换发生在 `tools/signals.py`）
5. **编排层 middleware 链**（`BudgetGuard` / `ExpensiveToolGuard` / `NoProgressGuard` / `VerdictGuard` / `DisclaimerInjector` / `TelemetrySink`）——横切关注点独立可测，否则 loop 会变成第二个 main.py
6. **传输解耦靠 `EventSink`**：SSE / CLI / 测试消费同一套事件；`llm` 与 `clock` 是参数不是全局
7. **路由降级用装饰器包装**（`DegradingClassifier`），不是散落的 try/except

**组合根（唯一集中组装点）：** `tradingagents/tools/__init__.py::build_registry(ports_factory)` —— **显式组装，不用装饰器自动注册**（自动注册对 import 顺序敏感，测试中难以构造干净实例）。

**扩展点（判据是"是否碰旧文件"）：**

| 要加什么 | 改动 | 碰旧文件？ |
|---|---|---|
| 新信号维度 | 新增 `signals/<x>.py` + `DIMENSIONS` +1 行 + 权重表 +1 项 | 仅组合处 1 行 |
| 新探针 | 新增 `signals/<x>.py` 实现 `Probe` + 组合根注册 | ❌ |
| 新工具 | `registry.register(ToolSpec)` | ❌ |
| 新传输 | 新增 adapter + `EventSink` 实现 | ❌ |
| 新横切关注点 | 新增 middleware + `MIDDLEWARE` +1 行 | 仅组合处 1 行 |
| 新数据源 | 实现对应 `Protocol` + 走既有 provider 注册 | ❌ |
| 新 HTTP 路由 | 新增 `api/routers/*.py` | ❌ |
| 换 LLM 厂商 | 无改动（复用 `create_llm_client` / `BaseLLMClient`） | ❌ |

**防退化护栏（约束必须是会失败的测试）：**

```python
# tests/test_architecture_boundaries.py
test_signals_has_no_io_or_llm_imports()      # C3：纯函数性由构建时断言保证
test_tradingagents_never_imports_api()       # C2
test_new_routes_not_added_to_main()          # C4
test_every_langchain_tool_is_registered()    # 工具定义单一来源（D1）
test_same_pattern_different_position_differs()  # D5 核心不变量
```

第一条尤其关键：它把「洗盘判定可信」从**设计承诺**变成**构建时断言**——任何人在信号层偷偷加一次网络请求或 LLM 调用都会立刻失败。

---

## Risks / Trade-offs

| 风险 | 缓解 |
|---|---|
| **数据缺口导致判定质量虚高**：无分时/筹码时仍给出高置信「洗盘」 | D8 硬约束：缺分时或筹码任一项则**封顶置信度**并在输出中披露；只有日线时显式标注「日线近似」 |
| **洗盘断言越界为投资建议**：对他人意图的推断在合规上敏感 | 全部输出概率化表述 + 免责声明（复用既有机制）+ 禁止指令式措辞 + 必须携带证伪条件；规格中以 Requirement 固化 |
| **中档成本失控**：Agent 反复调工具 | 硬性轮数上限 + token 预算 + 超限返回部分证据；`pipeline_*` 在循环内**拒绝调用**（D11） |
| **工具定义漂移**：出现第二份定义 | 注册表单一来源 + 测试断言「不存在未注册的 `@tool`」+ 命名不做 sanitize 映射（D1/D10） |
| **路由误升级到重档**：成本黑洞 | D3 的 normative 降级规则（失败→中档、模糊→更低档）+ 路由记录可观测 tier 分布 |
| **LLM 覆盖判定**：护栏被绕过 | 出站校验（state 与置信度一致性），违规即拒绝/修复；护栏本身单测覆盖 |
| **校准闭环冷启动**：样本不足时乱调权重 | 最小样本闸门（拒绝改权重并报告样本量）+ 版本化激活 + 旧版本保留对照（D12） |
| **回填污染统计**：停牌/退市/不可评估样本混入 | `evaluable=false` 单列，不计命中/未命中；T+N 按交易日计数 |
| **MCP lifespan 挂载陷阱** | 明确要求合并 lifespan，禁止裸 `app.mount`；新增既有路由回归测试 |
| **误把 `insufficient_evidence` 变成观点** | 四态中的弃权态在任何下游不得被转换（规格固化）；弃权率单独统计，与既有 `signal_processing.py` 弃权率文化一致 |
| **既有分析图被迁移破坏** | 迁移期间保留 thin wrapper；新增「分析图行为不变」回归测试；结构化变体与字符串形态并存（D2） |
| **数据库体积**：判定与结果表持续增长 | 判定表按 `(symbol, as_of, signal_version)` 建唯一索引去重；`contributions`/`evidence` 存结构化摘要而非完整原始数据 |
| **前端改造范围**：`ChatCopilotPanel.tsx` 现有 1021 行，改造可能影响既有任务流 | 保留「启动深度分析」为显式动作；新对话为主路径，旧任务事件流作为重档视图复用 |
| **持仓数据为空**：`trade_ledger` 实测 **0 行**，持仓仅有静态导入快照（无成交流水、成本不随加减仓更新） | D17 把持仓侧接线拆到 P0.5，作为硬前置；P0 阶段明确按 `unknown` 处理，**不假装持仓感知已通** |
| **分层腐化**：新代码逐层退化为第二个 9591 行 `main.py` | D19 的四条硬约束 + `tests/test_architecture_boundaries.py` 把边界变成会失败的测试 |
| **全局 config 串味**：`dataflows/config.py` 的 `global _config` 在并发下可能跨请求污染 | 新链路不碰它；权重 / 端口 / 时钟全部显式传参，由组合根注入 |

---

## Migration Plan

1. **数据层先行**（无破坏）：新增分时与筹码取数 + `freshness/contract.yaml` 新增条目；用单独测试验证 freshness 判定与并发锁行为
2. **信号层**（纯新增，无调用方）：`tradingagents/signals/` + 单测 + golden set；此阶段可独立合入
3. **工具层**：新增 `ToolRegistry` 与四类工具；**先只登记**现有 19 个工具（thin wrapper 委托），不切换分析图；加「无未注册 `@tool`」断言
4. **持久化**：新增 4 张表（`init_db()` 增量建表，可空兼容）；重启后端生效（与既有 `trade_plans` / `trade_ledger` 同样的注意项）
5. **编排与接口**：`intent_router` + `agent_service` + `conversation_service` + `/v1/chat`；`/v1/chat/completions` 改为经路由但**响应结构不变**
6. **MCP 出口**：`/mcp` 挂载（lifespan 合并）+ 白名单 + 鉴权桥接；用真实 MCP 客户端在 Claude Desktop / Cursor 验证「是不是在洗盘」
7. **校准闭环**：回填任务挂到既有调度（`scheduled_service`）；判定结果需等 T+3/T+5 数据自然成熟，**本阶段不得给出未经实测的准确率数字**
8. **前端**：`ChatCopilotPanel` 流式化 + `VerdictCard`；`npx tsc --noEmit` 必须通过

**兼容性：** 全部为增量（新表、新路由、新字段、新包）；既有 19 个工具行为不变；`insights-t1` 的 T+1 口径统计不变。

---

## Open Questions

原 6 项已全部收敛为决策，见 D14–D18：

| 原问题 | 收敛结果 |
|---|---|
| 1. 中档工具轮数上限 | → **D11 + D19**：上限 **3 轮 / 5 次调用**；主路径设计为「1 宽探针 + 可选 1 持仓工具 + 1 归纳」，ReAct 仅作兜底；补 `NoProgressGuard`（第 2 轮后无新证据即终止）。理由：轮数对成本的杠杆是**二次的**（每轮重发全部历史），5 次调用约合 3–5× 单次成本 |
| 2. `washout` 高置信阈值与弃权下限 | → **D14**：改用**结构定义**（完整性门控 + margin），不再需要标定魔法数字；弃权率目标 30–50% |
| 3. 板块归属口径 | → **D16**：行业中位数 primary + 主线 secondary，背离即信号；**不做**全量概念板块（内生性） |
| 4. 是否同步上 `hold_or_exit_probe` | → **D17**：**不新建**，verdict 接 `exit_engine_service`；但持仓数据来源为硬前置，拆到 P0.5 |
| 5. MCP 客户端鉴权形态 | → **D18**：不引入 `langchain-mcp-adapters`，用已有 `mcp` SDK `ClientSession` 自写 adapter |
| 6. 会话保留期 | → **D18**：分级保留（verdict / 元数据永久，正文 90 天，`tool_activity` 只存摘要） |

**仍需实测的参数（唯一保留的不确定性）：**

以下数值是 D14 / D15 协议中的**形状参数**，其取值须由回放样本的曲线决定，**不得凭直觉写死**：

| 参数 | 当前先验 | 由什么决定 | 落定条件 |
|---|---|---|---|
| 弃权 margin 阈值 | `0.15` | D14 标定协议第 ②–④ 步的曲线平台区 | 存在平台区且阈值 ±20% 扰动下拐点不移动 |
| 置信度锚点 | `0.30 → 70`、`0.45 → 85` | 同上 | 同上 |
| 高置信 margin 门槛 | `0.30` | 同上 | 同上 |
| 离线回放样本量 | `≥500` | D15；用于**否决**维度 | 覆盖 ≥100 只股票 × 三种市场状态 |
| 权重激活闸门 | `≥100` 前瞻判定 + 每维度 ≥30 | D15；用于**确认** | 独立于回放样本 |
| 分时 / 筹码历史深度 | **未验证** | 本环境网络受限，`stock_cyq_em` 实测 `ConnectionError` | P0 第 1 组任务中实测；若无历史则二者仅前瞻积累（D15 已给出该路径下校准照常启动的设计） |

**这不是"没想清楚"，而是刻意把"必须由数据决定"与"可以现在决定"分开**——前者的先验值带 `signal_version` 上线，被数据推翻时走 D12 的版本化激活。


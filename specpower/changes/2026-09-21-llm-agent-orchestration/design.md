## Context

本平台今天的形态：**确定性判定 + 证据校验**做得比多数开源 Agent 项目扎实，但**入口理解能力**落后于主流。反过来，主流 Agent 框架的**工具循环**很成熟，但普遍没有"数值必须有出处"这种机械把关。

所以本设计的核心不是"选哪个框架"，而是**把两者的强项拼起来，并明确交界在哪**。

### 参考的成熟实现（设计思想，非照搬）

| 来源 | 借鉴什么 | 不照搬什么 |
|---|---|---|
| ReAct（Reason+Act） | "思考→行动→观察"的循环骨架；观察结果必须回流再判断 | 每步都让模型自由决定：步数不可预知，成本与延迟无法承诺 |
| OpenAI / Anthropic 的 function calling 与 tool use | 工具用 JSON Schema 声明；模型输出**结构化调用**而不是散文指令；支持并行调用 | 直接依赖单一厂商 SDK：本平台要跨 provider（deepseek/doubao/openai 并存） |
| Anthropic《Building effective agents》的工作流 vs 智能体之分 | 能用**确定性工作流**解决的就别上自主智能体；路由/编排/并行/评估者是四种可组合的工作流模式 | 全盘自主：金融问答里"问什么答什么"的确定性远重于自主性 |
| MCP（Model Context Protocol） | 工具与宿主解耦；工具描述即契约；已在本仓库有出口实践 | 把 MCP 当**唯一**工具面：内部编排走进程内调用更省一次序列化与鉴权往返 |
| LangGraph | 状态机 + 条件边 + **checkpoint 断点续跑** + 人审中断点 | 用它当"让模型自己走图"的运行时：本平台需要计划可落库、可重放 |
| 工具检索 / 按需加载 | 工具数量增长后，只注入**相关**工具子集 | 一次性把所有工具塞进提示词（撑上下文、降低选择准确率） |

参考：[Ai System Design Guide: Architecture Patterns for Tool-Use Agents](https://raw.githubusercontent.com/ombharatiya/ai-system-design-guide/b5819ccbfaef543243b0030ef4893883a449e11e/17-tool-use-and-computer-agents/02-architecture-patterns.md)、[AWS Prescriptive Guidance: MCP strategies](https://docs.aws.amazon.com/prescriptive-guidance/latest/mcp-strategies/mcp-strategies.pdf)、[Agentic workflows 基础设施指南](https://assets.azion.com/en/blog/agentic-workflows-infrastructure-guide/)。

## Goals / Non-Goals

**目标**：任意自然语言进来 → 得到有依据、可审计、可复现的回答；平台独有能力全部可被 Agent 调用；多轮对话有状态。

**非目标**：让模型决定判定；让模型自由漫游工具空间；替换既有 LangGraph 重档管道。

## Design Decisions

### D1 控制平面：LLM 规划，代码执行（"规划-执行"而非自由 ReAct）

三种循环形态对比：

| 形态 | 步数可预知 | 成本可控 | 可审计/可回放 | 处理开放式问题 |
|---|---|---|---|---|
| A 纯 ReAct 自由循环 | ❌ | ❌ 方差大 | ⚠️ 需完整记录才勉强 | ✅ 最强 |
| B 规划-执行（Plan-and-Execute） | ✅ 计划即步数 | ✅ 计划即可估价 | ✅ 计划落库即审计 | ✅ 够用（受控 replan 补） |
| C 状态机 + 条件边（LangGraph） | ✅ 节点固定 | ✅ | ✅ | ⚠️ 受限于预定义节点 |

**选 B**，并按需引入 C 的能力（断点续跑/人审中断）：

```
用户话 + 会话槽位 + 工具清单摘要
        ↓
[1] 规则前置分类（零 LLM）：命中已知模式 → 直接得 Plan
        ↓ 未命中 / 低置信
[2] LlmPlanner（结构化输出 + schema 校验 + 失败降级到中档+追问）
        ↓  Plan{intent, symbols, horizon, steps[], need_clarify, budget_tier}
[3] Plan 落库（turn_id → plan_json）——可审计、可重放、可评测
        ↓
[4] 确定性执行信封（见 D2）→ 证据账本
        ↓
[5] LlmComposer：结构化 claims+refs → 零 LLM 校验 → 至多一次修复 → 降级说明
        ↓
[6] 回答 + 证据 + 判定 + 未覆盖项 + trace
```

**受控 replan**：只允许两种情况重规划——(a) 某步工具失败且计划里有等价替代工具；(b) 证据不足以回答且还有预算。重规划次数上限 1，且必须落库。**不允许**"模型觉得还能再查点什么"就无限下一轮。

### D2 执行信封：所有强制项都在代码里

| 强制项 | 现状 | 本提案新增 |
|---|---|---|
| 工具调用次数 / 模型调用次数预算 | ✅ `BudgetGuard` | 按 Plan 预估成本，超预算在**执行前**拒绝而不是中途截断 |
| 每工具超时 | ✅ `ToolSpec.latency_budget_ms` | 分档超时（数据类 15s / 扫描类 120s / 任务类 立即返回 job_id） |
| 档位目标延迟 | ✅ 本轮已让"超出即告知" | 同上，Plan 阶段给出预期耗时区间 |
| 工具结果 → 证据 | ✅ `ToolResult.evidence` | 证据**账本**（跨轮累积、编号全局唯一、标注轮次与 as_of） |
| 无进展检测 | ✅ `NoProgressGuard` | 扩展到"计划步与已执行步重复"即停 |
| 熔断 | ❌ | 数据源连续失败 N 次 → 该源短路并在回答里声明（避免每轮都白等 60s） |
| 幂等与去重 | ❌ | 同一 (tool, args, as_of) 在会话内复用结果（省成本，也让多轮一致） |
| 并发 | ❌（串行） | 计划里无依赖的步骤并行执行；SSE 事件带 step_id |

### D3 工具面：三层 + 决策粒度

**按"决策问题"封装，而不是按"数据表"封装**——这是原子化的关键判据：

| 层 | 判据 | 例子 | 数量级 |
|---|---|---|---|
| **L1 决策工具** | 一个工具回答一个判断问题，输出含结论+依据+弃权 | `signal_washout_probe`、`portfolio_risk_review`、`market_mainline_view`、`report_conclusion_lookup`、`verdict_outcome_lookup` | ~10 个，**常驻提示词** |
| **L2 证据工具** | 提供事实，不做判断 | 现有 19 个数据工具、新闻、筹码/分时/资金流、板块 | ~40 个，**按需检索注入** |
| **L3 任务工具** | 分钟级，**异步** | 全文研报生成（`submit_report_job` → `poll_report_job` → `get_report`） | ~5 个 |

**L3 为什么必须异步**：同步工具面里放分钟级管道，会让 MCP 客户端超时重试、让对话循环卡住、让预算记账失真。异步协议：`submit` 立即返回 `job_id`（工具耗时 <1s），`poll` 返回进度与终态，`get` 取结果。会话槽位里记住 job_id，多轮里自然续接。

**工具面的三个出口共用一份 `ToolSpec`**（已有设计）：对话 Agent（进程内）、MCP（对外）、LangGraph 分析师管道。新增两个属性：`asynchronous: bool`、`plan_layer: "L1"|"L2"|"L3"`。

### D4 工具检索：不让工具数量压垮上下文

L1 常驻；L2 按 `Plan.intent` 检索（首版用**关键词+标签**检索，不引入向量库——工具只有几十个，加 embedding 是过度设计；工具数过百再上向量检索）。注入给 Planner 的是**工具清单摘要**（名称+一句话+成本），注入给 Composer 的是**已取到的证据**而不是工具清单。

### D5 会话状态：显式槽位，而不是"把历史塞进提示词"

```python
@dataclass
class ConversationState:
    conversation_id: str
    slots: Slots            # symbols / horizon / as_of / focus(洗盘|估值|资金|主线…)
    ledger: EvidenceLedger  # 跨轮证据，同 as_of 去重，带 round
    pending: PendingAction | None   # 待确认动作（保持既有"提议-确认"语义）
    uncovered: tuple[str, ...]      # 历史未覆盖项（新问题若涉及，需重新取数或声明）
    job_ids: tuple[str, ...]        # 异步任务
```

指代消解（"它""这只""那两个"）优先在**槽位层**做（确定性：指向上一轮 symbols），LLM 只在槽位冲突时介入。这比"把 6 轮对话塞进提示词让模型猜"便宜且可测。

### D6 回答层不动：这是护城河

零 LLM 校验器、覆盖声明、数字必须出自被引用证据、判定不可覆写、免责与语言护栏、信号层纯函数（AST 强制）——**一律保持**。LLM 自由度只增加在"决定做什么"，不增加在"结果算不算数"。

新增一条：**L1 决策工具的输出不得被 Composer 改写**（今天已做到：判定由代码渲染、模型只解释）。同理，L1 的结论文本也应由代码渲染、模型只做连接与解释。

### D7 可观测与评测：没有评测就没法说"更好了"

* **turn trace**：`turn_id`、plan_json、每步 `{tool, args_hash, elapsed_ms, ok, evidence_count}`、tokens、成本、校验结论、终态。落库（已有 `chat_messages` 可扩展）。
* **黄金集**：50–100 条真实问句 → 期望 `{intent, symbols, 必需工具集合, 必须弃权的维度}`。指标：planning 准确率、工具精确率/召回率、错误工具调用率、弃权正确率、P50/P95 延迟、单轮成本。
* **回归门槛**：新版本在黄金集上 planning 准确率不得低于上一版，成本不得上升超过 20%。

### D8 安全与合规

* 越权：工具的服务层实现**必须**以 `user_id` 为作用域（现有服务已是）；Agent 侧不做鉴权判断（不可信）。
* 动作类工具：仅 `surface="product_chat"` 且必须用户确认（现状保持）。
* 免责与"不得给出买卖指令"：`DirectiveLanguageGuard` + `DisclaimerInjector` 保持拦截所有出口（含 MCP）。
* 提示注入：工具返回的**外部文本**（新闻、公告）一律作为数据而非指令注入，且在提示词里显式标注为不可信区间。

## Risks / Trade-offs

| 风险 | 影响 | 处理 |
|---|---|---|
| LLM planner 不稳定 | 同一问题不同 plan | 规则前置 + schema 校验 + 低置信降级 + 黄金集门槛 + plan 落库（可观测漂移） |
| 工具面扩张导致选择变差 | 调错工具、成本上升 | 三层 + 检索注入 + L1 常驻 + 黄金集工具精确率指标 |
| 成本上升 | 每轮多一次 planner 调用 | 规则前置吃掉高频问句；planner 用便宜模型（可与回答模型分开配置） |
| 延迟上升 | planner 增加一次往返 | 规则命中的问句零新增延迟；未命中的本来就答不了（今天直接 400） |
| 异步任务与多轮耦合复杂 | 状态错乱 | job_id 进槽位；poll 幂等；终态一次性消费 |
| 原子化把服务层逻辑复制一遍 | 出现第二份实现漂移 | 工具 handler **只做转换**，业务逻辑留在服务层（沿用 `signals.py` 的既有做法） |

## Migration Plan（可回滚）

1. 每一步都**新增**，不删旧路径：规则分类器保留，LLM planner 作为其后的兜底实现注入 `DegradingClassifier`。
2. 灰度开关：环境变量/用户白名单启用 LLM planner；关闭即回到今天行为。
3. 工具逐个接入，每个工具独立可回滚（未注入端口 = 不注册 = 与今天一致）。
4. 观测先行：先上 trace 与黄金集基线，再上 planner，才能证明"更好了"。

## Open Questions

见 `proposal.md` 文末三条产品决策。

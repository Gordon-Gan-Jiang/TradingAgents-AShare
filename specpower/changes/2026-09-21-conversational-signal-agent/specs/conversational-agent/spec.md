## ADDED Requirements

### Requirement: Streaming answer protocol
The conversational endpoint SHALL stream the assistant's answer as server-sent events, emitting answer text deltas as they are produced rather than returning only a job acknowledgement.

#### Scenario: Answer text streamed
- **WHEN** a user submits a conversational request
- **THEN** the response stream MUST contain incremental answer text events before the stream terminates

#### Scenario: Structured probe result streamed
- **WHEN** a deterministic probe produces a verdict during the turn
- **THEN** the stream MUST emit a verdict event carrying the structured payload, separate from the prose answer

#### Scenario: Tool activity streamed
- **WHEN** a tool is invoked during the turn
- **THEN** the stream MUST emit a tool-activity event identifying the tool and its completion status

#### Scenario: Terminal event always sent
- **WHEN** a turn ends for any reason, including error or cancellation
- **THEN** the stream MUST emit a terminal event describing the outcome

#### Scenario: No answer regression to acknowledgement
- **WHEN** a conversational request is served
- **THEN** the response MUST NOT consist solely of a job identifier acknowledgement

### Requirement: Bounded tool-calling loop
The conversational agent SHALL execute a bounded tool-calling loop with a configured maximum number of rounds and a token budget.

#### Scenario: Round limit reached
- **WHEN** the loop reaches its configured maximum round count
- **THEN** it MUST stop invoking tools and produce an answer from the evidence already collected, stating that the investigation was truncated

#### Scenario: Token budget reached
- **WHEN** accumulated token usage reaches the configured turn budget
- **THEN** the loop MUST stop and return the partial answer with an explicit budget notice

#### Scenario: Loop cannot recurse into pipeline
- **WHEN** the agent considers invoking a tool classified `expensive`
- **THEN** the invocation MUST be refused and the agent MUST instead offer to start the heavy tier as an explicit user-confirmed action

### Requirement: Verdict immutability
The conversational agent SHALL treat deterministic probe verdicts as authoritative and MUST NOT alter, override, or contradict a verdict's state or confidence in its prose answer.

#### Scenario: Model attempts to change the verdict
- **WHEN** a model response asserts a different verdict state or a confidence outside the probe's reported range
- **THEN** the guard MUST reject or repair the response so that the emitted verdict event and the prose remain consistent with the probe output

#### Scenario: Model may add interpretation
- **WHEN** the model explains a verdict in natural language, references the user's position, or suggests observation points
- **THEN** the added interpretation MUST be permitted, provided it does not assert a different verdict state

#### Scenario: Confidence not inflated
- **WHEN** the probe reports reduced confidence because inputs were unavailable or degraded
- **THEN** the prose MUST NOT present the judgement as more certain than the probe reported

### Requirement: Batch-capable tools for multi-instrument questions
The tool surface SHALL provide batch variants for probes so that a question spanning many instruments is answerable within the tool-call budget.

#### Scenario: Portfolio-wide question served in one call
- **WHEN** a question concerns every instrument in a user's holdings
- **THEN** the probe MUST be invocable once with a list of symbols, returning a per-symbol result and a ranking, rather than requiring one call per instrument

#### Scenario: Per-symbol evidence preserved in batch
- **WHEN** a batch probe returns
- **THEN** each instrument's evidence MUST be present in the same result, so that grounding verification remains complete

#### Scenario: Batch bounded
- **WHEN** a batch request exceeds the configured instrument limit
- **THEN** the request MUST be refused or truncated with an explicit notice, and MUST NOT silently evaluate a subset

#### Scenario: Per-symbol abstention preserved
- **WHEN** some instruments in a batch lack sufficient evidence
- **THEN** those instruments MUST individually report `insufficient_evidence` and MUST NOT be dropped or coerced into a directional state

### Requirement: Action proposals require explicit confirmation
When a request implies an action on platform state, the assistant SHALL propose the action and MUST NOT execute it without explicit user confirmation.

#### Scenario: Monitoring request becomes a proposal
- **WHEN** a user asks to be alerted on a price condition
- **THEN** the assistant MUST propose creating the corresponding alert or tracking task and MUST NOT create it unconfirmed

#### Scenario: Confirmation executes
- **WHEN** the user confirms a proposed action
- **THEN** the action MUST be executed through the existing platform service for that capability rather than by a new parallel mechanism

#### Scenario: Declined proposal recorded
- **WHEN** the user declines or ignores a proposed action
- **THEN** no state change MUST occur and the proposal MUST be recorded in the conversation

#### Scenario: Actions unavailable on external surface
- **WHEN** the request arrives over the external tool surface
- **THEN** no state-changing action MUST be offered or performed in this version

#### Scenario: Prompt acknowledgement is not execution
- **WHEN** the assistant cannot execute an action
- **THEN** it MUST NOT imply that the action has been set up

### Requirement: Clarification before assumption
When required context is missing, the assistant SHALL ask for it and MUST NOT proceed on an assumed default.

#### Scenario: Position status unknown
- **WHEN** a position-related question is asked for an instrument that is not in the user's resolved holdings
- **THEN** the assistant MUST ask whether the user holds it and at what cost, and MUST NOT answer on the assumption of no position

#### Scenario: Ambiguous instrument
- **WHEN** a request matches more than one plausible instrument
- **THEN** the assistant MUST present the candidates and await selection

#### Scenario: Missing objective
- **WHEN** a judgement depends on the user's objective, horizon, or risk tolerance and none is configured
- **THEN** the assistant MUST ask, or explicitly state which assumption it is using, and MUST NOT silently apply a default

#### Scenario: Context reuse within a conversation
- **WHEN** the user has already supplied a missing value earlier in the same conversation
- **THEN** the assistant MUST reuse it and MUST NOT ask again

### Requirement: Conversation persistence
The system SHALL persist conversations and their messages with per-user isolation.

#### Scenario: Conversation created
- **WHEN** a user starts a new conversational turn without specifying an existing conversation
- **THEN** a conversation record MUST be created and linked to that user

#### Scenario: Messages persisted
- **WHEN** a turn completes
- **THEN** the user message, the assistant answer, the tool activity, and any verdict reference MUST be persisted against the conversation

#### Scenario: Cross-user isolation
- **WHEN** a user requests a conversation belonging to another user
- **THEN** the request MUST be refused and MUST NOT disclose any content of that conversation

#### Scenario: Message ordering stable
- **WHEN** a conversation is replayed
- **THEN** messages MUST be returned in a stable chronological order

### Requirement: Multi-turn context reuse
The conversational agent SHALL resolve user context from existing platform state rather than requiring it to be restated each turn.

#### Scenario: Position context resolved
- **WHEN** a request concerns a symbol the user holds
- **THEN** the agent MUST resolve the position from the trade ledger or imported positions and pass it to position-aware tools

#### Scenario: Preference context resolved
- **WHEN** a request is served for a user with configured risk profile or investment horizon
- **THEN** those preferences MUST be applied without re-asking in the same conversation

#### Scenario: Prior verdict referenced
- **WHEN** the conversation contains an earlier probe verdict for the same symbol
- **THEN** the agent MUST be able to reference that verdict and its subsequent outcome

#### Scenario: Context not fabricated
- **WHEN** position, preference, or prior-verdict context cannot be resolved
- **THEN** the agent MUST proceed as `unknown` and MUST NOT assume a default holding

### Requirement: Cancellation and resource release
A streaming turn SHALL be cancellable, and cancellation MUST release in-flight work.

#### Scenario: Client disconnects
- **WHEN** the client aborts a streaming turn
- **THEN** the server MUST stop the loop, release the model call, and MUST NOT persist a partial answer as a completed turn

#### Scenario: Cancelled turn recorded
- **WHEN** a turn is cancelled
- **THEN** the conversation MUST record the turn as cancelled rather than completed

### Requirement: Disclosure and safe phrasing
Every conversational answer that conveys a judgement SHALL carry the platform's research-only disclaimer and MUST avoid directive trading language.

#### Scenario: Disclaimer present
- **WHEN** an answer contains a judgement, verdict, or position-related suggestion
- **THEN** the answer MUST include the research-only disclaimer

#### Scenario: No directive phrasing
- **WHEN** an answer is generated
- **THEN** it MUST NOT contain imperative trade instructions such as unconditional buy or sell commands, and MUST instead be phrased as conditional observations with stated invalidation conditions

### Requirement: Turn accounting
Each conversational turn SHALL record its tier, model, tool invocations, token usage, and latency.

#### Scenario: Accounting persisted
- **WHEN** a turn completes
- **THEN** tier, model identity, tool names, token counts, and elapsed time MUST be persisted

#### Scenario: Cost attribution queryable
- **WHEN** turn records are queried
- **THEN** cost and latency MUST be attributable per tier, so that medium-tier savings relative to the heavy tier are measurable

### Requirement: 对话页可选择模型
The system SHALL let the user choose which model profile answers on the conversational panel, and the chosen profile SHALL determine the model actually used.

#### Scenario: 选择随请求发送
- **WHEN** 用户在研判对话页选择一个模型档案并提问
- **THEN** 请求 MUST 携带该档案的 `model_profile_id`，且回答 MUST 由该档案的 provider/模型/密钥产生

#### Scenario: 选择被记住
- **WHEN** 用户选择过某个档案后刷新页面
- **THEN** 选择 MUST 保持；若该档案已被删除或停用，MUST 回落到默认档案而不是继续发送失效 id

#### Scenario: 模型失败时可自助修复
- **WHEN** 回答因模型鉴权/未配置而失败
- **THEN** 界面 MUST 给出直达「系统 → 模型管理」的入口，且提示中的导航名 MUST 与界面实际一致

#### Scenario: 未配置任何档案
- **WHEN** 用户没有任何启用中的模型档案
- **THEN** 界面 MUST 提示去新建，MUST NOT 呈现一个看似可用的空选择器

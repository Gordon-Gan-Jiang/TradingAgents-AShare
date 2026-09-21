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

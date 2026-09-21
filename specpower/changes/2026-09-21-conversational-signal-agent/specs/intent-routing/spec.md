## ADDED Requirements

### Requirement: Three-tier intent classification
The system SHALL classify every conversational request into exactly one of three execution tiers — `light`, `medium`, or `heavy` — before any analysis work is scheduled.

#### Scenario: Data question classified light
- **WHEN** a request asks only for a factual datum such as a current price, turnover rate, or net fund flow
- **THEN** the request MUST be classified `light` and MUST NOT start the multi-analyst pipeline

#### Scenario: Judgement question classified medium
- **WHEN** a request asks for a judgement that a deterministic probe can ground, such as whether a symbol is being shaken out or whether an existing position should be held
- **THEN** the request MUST be classified `medium` and MUST be served by probe tools plus a single summarising model call

#### Scenario: Full research request classified heavy
- **WHEN** a request explicitly asks for a full report, a comprehensive analysis, or a complete multi-analyst review
- **THEN** the request MUST be classified `heavy` and MUST be served by the existing multi-analyst pipeline

#### Scenario: Single-symbol extraction no longer implies heavy
- **WHEN** a request names a symbol but expresses a bounded question
- **THEN** the presence of a resolvable symbol alone MUST NOT cause classification as `heavy`

### Requirement: Tier budgets
Each tier SHALL have a declared latency and cost budget that the orchestrator enforces.

#### Scenario: Light tier budget
- **WHEN** a request is served at the `light` tier
- **THEN** the target latency MUST be under 5 seconds and the model call count MUST be one

#### Scenario: Medium tier budget
- **WHEN** a request is served at the `medium` tier
- **THEN** the target latency MUST be under 30 seconds and the number of tool invocations MUST NOT exceed a configured maximum

#### Scenario: Budget exceeded
- **WHEN** a tier's latency or tool-call budget is exhausted while work is still incomplete
- **THEN** the orchestrator MUST return the evidence gathered so far together with an explicit incompleteness notice, and MUST NOT silently escalate to a higher tier

### Requirement: Unreliable routing degrades downward
When classification is uncertain or fails, the system SHALL degrade to `medium` and MUST NOT degrade to `heavy`.

#### Scenario: Classifier unavailable
- **WHEN** the classification step errors, times out, or returns an unparseable result
- **THEN** the request MUST be served at the `medium` tier

#### Scenario: Ambiguous classification
- **WHEN** the classifier reports low confidence between two adjacent tiers
- **THEN** the lower-cost tier MUST be selected

### Requirement: Explicit clarification instead of guessing
When the request cannot be routed because no actionable subject can be identified, the system SHALL ask for clarification rather than selecting a tier.

#### Scenario: No resolvable symbol
- **WHEN** a judgement-type request cannot be resolved to a symbol after name and code resolution
- **THEN** the system MUST ask the user to identify the instrument and MUST NOT start any analysis

#### Scenario: Multiple candidate symbols
- **WHEN** a request resolves to more than one plausible instrument
- **THEN** the system MUST present the candidates and await selection

### Requirement: Routing decision observability
Every routing decision SHALL be recorded with its tier, the signals used, the classifier confidence, and the resolved entities.

#### Scenario: Decision persisted
- **WHEN** a request completes or fails
- **THEN** the routing decision record MUST include tier, classifier confidence, resolved symbol, and the reason for any degradation

#### Scenario: Tier distribution observable
- **WHEN** routing records are queried over a period
- **THEN** the tier distribution MUST be retrievable so that runaway `heavy` usage is detectable

### Requirement: 多标的问句不得丢标的
The system SHALL treat a message that names several explicit codes as a multi-instrument turn and SHALL judge every named instrument.

#### Scenario: 两个代码的比较问句
- **WHEN** 用户提交「600519 和 300750 哪个好」
- **THEN** 系统 MUST 解析出两个标的并走批量判定，MUST NOT 只保留第一个

#### Scenario: 逐只结论与比较
- **WHEN** 回合涉及多只标的
- **THEN** 回答 MUST 逐只给出结论再作比较，且 MUST NOT 把某一只的结论套到另一只上

#### Scenario: 名称歧义仍要追问且列出候选
- **WHEN** 用户用名称提到两只（如「茅台和宁德时代哪个好」）但未写代码
- **THEN** 系统 MUST 追问选哪一只，且追问 MUST 列出已解析出的候选代码

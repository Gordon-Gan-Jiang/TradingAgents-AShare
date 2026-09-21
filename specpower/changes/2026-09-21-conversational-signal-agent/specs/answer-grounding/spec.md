## ADDED Requirements

### Requirement: Evidence completeness on every tool result
Every tool result SHALL expose an evidence set covering every statable scalar in its payload, and SHALL declare which data dimensions were available and which were not.

#### Scenario: Scalars covered
- **WHEN** a tool result contains a numeric or dated value that a caller could state in an answer
- **THEN** that value MUST appear in the result's evidence set with its unit, source key, and `as_of`

#### Scenario: Coverage declared
- **WHEN** a tool result is returned
- **THEN** it MUST declare an `available` list of data dimensions and an `unavailable` list of dimensions that could not be read

#### Scenario: Scored observables are not the only evidence
- **WHEN** a tool returns values that did not contribute to scoring but may be quoted
- **THEN** those values MUST still be present in the evidence set

#### Scenario: Missing evidence blocks the value
- **WHEN** a caller states a value that is absent from every evidence set produced in the turn
- **THEN** that statement MUST be rejected as unsourced

### Requirement: Answer composed from cited claims
The assistant answer SHALL be produced by rendering a structured set of claims, each carrying explicit references to evidence items, and the assistant MUST NOT emit its explanatory prose as free-form unverifiable text.

#### Scenario: Claims carry references
- **WHEN** the model produces an answer
- **THEN** every claim MUST carry at least one reference to an evidence item supplied in the turn

#### Scenario: Renderer produces the prose
- **WHEN** the answer text is emitted
- **THEN** it MUST be produced by the renderer from the structured claims, and each rendered claim MUST remain traceable to its references

#### Scenario: Streaming preserved
- **WHEN** the answer is streamed
- **THEN** it MUST still be streamed incrementally as claims are rendered, and streaming MUST NOT be achieved by bypassing the structured form

#### Scenario: Verdict reference required
- **WHEN** the answer conveys a probe verdict
- **THEN** it MUST reference the probe result produced in the same turn, and MUST NOT restate a verdict from model memory

#### Scenario: Unavailable dimensions surfaced
- **WHEN** the turn's coverage declares a dimension unavailable
- **THEN** the structured answer MUST list it as unavailable rather than omitting it silently

### Requirement: Mechanical citation verification
The system SHALL verify every answer against the turn's evidence using deterministic code, and MUST NOT rely on a language model to judge whether the answer is grounded.

#### Scenario: Unknown reference rejected
- **WHEN** a claim references an identifier that is not present in the turn's evidence set
- **THEN** the answer MUST be rejected

#### Scenario: Uncited claim rejected
- **WHEN** a claim carries no reference
- **THEN** the answer MUST be rejected

#### Scenario: Number must match the cited evidence
- **WHEN** a claim states a number
- **THEN** that number MUST match a scalar belonging to the evidence items that claim references, and matching a scalar from an unrelated evidence item MUST NOT be accepted

#### Scenario: Unit normalisation applied
- **WHEN** a stated number is expressed in a different but equivalent unit or magnitude than the evidence value
- **THEN** the comparison MUST normalise units before deciding, so that equivalent expressions are accepted

#### Scenario: Rounding tolerance applied
- **WHEN** a stated number differs from the evidence value only by rounding within the configured relative tolerance
- **THEN** the comparison MUST accept it rather than reporting a fabrication

#### Scenario: Ratio and percentage treated as numbers
- **WHEN** a claim states a ratio or percentage
- **THEN** it MUST be verified as a numeric claim against the cited evidence

#### Scenario: Date assertions verified
- **WHEN** a claim asserts a date or a relative day such as yesterday
- **THEN** it MUST resolve to a date present in the `as_of` values of the cited evidence

#### Scenario: Verification performed without a model
- **WHEN** verification runs
- **THEN** it MUST be implemented as deterministic code with unit tests, and MUST NOT invoke any model

### Requirement: Bounded repair then honest degradation
A failed verification SHALL trigger at most one repair attempt, after which the system SHALL degrade to an explicit insufficiency answer rather than emitting unverified content.

#### Scenario: Single repair attempt
- **WHEN** verification fails
- **THEN** the system MUST attempt at most one regeneration constrained by the verification failure

#### Scenario: Repair fails
- **WHEN** the regenerated answer fails verification again
- **THEN** the system MUST return an explicit answer stating that a grounded answer could not be produced, listing the evidence that was available

#### Scenario: No unverified content emitted
- **WHEN** an answer cannot pass verification
- **THEN** no unverified text MUST be emitted to the user

#### Scenario: Rejection recorded
- **WHEN** a rejection or repair occurs
- **THEN** it MUST be recorded with the failing reason so that rejection rates are measurable

### Requirement: Structured output fallback is measured
When structured answer parsing fails, the system SHALL fall back to a weaker textual verification path and SHALL record the fallback rate rather than treating it as normal operation.

#### Scenario: Parse failure falls back
- **WHEN** the structured answer cannot be parsed
- **THEN** the system MUST apply the textual numeric post-check and MUST mark the turn as fallback

#### Scenario: Fallback rate exposed
- **WHEN** answer quality metrics are requested
- **THEN** the structured fallback rate MUST be reported

#### Scenario: Fallback not silently accepted
- **WHEN** the fallback rate is non-trivial
- **THEN** it MUST be surfaced as an actionable signal that the prompt or schema requires adjustment, and MUST NOT be presented as acceptable steady state

### Requirement: Unsourced assertion rate is structurally zero
The system SHALL guarantee by construction, not by monitoring, that no unsourced numeric or date assertion reaches the user.

#### Scenario: Guarantee by rejection
- **WHEN** an assertion cannot be traced to the turn's evidence
- **THEN** it MUST be rejected before emission, so that the observable unsourced rate is zero rather than merely low

#### Scenario: Distinction recorded in reporting
- **WHEN** answer quality is reported
- **THEN** metrics guaranteed by construction MUST be distinguished from metrics that are only monitored and calibrated

### Requirement: Explicit insufficiency instead of vague hedging
When no evidence can support an answer, the system SHALL refuse explicitly and state what is missing, and MUST NOT substitute hedging language for an answer.

#### Scenario: Unsupported question refused
- **WHEN** a question cannot be supported by any available tool, such as a request to predict the next day's direction
- **THEN** the system MUST decline and state which evidence would be required

#### Scenario: Unavailable dimension disclosed
- **WHEN** a dimension relevant to the question is unavailable
- **THEN** the answer MUST state that the dimension was not covered and MUST NOT describe it as having no material impact

#### Scenario: Causal claims require sources
- **WHEN** the answer offers a cause for a price move
- **THEN** the cause MUST be supported by cited evidence such as news, announcements, or disclosed trading records, and MUST NOT be inferred from price action alone

#### Scenario: Refusal recorded as valid outcome
- **WHEN** the system declines for lack of evidence
- **THEN** the refusal MUST be recorded as a deliberate outcome and MUST NOT be counted as a system failure

### Requirement: Verdict authority preserved through rendering
Rendering SHALL NOT alter the verdict state or confidence produced by the probe, and the rendered answer MUST remain consistent with the probe result.

#### Scenario: Rendered state matches probe
- **WHEN** an answer renders a verdict
- **THEN** the rendered state and confidence MUST equal the probe result values

#### Scenario: Renderer cannot upgrade confidence
- **WHEN** the probe reported reduced confidence because inputs were unavailable
- **THEN** the rendered answer MUST NOT present the judgement with higher confidence

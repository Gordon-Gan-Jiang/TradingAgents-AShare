## ADDED Requirements

### Requirement: Four-state washout verdict
The washout probe SHALL return exactly one of four states — `washout`, `distribution`, `trend_break`, or `insufficient_evidence` — and MUST NOT return a binary yes/no judgement.

#### Scenario: Washout state returned
- **WHEN** contraction, intraday absorption, and relative-strength evidence align at an early-stage position
- **THEN** the verdict state MUST be `washout` with a confidence value and the contributing evidence

#### Scenario: Distribution state returned
- **WHEN** volume expands on decline, super-large orders dominate net outflow, and the position is late-stage
- **THEN** the verdict state MUST be `distribution`

#### Scenario: Trend break state returned
- **WHEN** the symbol breaks its reference moving average and fails to reclaim it within the configured window while relative strength deteriorates
- **THEN** the verdict state MUST be `trend_break`

#### Scenario: Insufficient evidence state returned
- **WHEN** required components are unavailable or mutually contradictory beyond the configured tolerance
- **THEN** the verdict state MUST be `insufficient_evidence`, and the response MUST name the missing components

### Requirement: Eight-dimension discriminant matrix
The probe SHALL evaluate at least eight evidence dimensions — downside volume behaviour, price drawdown and recovery, moving-average structure, relative strength versus board and market, price position, capital flow composition, chip distribution, and intraday absorption — and MUST report each dimension's raw observables.

#### Scenario: All dimensions reported
- **WHEN** a verdict is produced
- **THEN** the result MUST report every dimension's raw observable, weight, contribution, and availability status, including dimensions contributing zero

#### Scenario: Dimension availability distinguished
- **WHEN** a dimension cannot be computed from available data
- **THEN** it MUST be reported as unavailable with a reason, and MUST NOT be reported as a neutral zero contribution without that marker

### Requirement: Position modulates rather than adds
The probe SHALL apply the position dimension as a modulator on the price and volume evidence, and MUST NOT include position as an independent additive term.

#### Scenario: Identical pattern at different positions differs
- **WHEN** the same contraction-and-drawdown observations are evaluated at an early-stage and a late-stage cumulative advance
- **THEN** the modulation MUST lower the washout-side score at the late-stage position and raise it at the early-stage position, with all other observables identical

#### Scenario: Modulator exposed
- **WHEN** a verdict is returned
- **THEN** the raw position observables and the applied modulator factor MUST be exposed separately from additive contributions

#### Scenario: Late-stage distribution bias
- **WHEN** a symbol's cumulative advance exceeds the late-stage threshold and downside volume expands
- **THEN** the probe MUST be biased toward `distribution` over `washout`

### Requirement: Contraction must not be read as accumulation without relative turnover
The probe SHALL require relative turnover evidence before treating contraction as washout-side evidence.

#### Scenario: Broad contraction blocks washout
- **WHEN** turnover is below both the symbol's own history and its board median
- **THEN** the contraction dimension MUST NOT contribute positive washout evidence and MUST increase the `distribution` or `trend_break` side

#### Scenario: Relative-strength contraction permits washout
- **WHEN** turnover is below the symbol's own history but at or above the board median
- **THEN** the contraction dimension MAY contribute positive washout evidence

#### Scenario: Peer data unavailable
- **WHEN** board median turnover cannot be computed
- **THEN** the contraction dimension MUST be marked unavailable and the verdict confidence MUST be reduced

### Requirement: Mandatory falsification conditions
Every verdict other than `insufficient_evidence` SHALL include at least two falsification conditions, each expressed as an observable, checkable future condition with a threshold and a time window.

#### Scenario: Falsifiers present
- **WHEN** a verdict state other than `insufficient_evidence` is returned
- **THEN** the result MUST include at least two falsification conditions naming the observable, the threshold, and the window

#### Scenario: Falsifier references real anchors
- **WHEN** a falsification condition references a price or flow threshold
- **THEN** that threshold MUST be derived from the computed observables and MUST be traceable to them

#### Scenario: Falsified verdict signalled on subsequent evaluation
- **WHEN** a later evaluation determines that a persisted verdict's falsification condition was met
- **THEN** that verdict MUST be marked falsified and MUST be surfaced as such when referenced

### Requirement: Structural abstention criteria
The probe SHALL decide abstention from evidence completeness and the separation between the two directional evidence masses, and MUST NOT use an absolute score threshold on a single aggregated score.

#### Scenario: Core dimensions unavailable
- **WHEN** any of the four core dimensions — volume behaviour, price action, position, or relative strength — cannot be computed
- **THEN** the verdict state MUST be `insufficient_evidence` and the unavailable dimension MUST be named

#### Scenario: Insufficient evidence separation
- **WHEN** the separation margin between the washout-side and distribution-side evidence masses is below the configured minimum ratio
- **THEN** the verdict state MUST be `insufficient_evidence`

#### Scenario: Margin defined as a ratio
- **WHEN** the separation margin is computed
- **THEN** it MUST be computed as the absolute difference of the two evidence masses divided by their sum, so that no absolute score threshold is required

#### Scenario: Contradictory evidence
- **WHEN** dimensions support opposing states with approximately equal strength as measured by the margin ratio
- **THEN** the verdict MUST be `insufficient_evidence` and the contradiction MUST be described

#### Scenario: Abstention is a valid product outcome
- **WHEN** the probe abstains
- **THEN** the abstention MUST be reported as an explicit outcome and MUST NOT be converted into a directional opinion by any downstream layer

### Requirement: Confidence derived by anchored mapping
The probe SHALL derive its reported confidence from the evidence separation margin through a monotone anchored mapping, and MUST NOT report a confidence value that is independent of the computed margin.

#### Scenario: Confidence follows margin
- **WHEN** the separation margin is identical across two evaluations with identical input data
- **THEN** the reported confidence MUST be identical

#### Scenario: Quantisation disclosed
- **WHEN** a verdict is reported
- **THEN** the raw separation margin MUST be exposed alongside the confidence value so the mapping can be inspected

### Requirement: High-confidence verdict requires structural evidence
A verdict SHALL be labelled high confidence only when both intraday absorption evidence and chip-distribution evidence are available and the separation margin meets the configured high-confidence ratio.

#### Scenario: Both required evidence classes available
- **WHEN** intraday absorption and chip-distribution evidence are both available and the margin meets the high-confidence ratio
- **THEN** the verdict MAY be labelled high confidence

#### Scenario: Intraday evidence unavailable
- **WHEN** intraday bars are unavailable for the evaluation date
- **THEN** the verdict MUST NOT be labelled high confidence and the missing evidence MUST be disclosed

#### Scenario: Chip evidence unavailable
- **WHEN** chip-distribution data is unavailable
- **THEN** the verdict MUST NOT be labelled high confidence and the missing evidence MUST be disclosed

#### Scenario: Daily-only approximation labelled
- **WHEN** the probe produces a verdict using daily bars only
- **THEN** the result MUST be explicitly labelled as a daily-only approximation and MUST remain usable for calibration

### Requirement: Abstention rate as a first-class quality signal
The system SHALL track and expose the probe's abstention rate so that a probe which never abstains is detectable.

#### Scenario: Abstention rate reported
- **WHEN** probe quality metrics are requested
- **THEN** the abstention rate MUST be reported together with directional accuracy

#### Scenario: Implausibly low abstention rate
- **WHEN** the abstention rate falls below the configured healthy lower bound
- **THEN** the system MUST surface this as a quality warning rather than as an improvement

### Requirement: Evidence anchoring with verbatim sources
The probe SHALL attach to every contributing dimension the underlying raw values with their source keys and `as_of` timestamps.

#### Scenario: Contribution traced
- **WHEN** a dimension contributes to the verdict
- **THEN** the result MUST include the raw values, the source key, and `as_of` for that dimension

#### Scenario: Source unavailable
- **WHEN** a dimension's source could not be read
- **THEN** the result MUST record the source failure with its reason and MUST NOT fabricate a value

### Requirement: Verdict persistence and outcome linkage
Each verdict SHALL be persisted with its full weight snapshot and linked to a later outcome evaluation.

#### Scenario: Verdict persisted
- **WHEN** a verdict is produced
- **THEN** it MUST be persisted with symbol, evaluation date, verdict state, confidence, all dimension contributions, the falsification conditions, `signal_version`, and the requesting user

#### Scenario: Outcome scheduled
- **WHEN** a verdict is persisted
- **THEN** outcome evaluations at the configured horizons MUST be scheduled

#### Scenario: Historical verdicts immutable
- **WHEN** weights or rules change
- **THEN** previously persisted verdicts MUST NOT be rewritten, and their `signal_version` MUST continue to identify the model that produced them

### Requirement: Probe exposure on all consumer surfaces
The washout probe SHALL be reachable from the conversational tier, from a direct HTTP endpoint, and from the MCP surface.

#### Scenario: Conversational invocation
- **WHEN** a user asks whether a symbol is being shaken out
- **THEN** the medium tier MUST invoke the probe and stream its structured verdict alongside the prose answer

#### Scenario: Direct HTTP invocation
- **WHEN** a caller requests the probe endpoint for a symbol and date
- **THEN** the structured verdict MUST be returned without requiring a model call

#### Scenario: MCP invocation
- **WHEN** an external agent invokes the probe over MCP
- **THEN** the structured verdict MUST be returned with evidence, freshness, and the disclaimer

### Requirement: Research-only framing
The probe output SHALL be framed as a probabilistic research judgement about unobservable intent, and MUST include the disclaimer and invalidation conditions.

#### Scenario: Framing present
- **WHEN** a verdict is returned on any surface
- **THEN** the payload MUST state that the judgement is a probabilistic inference from observable data, include the disclaimer, and include the invalidation conditions

#### Scenario: No directive output
- **WHEN** a verdict is rendered
- **THEN** it MUST NOT contain directive trade instructions and MUST NOT assert certainty about another party's intent

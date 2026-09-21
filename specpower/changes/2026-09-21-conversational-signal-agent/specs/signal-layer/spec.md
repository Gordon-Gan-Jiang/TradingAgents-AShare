## ADDED Requirements

### Requirement: Deterministic signal computation
The signal layer SHALL compute every market-behaviour metric as a pure, deterministic function of explicitly supplied input data, and MUST NOT invoke any LLM, network call, or wall-clock read inside a signal function.

#### Scenario: Identical inputs produce identical output
- **WHEN** the same input dataset and the same `as_of` timestamp are supplied to a signal function twice
- **THEN** the returned score components MUST be byte-identical, with no dependence on LLM sampling or current time

#### Scenario: Signal function requires explicit data
- **WHEN** a signal function is invoked
- **THEN** it MUST receive its data through parameters or an injected data port, and MUST NOT fetch data itself

#### Scenario: Insufficient input rows
- **WHEN** a signal function is given fewer rows than its declared minimum window
- **THEN** it MUST return an `insufficient_evidence` result for that component rather than extrapolating or defaulting to a neutral value

### Requirement: Signal version identifier
Every signal computation SHALL carry a `signal_version` string that identifies the scoring model and weight set used.

#### Scenario: Version attached to output
- **WHEN** any signal or probe result is produced
- **THEN** it MUST include a non-empty `signal_version`

#### Scenario: Version changes with weights
- **WHEN** any dimension weight or scoring rule changes
- **THEN** `signal_version` MUST change, so that persisted verdicts remain attributable to the model that produced them

### Requirement: Volume contraction measurement
The signal layer SHALL measure downside volume contraction as a ratio of current downside-bar volume to a trailing average volume, and SHALL measure whether volume recovers on the subsequent rebound.

#### Scenario: Downside contraction ratio
- **WHEN** a down bar's volume is compared against the trailing 5-day average volume
- **THEN** the layer MUST emit the ratio as a raw observable alongside the derived component score

#### Scenario: Rebound volume recovery
- **WHEN** a rebound bar follows a contraction sequence
- **THEN** the layer MUST emit a rebound-volume-recovery ratio, because contraction without recovery is indistinguishable from loss of interest

### Requirement: Relative turnover disambiguation
The signal layer SHALL compare a symbol's turnover rate against the median turnover of its sector peers, and MUST NOT rely solely on the symbol's own historical turnover when classifying contraction.

#### Scenario: Symbol turnover below own history and below sector median
- **WHEN** a symbol's turnover is below its own 5-day average and also below its sector median
- **THEN** the result MUST be classified as `broad_contraction` and MUST NOT contribute positive evidence for accumulation

#### Scenario: Symbol turnover below own history but at or above sector median
- **WHEN** a symbol's turnover is below its own 5-day average but at or above its sector median
- **THEN** the result MUST be classified as `relative_strength_contraction` and MAY contribute positive evidence for accumulation

#### Scenario: Sector median unavailable
- **WHEN** sector peer turnover cannot be computed for the symbol's board
- **THEN** the layer MUST mark the component `insufficient_evidence` and MUST NOT silently fall back to self-relative comparison

### Requirement: Position modulation as an interaction term
The signal layer SHALL treat price position (cumulative advance and distance from prior high) as a modulator applied to the price/volume evidence, and MUST NOT treat it as an independently weighted additive component.

#### Scenario: Same pattern at different positions
- **WHEN** an identical contraction-and-drawdown pattern is evaluated once at a cumulative 60-day advance below the early-stage threshold and once above the late-stage threshold
- **THEN** the position modulator MUST reduce the accumulation-side score in the late-stage case and MUST increase it in the early-stage case, with all other components held equal

#### Scenario: Position reported explicitly
- **WHEN** any modulated result is returned
- **THEN** the output MUST expose the raw position observables and the applied modulator factor separately from the component contributions

### Requirement: Intraday absorption measurement
The signal layer SHALL measure downside absorption from intraday data, including lower-shadow ratio, intraday drawdown recovery rate, and closing-session flow direction.

#### Scenario: Intraday data available
- **WHEN** intraday bars for the evaluation date are available
- **THEN** the layer MUST emit lower-shadow ratio and intraday recovery rate as raw observables with their source and `as_of`

#### Scenario: Intraday data unavailable
- **WHEN** intraday bars are unavailable for the evaluation date
- **THEN** the absorption component MUST be marked `unavailable`, its contribution MUST be zero, and the overall result MUST carry a reduced-confidence flag rather than a fabricated value

### Requirement: Chip distribution metrics
The signal layer SHALL consume chip-distribution data (profit-taking ratio, average cost, concentration) when available and expose them as observables.

#### Scenario: Chip data present
- **WHEN** chip-distribution data is available for the symbol and date
- **THEN** profit-taking ratio, average cost, and concentration MUST be emitted as observables with their `as_of`

#### Scenario: Chip data absent
- **WHEN** chip-distribution data is unavailable
- **THEN** the chip component MUST be marked `unavailable` and MUST NOT be substituted by a proxy metric without a documented, versioned rationale

### Requirement: Evidence anchoring on every signal output
Every signal output SHALL attach, for each observable that influenced the score, the raw value, the source key, and the data `as_of` timestamp.

#### Scenario: Component traced to source
- **WHEN** a score component contributes a non-zero amount to a result
- **THEN** the result MUST include an evidence entry carrying the raw value, the source key, and `as_of`

#### Scenario: Unanchored component rejected
- **WHEN** a component contributes to a score but has no corresponding evidence entry
- **THEN** the result MUST be rejected as malformed and MUST NOT be returned to a caller

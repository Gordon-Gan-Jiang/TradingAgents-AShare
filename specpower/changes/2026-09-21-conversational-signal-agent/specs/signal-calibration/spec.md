## ADDED Requirements

### Requirement: Weight snapshot captured with every verdict
Every persisted verdict SHALL record the complete weight set and rule identifiers used to produce it, sufficient to recompute the same score from the same observables.

#### Scenario: Snapshot persisted
- **WHEN** a verdict is persisted
- **THEN** the stored record MUST include every dimension weight, the modulator parameters, and the `signal_version`

#### Scenario: Recomputable
- **WHEN** a stored verdict is replayed against its stored observables and stored weights
- **THEN** the recomputed score MUST match the stored score

#### Scenario: Snapshot is immutable
- **WHEN** weights change after a verdict is stored
- **THEN** the stored snapshot MUST NOT be altered

### Requirement: Horizon outcome backfill
The system SHALL backfill realised outcomes for each persisted verdict at multiple horizons with a minimum of T+3 and T+5 trading days.

#### Scenario: Backfill executes
- **WHEN** the configured horizon has elapsed for a verdict
- **THEN** the realised maximum high, closing price, and return relative to the evaluation date MUST be recorded against that verdict

#### Scenario: Trading calendar respected
- **WHEN** horizons are computed
- **THEN** they MUST count A-share trading days, not calendar days

#### Scenario: Suspended or delisted symbol
- **WHEN** the symbol has no data over the horizon window
- **THEN** the outcome MUST be recorded as not evaluable with the reason, and MUST NOT be counted as a hit or a miss

#### Scenario: Idempotent backfill
- **WHEN** the backfill job runs more than once for the same horizon
- **THEN** it MUST NOT create duplicate outcome rows or alter an already recorded outcome

### Requirement: Operational definition of verdict outcomes
The system SHALL define, and apply consistently, a stated operational outcome for each verdict state.

#### Scenario: Washout success defined
- **WHEN** a `washout` verdict is evaluated
- **THEN** it MUST be counted as realised if the symbol makes a new high above the evaluation-date high within the configured horizon

#### Scenario: Distribution success defined
- **WHEN** a `distribution` verdict is evaluated
- **THEN** it MUST be counted as realised if the symbol fails to make a new evaluation-date high and closes below the evaluation-date close by the configured threshold within the horizon

#### Scenario: Trend break success defined
- **WHEN** a `trend_break` verdict is evaluated
- **THEN** it MUST be counted as realised if the symbol does not reclaim the broken reference level within the configured window

#### Scenario: Abstention excluded from accuracy
- **WHEN** accuracy statistics are computed
- **THEN** `insufficient_evidence` verdicts MUST be excluded from directional accuracy and reported separately as an abstention rate

### Requirement: Per-dimension discriminative power
The system SHALL compute, per evidence dimension, its realised discriminative power on the accumulated outcome sample.

#### Scenario: Dimension statistics produced
- **WHEN** the evaluation job runs
- **THEN** for each dimension it MUST report the sample count, hit rate in each verdict state, and a stated association measure between the dimension's contribution and the realised outcome

#### Scenario: Dimension with no predictive power identified
- **WHEN** a dimension's realised association is null or negative
- **THEN** it MUST be reported as non-discriminative rather than retained silently at its current weight

#### Scenario: Statistics honest about sample size
- **WHEN** a dimension's sample count is below the reporting threshold
- **THEN** the statistics MUST be reported as insufficient sample rather than as a point estimate

### Requirement: Offline replay and forward accumulation have disjoint authorities
The system SHALL support both offline replay over historical data and forward accumulation over live verdicts, and SHALL restrict offline replay to rejecting dimensions while reserving weight activation for forward samples.

#### Scenario: Offline replay produces samples
- **WHEN** the replay harness evaluates a historical symbol and date
- **THEN** it MUST use only data available up to that `as_of` date and MUST record the verdict as a replay sample

#### Scenario: Offline samples may reject a dimension
- **WHEN** a dimension shows no discriminative power on the replay sample
- **THEN** that dimension MAY be rejected or zero-weighted on the basis of replay evidence alone

#### Scenario: Offline samples may not activate weights
- **WHEN** a proposed weight change is justified solely by replay samples
- **THEN** the system MUST refuse to activate it, because the same code selects the sample and tunes the parameters

#### Scenario: Forward samples activate weights
- **WHEN** a weight change is activated
- **THEN** it MUST be supported by forward samples that were produced under a previously activated version without hindsight

#### Scenario: Insufficient local price history
- **WHEN** the locally stored daily price series does not cover the required replay window
- **THEN** the harness MUST fetch the required daily history through the existing vendor routing rather than skipping the sample

#### Scenario: Intraday or chip history unavailable
- **WHEN** intraday or chip-distribution history is unavailable for the replay window
- **THEN** replay MUST still proceed on the daily-only path, and those components MUST be accumulated forward instead

### Requirement: Minimum sample gate before weight change
The system SHALL refuse to change any dimension weight until the accumulated evaluated sample reaches a configured minimum, and MUST NOT apply calibration to under-powered samples.

#### Scenario: Sample below minimum
- **WHEN** calibration is requested and the evaluated sample is below the configured minimum
- **THEN** the system MUST decline to change weights and MUST report the current sample size against the requirement

#### Scenario: Sample above minimum
- **WHEN** the evaluated sample meets the minimum
- **THEN** proposed weight changes MUST be produced together with the sample size and the observed effect for review

#### Scenario: Per-dimension floor enforced
- **WHEN** a specific dimension's weight is proposed for change
- **THEN** that dimension MUST have at least the configured per-dimension minimum number of usable samples, independent of the total sample size

#### Scenario: Existing starvation not repeated
- **WHEN** the calibration mechanism is introduced
- **THEN** the minimum sample requirement MUST be documented and enforced in code, so that the platform does not repeat its prior pattern of strategy weights starved of samples

### Requirement: Time-ordered sample splitting
Calibration SHALL split samples by time and MUST NOT split them randomly.

#### Scenario: Parameters tuned on earlier segment
- **WHEN** thresholds or weights are tuned
- **THEN** only the earlier segment of the time-ordered samples MUST be used

#### Scenario: Accuracy reported on later segment
- **WHEN** realised accuracy is reported
- **THEN** it MUST be computed on the later, held-out segment of the time-ordered samples

#### Scenario: Random splitting rejected
- **WHEN** a splitting strategy is applied
- **THEN** random or shuffled splitting MUST be rejected, because serial correlation in market data leaks across a random split

### Requirement: Threshold calibration protocol
Threshold parameters for evidence separation SHALL be selected by locating a stable plateau on the measured curve, and MUST NOT be selected by choosing a maximum.

#### Scenario: Curve produced
- **WHEN** threshold calibration runs
- **THEN** it MUST produce the realised hit rate and abstention rate across a swept range of separation thresholds

#### Scenario: Plateau located
- **WHEN** a threshold is selected
- **THEN** it MUST correspond to a region where the hit rate has stopped improving steeply, and MUST be verified stable under a perturbation of at least twenty percent

#### Scenario: No plateau available
- **WHEN** no stable plateau exists on the curve
- **THEN** the system MUST report that the dimension set lacks discriminative power and MUST NOT proceed to tune thresholds


### Requirement: Versioned weight activation
Weight changes SHALL take effect only through an explicit, version-identified activation, and the previous version MUST remain selectable for comparison.

#### Scenario: Activation is explicit
- **WHEN** proposed weights are accepted
- **THEN** a new `signal_version` MUST be created and recorded with the sample size and the rationale for the change

#### Scenario: Previous version retained
- **WHEN** a new weight version is activated
- **THEN** the previous version MUST remain available so that verdicts can be produced under it for comparison

#### Scenario: Comparison measurable
- **WHEN** two weight versions have both produced evaluated verdicts
- **THEN** their realised accuracy MUST be comparable over the same symbol and date population

### Requirement: No retroactive evaluation rewriting
Calibration SHALL NOT retroactively relabel historical verdicts as correct or incorrect beyond the stated operational definition applied at evaluation time.

#### Scenario: Definition changes later
- **WHEN** the operational success definition changes
- **THEN** existing outcome records MUST retain their original classification, and the new definition MUST apply only to new evaluations

#### Scenario: Historical accuracy reported honestly
- **WHEN** accuracy is reported across a period spanning a definition change
- **THEN** the report MUST state which definitions applied to which subset

### Requirement: Calibration observability
The system SHALL expose calibration state so that model quality is inspectable rather than asserted.

#### Scenario: Calibration status retrievable
- **WHEN** calibration state is requested
- **THEN** the current `signal_version`, accumulated sample per verdict state, abstention rate, realised accuracy per horizon, and per-dimension statistics MUST be returned

#### Scenario: Unmeasured claims prohibited
- **WHEN** model quality is described in product surfaces or documentation
- **THEN** figures MUST be derived from the recorded outcome data, and unmeasured or estimated figures MUST NOT be presented as measured

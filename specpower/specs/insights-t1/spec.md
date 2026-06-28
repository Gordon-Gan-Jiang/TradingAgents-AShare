### Requirement: T+1 accuracy insights
The system SHALL expose API endpoints for recommendation and report accuracy trends over configurable lookback windows.

#### Scenario: Recommendations trend
- **WHEN** a user requests `/v1/insights/t1/recommendations-trend`
- **THEN** the response MUST include time-bucketed hit-rate or accuracy metrics

#### Scenario: Reports accuracy trend
- **WHEN** a user requests `/v1/insights/t1/reports-accuracy-trend`
- **THEN** the response MUST include direction correctness statistics derived from stored reports

### Requirement: Multi-model consensus T+1
The system SHALL compute T+1 accuracy for multi-model consensus decisions separately from single-model reports.

#### Scenario: Consensus trend endpoint
- **WHEN** `/v1/insights/t1/multi-model-consensus-trend` is called
- **THEN** the payload MUST include portfolio-scoped and optional all-scope series

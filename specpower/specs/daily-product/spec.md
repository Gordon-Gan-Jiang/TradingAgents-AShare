### Requirement: Daily product scan runs
The system SHALL support initiating market scan runs with strategy profiles and retrieving run status and results.

#### Scenario: Start daily product run
- **WHEN** POST `/v1/daily-product/run`
- **THEN** a run record MUST be created with status trackable via GET `/v1/daily-product/runs/{run_id}`

#### Scenario: Scan results retrieval
- **WHEN** GET `/v1/daily-product/runs/{run_id}/scan-results`
- **THEN** ranked scan candidates for that run MUST be returned

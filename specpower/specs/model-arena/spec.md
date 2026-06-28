### Requirement: Model arena runs
The system SHALL support launching parallel analysis runs across multiple model profiles for the same symbol and trade date.

#### Scenario: Create arena run
- **WHEN** POST `/v1/model-arena/runs` with symbol and profile list
- **THEN** the system MUST enqueue or execute runs and return a trackable run id

### Requirement: Leaderboard and drift
The system SHALL expose leaderboard, symbol comparison, and drift alert views derived from historical arena outcomes.

#### Scenario: Drift alerts
- **WHEN** a model's recent accuracy deviates from its baseline beyond configured threshold
- **THEN** `/v1/model-arena/drift-alerts` MUST include an alert entry for that model

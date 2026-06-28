### Requirement: Multi-analyst consensus summary
Every completed analysis result MAY include a deterministic `consensus_summary` derived from analyst traces and verdicts.

#### Scenario: Weighted direction fusion
- **WHEN** multiple analysts return direction verdicts with confidence
- **THEN** `consensus_summary` MUST include an aggregate direction score and participating agent breakdown

#### Scenario: Attach to persisted report
- **WHEN** a report job finalizes
- **THEN** `consensus_summary` MUST be stored in `result_data` when analyst traces are present

## ADDED Requirements

### Requirement: Structured analyst JSON blocks
Analyst agents SHALL emit an optional structured JSON block alongside markdown reports for downstream parsing.

#### Scenario: Extract structured metrics
- **WHEN** an analyst response contains a valid structured JSON fence
- **THEN** `extract_analyst_structured_json` MUST parse risks and key metrics into trace metadata

### Requirement: Decision critic revision loop
The graph MAY include a decision critic that requests trader revision when hard constraints are violated.

#### Scenario: Revise verdict
- **WHEN** the critic returns verdict `revise`
- **THEN** `risk_feedback_state.revision_required` MUST be true and trader MUST be re-invoked within max retries

## ADDED Requirements

### Requirement: Dual-horizon trace merge
The report quality layer SHALL merge short and medium horizon analyst traces into a unified trace list for persistence.

#### Scenario: Merge on dual-horizon job
- **WHEN** a job completes with both horizon outputs
- **THEN** `analyst_traces` MUST contain deduplicated entries from both horizons

### Requirement: Verdict reconciliation
The system SHALL reconcile final verdict fields with consensus output when they materially disagree.

#### Scenario: Reconcile after consensus attach
- **WHEN** consensus summary conflicts with extracted report direction
- **THEN** quality pass MUST apply documented reconciliation rules before save

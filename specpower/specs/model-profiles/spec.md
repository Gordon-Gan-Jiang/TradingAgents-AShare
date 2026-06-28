### Requirement: Model profile CRUD
The system SHALL allow each user to create, list, update, and delete model profiles storing LLM provider, model names, and optional labels.

#### Scenario: Create profile
- **WHEN** a user POSTs a valid model profile
- **THEN** the profile MUST be persisted and returned with a stable `id`

#### Scenario: Bind profile to analysis
- **WHEN** an analysis job specifies `model_profile_id`
- **THEN** the job MUST record profile metadata on the resulting report

### Requirement: Model profile warmup
The system SHALL verify connectivity to configured LLM endpoints via a warmup call.

#### Scenario: Warmup success
- **WHEN** warmup succeeds for a profile
- **THEN** the API MUST return success status without mutating profile credentials

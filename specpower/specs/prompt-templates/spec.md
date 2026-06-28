### Requirement: User prompt template management
The system SHALL allow authenticated users to create, list, update, and delete named prompt templates.

#### Scenario: List templates
- **WHEN** GET `/v1/prompt-templates`
- **THEN** only templates owned by the current user MUST be returned

#### Scenario: Patch template
- **WHEN** PATCH `/v1/prompt-templates/{id}` with valid body
- **THEN** the updated template MUST be persisted and returned

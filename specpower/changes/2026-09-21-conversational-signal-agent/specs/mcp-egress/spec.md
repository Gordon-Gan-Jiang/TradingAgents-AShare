## ADDED Requirements

### Requirement: MCP server endpoint
The system SHALL expose a Model Context Protocol server over the streamable HTTP transport, mounted within the existing API application.

#### Scenario: Endpoint reachable
- **WHEN** an MCP client connects to the configured endpoint path
- **THEN** it MUST receive the server's capability description and tool list

#### Scenario: Application lifecycle shared
- **WHEN** the MCP server is mounted
- **THEN** its startup and shutdown MUST be integrated with the host application's lifespan, and mounting MUST NOT be performed in a way that bypasses lifespan handling

#### Scenario: Existing HTTP routes unaffected
- **WHEN** the MCP server is mounted
- **THEN** all pre-existing API routes MUST continue to respond as before

### Requirement: Authentication via existing API tokens
The MCP surface SHALL authenticate callers using the platform's existing API token mechanism and MUST NOT introduce a parallel credential system.

#### Scenario: Valid token accepted
- **WHEN** an MCP request presents a valid, unrevoked user API token
- **THEN** the request MUST be served in the context of that token's user

#### Scenario: Missing token rejected
- **WHEN** an MCP request presents no token
- **THEN** the request MUST be rejected and no tool MUST execute

#### Scenario: Revoked token rejected
- **WHEN** a previously issued token has been revoked by its user
- **THEN** MCP requests bearing it MUST be rejected

#### Scenario: No anonymous read path
- **WHEN** any MCP tool is invoked
- **THEN** a resolved user MUST be present in the execution context

### Requirement: Per-user data isolation
Every MCP tool invocation SHALL be scoped to the authenticated user, and MUST NOT expose another user's positions, plans, verdicts, or conversations.

#### Scenario: Position-bearing tool scoped
- **WHEN** a position-aware tool runs over MCP
- **THEN** it MUST resolve positions for the authenticated user only

#### Scenario: Verdict history scoped
- **WHEN** a tool returns stored verdicts
- **THEN** only verdicts owned by the authenticated user MUST be returned

#### Scenario: Cross-user access attempt refused
- **WHEN** a request supplies an identifier belonging to another user
- **THEN** the request MUST be refused without disclosing the existence of that record

### Requirement: Tool exposure whitelist
The MCP surface SHALL expose only tools classified as synchronous, and MUST NOT expose tools that start the multi-analyst pipeline or that mutate user state.

#### Scenario: Pipeline tools excluded
- **WHEN** the MCP tool list is served
- **THEN** no expensive pipeline tool MUST appear in it

#### Scenario: Mutating tools excluded in V1
- **WHEN** a tool mutates watchlists, portfolios, ledger entries, or configurations
- **THEN** it MUST NOT be exposed over MCP in this version

#### Scenario: Excluded tool invoked directly
- **WHEN** a caller attempts to invoke an excluded tool by name
- **THEN** the invocation MUST be refused with an explicit not-exposed error

### Requirement: Tool naming compatibility
MCP-exposed tool names SHALL conform to the character set accepted by mainstream MCP clients, with no per-surface renaming.

#### Scenario: Names contain only accepted characters
- **WHEN** the tool list is served over MCP
- **THEN** every tool name MUST consist only of letters, digits, underscores, and hyphens

#### Scenario: Canonical name reuse
- **WHEN** a tool is exposed over MCP
- **THEN** its MCP name MUST equal its registry canonical name, so that no sanitisation mapping is required

### Requirement: Latency and failure behaviour
Every MCP tool SHALL declare and honour a latency budget and SHALL return a structured error rather than an empty success when it cannot complete.

#### Scenario: Budget exceeded
- **WHEN** a tool exceeds its declared latency budget
- **THEN** it MUST return a structured timeout error identifying the source that was slow

#### Scenario: Upstream unavailable
- **WHEN** an underlying data source is unavailable
- **THEN** the tool MUST return a structured error or a degraded result with the reason, and MUST NOT return a silently empty payload

### Requirement: Freshness and disclaimer propagation
MCP tool results SHALL carry data freshness and the research-only disclaimer.

#### Scenario: Freshness present
- **WHEN** a tool result is returned over MCP
- **THEN** the result MUST include freshness information for the sources read, consistent with the platform's freshness contract

#### Scenario: Disclaimer present
- **WHEN** a result contains a judgement, verdict, or position-derived observation
- **THEN** the result MUST include the research-only disclaimer

### Requirement: Invocation observability
The MCP surface SHALL log invocations for audit and capacity analysis.

#### Scenario: Invocation logged
- **WHEN** a tool is invoked over MCP
- **THEN** the tool name, resolved user, duration, and outcome MUST be logged without recording credentials

#### Scenario: Aggregate usage queryable
- **WHEN** MCP invocation logs are queried
- **THEN** per-tool call counts and latency distributions MUST be retrievable

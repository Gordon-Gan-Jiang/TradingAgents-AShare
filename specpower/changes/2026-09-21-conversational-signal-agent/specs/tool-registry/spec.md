## ADDED Requirements

### Requirement: Single source of truth for tool definitions
The system SHALL define every agent-invocable capability exactly once as a `ToolSpec` in a central registry, and every consumer-facing tool surface MUST be generated from that registry.

#### Scenario: New capability registered once
- **WHEN** a new capability becomes available to agents
- **THEN** it MUST be added to the registry as a `ToolSpec`, and no separate hand-written definition may exist for any consumer

#### Scenario: No unregistered langchain tool
- **WHEN** the test suite scans the codebase for `@tool`-decorated functions
- **THEN** every discovered tool MUST either be registered in the registry or be explicitly listed in a documented exemption set, and the scan MUST fail otherwise

#### Scenario: Adapter output parity
- **WHEN** the LangChain adapter and the MCP adapter both materialize the same `ToolSpec`
- **THEN** the tool name, description, and input schema MUST be identical across both surfaces

### Requirement: ToolSpec contract
A `ToolSpec` SHALL declare its canonical name, human title, LLM-facing description, JSON Schema input, output contract identifier, handler, cost class, latency budget, freshness sources, and required user-context keys.

#### Scenario: Cost class declared
- **WHEN** a `ToolSpec` is registered
- **THEN** it MUST declare a cost class of `cheap`, `medium`, or `expensive`

#### Scenario: Latency budget declared
- **WHEN** a `ToolSpec` is registered
- **THEN** it MUST declare a positive `latency_budget_ms`

#### Scenario: Required user context declared
- **WHEN** a tool depends on portfolio or risk-profile context
- **THEN** the dependency MUST be declared in `requires`, so the orchestrator can resolve it before invocation

#### Scenario: Pipeline tools excluded from synchronous surfaces
- **WHEN** a `ToolSpec` is classified as `expensive` because it runs the multi-analyst pipeline
- **THEN** it MUST be excluded from the synchronous tool surface exposed to conversational and external callers

### Requirement: ToolResult structured contract
A tool invocation SHALL return a structured `ToolResult` containing machine-readable `data`, an evidence list, a freshness descriptor, a `degraded` flag, and an `as_of` timestamp; and MUST NOT return a pre-formatted prose string as its primary payload.

#### Scenario: Structured payload returned
- **WHEN** a tool completes successfully
- **THEN** `ToolResult.data` MUST be a structured mapping whose values are directly consumable for arithmetic by a caller

#### Scenario: Freshness carried through
- **WHEN** a tool reads market data
- **THEN** `ToolResult.freshness` MUST be populated from the existing freshness contract evaluation for the underlying sources

#### Scenario: Degraded result flagged
- **WHEN** a tool returns a result produced from stale, missing, or fallback data
- **THEN** `ToolResult.degraded` MUST be true and the result MUST carry the reason

#### Scenario: Render is not a contract
- **WHEN** a caller needs a text rendering of a result for prompt injection
- **THEN** the rendering MUST be produced by a separate renderer and MUST NOT be treated as the tool's return contract

### Requirement: Registry enumeration and schema export
The registry SHALL expose enumeration and JSON Schema export for all synchronous tools.

#### Scenario: Enumerate tools
- **WHEN** a caller asks the registry for synchronous tools
- **THEN** it MUST receive every non-expensive `ToolSpec` with name, description, and input schema

#### Scenario: Schema export matches invocation validation
- **WHEN** a tool is invoked with arguments that violate its declared input schema
- **THEN** the invocation MUST be rejected before the handler executes, with the same schema used for export

### Requirement: Migration of existing analyst tools
The nineteen existing analyst data tools SHALL be registered in the registry while preserving their current behaviour for the existing analysis graph.

#### Scenario: Graph behaviour preserved
- **WHEN** the existing multi-analyst pipeline runs after migration
- **THEN** each analyst node MUST receive the same tool names and equivalent tool outputs as before the migration

#### Scenario: Thin wrapper retained during transition
- **WHEN** an existing `@tool` function is migrated
- **THEN** it MUST delegate to the registry-registered handler rather than duplicating logic

#### Scenario: Structured variants added without removal
- **WHEN** a data tool gains a structured `ToolResult` variant
- **THEN** the original string-returning form MUST remain available until the analysis graph is migrated off it

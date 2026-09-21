## ADDED Requirements

### Requirement: Enforced layer dependency direction
The codebase SHALL enforce a single downward dependency direction across the analysis engine, tool layer, agent layer, and application layer, and the build SHALL fail when the direction is violated.

#### Scenario: Engine does not depend on application layer
- **WHEN** the boundary test scans the analysis engine package
- **THEN** no module in it MUST import the application layer package, and the test MUST fail otherwise

#### Scenario: Tool layer does not depend on application layer
- **WHEN** the boundary test scans the tool and agent packages
- **THEN** they MUST NOT import the application layer or the HTTP layer

#### Scenario: Signals do not depend on tool layer
- **WHEN** the boundary test scans the signal package
- **THEN** it MUST NOT import the tool layer, the agent layer, the application layer, or the graph packages

#### Scenario: No cycles within a layer
- **WHEN** module import graphs within the signal package are analysed
- **THEN** shared contracts MUST live in a types module and no import cycle MUST exist

### Requirement: Signal layer purity is machine-verified
The signal package SHALL contain no network access, no model invocation, and no ambient time or randomness, and the build SHALL fail when it does.

#### Scenario: Forbidden imports rejected
- **WHEN** the purity test scans every module in the signal package
- **THEN** imports of HTTP clients, market-data SDKs, model provider SDKs, and agent frameworks MUST be absent, and the test MUST fail if any is present

#### Scenario: Ambient clock rejected
- **WHEN** the purity test scans for direct wall-clock reads inside the signal package
- **THEN** any such read MUST fail the test; time MUST enter through an explicit parameter

#### Scenario: Determinism verified
- **WHEN** the same inputs are evaluated twice
- **THEN** the outputs MUST be identical, and a test MUST assert this for each signal dimension

### Requirement: Explicit composition over implicit registration
Assembling tools, dimensions, and middleware SHALL happen in an explicit composition step, and MUST NOT rely on import-time side effects or decorator-based auto-registration.

#### Scenario: Single composition entry point
- **WHEN** the tool registry is needed
- **THEN** it MUST be constructed by an explicit builder function that a caller invokes, and no import of a module MUST be required to make a tool appear

#### Scenario: Dimension order visible in one place
- **WHEN** the set and order of evidence dimensions is inspected
- **THEN** it MUST be declared as a single explicit ordered collection, and adding a dimension MUST be a one-line change at that declaration

#### Scenario: No import-order sensitivity
- **WHEN** the registry is built in a test process that has imported modules in an arbitrary order
- **THEN** the resulting tool set MUST be identical

### Requirement: Explicit dependency injection over module-global state
New analysis and agent code SHALL receive configuration, data ports, and clocks as explicit parameters, and MUST NOT read module-global mutable configuration.

#### Scenario: Weights injected
- **WHEN** a probe is evaluated
- **THEN** its weight set MUST be passed in, so that two concurrent evaluations may use different weight versions without interference

#### Scenario: Ports injected
- **WHEN** a probe needs market data
- **THEN** it MUST receive data ports as parameters, so that a test can supply in-memory fakes without patching global state

#### Scenario: No new global configuration
- **WHEN** new code needs a configuration value
- **THEN** it MUST NOT add or mutate the existing module-global configuration object

#### Scenario: Concurrent isolation
- **WHEN** two evaluations run concurrently with different weight versions and different model configurations
- **THEN** neither MUST observe the other's parameters

### Requirement: Transport decoupled through adapters and a single event contract
Tool transports and conversation output channels SHALL be adapters over shared contracts, and adapters MUST NOT depend on one another.

#### Scenario: Adapters share only the tool specification
- **WHEN** a transport adapter materialises a tool
- **THEN** it MUST accept only the tool specification type and MUST NOT import any other adapter

#### Scenario: Domain results do not know transports
- **WHEN** the signal package produces a result
- **THEN** it MUST NOT reference the tool specification or any transport type; conversion MUST occur in the tool layer

#### Scenario: Consumers share one event contract
- **WHEN** conversation events are produced
- **THEN** server-sent events, command-line output, and tests MUST consume the same event contract, and adding a new consumer MUST NOT require changes to the agent loop

#### Scenario: New transport requires no engine change
- **WHEN** a new output channel is added
- **THEN** it MUST be implementable as a new adapter and a new sink implementation only

### Requirement: Protocol-based extension points
Extension points SHALL be declared as narrow protocols rather than fat base classes, so that an implementer is not required to satisfy unrelated methods.

#### Scenario: Narrow port
- **WHEN** a new data port is declared
- **THEN** it MUST declare only the methods its consumers need, and an existing object MUST be usable as an implementation without inheritance

#### Scenario: Test double without inheritance
- **WHEN** a unit test supplies a fake port
- **THEN** it MUST be possible without subclassing a framework base class

#### Scenario: Fat base class not extended
- **WHEN** a new extension point is introduced
- **THEN** it MUST NOT extend the existing monolithic provider base class, and MUST NOT add abstract methods to it

### Requirement: New HTTP routes are registered outside the application entry module
New HTTP endpoints SHALL be declared on routers within the routing package, and MUST NOT be declared in the application entry module.

#### Scenario: Route declared on a router
- **WHEN** a new endpoint is added
- **THEN** it MUST be declared on a router object and mounted by the entry module

#### Scenario: Entry module guard
- **WHEN** the boundary test scans the application entry module for new endpoint decorators introduced by this change
- **THEN** none MUST be found, and the test MUST fail otherwise

#### Scenario: Existing routes unaffected
- **WHEN** routers are mounted
- **THEN** all pre-existing endpoints MUST continue to respond as before

### Requirement: Extension without modifying existing modules
Adding a new evidence dimension, probe, tool, transport, or cross-cutting concern SHALL require only new files plus a single declaration change at the composition point.

#### Scenario: New dimension is additive
- **WHEN** a new evidence dimension is added
- **THEN** the existing probe implementation MUST NOT require modification

#### Scenario: New probe is additive
- **WHEN** a new probe is added
- **THEN** persistence, outcome backfill, calibration statistics, and the front-end verdict card MUST NOT require modification, because they consume the shared probe result contract

#### Scenario: New cross-cutting concern is additive
- **WHEN** a new cross-cutting concern such as redaction or rate limiting is added to the conversation loop
- **THEN** it MUST be implementable as a middleware unit without modifying the loop body

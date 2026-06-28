## ADDED Requirements

### Requirement: User pool recommendation
The system SHALL rank symbols from a user's tracking pool, watchlist, and optional seeds using configurable scoring weights and return top-K candidates without placing orders.

#### Scenario: Recommend from watchlist and tracking
- **WHEN** a user invokes recommendation with default pool sources
- **THEN** the response MUST include ranked `items` with symbol, score, and rationale fields
- **THEN** the system MUST NOT execute trades

#### Scenario: Tradability guard
- **WHEN** `enforce_tradability` is true and a candidate is limit-up or illiquid
- **THEN** that symbol MUST be excluded or down-ranked per tradability rules

### Requirement: Scheduled and manual recommendation push
The system SHALL push recommendation summaries to configured WeCom and/or WPS webhooks during trading-day open/close windows.

#### Scenario: Manual push
- **WHEN** a user triggers manual push with phase `open` or `close`
- **THEN** the system MUST send at most one deduplicated message per user per phase unless `force=true`

#### Scenario: Non-trading day skip
- **WHEN** the current calendar day is not an A-share trading day
- **THEN** scheduled pushes MUST NOT run

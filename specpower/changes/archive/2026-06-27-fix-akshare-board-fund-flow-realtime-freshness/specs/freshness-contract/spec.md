## MODIFIED Requirements

### Requirement: Freshness contract configuration
The system SHALL maintain a machine-readable freshness contract (YAML) mapping each `data_collector` source key to category, criticality, anchor field, grace minutes or freshness window, and market scope (`CN` for V1).

#### Scenario: Complete registry coverage
- **WHEN** the validator initializes for a standard analysis run
- **THEN** contract entries MUST exist for at least: `stock_data`, `indicators`, `individual_fund_flow`, `board_fund_flow`, `zt_pool`, `lhb_detail`, `fundamentals`, `balance_sheet`, `cashflow`, `income_statement`, `news`, `global_news`, `hot_stocks_xq`, `insider_transactions`

#### Scenario: Unknown data source
- **WHEN** a fetched source has no contract entry
- **THEN** the system MUST assign status `unknown` and MUST NOT alone elevate report `overall_status` to `stale`

#### Scenario: Board fund flow provider compatibility
- **WHEN** the board fund flow provider fetches industry sector fund-flow rankings
- **THEN** it MUST attempt the legacy akshare API when available
- **THEN** it MUST fall back to a supported sector fund-flow ranking API when the legacy symbol is absent
- **THEN** successful responses MUST include a parseable calendar anchor (e.g. `数据截止 YYYY-MM-DD`) for freshness evaluation

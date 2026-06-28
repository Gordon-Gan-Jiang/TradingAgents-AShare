### Requirement: Data freshness taxonomy
The system SHALL classify every registered market data source into exactly one of four freshness categories: `event_driven`, `calendar_anchored`, `disclosure_period`, or `conditional_daily`.

#### Scenario: Calendar-anchored source registration
- **WHEN** a data source such as daily OHLCV or individual fund flow is registered in the freshness contract
- **THEN** it MUST be assigned category `calendar_anchored` with an `expected_rule` that references a trading-day anchor

#### Scenario: Event-driven source registration
- **WHEN** a data source such as stock news or global news is registered
- **THEN** it MUST be assigned category `event_driven` and MUST NOT use trading-day equality as its primary freshness criterion

#### Scenario: Disclosure-period source registration
- **WHEN** a data source such as balance sheet or income statement is registered
- **THEN** it MUST be assigned category `disclosure_period` with an anchor based on the latest disclosed report period before the analysis date

#### Scenario: Conditional-daily source registration
- **WHEN** a data source such as LHB detail is registered
- **THEN** it MUST be assigned category `conditional_daily` with `empty_ok` support to distinguish normal absence from source lag

### Requirement: Analysis-mode-relative anchor calculation
The system SHALL compute `expected_anchor` for calendar-anchored data based on `analysis_trade_date`, current market phase, and `analysis_mode` — not solely on the current calendar moment.

#### Scenario: Historical backtest analysis
- **WHEN** `analysis_trade_date` is strictly before the current calendar day and is a valid A-share trading day
- **THEN** `expected_anchor` MUST equal `analysis_trade_date`

#### Scenario: Intraday analysis on today
- **WHEN** `analysis_trade_date` equals today and market phase is `pre_open`, `in_session`, or `lunch_break`
- **THEN** `expected_anchor` MUST equal the previous A-share trading day before today

#### Scenario: Post-close analysis on today
- **WHEN** `analysis_trade_date` equals today, today is an A-share trading day, and market phase is `post_close`
- **THEN** `expected_anchor` MUST equal today

#### Scenario: Non-trading calendar day
- **WHEN** the current calendar day is not an A-share trading day
- **THEN** `expected_anchor` MUST equal the most recent A-share trading day before today

### Requirement: Grace period before stale classification
The freshness contract MUST support per-source `grace_minutes` that downgrade calendar-anchored lag from `stale` to `warning` when lag occurs within the grace window after market close.

#### Scenario: Fund flow within grace window
- **WHEN** individual fund flow cutoff is one trading day behind `expected_anchor` but the evaluation time is within 120 minutes after 15:00 on an A-share trading day
- **THEN** the source status MUST be `warning` and MUST NOT be `stale`

#### Scenario: Fund flow beyond grace window
- **WHEN** individual fund flow cutoff is strictly less than `expected_anchor` and the grace window has elapsed
- **THEN** the source status MUST be `stale`

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
### Requirement: Per-source freshness status enumeration
The system SHALL assign each evaluated data source one of: `fresh`, `warning`, `stale`, `error`, `empty_ok`, `not_applicable`, or `unknown`.

#### Scenario: Calendar-anchored data meets anchor
- **WHEN** a calendar-anchored source's `cutoff_date` is greater than or equal to `expected_anchor`
- **THEN** its status MUST be `fresh`

#### Scenario: Fetch failure on registered source
- **WHEN** a registered data source fetch raises an exception, times out, returns unparseable content, or returns empty data when `empty_ok` is false
- **THEN** its status MUST be `error` and MUST NOT be classified as `stale` or `unknown`

#### Scenario: LHB on non-abnormal day
- **WHEN** LHB detail returns empty data for a date with no abnormal-trading record and is marked `empty_ok`
- **THEN** its status MUST be `empty_ok` and MUST NOT be classified as `stale`

#### Scenario: Event-driven within window
- **WHEN** an event-driven source's latest publish time is within its configured freshness window
- **THEN** its status MUST be `fresh` or `not_applicable` and MUST NOT use trading-day anchor comparison

### Requirement: Standardized freshness metadata output
Every data fetch executed through the analysis pipeline SHALL produce a `freshness_meta` object containing: `source_key`, `category`, `criticality`, `status`, `anchor_expected`, `anchor_actual`, `evaluated_at`, optional `issue_type`, optional `error_code`, optional `error_message`, and optional `lag_note` and `lag_trading_days`.

#### Scenario: Fund flow fetch with lag beyond grace
- **WHEN** individual fund flow last row date is behind `expected_anchor` and grace has elapsed
- **THEN** `freshness_meta` MUST have `status: stale`, populated anchor fields, and a human-readable `lag_note`

#### Scenario: Fund flow within grace window
- **WHEN** individual fund flow cutoff lags `expected_anchor` but evaluation is within configured grace
- **THEN** `freshness_meta` MUST have `status: warning` and MUST NOT appear in `blocking_sources`

#### Scenario: Fresh stock data fetch
- **WHEN** daily OHLCV last bar date equals `expected_anchor`
- **THEN** `freshness_meta` MUST have `status: fresh`

### Requirement: DataCollector and graph state aggregation
The DataCollector SHALL aggregate per-source `freshness_meta` into a `freshness_pool` passed through graph state to all analyst agents and the report persistence layer.

#### Scenario: Parallel fetch completion
- **WHEN** DataCollector finishes fetching all configured sources for a symbol and trade date
- **THEN** it MUST expose `freshness_pool` mapping each registered `source_key` to its `freshness_meta`

#### Scenario: Provider timeout on critical source
- **WHEN** a calendar-anchored source with criticality `critical` fails due to timeout or provider exception
- **THEN** its `freshness_meta` MUST have `status: error`, `issue_type: fetch_error`, a non-empty `error_code`, and a sanitized `error_message`
- **THEN** analysis MUST continue without hard abort

#### Scenario: Parse failure after fetch
- **WHEN** a provider returns a response that cannot be parsed into the expected cutoff fields
- **THEN** its `freshness_meta` MUST have `status: error` with `error_code: parse_error`

### Requirement: Criticality-based stale and error handling
The freshness contract MUST assign criticality (`critical`, `important`, `informational`) determining downstream impact when status is `stale`, `warning`, or `error`.

#### Scenario: Critical source fetch error
- **WHEN** a `critical` source such as `stock_data` has status `error`
- **THEN** the source MUST be included in `blocking_sources` and `fetch_errors`
- **THEN** the source MUST trigger report-level `overall_status: error` and confidence capping

#### Scenario: Critical source stale
- **WHEN** a `critical` source such as `individual_fund_flow` has status `stale`
- **THEN** the source MUST be included in `blocking_sources` and MUST trigger report-level confidence capping

#### Scenario: Informational source stale
- **WHEN** only `informational` sources such as `hot_stocks_xq` are stale
- **THEN** the report `overall_status` MUST remain `fresh` if all critical and important sources are fresh or empty_ok

### Requirement: Provider integration parity
All CN market data providers used in production analysis MUST apply the shared freshness validator for equivalent data sources.

#### Scenario: AkShare fund flow path
- **WHEN** fund flow is fetched via `cn_akshare_provider`
- **THEN** it MUST produce `freshness_meta` equivalent to validation via the shared validator used by `cn_eastmoney_http_provider`

#### Scenario: Derived indicators inherit stock data freshness
- **WHEN** technical indicators are computed from OHLCV inside DataCollector
- **THEN** indicator freshness MUST inherit the `stock_data` anchor evaluation and MUST NOT apply a separate grace window

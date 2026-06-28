## MODIFIED Requirements

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

#### Scenario: Intraday-only source omitted on historical analysis
- **WHEN** a source has `intraday_only: true` in the freshness contract
- **AND** the analysis `trade_date` is before the current CN trading calendar day OR the analysis mode is not an intraday collection mode
- **AND** the collector did not fetch that source (raw result is `None`)
- **THEN** the validator MUST mark that source `not_applicable`
- **THEN** the source MUST NOT contribute `error` to overall freshness aggregation

#### Scenario: Intraday-only source fetched but empty on same-day analysis
- **WHEN** a source has `intraday_only: true`
- **AND** the collector fetched the source on the current trading day
- **AND** the raw result is empty or indicates provider failure
- **THEN** the validator MUST mark the source `error` with an appropriate fetch error message

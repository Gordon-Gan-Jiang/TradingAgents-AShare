### Requirement: Report-level freshness summary
Every completed analysis report SHALL include a `freshness_summary` persisted in `result_data` and a denormalized `freshness_status` column for list queries.

#### Scenario: All critical sources fresh
- **WHEN** all `critical` and `important` sources have status `fresh` or `empty_ok`
- **THEN** `freshness_summary.overall_status` MUST be `fresh` and `freshness_status` column MUST be `fresh`

#### Scenario: Important source fetch error without critical error
- **WHEN** no critical source has status `error` but at least one important source has status `error`
- **THEN** the report `overall_status` MUST be `error`

#### Scenario: Any critical source fetch error
- **WHEN** at least one `critical` source has status `error`
- **THEN** `freshness_summary.overall_status` MUST be `error`
- **THEN** `freshness_summary.fetch_errors` MUST list all sources with `issue_type: fetch_error`
- **THEN** `freshness_status` column MUST be `error`

#### Scenario: Any critical source stale
- **WHEN** at least one `critical` source has status `stale`
- **THEN** `freshness_summary.overall_status` MUST be `stale`
- **THEN** `freshness_summary.blocking_sources` MUST list all critical stale sources
- **THEN** `freshness_status` column MUST be `stale`

#### Scenario: Important source stale without critical stale
- **WHEN** no critical source is stale but at least one important source is stale
- **THEN** `freshness_summary.overall_status` MUST be `warning`
- **THEN** `freshness_status` column MUST be `warning`

### Requirement: Confidence hard cap by overall status
The report persistence layer MUST clamp stored confidence based on `freshness_summary.overall_status` using a dual-track policy (code clamp plus LLM prompt constraint).

#### Scenario: Error report confidence clamp
- **WHEN** a report is saved with `overall_status: error` and extracted confidence exceeds 40
- **THEN** the persisted confidence MUST be reduced to 40

#### Scenario: Stale report confidence clamp
- **WHEN** a report is saved with `overall_status: stale` and extracted confidence exceeds 50
- **THEN** the persisted confidence MUST be reduced to 50

#### Scenario: Warning report confidence clamp
- **WHEN** a report is saved with `overall_status: warning` and extracted confidence exceeds 70
- **THEN** the persisted confidence MUST be reduced to 70

### Requirement: Section-level freshness impact mapping
The system SHALL map each report section to primary data sources and record per-section status in `freshness_summary.section_impacts`.

#### Scenario: Smart money report with fetch error on fund flow
- **WHEN** `individual_fund_flow` has status `error` during analysis producing `smart_money_report`
- **THEN** `section_impacts.smart_money_report` MUST be `error`

#### Scenario: Smart money report with stale fund flow
- **WHEN** `individual_fund_flow` is stale during analysis producing `smart_money_report`
- **THEN** `section_impacts.smart_money_report` MUST be `stale`

#### Scenario: News report event-driven only
- **WHEN** only event-driven sources feed `news_report` and none exceed freshness windows
- **THEN** `section_impacts.news_report` MUST be `fresh` or `not_applicable`

#### Scenario: Final decision inherits worst critical impact
- **WHEN** `final_trade_decision` is generated
- **THEN** its effective freshness impact MUST reflect the worst status among all critical and important sources used in the run

### Requirement: LLM context injection
Analyst agents and the final decision stage MUST receive a concise freshness context block from `freshness_pool` before generating conclusions.

#### Scenario: Fetch error in smart money analyst
- **WHEN** `individual_fund_flow` status is `error`
- **THEN** the smart money analyst prompt MUST state that fund-flow data could not be fetched
- **THEN** the prompt MUST instruct the model not to infer fund-flow conclusions from missing data

#### Scenario: Stale fund flow in smart money analyst
- **WHEN** `individual_fund_flow` status is `stale`
- **THEN** the smart money analyst prompt MUST warn that fund-flow data lags the expected anchor
- **THEN** the prompt MUST instruct the model not to state high-confidence conclusions based solely on that dataset

#### Scenario: Warning within grace
- **WHEN** a critical source has status `warning` due to grace period
- **THEN** the injected context MUST state the data may still be updating and MUST NOT describe it as fully settled

### Requirement: Report UI staleness badge
The frontend MUST display staleness indicators based on `freshness_status` without requiring users to open the full report.

#### Scenario: Error report in list view
- **WHEN** a report has `freshness_status: error`
- **THEN** the list MUST show a red badge labeled 「数据异常」 distinct from the stale badge label

#### Scenario: Stale report in list view
- **WHEN** a report has `freshness_status: stale`
- **THEN** the list MUST show a red badge labeled 「数据过时」

#### Scenario: Fresh report in list view
- **WHEN** a report has `freshness_status: fresh`
- **THEN** the list MUST NOT display a freshness badge

#### Scenario: Reports filter by data quality issues
- **WHEN** a user enables the 「仅数据异常/过时」 filter on the reports list
- **THEN** only reports with `freshness_status` of `error`, `warning`, or `stale` MUST be shown

#### Scenario: Report detail banner for fetch errors
- **WHEN** a user opens a report with `overall_status: error`
- **THEN** the detail view MUST show a banner listing `fetch_errors` with `source_key`, `error_code`, and `error_message`

#### Scenario: Report detail banner for lag
- **WHEN** a user opens a report with `overall_status` of `warning` or `stale`
- **THEN** the detail view MUST show a banner listing lag-related `blocking_sources`, `expected_anchor`, and stale sources' `anchor_actual`

#### Scenario: Section-level indicator
- **WHEN** a section has `section_impacts` status `error`, `stale`, or `warning`
- **THEN** ReportViewer MUST show an icon adjacent to the section title with tooltip from `lag_note` or `error_message`

### Requirement: Immutable analysis-time snapshot
The persisted `freshness_summary` MUST reflect freshness at analysis completion and MUST NOT be recomputed on read to change the stored verdict.

#### Scenario: User views old report after market moves
- **WHEN** a user opens a report generated three days ago with snapshot `overall_status: fresh`
- **THEN** the displayed `overall_status` MUST remain `fresh`
- **THEN** the UI MAY show a separate gray `report_age_note` when report age exceeds three trading days

### Requirement: Notification disclosure
Email and enterprise notification outputs MUST include freshness status when `freshness_summary` is present.

#### Scenario: Email report with fetch error
- **WHEN** an email is sent for a report with `overall_status: error`
- **THEN** the email body MUST include a one-line fetch-error warning listing at least one `fetch_errors` source before the decision summary

#### Scenario: Email report with stale data
- **WHEN** an email is sent for a report with `overall_status: stale`
- **THEN** the email body MUST include a one-line freshness warning before the decision summary

## MODIFIED Requirements

### Requirement: Report UI staleness badge
The frontend MUST display staleness indicators based on `freshness_status` without requiring users to open the full report. On the **历史报告** list table (`Reports.tsx`), freshness MUST appear in a dedicated **「数据状态」** column and MUST NOT be nested inside the **「决策建议」** column.

#### Scenario: Error report in list view
- **WHEN** a report has `freshness_status: error`
- **THEN** the **「数据状态」** column MUST show a red badge labeled 「数据异常」
- **THEN** the **「决策建议」** column MUST NOT show a freshness badge

#### Scenario: Stale report in list view
- **WHEN** a report has `freshness_status: stale`
- **THEN** the **「数据状态」** column MUST show a red badge labeled 「数据过时」
- **THEN** the **「决策建议」** column MUST NOT show a freshness badge

#### Scenario: Warning report in list view
- **WHEN** a report has `freshness_status: warning`
- **THEN** the **「数据状态」** column MUST show an amber badge labeled 「部分滞后」
- **THEN** the **「决策建议」** column MUST NOT show a freshness badge

#### Scenario: Fresh report in list view
- **WHEN** a report has `freshness_status: fresh`
- **THEN** the **「数据状态」** column MUST show a neutral placeholder 「—」
- **THEN** the list MUST NOT display a colored freshness badge for that row

#### Scenario: Legacy or in-flight report without freshness
- **WHEN** a report has `freshness_status` of `null` or is `pending`, `running`, or `failed`
- **THEN** the **「数据状态」** column MUST show 「—」
- **THEN** the **「决策建议」** column MUST show only task status or decision text

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

#### Scenario: Column header tooltip
- **WHEN** a user hovers the **「数据状态」** column header on the reports list
- **THEN** the UI MUST show a tooltip explaining 「数据异常 / 数据过时 / 部分滞后」 meanings and that 「—」 means no issue or not yet evaluated

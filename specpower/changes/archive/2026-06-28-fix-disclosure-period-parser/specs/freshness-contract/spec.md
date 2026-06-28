## ADDED Requirements

### Requirement: Disclosure-period parsing from financial report payloads
The freshness parser SHALL derive the latest disclosed report period from financial statement payloads that encode period ends as `YYYYMMDD` column headers or `报告日` row values.

#### Scenario: Latest YYYYMMDD column among multiple periods
- **WHEN** a balance sheet, income statement, cashflow, or fundamentals abstract table contains period columns such as `20260331` and `20251231`
- **THEN** the parser MUST select the latest period end date
- **THEN** it MUST map `03`, `06`, `09`, and `12` month-ends to `Q1`, `Q2`, `Q3`, and `Q4` respectively
- **THEN** it MUST NOT classify `20251231` as `2025-Q1`

#### Scenario: Explicit disclosure period marker in provider output
- **WHEN** a provider prefixes financial report text with `最新披露期: YYYY-Qn`
- **THEN** the parser MUST use that explicit period as the authoritative `anchor_actual`

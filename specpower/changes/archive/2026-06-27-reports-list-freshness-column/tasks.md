# Reports List Freshness Column — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use specpower:build Phase B (inline mode) to implement this plan task-by-task.

**Goal:** Add a dedicated 「数据状态」 column to the reports list table, separating data quality from decision text.

**Architecture:** Pure display helpers in `freshnessListDisplay.ts`, thin `FreshnessStatusCell` wrapper reusing `FreshnessBadge`, `Reports.tsx` table wired to exported headers and tooltip constant. No backend changes.

**Tech Stack:** React 18, TypeScript, Vitest, existing `freshness_status` list API field.

---

## 1. Spec & component prep

- [x] 1.1 Backend smoke: `REPORT_SUMMARY_COLUMNS` includes `freshness_status`
- [x] 1.2 `freshnessListDisplay.ts` — tooltip, headers, `shouldShowFreshnessBadge`
- [x] 1.3 `FreshnessStatusCell.tsx` — badge or em dash
- [x] 1.4 Unit tests in `freshnessListDisplay.test.ts`

## 2. Reports table UI

- [x] 2.1 Table headers from `REPORT_LIST_TABLE_HEADERS` with 「数据状态」 between 模型 and 决策建议
- [x] 2.2 Body cell renders `<FreshnessStatusCell status={report.freshness_status} />`
- [x] 2.3 Decision column: removed nested `FreshnessBadge`, only `renderStatusBadge`
- [x] 2.4 Column header `title={FRESHNESS_COLUMN_TOOLTIP}` for 「数据状态」

## 3. Verification

- [x] 3.1 `npm test` — 13 passed (includes freshness list display tests)
- [x] 3.2 `npm run build` — tsc + vite build pass
- [x] 3.3 Dashboard unchanged (`FreshnessBadge` still inline on cards)

import { describe, expect, it } from 'vitest'

import {
    FRESHNESS_COLUMN_TOOLTIP,
    REPORT_LIST_TABLE_HEADERS,
    shouldShowFreshnessBadge,
} from '@/components/freshnessListDisplay'

describe('shouldShowFreshnessBadge', () => {
    it('shows badge for error, stale, and warning', () => {
        expect(shouldShowFreshnessBadge('error')).toBe(true)
        expect(shouldShowFreshnessBadge('stale')).toBe(true)
        expect(shouldShowFreshnessBadge('warning')).toBe(true)
    })

    it('shows placeholder for fresh, null, and undefined', () => {
        expect(shouldShowFreshnessBadge('fresh')).toBe(false)
        expect(shouldShowFreshnessBadge(null)).toBe(false)
        expect(shouldShowFreshnessBadge(undefined)).toBe(false)
    })
})

describe('REPORT_LIST_TABLE_HEADERS', () => {
    it('places 数据状态 between 模型 and 决策建议', () => {
        const modelIdx = REPORT_LIST_TABLE_HEADERS.indexOf('模型')
        const freshnessIdx = REPORT_LIST_TABLE_HEADERS.indexOf('数据状态')
        const decisionIdx = REPORT_LIST_TABLE_HEADERS.indexOf('决策建议')

        expect(freshnessIdx).toBe(modelIdx + 1)
        expect(decisionIdx).toBe(freshnessIdx + 1)
    })
})

describe('FRESHNESS_COLUMN_TOOLTIP', () => {
    it('documents all three issue labels and the dash meaning', () => {
        expect(FRESHNESS_COLUMN_TOOLTIP).toContain('数据异常')
        expect(FRESHNESS_COLUMN_TOOLTIP).toContain('数据过时')
        expect(FRESHNESS_COLUMN_TOOLTIP).toContain('部分滞后')
        expect(FRESHNESS_COLUMN_TOOLTIP).toContain('—')
    })
})

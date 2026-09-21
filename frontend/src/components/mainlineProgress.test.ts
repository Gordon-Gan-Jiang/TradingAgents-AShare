import { describe, expect, it } from 'vitest'

import { getMainlineProgress, mainlinePhaseLabel, mainlinePhaseStepIndex } from '@/components/mainlineProgress'

describe('getMainlineProgress', () => {
    it('returns 100 when completed or failed', () => {
        expect(getMainlineProgress({ status: 'completed' }).percent).toBe(100)
        expect(getMainlineProgress({ status: 'failed' }).label).toBe('失败')
    })

    it('uses phase base and stays below 100 while running', () => {
        const view = getMainlineProgress({
            status: 'running',
            phase: 'analyst',
            serverProgress: 52,
            createdAt: new Date(Date.now() - 30_000).toISOString(),
            nowMs: Date.now(),
        })
        expect(view.label).toBe('识别主线')
        expect(view.percent).toBeGreaterThanOrEqual(52)
        expect(view.percent).toBeLessThan(100)
    })

    it('keeps pending in the queue band', () => {
        const view = getMainlineProgress({ status: 'pending', phase: 'pending', nowMs: Date.now(), createdAt: new Date().toISOString() })
        expect(view.percent).toBeGreaterThanOrEqual(8)
        expect(view.percent).toBeLessThan(30)
    })
})

describe('mainlinePhaseStepIndex', () => {
    it('maps collecting to the first step and completed past the last', () => {
        expect(mainlinePhaseStepIndex('collecting')).toBe(0)
        expect(mainlinePhaseStepIndex('selector')).toBe(2)
        expect(mainlinePhaseStepIndex('completed')).toBe(4)
        expect(mainlinePhaseStepIndex('failed')).toBe(-1)
        expect(mainlinePhaseLabel('analyst')).toBe('识别主线')
    })
})

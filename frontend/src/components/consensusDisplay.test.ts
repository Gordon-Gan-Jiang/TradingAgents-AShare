import { describe, expect, it } from 'vitest'

import {
    metricTone,
    needsParticipationWarning,
    participationLabel,
    resolveParticipation,
    strengthTone,
} from '@/components/consensusDisplay'
import type { ConsensusSummary } from '@/types'

/**
 * 「共识强度 100/100」曾经在 7 人面板只有 1 人投票时也显示 —— 与 7 人一致时逐字节
 * 相同，且在卡面上是醒目的绿色。后端修复后新增 participant_n / expected_n /
 * participation_ratio / low_participation 四个字段，本模块负责把参与度呈现在卡面上，
 * 并且**在参与度低时不得继续用「高确信」的绿色**呈现强度。
 *
 * 注意：本仓库的 vitest 环境是 node 且只收 `*.test.ts`，无法做组件测试，
 * 因此展示判定逻辑按既有惯例（progressFeedback / verdictPresentation）抽成纯函数。
 */

const EMERALD = 'text-emerald-600 dark:text-emerald-400'
const AMBER = 'text-amber-600 dark:text-amber-400'
const ROSE = 'text-rose-600 dark:text-rose-400'

function summary(overrides: Partial<ConsensusSummary> = {}): ConsensusSummary {
    return {
        consensus_direction: '看多',
        consensus_score: 1,
        consensus_strength: 100,
        disagreement_score: 0,
        stability_score: 79,
        execution_mode: 'conditional',
        execution_mode_label: '条件执行',
        dominant_horizon: 'short',
        horizon_conflict: false,
        support_ratio: 1,
        oppose_ratio: 0,
        neutral_ratio: 0,
        ...overrides,
    }
}

/** 7/7 全体参与、强度 100（正常路径，不得被判为低参与度） */
const FULL = summary({
    participant_n: 7,
    expected_n: 7,
    participation_ratio: 1,
    low_participation: false,
    execution_mode: 'direct',
    agent_breakdown: Array.from({ length: 7 }, (_, i) => ({
        agent: `a${i}`,
        label: `分析师${i}`,
        horizon: 'short',
        verdict: '看多',
        weight: 1,
        effective_weight: 1,
        contribution: 1,
    })),
})

/** 1/7 参与、强度同样是 100（审计 §2.2 的 f7e60b27 形态） */
const ONE_OF_SEVEN = summary({
    participant_n: 1,
    expected_n: 7,
    participation_ratio: 0.143,
    low_participation: true,
    unparsed_analyst_n: 6,
    recovered_analyst_n: 0,
    agent_breakdown: [
        {
            agent: 'a0',
            label: '市场面',
            horizon: 'short',
            verdict: '看多',
            weight: 1,
            effective_weight: 1,
            contribution: 1,
        },
    ],
})

/** 旧口径摘要：四个新字段都不存在，无法判断参与度 */
const LEGACY = summary({ consensus_strength: 92, execution_mode: 'direct' })

describe('resolveParticipation', () => {
    it('优先使用后端给出的显式字段', () => {
        const p = resolveParticipation(FULL)
        expect(p).not.toBeNull()
        expect(p?.participant).toBe(7)
        expect(p?.expected).toBe(7)
        expect(p?.low).toBe(false)
    })

    it('标记出 1/7 的低参与度', () => {
        const p = resolveParticipation(ONE_OF_SEVEN)
        expect(p?.participant).toBe(1)
        expect(p?.expected).toBe(7)
        expect(p?.low).toBe(true)
    })

    it('显式字段缺失但口径为新（有 recovered_analyst_n）时按轨迹自行推算', () => {
        const inWindow = summary({
            consensus_strength: 100,
            unparsed_analyst_n: 6,
            recovered_analyst_n: 0,
            agent_breakdown: ONE_OF_SEVEN.agent_breakdown,
        })
        const p = resolveParticipation(inWindow)
        expect(p, '新口径数据应能推算参与度').not.toBeNull()
        expect(p?.participant).toBe(1)
        // expected = 投票者 + 未给出方向者
        expect(p?.expected).toBe(7)
        expect(p?.low).toBe(true)
    })

    it('旧口径（无 recovered_analyst_n）不得凭空推算', () => {
        expect(resolveParticipation(LEGACY)).toBeNull()
    })

    it('无 agent_breakdown 时不得推算', () => {
        const noBreakdown = summary({ unparsed_analyst_n: 3, recovered_analyst_n: 0 })
        expect(resolveParticipation(noBreakdown)).toBeNull()
    })
})

describe('participationLabel', () => {
    it('低参与度时给出人数与警示', () => {
        const label = participationLabel(ONE_OF_SEVEN)
        expect(label).toContain('1/7')
        expect(label).toContain('参与')
    })

    it('全体参与时给出人数', () => {
        const label = participationLabel(FULL)
        expect(label).toContain('7/7')
    })

    it('无法判断时返回 null（不得编造）', () => {
        expect(participationLabel(LEGACY)).toBeNull()
    })
})

describe('needsParticipationWarning', () => {
    it('低参与度为真', () => {
        expect(needsParticipationWarning(ONE_OF_SEVEN)).toBe(true)
    })

    it('全体参与为假', () => {
        expect(needsParticipationWarning(FULL)).toBe(false)
    })

    it('旧口径无法判断时不得报警（不猜）', () => {
        expect(needsParticipationWarning(LEGACY)).toBe(false)
    })
})

describe('strengthTone — 低参与度不得呈现为「高确信」绿', () => {
    it('强度高 + 全体参与 = 绿（正常路径不得被误伤）', () => {
        expect(strengthTone(FULL)).toBe(EMERALD)
    })

    it('强度高 + 1/7 参与 = 琥珀（警示），而不是绿', () => {
        expect(strengthTone(ONE_OF_SEVEN)).toBe(AMBER)
    })

    it('强度本就低时仍按数值取色', () => {
        const weak = summary({ consensus_strength: 30, low_participation: false })
        expect(strengthTone(weak)).toBe(ROSE)
    })

    it('旧口径（无法判断参与度）保持原数值取色，行为不回退', () => {
        expect(strengthTone(LEGACY)).toBe(EMERALD)
    })
})

describe('metricTone 阈值不得改变（其余三个指标共用）', () => {
    it('75/45 为分界', () => {
        expect(metricTone(75)).toBe(EMERALD)
        expect(metricTone(74)).toBe(AMBER)
        expect(metricTone(45)).toBe(AMBER)
        expect(metricTone(44)).toBe(ROSE)
    })
})

import { describe, expect, it } from 'vitest'

import {
    buildVerdictView,
    componentRows,
    confidenceBand,
    coverageNotice,
    outcomeRows,
    stateBadge,
} from '@/components/verdictPresentation'
import type { VerdictHistoryItem, WashoutVerdict } from '@/types'

function verdict(overrides: Partial<WashoutVerdict> = {}): WashoutVerdict {
    return {
        probe: 'washout',
        symbol: '600519.SH',
        as_of: '2026-01-16',
        state: 'washout',
        state_label: '洗盘（震仓）',
        confidence: 61,
        margin: 0.42,
        scores: { washout: 3.1, distribution: 0.4, trend_break: 0.0 },
        high_confidence: false,
        daily_only: true,
        signal_version: 'washout-v1.0.0',
        applied_gates: [],
        unavailable: ['chip_distribution', 'intraday_absorption'],
        falsifiers: [
            {
                observable: 'next_day_volume_ratio',
                threshold: '> 1.5',
                window_days: 1,
                description: '次日放量下跌 → 假设失效',
                derived_from: 'volume_contraction',
            },
            {
                observable: 'close_vs_short_ma',
                threshold: '收盘站回 5 日线上方',
                window_days: 3,
                description: '3 个交易日内未站回 5 日线 → 降级',
                derived_from: 'ma_structure',
            },
        ],
        components: [
            {
                key: 'volume_contraction',
                available: true,
                side: 'washout',
                magnitude: 0.73,
                weight: 1.2,
                raw: { rebound_volume_recovery: 1.228 },
                unavailable_reason: null,
            },
            {
                key: 'price_action',
                available: true,
                side: null,
                magnitude: 0.0,
                weight: 1.0,
                raw: { intraday_recovery_ratio: 0.88 },
                unavailable_reason: null,
            },
            {
                key: 'chip_distribution',
                available: false,
                side: null,
                magnitude: 0.0,
                weight: 0.7,
                raw: { profit_ratio: null },
                unavailable_reason: 'chip distribution unavailable',
            },
        ],
        modulator: { key: 'position', factor: 1.5, raw: { cum_gain: -0.04 } },
        disclaimer: '本结论为基于可观测数据的概率化研究判断，不构成投资建议。',
        verdict_id: 'v1',
        ...overrides,
    }
}

describe('stateBadge', () => {
    it('renders actionable labels for the three directional states', () => {
        expect(stateBadge('washout').label).toContain('洗盘')
        expect(stateBadge('distribution').label).toContain('派发')
        expect(stateBadge('trend_break').label).toContain('趋势破坏')
    })

    it('makes abstention visibly different from a directional call', () => {
        // 弃权必须一眼看出「没有结论」，否则用户会把它当成一个弱化版观点
        const abstain = stateBadge('insufficient_evidence')
        expect(abstain.label).toContain('证据不足')
        expect(abstain.tone).not.toBe(stateBadge('washout').tone)
        expect(abstain.isAbstention).toBe(true)
        expect(stateBadge('washout').isAbstention).toBe(false)
    })
})

describe('confidenceBand', () => {
    it('never claims high confidence when the structural condition fails', () => {
        // daily_only 时结构条件不满足 → 无论分数多高都不能显示「高置信」
        const band = confidenceBand(61, false)
        expect(band.label).not.toContain('高')
        expect(band.highConfidence).toBe(false)
    })

    it('marks high confidence only when the flag is set', () => {
        const band = confidenceBand(85, true)
        expect(band.highConfidence).toBe(true)
        expect(band.label).toContain('高')
    })

    it('labels abstention as having no confidence', () => {
        const band = confidenceBand(0, false, { abstention: true })
        expect(band.label).toContain('不评估')
    })
})

describe('componentRows', () => {
    it('marks unavailable dimensions distinctly instead of showing a zero contribution', () => {
        const rows = componentRows(verdict())
        const chip = rows.find((r) => r.key === 'chip_distribution')!
        expect(chip.available).toBe(false)
        expect(chip.direction).toBe('unavailable')
        // 中性 0 与「拿不到数据」在界面上必须能区分
        const neutral = rows.find((r) => r.key === 'price_action')!
        expect(neutral.available).toBe(true)
        expect(neutral.direction).toBe('neutral')
    })

    it('maps side to a directional label', () => {
        const rows = componentRows(verdict())
        expect(rows.find((r) => r.key === 'volume_contraction')!.direction).toBe('washout')
    })

    it('exposes raw observables so the user can inspect the basis', () => {
        const rows = componentRows(verdict())
        const volume = rows.find((r) => r.key === 'volume_contraction')!
        expect(volume.observables.length).toBeGreaterThan(0)
        expect(volume.observables[0].field).toBe('rebound_volume_recovery')
    })

    it('does not include the position modulator as an additive component', () => {
        // 位置是调制器不是维度：混进组件表会让用户以为它是第 N 个加权项
        const rows = componentRows(verdict())
        expect(rows.some((r) => r.key === 'position')).toBe(false)
    })
})

describe('coverageNotice', () => {
    it('flags daily-only verdicts so the missing intraday evidence is explicit', () => {
        const notice = coverageNotice(verdict())
        expect(notice.dailyOnly).toBe(true)
        expect(notice.text).toContain('日线')
    })

    it('lists unavailable dimensions', () => {
        const notice = coverageNotice(verdict())
        expect(notice.unavailable).toContain('chip_distribution')
        expect(notice.text).toContain('chip_distribution')
    })

    it('explains applied gates rather than hiding them', () => {
        const notice = coverageNotice(
            verdict({ applied_gates: ['broad_contraction_blocks_washout'] }),
        )
        expect(notice.gates[0].explanation).toContain('缩量')
    })

    it('is quiet when there is nothing to disclose', () => {
        const notice = coverageNotice(
            verdict({ daily_only: false, unavailable: [], applied_gates: [] }),
        )
        expect(notice.text).toBe('')
        expect(notice.dailyOnly).toBe(false)
    })
})

describe('buildVerdictView', () => {
    it('always carries the disclaimer', () => {
        expect(buildVerdictView(verdict()).disclaimer).toContain('不构成投资建议')
    })

    it('shows the position modulator separately with its factor', () => {
        const view = buildVerdictView(verdict())
        expect(view.modulator).not.toBeNull()
        expect(view.modulator!.factor).toBeCloseTo(1.5)
        expect(view.modulator!.explanation).toContain('早期')
    })

    it('explains a late-stage modulator as biasing toward distribution', () => {
        const view = buildVerdictView(
            verdict({ modulator: { key: 'position', factor: 0.5, raw: { cum_gain: 0.99 } } }),
        )
        expect(view.modulator!.explanation).toContain('派发')
    })

    it('renders every falsifier with its threshold and window', () => {
        const view = buildVerdictView(verdict())
        expect(view.falsifiers).toHaveLength(2)
        expect(view.falsifiers[0].threshold).toBe('> 1.5')
        expect(view.falsifiers[0].window).toContain('1')
    })

    it('surfaces the scores so the decision is inspectable', () => {
        const view = buildVerdictView(verdict())
        expect(view.scores.washout).toBeCloseTo(3.1)
        expect(view.scores.distribution).toBeCloseTo(0.4)
    })

    it('does not present an abstention as holding a position', () => {
        const view = buildVerdictView(
            verdict({ state: 'insufficient_evidence', state_label: '证据不足', confidence: 0 }),
        )
        expect(view.badge.isAbstention).toBe(true)
        expect(view.summaryLine).toContain('未给出方向')
    })
})

describe('position view (D1 修复)', () => {
    it('shows the imported/ledger position with its provenance', () => {
        // 修复前：后端已返回 position_context，但类型里没有它、卡片也不显示
        const view = buildVerdictView(
            verdict({
                position_context: {
                    symbol: '600519.SH',
                    known: true,
                    source: 'trade_ledger',
                    quantity: 300,
                    sellable: 200,
                    average_cost: 1850,
                    position_pct: null,
                    market_value: null,
                    security_name: null,
                    as_of: '2026-03-02',
                    is_flat: false,
                    note: '来源：交易台账',
                },
            }),
        )
        expect(view.position).not.toBeNull()
        expect(view.position!.known).toBe(true)
        expect(view.position!.sourceLabel).toContain('台账')
        expect(view.position!.quantity).toBe(300)
        expect(view.position!.averageCost).toBe(1850)
    })

    it('states plainly that no position was provided', () => {
        const view = buildVerdictView(
            verdict({
                position_context: {
                    symbol: '600519.SH',
                    known: false,
                    source: 'none',
                    quantity: null,
                    sellable: null,
                    average_cost: null,
                    position_pct: null,
                    market_value: null,
                    security_name: null,
                    as_of: null,
                    is_flat: false,
                    note: '未提供持仓',
                },
            }),
        )
        expect(view.position!.known).toBe(false)
        expect(view.position!.sourceLabel).toContain('未提供')
        expect(view.position!.averageCost).toBeNull()
    })

    it('distinguishes a known flat position from unknown', () => {
        const view = buildVerdictView(
            verdict({
                position_context: {
                    symbol: '600519.SH', known: true, source: 'trade_ledger',
                    quantity: 0, sellable: 0, average_cost: null, position_pct: null,
                    market_value: null, security_name: null, as_of: '2026-03-02',
                    is_flat: true, note: '已清仓',
                },
            }),
        )
        expect(view.position!.flat).toBe(true)
        expect(view.position!.sourceLabel).not.toContain('未提供')
    })

    it('is null when the backend sent no position context', () => {
        expect(buildVerdictView(verdict()).position).toBeNull()
    })
})

describe('verdict outcome rows (9.5 修复)', () => {
    const item = {
        verdict_id: 'v1',
        symbol: '600519.SH',
        as_of: '2026-03-04',
        probe: 'washout',
        state: 'washout' as const,
        state_label: '洗盘（震仓）',
        confidence: 61,
        margin: 0.42,
        daily_only: true,
        high_confidence: false,
        signal_version: 'washout-v1.0.0',
        falsifiers: [],
        outcomes: [
            { horizon_days: 3, ref_date: '2026-03-09', ret_pct: 1.2, threshold_hit: false, evaluable: true, reason: null },
            { horizon_days: 5, ref_date: '2026-03-11', ret_pct: 4.5, threshold_hit: true, evaluable: true, reason: null },
            { horizon_days: 5, ref_date: '2026-03-11', ret_pct: null, threshold_hit: false, evaluable: false, reason: 'insufficient window: expected 5 bars, got 2' },
        ],
    }

    it('renders realised outcomes with horizon and hit state', () => {
        const rows = outcomeRows(item)
        expect(rows).toHaveLength(3)
        expect(rows[0].horizonLabel).toContain('3')
        expect(rows[1].hit).toBe(true)
    })

    it('marks non-evaluable outcomes instead of counting them as misses', () => {
        // 停牌导致的不可评估既不算命中也不算未命中——界面必须能区分
        const rows = outcomeRows(item)
        const notEvaluable = rows.find((r) => !r.evaluable)!
        expect(notEvaluable.label).toContain('不可评估')
        expect(notEvaluable.hit).toBe(false)
    })

    it('reports no outcomes as pending rather than as a failure', () => {
        expect(outcomeRows({ ...item, outcomes: [] })[0].label).toContain('待回填')
    })
})

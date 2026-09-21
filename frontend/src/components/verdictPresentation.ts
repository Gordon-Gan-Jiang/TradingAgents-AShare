/**
 * 判定卡片的展示逻辑（纯函数，可单测）。
 *
 * ## 为什么把展示逻辑从组件里抽出来
 *
 * 本仓库前端已有的约定是「纯逻辑模块 + `.test.ts`」——组件本身不单测，
 * 但**容易出错的部分必须被断言覆盖**。判定卡片里真正容易做错的是语义：
 *
 * * 弃权（`insufficient_evidence`）不能被渲染成一个弱化版方向性观点
 * * 「拿不到数据」与「中性 0 证据」在界面上必须能区分
 * * 位置调制器不是第 N 个加权维度，必须与加法贡献分开展示
 * * `daily_only` 时必须让「缺分时证据」可见，且不得显示高置信
 * * 被触发的门控（缩量加分被撤回）必须解释出来，而不是悄悄消失在数字里
 *
 * 这些都不是样式问题，而是「不能让用户误解结论强度」的问题，因此值得被测试钉住。
 */

import type { PositionContext, SignalComponent, VerdictHistoryItem, WashoutVerdict } from '@/types'

export type Tone = 'bullish' | 'bearish' | 'warning' | 'neutral' | 'muted'

export interface StateBadge {
    label: string
    tone: Tone
    /** 弃权：没有给出方向性结论。UI 必须与方向性判定显著区分。 */
    isAbstention: boolean
    description: string
}

export interface ConfidenceBand {
    label: string
    tone: Tone
    highConfidence: boolean
}

export interface ComponentRow {
    key: string
    label: string
    available: boolean
    direction: 'washout' | 'distribution' | 'trend_break' | 'neutral' | 'unavailable'
    magnitude: number
    weight: number
    observables: Array<{ field: string; value: string }>
    note: string | null
}

export interface FalsifierRow {
    observable: string
    threshold: string
    window: string
    description: string
    derivedFrom: string
}

export interface ModulatorView {
    factor: number
    explanation: string
    raw: Array<{ field: string; value: string }>
}

export interface CoverageNotice {
    dailyOnly: boolean
    unavailable: string[]
    gates: Array<{ key: string; explanation: string }>
    text: string
}

export interface PositionView {
    known: boolean
    /** 「交易台账」/「导入持仓快照」/「未提供持仓」 */
    sourceLabel: string
    quantity: number | null
    sellable: number | null
    averageCost: number | null
    asOf: string | null
    flat: boolean
    note: string
}

export interface OutcomeRow {
    horizonLabel: string
    refDate: string
    retPct: number | null
    hit: boolean
    evaluable: boolean
    label: string
    tone: Tone
}

const POSITION_SOURCE_LABELS: Record<string, string> = {
    trade_ledger: '交易台账',
    imported_snapshot: '导入持仓快照',
    none: '未提供持仓',
}

export function positionView(verdict: WashoutVerdict): PositionView | null {
    const ctx: PositionContext | null | undefined = verdict.position_context
    if (!ctx) return null
    return {
        known: Boolean(ctx.known),
        sourceLabel: POSITION_SOURCE_LABELS[ctx.source] ?? ctx.source,
        quantity: ctx.quantity ?? null,
        sellable: ctx.sellable ?? null,
        averageCost: ctx.average_cost ?? null,
        asOf: ctx.as_of ?? null,
        flat: Boolean(ctx.is_flat),
        note: ctx.note ?? '',
    }
}

export function outcomeRows(item: VerdictHistoryItem): OutcomeRow[] {
    const outcomes = item.outcomes ?? []
    if (outcomes.length === 0) {
        return [
            {
                horizonLabel: 'T+3 / T+5',
                refDate: '',
                retPct: null,
                hit: false,
                evaluable: false,
                // 还没到回填时间 ≠ 判定失败，措辞必须区分
                label: '待回填',
                tone: 'muted',
            },
        ]
    }
    return outcomes.map((o) => {
        const horizonLabel = `T+${o.horizon_days}`
        if (!o.evaluable) {
            return {
                horizonLabel,
                refDate: o.ref_date,
                retPct: null,
                hit: false,
                evaluable: false,
                // 停牌导致的不可评估既不算命中也不算未命中
                label: `不可评估（${o.reason ?? '窗口不完整'}）`,
                tone: 'muted' as Tone,
            }
        }
        return {
            horizonLabel,
            refDate: o.ref_date,
            retPct: o.ret_pct,
            hit: Boolean(o.threshold_hit),
            evaluable: true,
            label: o.threshold_hit ? '兑现' : '未兑现',
            tone: (o.threshold_hit ? 'bullish' : 'warning') as Tone,
        }
    })
}

export interface VerdictView {
    badge: StateBadge
    band: ConfidenceBand
    summaryLine: string
    scores: { washout: number; distribution: number; trend_break: number }
    components: ComponentRow[]
    falsifiers: FalsifierRow[]
    modulator: ModulatorView | null
    coverage: CoverageNotice
    position: PositionView | null
    disclaimer: string
    signalVersion: string
}

const STATE_META: Record<string, { label: string; tone: Tone; description: string }> = {
    washout: {
        label: '洗盘（震仓）',
        tone: 'bullish',
        description: '下杀过程中存在承接，判定为震仓而非结构性走坏。',
    },
    distribution: {
        label: '派发（出货）',
        tone: 'bearish',
        description: '量能扩张且资金构成偏向机构出货，判定为派发。',
    },
    trend_break: {
        label: '趋势破坏',
        tone: 'warning',
        description: '结构已坏（多日未收复均线），持有理由需要重审。',
    },
    insufficient_evidence: {
        label: '证据不足',
        tone: 'muted',
        description: '证据缺失或两侧过于接近，本次未给出方向性判定。',
    },
}

const COMPONENT_LABELS: Record<string, string> = {
    volume_contraction: '量能（缩量/反抽）',
    price_action: '价格行为（下影/收盘位置）',
    ma_structure: '均线结构',
    relative_strength: '相对强度（板块/主线）',
    capital_composition: '资金构成',
    chip_distribution: '筹码分布',
    intraday_absorption: '分时承接',
}

const GATE_EXPLANATIONS: Record<string, string> = {
    broad_contraction_blocks_washout:
        '个股缩量且换手低于板块中位，无法区分「主力锁仓」与「无人问津」，缩量侧加分已撤回。',
    contraction_credit_withheld_no_peer:
        '缺少板块中位换手数据，无法排除「流动性枯竭」的解释，缩量侧加分已撤回（未回退到仅自身比较）。',
}

const STATE_DIRECTION_LABEL: Record<string, ComponentRow['direction']> = {
    washout: 'washout',
    distribution: 'distribution',
    trend_break: 'trend_break',
}

export function stateBadge(state: string): StateBadge {
    const meta = STATE_META[state] ?? {
        label: state || '未知',
        tone: 'neutral' as Tone,
        description: '未知状态。',
    }
    return {
        label: meta.label,
        tone: meta.tone,
        isAbstention: state === 'insufficient_evidence',
        description: meta.description,
    }
}

export function confidenceBand(
    confidence: number,
    highConfidence: boolean,
    options: { abstention?: boolean } = {},
): ConfidenceBand {
    if (options.abstention) {
        // 弃权时置信度没有意义：显示成「低置信」会让用户以为这是一个弱观点
        return { label: '不评估（无方向性结论）', tone: 'muted', highConfidence: false }
    }
    if (highConfidence) {
        return { label: `高置信 · ${confidence}`, tone: 'bullish', highConfidence: true }
    }
    if (confidence >= 40) {
        return { label: `中等置信 · ${confidence}`, tone: 'neutral', highConfidence: false }
    }
    return { label: `低置信 · ${confidence}`, tone: 'warning', highConfidence: false }
}

function formatValue(value: unknown): string {
    if (value === null || value === undefined) return '—'
    if (typeof value === 'number') {
        if (Number.isInteger(value)) return String(value)
        return Math.abs(value) >= 1000 ? value.toFixed(0) : value.toFixed(4).replace(/0+$/, '')
    }
    if (typeof value === 'boolean') return value ? '是' : '否'
    return String(value)
}

export function componentRows(verdict: WashoutVerdict): ComponentRow[] {
    return (verdict.components ?? [])
        // 位置是调制器，不是加法维度；混进组件表会让用户以为它是第 N 个加权项
        .filter((c: SignalComponent) => c.key !== 'position')
        .map((c) => ({
            key: c.key,
            label: COMPONENT_LABELS[c.key] ?? c.key,
            available: c.available,
            direction: c.available
                ? (c.side ? STATE_DIRECTION_LABEL[c.side] : 'neutral')
                : 'unavailable',
            magnitude: c.magnitude,
            weight: c.weight,
            observables: Object.entries(c.raw ?? {})
                .filter(([, v]) => v !== null && v !== undefined)
                .map(([field, value]) => ({ field, value: formatValue(value) })),
            note: c.available ? null : (c.unavailable_reason ?? '数据不可得'),
        }))
}

export function coverageNotice(verdict: WashoutVerdict): CoverageNotice {
    const gates = (verdict.applied_gates ?? []).map((key) => ({
        key,
        explanation: GATE_EXPLANATIONS[key] ?? `已施加门控：${key}`,
    }))
    const unavailable = verdict.unavailable ?? []

    const parts: string[] = []
    if (verdict.daily_only) {
        parts.push('本轮仅有日线数据，缺少分时承接证据，置信度已相应限制。')
    }
    if (unavailable.length) {
        parts.push(`数据未覆盖：${unavailable.join('、')}（相关维度不参与判定，而非「无影响」）。`)
    }
    for (const gate of gates) {
        parts.push(gate.explanation)
    }

    return {
        dailyOnly: Boolean(verdict.daily_only),
        unavailable,
        gates,
        text: parts.join(' '),
    }
}

export function modulatorView(verdict: WashoutVerdict): ModulatorView | null {
    const modulator = verdict.modulator
    if (!modulator) return null

    const factor = modulator.factor
    let explanation: string
    if (factor > 1.05) {
        explanation =
            '位置处于早期（累计涨幅低），量价证据被放大：同形态在启动初期更可能是中继洗盘。'
    } else if (factor < 0.95) {
        explanation =
            '位置处于晚期（累计涨幅高），量价证据被压向派发侧：同形态在翻倍高位更可能是派发前兆。'
    } else {
        explanation = '位置处于中段，对量价证据的方向性影响中性。'
    }

    return {
        factor,
        explanation,
        raw: Object.entries(modulator.raw ?? {})
            .filter(([, v]) => v !== null && v !== undefined)
            .map(([field, value]) => ({ field, value: formatValue(value) })),
    }
}

export function falsifierRows(verdict: WashoutVerdict): FalsifierRow[] {
    return (verdict.falsifiers ?? []).map((f) => ({
        observable: f.observable,
        threshold: f.threshold,
        window: `${f.window_days} 个交易日内`,
        description: f.description,
        derivedFrom: f.derived_from,
    }))
}

export function buildVerdictView(verdict: WashoutVerdict): VerdictView {
    const badge = stateBadge(verdict.state)
    const coverage = coverageNotice(verdict)

    const summaryLine = badge.isAbstention
        ? `${verdict.symbol} @ ${verdict.as_of}：本次未给出方向（证据不足）`
        : `${verdict.symbol} @ ${verdict.as_of}：${badge.label}`

    return {
        badge,
        band: confidenceBand(verdict.confidence, Boolean(verdict.high_confidence), {
            abstention: badge.isAbstention,
        }),
        summaryLine,
        scores: {
            washout: verdict.scores?.washout ?? 0,
            distribution: verdict.scores?.distribution ?? 0,
            trend_break: verdict.scores?.trend_break ?? 0,
        },
        components: componentRows(verdict),
        falsifiers: falsifierRows(verdict),
        modulator: modulatorView(verdict),
        coverage,
        position: positionView(verdict),
        disclaimer: verdict.disclaimer,
        signalVersion: verdict.signal_version,
    }
}

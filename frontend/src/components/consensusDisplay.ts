import type { ConsensusSummary } from '@/types'

/**
 * 共识卡片上「参与度」的展示判定。
 *
 * 为什么需要这一层：`consensus_strength` 只衡量**投票者内部有多一致**，弃权者按
 * 约定 D2 不计入分母。于是 7 人面板里只有 1 人看多时强度同样是 100/100 —— 与 7 人
 * 一致时逐字节相同，而卡面上它还是醒目的绿色。审计 §2.2 实测：6 份已落库报告强度
 * ≥85 而参与者 ≤3，f7e60b27 为 1/7 → 100。
 *
 * 后端已补上 participant_n / expected_n / participation_ratio / low_participation
 * 四个字段（`api/services/consensus_service.py`）。本模块负责：把人数摆到卡面上，
 * 并且**在参与度低时不再用「高确信」的绿色呈现强度** —— 数字本身不改（把它乘上
 * 覆盖率会让「强度」失去精确含义），改的是它被呈现出的确信程度。
 */

/** 共识强度/分歧/稳定性共用的数值色调阈值。 */
export function metricTone(value: number): string {
    if (value >= 75) return 'text-emerald-600 dark:text-emerald-400'
    if (value >= 45) return 'text-amber-600 dark:text-amber-400'
    return 'text-rose-600 dark:text-rose-400'
}

/**
 * 与后端 `consensus_service._LOW_PARTICIPATION_RATIO` / `_DIRECT_MIN_PARTICIPANTS`
 * 保持一致。仅在**兼容窗口**（后端尚未写入可选字段的报告）里本地推算时使用。
 */
const LOW_PARTICIPATION_RATIO = 0.5
const MIN_PARTICIPANTS = 3

export interface Participation {
    /** 真正投票、计入加权的分析师数 */
    participant: number
    /** 出报告的分析师数（投票 + 弃权） */
    expected: number
    /** 参与度是否低到不足以支撑一个面板结论 */
    low: boolean
}

function asCount(value: unknown): number | null {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

function isLow(participant: number, expected: number): boolean {
    if (expected <= 0) return true
    return participant / expected < LOW_PARTICIPATION_RATIO || participant < MIN_PARTICIPANTS
}

/**
 * 解析参与度。
 *
 * 返回 ``null`` 表示**无法判断**（旧口径摘要）——此时界面必须什么都不说，
 * 不得猜一个参与度出来。
 */
export function resolveParticipation(
    summary: ConsensusSummary | null | undefined,
): Participation | null {
    if (!summary) return null

    const participant = asCount(summary.participant_n)
    const expected = asCount(summary.expected_n)
    if (participant !== null && expected !== null && expected > 0) {
        const low = typeof summary.low_participation === 'boolean'
            ? summary.low_participation
            : isLow(participant, expected)
        return { participant, expected, low }
    }

    // 兼容窗口：报告生成于「共识口径已收紧」与「参与度字段上线」之间，只有
    // recovered_analyst_n 而没有四个新字段。此时可以按轨迹自行推算。
    //
    // 旧口径（连 recovered_analyst_n 都没有）**不得**推算：它的 unparsed_analyst_n
    // 把「缺 VERDICT 块」也算作失败，会把这些报告误判成低参与度。
    if (summary.recovered_analyst_n === undefined) return null

    const breakdown = summary.agent_breakdown
    const unparsed = asCount(summary.unparsed_analyst_n)
    if (!Array.isArray(breakdown) || breakdown.length === 0 || unparsed === null) return null

    const derivedExpected = breakdown.length + unparsed
    return {
        participant: breakdown.length,
        expected: derivedExpected,
        low: isLow(breakdown.length, derivedExpected),
    }
}

/** 参与度文案；无法判断时返回 ``null``（不得编造）。 */
export function participationLabel(
    summary: ConsensusSummary | null | undefined,
): string | null {
    const p = resolveParticipation(summary)
    if (!p) return null
    const base = `${p.participant}/${p.expected} 位分析师参与`
    return p.low ? `${base}（参与度低，结论仅代表少数声音）` : base
}

/** 是否应显示「参与度低」警示。无法判断时为 ``false``（不猜）。 */
export function needsParticipationWarning(
    summary: ConsensusSummary | null | undefined,
): boolean {
    return resolveParticipation(summary)?.low === true
}

/**
 * 共识强度的色调。
 *
 * 强度高但参与度低时降级为琥珀警示：此时「100」说的是「投票的少数人意见一致」，
 * 而不是「面板共识很强」，用绿色呈现会让人误以为是后者。
 */
export function strengthTone(summary: ConsensusSummary): string {
    if (needsParticipationWarning(summary)) return 'text-amber-600 dark:text-amber-400'
    return metricTone(summary.consensus_strength)
}

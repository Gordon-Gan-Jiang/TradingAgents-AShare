export function detectDecisionLabel(text?: string | null): string | null {
    if (!text) return null
    const normalized = text.toLowerCase()
    if (normalized.includes('增持')) return '增持'
    if (normalized.includes('减持')) return '减持'
    if (normalized.includes('buy') || normalized.includes('买入')) return '买入'
    if (normalized.includes('sell') || normalized.includes('卖出')) return '卖出'
    if (normalized.includes('watch') || normalized.includes('观望')) return '观望'
    if (normalized.includes('hold') || normalized.includes('持有')) return '持有'
    return null
}

export function sanitizeReportMarkdown(text?: string | null): string {
    if (!text) return ''
    return text
        .replace(/<!--\s*VERDICT:[^>]*-->/gi, '') // strip machine-readable verdict tag
        .replace(/FINAL TRANSACTION PROPOSAL:\s*\**\s*BUY\s*\**/gi, '最终交易建议：买入')
        .replace(/FINAL TRANSACTION PROPOSAL:\s*\**\s*SELL\s*\**/gi, '最终交易建议：卖出')
        .replace(/FINAL TRANSACTION PROPOSAL:\s*\**\s*HOLD\s*\**/gi, '最终交易建议：观望')
        .replace(/FINAL VERDICT:\s*/gi, '最终裁决：')
        .replace(/HOLD with Conditional Trigger/gi, '观望（条件触发）')
        .replace(/BUY with Conditional Trigger/gi, '买入（条件触发）')
        .replace(/SELL with Conditional Trigger/gi, '卖出（条件触发）')
}

export function buildAgentSummary(text?: string | null): string {
    const cleaned = sanitizeReportMarkdown(text)
        .replace(/^#+\s*/gm, '')
        .replace(/\*\*/g, '')
        .replace(/\|/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
    const decision = detectDecisionLabel(cleaned)
    if (decision) return decision
    if (/偏多|看多|上涨|突破/.test(cleaned)) return '偏多'
    if (/偏空|看空|下跌|回撤/.test(cleaned)) return '偏空'
    if (/中性|震荡/.test(cleaned)) return '中性'
    if (cleaned.includes('风险')) return '风控结论'
    if (cleaned.includes('计划')) return '计划已生成'
    return cleaned.slice(0, 18) || '报告已生成'
}

export interface Verdict {
    direction: string
    reason: string
    /** 0–100，与后端 analyst_traces 一致；旧报告无 JSON 字段时用方向回退估算 */
    confidence: number
}

// Map English direction values (en.py prompts) to Chinese display labels
const DIRECTION_ALIAS: Record<string, string> = {
    BULLISH:       '看多',
    LEAN_BULLISH:  '偏多',
    BEARISH:       '看空',
    LEAN_BEARISH:  '偏空',
    NEUTRAL:       '中性',
    CAUTIOUS:      '谨慎',  // 向后兼容旧报告
}

function fallbackConfidenceFromDirection(direction: string): number {
    if (direction === '看多' || direction === '看空') return 68
    if (direction === '偏多' || direction === '偏空') return 58
    if (direction === '中性') return 48
    return 45
}

function coerceVerdictConfidence(raw: unknown, direction: string): number {
    if (raw === null || raw === undefined || raw === '') {
        return fallbackConfidenceFromDirection(direction)
    }
    if (typeof raw === 'boolean') return fallbackConfidenceFromDirection(direction)
    if (typeof raw === 'number') {
        if (raw >= 0 && raw <= 1) return Math.min(100, Math.max(0, Math.round(raw * 100)))
        return Math.min(100, Math.max(0, Math.round(raw)))
    }
    if (typeof raw === 'string') {
        const s = raw.trim()
        if (/^\d+$/.test(s)) return Math.min(100, Math.max(0, parseInt(s, 10)))
        const fv = parseFloat(s)
        if (!Number.isNaN(fv)) {
            if (fv >= 0 && fv <= 1) return Math.min(100, Math.max(0, Math.round(fv * 100)))
            return Math.min(100, Math.max(0, Math.round(fv)))
        }
        const bucket: Record<string, number> = {
            高: 82,
            中: 62,
            低: 42,
            high: 82,
            medium: 62,
            mid: 62,
            low: 42,
        }
        const k = s.toLowerCase()
        if (bucket[s] !== undefined) return bucket[s]
        if (bucket[k] !== undefined) return bucket[k]
    }
    return fallbackConfidenceFromDirection(direction)
}

/**
 * Extract the structured verdict embedded by the agent as an HTML comment.
 * Format: <!-- VERDICT: {"direction": "...", "reason": "...", "confidence": 72} -->
 */
export function extractVerdict(text?: string | null): Verdict | null {
    if (!text) return null
    const m = text.match(/<!--\s*VERDICT:\s*(\{[\s\S]*?\})\s*-->/i)
    if (!m) return null
    try {
        const rawJson = m[1].trim().replace(/\n/g, ' ').replace(/\r/g, ' ')
        const parsed = JSON.parse(rawJson) as { direction?: string; reason?: string; confidence?: unknown }
        if (!parsed.direction || !parsed.reason) return null
        const direction = DIRECTION_ALIAS[parsed.direction.toUpperCase()] ?? parsed.direction
        const confidence = coerceVerdictConfidence(parsed.confidence, direction)
        return { direction, reason: parsed.reason.trim().slice(0, 42), confidence }
    } catch {
        return null
    }
}

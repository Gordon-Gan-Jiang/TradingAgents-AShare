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

/**
 * 报告尾部会出现的机读块标签。它们承载的是已被解析过的结构化数据，
 * 渲染正文时必须剥离，否则原始 JSON 会漏进用户读到的报告。
 *
 * 注意：TRADE_PLAN 刻意**不在**此列。它的结构化解析目前不可用（块写在
 * trader_investment_plan，而解析器只读 final_trade_decision），原始块是用户
 * 唯一能看到止盈阶梯/止损价的途径，剥离等于销毁信息。
 */
const STRIPPED_MACHINE_TAGS = [
    'VERDICT',
    'ANALYST_JSON',
    'SELECTOR_JSON',
    'MAINLINE_JSON',
    'RISK_JUDGE',
    'CRITIC_RESULT',
    'DEBATE_STATE',
] as const

/**
 * 括号配平扫描：从 start 处的 `{` 起找到第一个完整 JSON 对象原文。
 * 跳过字符串内部与转义后的花括号（与后端 analyst_structured._scan_json_object 同构）。
 */
function scanBalancedObject(text: string, start: number): string | null {
    let depth = 0
    let inString = false
    let escaped = false
    for (let i = start; i < text.length; i += 1) {
        const ch = text[i]
        if (inString) {
            if (escaped) escaped = false
            else if (ch === '\\') escaped = true
            else if (ch === '"') inString = false
            continue
        }
        if (ch === '"') inString = true
        else if (ch === '{') depth += 1
        else if (ch === '}') {
            depth -= 1
            if (depth === 0) return text.slice(start, i + 1)
            if (depth < 0) return null
        }
    }
    return null
}

function stripWrappedMachineBlocks(text: string): string {
    const pattern = new RegExp(`<!--\\s*(?:${STRIPPED_MACHINE_TAGS.join('|')})\\b[\\s\\S]*?-->`, 'gi')
    return text.replace(pattern, '')
}

/** 剥离裸写（模型漏掉 <!-- --> 包裹）的机读块：标签 + 其后第一个配平对象 */
function stripBareMachineBlocks(text: string): string {
    let out = text
    for (const tag of STRIPPED_MACHINE_TAGS) {
        const marker = new RegExp(`${tag}\\s*:\\s*`, 'i').exec(out)
        if (!marker) continue
        const objStart = out.indexOf('{', marker.index)
        if (objStart < 0) continue
        const raw = scanBalancedObject(out, objStart)
        if (!raw) continue
        out = out.slice(0, marker.index) + out.slice(objStart + raw.length)
    }
    return out
}

export function sanitizeReportMarkdown(text?: string | null): string {
    if (!text) return ''
    return stripBareMachineBlocks(stripWrappedMachineBlocks(text))
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
    /**
     * 机读块给出的理由。ANALYST_JSON 块（分析师层现行唯一机读块）没有 reason 字段，
     * 此时为 undefined —— 宁可留空，也不编造理由文案。
     */
    reason?: string
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
 * 在 segment 内按出现顺序找出所有「带非空 direction 的 JSON 对象」。
 *
 * 逐个 `{` 尝试配平扫描，因此能容忍生产库里真实出现过的形态：
 *  - 双花括号 `{{...}}`（取内层对象）
 *  - 两个相邻对象 `{...}{...}`（取第一个带 direction 的）
 *  - 正文里未闭合的 `{`（扫描失败即跳过该候选）
 * 无法解析的候选一律跳过，绝不猜测。
 */
function findDirectionalObjects(segment: string): Record<string, unknown>[] {
    const hits: Record<string, unknown>[] = []
    for (let i = 0; i < segment.length; i += 1) {
        if (segment[i] !== '{') continue
        const raw = scanBalancedObject(segment, i)
        if (!raw) continue
        try {
            const parsed = JSON.parse(raw.replace(/[\n\r]/g, ' ')) as unknown
            if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
                const direction = (parsed as Record<string, unknown>).direction
                if (typeof direction === 'string' && direction.trim()) {
                    hits.push(parsed as Record<string, unknown>)
                }
            }
        } catch {
            // 该候选不是合法 JSON（双花括号外层、被截断的块）—— 继续扫描下一个 `{`
        }
    }
    return hits
}

/** 机读块候选区域：优先 `<!-- TAG: ... -->` 包裹区，其次标签之后的一段文本 */
function machineBlockRegions(text: string, tag: string): string[] {
    const regions: string[] = []
    const wrapped = new RegExp(`<!--\\s*${tag}\\s*:\\s*([\\s\\S]*?)-->`, 'i').exec(text)
    if (wrapped) regions.push(wrapped[1])
    const marker = new RegExp(`${tag}\\s*:\\s*`, 'i').exec(text)
    if (marker) {
        const start = marker.index + marker[0].length
        regions.push(text.slice(start, start + 12000))
    }
    return regions
}

function buildVerdict(parsed: Record<string, unknown>): Verdict | null {
    const rawDirection = typeof parsed.direction === 'string' ? parsed.direction.trim() : ''
    if (!rawDirection) return null
    const direction = DIRECTION_ALIAS[rawDirection.toUpperCase()] ?? rawDirection
    const reasonSource = [parsed.reason, parsed.key_finding, parsed.summary].find(
        (v): v is string => typeof v === 'string' && v.trim() !== '',
    )
    return {
        direction,
        confidence: coerceVerdictConfidence(parsed.confidence, direction),
        reason: reasonSource ? reasonSource.trim().slice(0, 42) : undefined,
    }
}

/**
 * 提取报告里的方向结论。
 *
 * 优先级：VERDICT（最终裁决用，向后兼容）→ ANALYST_JSON（分析师层现行唯一机读块）。
 * 两者都读不出方向时返回 null —— 调用方据此显示「未给出方向」，绝不编造中性。
 */
export function extractVerdict(text?: string | null): Verdict | null {
    if (!text) return null
    for (const tag of ['VERDICT', 'ANALYST_JSON']) {
        for (const region of machineBlockRegions(text, tag)) {
            const hit = findDirectionalObjects(region)[0]
            if (hit) return buildVerdict(hit)
        }
    }
    return null
}

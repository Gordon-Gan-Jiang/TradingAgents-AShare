/**
 * 结构化抽取结果（风险项 / 关键指标）的**纯展示逻辑**。
 *
 * 抽成独立 `.ts` 模块而不是写在 `.tsx` 里，是因为本仓库的 vitest 配置只收
 * `src/**\/*.test.ts`（`environment: 'node'`，无 jsdom），组件无法直接测。
 * 这里只返回可序列化的数据（不含 lucide 图标组件），所以能在 node 环境里测。
 *
 * ## 为什么必须有兜底（审计 §4.6）
 *
 * 后端不再把无法识别的等级冒充成 `medium`，而是落成显式的 `unknown`。前端此前的
 * 写法是**直接下标取值**：
 *
 *     const config = LEVEL_CONFIG[risk.level]
 *     <div className={config.color}>
 *
 * 取值失败时 `config` 是 `undefined`，`config.color` 立刻抛 `TypeError`，
 * **整个报告页面崩掉**。而库里还存在修复前的历史脏数据（`"高"`、`"优"`、`""`），
 * 所以"接得住未知取值"是硬要求，不是防御性编程。
 *
 * ## 两条不变量
 *
 * 1. 任何输入（含 null/undefined/任意字符串）都必须返回**可用**的样式对象；
 * 2. `unknown` 必须**看得出来是"没识别出来"**，不能长得跟真实取值一样——
 *    否则又回到"不知道被显示成中等"的老问题。
 */

export interface LevelTone {
    key: 'high' | 'medium' | 'low' | 'unknown'
    label: string
    color: string
    bg: string
}

export interface StatusTone {
    key: 'good' | 'neutral' | 'bad' | 'unknown'
    color: string
    label: string
}

const LEVEL_TONES: Record<LevelTone['key'], LevelTone> = {
    high: { key: 'high', label: '高风险', color: 'text-rose-400', bg: 'bg-rose-500/20' },
    medium: { key: 'medium', label: '中风险', color: 'text-amber-400', bg: 'bg-amber-500/20' },
    low: { key: 'low', label: '低风险', color: 'text-emerald-400', bg: 'bg-emerald-500/20' },
    unknown: {
        key: 'unknown',
        label: '未识别',
        color: 'text-slate-400',
        bg: 'bg-slate-500/20',
    },
}

const STATUS_TONES: Record<StatusTone['key'], StatusTone> = {
    good: { key: 'good', color: 'text-rose-400', label: '偏优' },
    neutral: { key: 'neutral', color: 'text-slate-200', label: '中性' },
    bad: { key: 'bad', color: 'text-emerald-400', label: '偏劣' },
    unknown: { key: 'unknown', color: 'text-slate-500', label: '未识别' },
}

/**
 * 历史脏数据的映射。修复前后端把中文写成 `level`，这些行**已经在库里**，
 * 修完读取路径不能因为"不认识"就把真实的高风险显示成"未识别"。
 */
const LEVEL_ALIASES: Record<string, LevelTone['key']> = {
    '高': 'high',
    '高风险': 'high',
    '较高': 'high',
    '偏高': 'high',
    '严重': 'high',
    '重大': 'high',
    '中': 'medium',
    '中等': 'medium',
    '中风险': 'medium',
    '一般': 'medium',
    '低': 'low',
    '低风险': 'low',
    '较低': 'low',
    '偏低': 'low',
    '轻微': 'low',
}

const STATUS_ALIASES: Record<string, StatusTone['key']> = {
    '优': 'good',
    '优秀': 'good',
    '好': 'good',
    '良好': 'good',
    '正面': 'good',
    '中': 'neutral',
    '中性': 'neutral',
    '一般': 'neutral',
    '持平': 'neutral',
    '差': 'bad',
    '较差': 'bad',
    '劣': 'bad',
    '负面': 'bad',
}

function normalize(raw: unknown): string {
    return typeof raw === 'string' ? raw.trim().toLowerCase() : ''
}

export function levelTone(level?: string | null): LevelTone {
    const key = normalize(level)
    if (key in LEVEL_TONES) return LEVEL_TONES[key as LevelTone['key']]
    const alias = LEVEL_ALIASES[key]
    if (alias) return LEVEL_TONES[alias]
    return LEVEL_TONES.unknown
}

export function statusTone(status?: string | null): StatusTone {
    const key = normalize(status)
    if (key in STATUS_TONES) return STATUS_TONES[key as StatusTone['key']]
    const alias = STATUS_ALIASES[key]
    if (alias) return STATUS_TONES[alias]
    return STATUS_TONES.unknown
}

/**
 * §4.7：`null`/`undefined` = 抽取结果里**没有这个字段**（不知道），
 * `[]` = 模型明确给出空列表（**确认没有**）。两者不能给同一句话。
 */
export function risksEmptyText(items?: unknown[] | null): string {
    if (items == null) return '本次未抽取到风险项（不代表没有风险）'
    return '本次分析未发现明确风险'
}

export function metricsEmptyText(items?: unknown[] | null): string {
    if (items == null) return '本次未抽取到关键指标'
    return '本次分析未给出关键指标'
}

/**
 * 数据源探测报告（`/data-sources`）的纯展示逻辑。
 *
 * 页面只负责取数与渲染；状态映射、排序、聚合、格式化都收敛在这里，
 * 这样能用 vitest（node 环境、无 DOM、无网络）直接覆盖边界情况：
 * 后端新增状态取值、`latest_run` 为 null、字段缺失、时间戳格式不一致等。
 *
 * 所有入参类型都是「只需要这些字段」的宽松结构（`*Like`），
 * 因此既能接收 `@/types` 里的响应类型，也能在测试里直接构造最小对象。
 */

/** 缺失值的统一占位符（与既有页面的写法保持一致）。 */
export const EMPTY_PLACEHOLDER = '—'

/** select / 过滤器中「全部」的取值。 */
export const FILTER_ALL = 'all'

export interface ProbeResultLike {
    source?: string | null
    tool?: string | null
    label?: string | null
    category?: string | null
    status?: string | null
    latency_ms?: number | null
    row_count?: number | null
    /** row_count 的单位：rows = 真实记录数，lines = 预格式化文本的非空行数 */
    row_unit?: string | null
    version?: string | null
    error_kind?: string | null
    error_type?: string | null
    error_message?: string | null
    checked_at?: string | null
}

export interface ProbeRunLike {
    id?: string | null
    started_at?: string | null
    finished_at?: string | null
    status?: string | null
    trigger?: string | null
    trading_day?: boolean | null
    total_count?: number | null
    ok_count?: number | null
    degraded_count?: number | null
    failed_count?: number | null
    /** 超时单列，**不计入 failed_count**——"慢"与"挂"要分开看 */
    timeout_count?: number | null
    unsupported_count?: number | null
    duration_ms?: number | null
}

export interface ProbeHistoryPointLike {
    checked_at?: string | null
    source?: string | null
    tool?: string | null
    status?: string | null
    latency_ms?: number | null
    row_count?: number | null
    /** 与 ProbeResultLike 同义：lines 时 row_count 只是文本体量代理 */
    row_unit?: string | null
    version?: string | null
}

export interface ProbeMatrixSourceLike {
    source?: string | null
    tier?: number | null
    batchable?: boolean | null
    needs_credential?: boolean | null
    note?: string | null
}

export interface ProbeMatrixItemLike {
    tool?: string | null
    label?: string | null
    category?: string | null
    note?: string | null
    sources?: readonly ProbeMatrixSourceLike[] | null
}

function toFiniteNumber(value: number | null | undefined): number | null {
    return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function trimmed(value: string | null | undefined): string {
    return typeof value === 'string' ? value.trim() : ''
}

/** 不依赖 locale 的稳定比较：避免不同 Node/ICU 版本下排序结果漂移。 */
function compareText(a: string | null | undefined, b: string | null | undefined): number {
    const left = trimmed(a).toLowerCase()
    const right = trimmed(b).toLowerCase()
    if (left === right) return 0
    return left < right ? -1 : 1
}

function sameText(a: string | null | undefined, b: string | null | undefined): boolean {
    return trimmed(a).toLowerCase() === trimmed(b).toLowerCase()
}

// ---------------------------------------------------------------------------
// 状态映射
// ---------------------------------------------------------------------------

/** 归一化状态值（小写去空白）；空值返回 ''。 */
export function normalizeProbeStatus(status: string | null | undefined): string {
    return trimmed(status).toLowerCase()
}

/** 未识别状态的排序权重：排在 ok 之后，避免未知值插到问题前面。 */
export const UNKNOWN_PROBE_RANK = 5
/** 未识别状态的严重度：像「未探测/等待中」一样中性，不参与报警。 */
export const UNKNOWN_PROBE_SEVERITY = 3

const SLATE_BADGE =
    'border-slate-200 bg-slate-100 text-slate-600 dark:border-slate-600 dark:bg-slate-700/40 dark:text-slate-300'
const SLATE_DOT = 'bg-slate-400'
const SLATE_BAR = 'bg-slate-400 dark:bg-slate-500'
const SLATE_VALUE = 'text-slate-600 dark:text-slate-300'

export interface ProbeStatusMeta {
    /** 归一化后的状态值；空值/未知均为 '' 或原值小写 */
    status: string
    /** 中文标签；未知状态回退为原值 */
    label: string
    /** 是否是后端契约里已知的状态 */
    known: boolean
    /**
     * 表格排序权重：越小越靠前（failed → timeout → degraded → unsupported → ok → 等待中 → 未知）
     */
    rank: number
    /**
     * 归并时的严重度：越小越严重，用于「按数据源取最差状态」。
     * failed 0 → timeout 0.5 → degraded 1 → 等待中/已跳过 2 → 未知 3 → ok 4 → unsupported 5。
     * 注意 ok 优于 unsupported：某个源只是「不负责这个维度」时，
     * 不应该让一个本来可用的源被标成灰色（不支持不是故障）。
     * 超时取 0.5 这种小数是为了「挤」进 failed 与 degraded 之间，
     * 从而不必重排既有状态的数值（重排容易在别处引入难以察觉的顺序变化）。
     */
    severity: number
    badgeClass: string
    dotClass: string
    barClass: string
    valueClass: string
    rowClass: string
}

const PROBE_STATUS_STYLES: Record<string, Omit<ProbeStatusMeta, 'status' | 'known'>> = {
    failed: {
        label: '不可用',
        rank: 0,
        severity: 0,
        badgeClass:
            'border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300',
        dotClass: 'bg-rose-500',
        barClass: 'bg-rose-500',
        valueClass: 'text-rose-600 dark:text-rose-400',
        rowClass: 'bg-rose-50/60 dark:bg-rose-500/5',
    },
    /**
     * 超时 = 这一轮超过了硬截止（默认 20s），**不是**「源挂了」：源可能只是慢。
     * 因此刻意用紫色（与 failed 的玫红、degraded 的琥珀都不同），
     * 并且排序上紧跟在 failed 之后，避免「慢」被读成「死」。
     */
    timeout: {
        label: '超时',
        rank: 0.5,
        severity: 0.5,
        badgeClass:
            'border-violet-200 bg-violet-50 text-violet-700 dark:border-violet-500/30 dark:bg-violet-500/10 dark:text-violet-300',
        dotClass: 'bg-violet-500',
        barClass: 'bg-violet-500',
        valueClass: 'text-violet-600 dark:text-violet-400',
        rowClass: 'bg-violet-50/50 dark:bg-violet-500/5',
    },
    degraded: {
        label: '降级',
        rank: 1,
        severity: 1,
        badgeClass:
            'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300',
        dotClass: 'bg-amber-500',
        barClass: 'bg-amber-500',
        valueClass: 'text-amber-600 dark:text-amber-400',
        rowClass: 'bg-amber-50/50 dark:bg-amber-500/5',
    },
    unsupported: {
        label: '不支持',
        rank: 2,
        severity: 5,
        badgeClass: SLATE_BADGE,
        dotClass: SLATE_DOT,
        barClass: SLATE_BAR,
        valueClass: SLATE_VALUE,
        rowClass: '',
    },
    ok: {
        label: '可用',
        rank: 3,
        severity: 4,
        badgeClass:
            'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-500/30 dark:bg-emerald-500/10 dark:text-emerald-300',
        dotClass: 'bg-emerald-500',
        barClass: 'bg-emerald-500',
        valueClass: 'text-emerald-600 dark:text-emerald-400',
        rowClass: '',
    },
    pending: {
        label: '等待中',
        rank: 4,
        severity: 2,
        badgeClass: SLATE_BADGE,
        dotClass: SLATE_DOT,
        barClass: SLATE_BAR,
        valueClass: SLATE_VALUE,
        rowClass: '',
    },
    skipped: {
        label: '已跳过',
        rank: 4,
        severity: 2,
        badgeClass: SLATE_BADGE,
        dotClass: SLATE_DOT,
        barClass: SLATE_BAR,
        valueClass: SLATE_VALUE,
        rowClass: '',
    },
}

/**
 * 状态 → 标签/配色/排序权重。
 * 未知取值（后端新增状态、脏数据）一律回退为灰色 + 原值作为标签，绝不抛错。
 */
export function probeStatusMeta(status: string | null | undefined): ProbeStatusMeta {
    const key = normalizeProbeStatus(status)
    const style = PROBE_STATUS_STYLES[key]
    if (style) return { status: key, known: true, ...style }
    const raw = trimmed(status)
    return {
        status: key,
        label: raw || '未知',
        known: false,
        rank: UNKNOWN_PROBE_RANK,
        severity: UNKNOWN_PROBE_SEVERITY,
        badgeClass: SLATE_BADGE,
        dotClass: SLATE_DOT,
        barClass: SLATE_BAR,
        valueClass: SLATE_VALUE,
        rowClass: '',
    }
}

export function probeStatusRank(status: string | null | undefined): number {
    return probeStatusMeta(status).rank
}

export function probeStatusSeverity(status: string | null | undefined): number {
    return probeStatusMeta(status).severity
}

/** 取一组状态里最严重的一个（用于「数据源概览」的汇总圆点）。 */
export function worstProbeStatusMeta(statuses: ReadonlyArray<string | null | undefined>): ProbeStatusMeta {
    let worst: ProbeStatusMeta | null = null
    for (const status of statuses) {
        const meta = probeStatusMeta(status)
        if (!worst || meta.severity < worst.severity) worst = meta
    }
    return worst ?? probeStatusMeta(undefined)
}

/** 能力矩阵里「该组合还没被探测过」的占位状态。 */
export const UNPROBED_STATUS_META: ProbeStatusMeta = {
    status: '',
    label: '未探测',
    known: false,
    rank: UNKNOWN_PROBE_RANK,
    severity: UNKNOWN_PROBE_SEVERITY,
    badgeClass: SLATE_BADGE,
    dotClass: SLATE_DOT,
    barClass: SLATE_BAR,
    valueClass: SLATE_VALUE,
    rowClass: '',
}

// ---------------------------------------------------------------------------
// 聚合与排序
// ---------------------------------------------------------------------------

export interface ProbeSummaryCounts {
    total: number
    ok: number
    degraded: number
    failed: number
    /** 超过硬截止的目标数；后端不计入 failed，这里同样单列 */
    timeout: number
    unsupported: number
    /** 等待中/已跳过/未知等未归类状态 */
    other: number
}

function emptyCounts(): ProbeSummaryCounts {
    return { total: 0, ok: 0, degraded: 0, failed: 0, timeout: 0, unsupported: 0, other: 0 }
}

/** 按明细逐条统计（未知状态计入 other）。 */
export function countProbeResults(
    results: readonly ProbeResultLike[] | null | undefined,
): ProbeSummaryCounts {
    const counts = emptyCounts()
    for (const result of results ?? []) {
        counts.total += 1
        switch (normalizeProbeStatus(result?.status)) {
            case 'ok':
                counts.ok += 1
                break
            case 'degraded':
                counts.degraded += 1
                break
            case 'failed':
                counts.failed += 1
                break
            case 'timeout':
                counts.timeout += 1
                break
            case 'unsupported':
                counts.unsupported += 1
                break
            default:
                counts.other += 1
        }
    }
    return counts
}

/**
 * 摘要计数：优先用后端给出的运行摘要（它含未被明细覆盖的计数），
 * `latest_run` 为 null（从未探测过）或摘要计数全缺时退化为按明细统计。
 */
export function resolveProbeCounts(
    run: ProbeRunLike | null | undefined,
    results?: readonly ProbeResultLike[] | null,
): ProbeSummaryCounts {
    const fromResults = countProbeResults(results)
    const total = toFiniteNumber(run?.total_count)
    const ok = toFiniteNumber(run?.ok_count)
    const degraded = toFiniteNumber(run?.degraded_count)
    const failed = toFiniteNumber(run?.failed_count)
    // 超时单列：老后端不返回该字段（当时超时是并进 failed 的）时按 0 处理，行为与改造前一致
    const timeout = toFiniteNumber(run?.timeout_count)
    const unsupported = toFiniteNumber(run?.unsupported_count)
    if (!run || [total, ok, degraded, failed, timeout, unsupported].every(value => value === null)) {
        return fromResults
    }
    const resolved = {
        ok: ok ?? 0,
        degraded: degraded ?? 0,
        failed: failed ?? 0,
        timeout: timeout ?? 0,
        unsupported: unsupported ?? 0,
    }
    const named =
        resolved.ok + resolved.degraded + resolved.failed + resolved.timeout + resolved.unsupported
    const resolvedTotal = total ?? named
    return { total: resolvedTotal, ...resolved, other: Math.max(0, resolvedTotal - named) }
}

export interface ProbeSummaryCardSpec {
    key: keyof ProbeSummaryCounts
    label: string
    valueClass: string
}

/**
 * 摘要卡片：顺序固定（总数在前，其余按「好 → 坏」排列）。
 * 超时紧跟在「不可用」之后：它比不可用轻，但必须能从卡片上一眼区别于「0 个问题」。
 */
export const PROBE_SUMMARY_CARDS: readonly ProbeSummaryCardSpec[] = [
    { key: 'total', label: '本次探测总数', valueClass: 'text-slate-900 dark:text-slate-100' },
    { key: 'ok', label: '可用', valueClass: 'text-emerald-600 dark:text-emerald-400' },
    { key: 'degraded', label: '降级', valueClass: 'text-amber-600 dark:text-amber-400' },
    { key: 'failed', label: '不可用', valueClass: 'text-rose-600 dark:text-rose-400' },
    { key: 'timeout', label: '超时', valueClass: 'text-violet-600 dark:text-violet-400' },
    { key: 'unsupported', label: '不支持', valueClass: 'text-slate-500 dark:text-slate-400' },
]

/**
 * 明细排序：问题优先（failed → timeout → degraded → unsupported → ok → 等待中 → 未知），
 * 其次按数据源、维度排序。不修改入参数组。
 */
export function sortProbeResultRows<T extends ProbeResultLike>(
    rows: readonly T[] | null | undefined,
): T[] {
    return [...(rows ?? [])].filter(Boolean).sort((a, b) => {
        const byRank = probeStatusRank(a?.status) - probeStatusRank(b?.status)
        if (byRank !== 0) return byRank
        const bySource = compareText(a?.source, b?.source)
        if (bySource !== 0) return bySource
        const byTool = compareText(a?.tool, b?.tool)
        if (byTool !== 0) return byTool
        return compareText(a?.checked_at, b?.checked_at)
    })
}

export interface ProbeSourceSummary extends ProbeSummaryCounts {
    source: string
    worstMeta: ProbeStatusMeta
}

/** 按数据源归并（即「按数据源分组」），并按数据源名排序。 */
export function summariseBySource(
    rows: readonly ProbeResultLike[] | null | undefined,
): ProbeSourceSummary[] {
    const grouped = new Map<string, ProbeResultLike[]>()
    for (const row of rows ?? []) {
        const source = trimmed(row?.source) || '未知数据源'
        const bucket = grouped.get(source)
        if (bucket) bucket.push(row)
        else grouped.set(source, [row])
    }
    return [...grouped.entries()]
        .map(([source, bucket]) => ({
            source,
            ...countProbeResults(bucket),
            worstMeta: worstProbeStatusMeta(bucket.map(row => row?.status)),
        }))
        .sort((a, b) => compareText(a.source, b.source))
}

// ---------------------------------------------------------------------------
// 过滤
// ---------------------------------------------------------------------------

export interface ProbeResultFilter {
    /** 自由文本：匹配数据源 / 维度 / 类别 / 版本 / 状态标签 / 错误信息 */
    query?: string
    /** 精确匹配数据源；空串、undefined、'all' 表示不过滤 */
    source?: string
    /** 精确匹配类别；空串、undefined、'all' 表示不过滤 */
    category?: string
}

function isFilterAll(value: string | null | undefined): boolean {
    const key = trimmed(value).toLowerCase()
    return !key || key === FILTER_ALL
}

export function filterProbeResultRows<T extends ProbeResultLike>(
    rows: readonly T[] | null | undefined,
    filter: ProbeResultFilter = {},
): T[] {
    const query = trimmed(filter.query).toLowerCase()
    const filterBySource = !isFilterAll(filter.source)
    const filterByCategory = !isFilterAll(filter.category)
    return (rows ?? []).filter(row => {
        if (!row) return false
        if (filterBySource && !sameText(row.source, filter.source)) return false
        if (filterByCategory && !sameText(row.category, filter.category)) return false
        if (!query) return true
        const haystack = [
            row.source,
            row.tool,
            row.label,
            row.category,
            row.version,
            probeStatusMeta(row.status).label,
            probeErrorText(row),
        ]
            .map(value => trimmed(value).toLowerCase())
            .join(' ')
        return haystack.includes(query)
    })
}

/** 提取某字段的全部取值（去重、排序），用于生成筛选下拉项。 */
export function listProbeFieldValues(
    rows: readonly ProbeResultLike[] | null | undefined,
    field: 'source' | 'category',
): string[] {
    const values = new Set<string>()
    for (const row of rows ?? []) {
        const value = trimmed(row?.[field])
        if (value) values.add(value)
    }
    return [...values].sort(compareText)
}

// ---------------------------------------------------------------------------
// 格式化
// ---------------------------------------------------------------------------

/** 毫秒 → 「210 ms」/「1.2 s」；缺失或非法值 → 「—」。也用于运行耗时。 */
export function formatLatency(ms: number | null | undefined): string {
    const value = toFiniteNumber(ms)
    if (value === null || value < 0) return EMPTY_PLACEHOLDER
    if (value < 1000) return `${Math.round(value)} ms`
    return `${(value / 1000).toFixed(1)} s`
}

/** 后端 `row_unit` 的取值：lines = 预格式化文本的非空行数（体量代理，不是记录数）。 */
const PROBE_ROW_UNIT_LINES = 'lines'

/**
 * `row_unit` 是否表示「行数只是体量代理」。
 * 大小写/空白都归一化；缺失或未识别取值一律返回 false，保持旧行为。
 */
export function isApproxRowUnit(unit: string | null | undefined): boolean {
    return trimmed(unit).toLowerCase() === PROBE_ROW_UNIT_LINES
}

/**
 * 行数 → 「17 行」；缺失或非法值 → 「—」。0 是有效值（降级的典型表现）。
 * `row_unit === 'lines'` 时是预格式化文本的非空行数，加「≈」提醒它不是记录数
 * （比如 12 行文本可能只是 1 张表），避免被当成 12 条记录。
 */
export function formatRowCount(count: number | null | undefined, unit?: string | null): string {
    const value = toFiniteNumber(count)
    if (value === null || value < 0) return EMPTY_PLACEHOLDER
    return `${isApproxRowUnit(unit) ? '≈' : ''}${Math.round(value)} 行`
}

/** 后端时间戳固定为东八区；显式按 +08:00 换算，结果不随运行环境时区变化。 */
const SHANGHAI_OFFSET_MS = 8 * 60 * 60 * 1000
const HAS_TIMEZONE_SUFFIX = /(?:Z|[+-]\d{2}:?\d{2})$/i
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/

/** 解析后端时间戳；缺少时区信息时按东八区处理，无法解析返回 NaN。 */
export function parseProbeTime(value: string | null | undefined): number {
    const raw = trimmed(value)
    if (!raw) return Number.NaN
    if (HAS_TIMEZONE_SUFFIX.test(raw)) return Date.parse(raw)
    if (DATE_ONLY.test(raw)) return Date.parse(`${raw}T00:00:00+08:00`)
    return Date.parse(`${raw.includes('T') ? raw : raw.replace(' ', 'T')}+08:00`)
}

export interface ProbeTimeFormatOptions {
    /** 附带秒（明细里的 checked_at 用） */
    seconds?: boolean
    /** 附带年份（跨年趋势用） */
    year?: boolean
}

/** 时间戳 → 「09-24 09:00」（可选秒/年）；空值 → 「—」，无法解析则原样返回。 */
export function formatProbeTimestamp(
    value: string | null | undefined,
    options: ProbeTimeFormatOptions = {},
): string {
    const raw = trimmed(value)
    if (!raw) return EMPTY_PLACEHOLDER
    const parsed = parseProbeTime(raw)
    if (!Number.isFinite(parsed)) return raw
    // 用 UTC getter 读取东八区墙上时间，从而与运行环境时区解耦
    const shifted = new Date(parsed + SHANGHAI_OFFSET_MS)
    const pad = (num: number) => String(num).padStart(2, '0')
    const date = `${options.year ? `${shifted.getUTCFullYear()}-` : ''}${pad(
        shifted.getUTCMonth() + 1,
    )}-${pad(shifted.getUTCDate())}`
    const time = `${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}${
        options.seconds ? `:${pad(shifted.getUTCSeconds())}` : ''
    }`
    return `${date} ${time}`
}

/** 是否交易日 → 「交易日」/「非交易日」；字段缺失 → 「—」。 */
export function formatTradingDay(value: boolean | null | undefined): string {
    if (value === true) return '交易日'
    if (value === false) return '非交易日'
    return EMPTY_PLACEHOLDER
}

/** 非交易日：行情类数据为空属于预期，页面要单独提示。 */
export function isNonTradingDay(value: boolean | null | undefined): boolean {
    return value === false
}

const TRIGGER_LABELS: Record<string, string> = {
    scheduled: '定时',
    manual: '手动',
}

/** 触发方式 → 「定时」/「手动」；未知取值原样展示。 */
export function triggerLabel(trigger: string | null | undefined): string {
    const raw = trimmed(trigger)
    if (!raw) return EMPTY_PLACEHOLDER
    return TRIGGER_LABELS[raw.toLowerCase()] ?? raw
}

/**
 * 完整错误描述：优先 error_message，并带上 error_kind / error_type 便于排查。
 * 无错误信息时返回空串（调用方决定占位符）。
 */
export function probeErrorText(result: ProbeResultLike | null | undefined): string {
    if (!result) return ''
    const message = trimmed(result.error_message)
    const prefix = [trimmed(result.error_kind), trimmed(result.error_type)]
        .filter(Boolean)
        .join(' / ')
    if (prefix && message) return `${prefix}: ${message}`
    return prefix || message
}

/** 截断长文本；被截断时以省略号结尾。 */
export function truncateText(text: string | null | undefined, max = 60): string {
    const raw = typeof text === 'string' ? text : ''
    if (max <= 0 || raw.length <= max) return raw
    return `${raw.slice(0, Math.max(0, max - 1))}…`
}

/** 表格「错误摘要」列：截断展示，完整文本由调用方放进 title 属性。 */
export function probeErrorSummary(result: ProbeResultLike | null | undefined, max = 48): string {
    const text = probeErrorText(result)
    return text ? truncateText(text, max) : EMPTY_PLACEHOLDER
}

// ---------------------------------------------------------------------------
// 能力矩阵
// ---------------------------------------------------------------------------

export interface ProbeTierMeta {
    tier: number | null
    label: string
    className: string
    description: string
}

const TIER_STYLES: Record<number, { label: string; className: string; description: string }> = {
    1: {
        label: '主源',
        className:
            'border-violet-200 bg-violet-50 text-violet-700 dark:border-violet-500/30 dark:bg-violet-500/10 dark:text-violet-300',
        description: '首选数据源，正常情况下优先使用',
    },
    2: {
        label: '兜底',
        className:
            'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300',
        description: '主源不可用时自动降级使用',
    },
    3: {
        label: '最后手段',
        className: SLATE_BADGE,
        description: '前两级都不可用时才使用',
    },
}

/** tier 1 = 主源，2 = 兜底，3 = 最后手段；其他取值原样标号，缺失为「未分级」。 */
export function probeTierMeta(tier: number | null | undefined): ProbeTierMeta {
    const value = toFiniteNumber(tier)
    if (value === null) {
        return { tier: null, label: '未分级', className: SLATE_BADGE, description: '未标注优先级' }
    }
    const style = TIER_STYLES[value]
    if (style) return { tier: value, ...style }
    return {
        tier: value,
        label: `T${value}`,
        className: SLATE_BADGE,
        description: `未定义的第 ${value} 级优先级`,
    }
}

/** 矩阵图例（按 tier 升序）。 */
export const PROBE_TIER_LEGEND: readonly ProbeTierMeta[] = [1, 2, 3].map(tier => probeTierMeta(tier))

export interface CapabilitySourceRow {
    source: string
    tier: ProbeTierMeta
    batchable: boolean
    needs_credential: boolean
    note: string
    /** 最新一次探测的状态原值；未探测过为 null */
    status: string | null
    statusMeta: ProbeStatusMeta
    latencyLabel: string
    /** 是否已有探测结果（用于区分「未探测」与「不支持」） */
    probed: boolean
}

export interface CapabilityRow {
    tool: string
    label: string
    category: string
    note: string
    sources: CapabilitySourceRow[]
}

function capabilityKey(source: string | null | undefined, tool: string | null | undefined): string {
    return `${trimmed(source).toLowerCase()}||${trimmed(tool).toLowerCase()}`
}

/**
 * 把「期望的能力矩阵」与「实际的探测结果」拼在一起：
 * 每个维度下列出它期望的数据源（按 tier 排序），并带上该组合的最新状态。
 * 这样读者可以在同一行里对照「应该有谁」和「实际谁通了」。
 */
export function buildCapabilityRows(
    items: readonly ProbeMatrixItemLike[] | null | undefined,
    results?: readonly ProbeResultLike[] | null,
): CapabilityRow[] {
    const latestByKey = new Map<string, ProbeResultLike>()
    for (const result of results ?? []) {
        if (!result) continue
        const key = capabilityKey(result.source, result.tool)
        if (!latestByKey.has(key)) latestByKey.set(key, result)
    }

    const rows: CapabilityRow[] = []
    for (const item of items ?? []) {
        if (!item) continue
        const tool = trimmed(item.tool)
        const sources: CapabilitySourceRow[] = []
        for (const source of item.sources ?? []) {
            if (!source) continue
            const name = trimmed(source.source)
            const actual = latestByKey.get(capabilityKey(name, tool))
            sources.push({
                source: name || '未知数据源',
                tier: probeTierMeta(source.tier),
                batchable: source.batchable === true,
                needs_credential: source.needs_credential === true,
                note: trimmed(source.note),
                status: actual ? normalizeProbeStatus(actual.status) : null,
                statusMeta: actual ? probeStatusMeta(actual.status) : UNPROBED_STATUS_META,
                latencyLabel: actual ? formatLatency(actual.latency_ms) : EMPTY_PLACEHOLDER,
                probed: Boolean(actual),
            })
        }
        sources.sort((a, b) => {
            const aTier = a.tier.tier ?? Number.MAX_SAFE_INTEGER
            const bTier = b.tier.tier ?? Number.MAX_SAFE_INTEGER
            if (aTier !== bTier) return aTier < bTier ? -1 : 1
            return compareText(a.source, b.source)
        })
        rows.push({
            tool,
            label: trimmed(item.label) || tool || '未命名维度',
            category: trimmed(item.category),
            note: trimmed(item.note),
            sources,
        })
    }

    return rows.sort((a, b) => {
        const byCategory = compareText(a.category, b.category)
        if (byCategory !== 0) return byCategory
        const byLabel = compareText(a.label, b.label)
        if (byLabel !== 0) return byLabel
        return compareText(a.tool, b.tool)
    })
}

// ---------------------------------------------------------------------------
// 趋势
// ---------------------------------------------------------------------------

export interface ProbeTrendBar {
    id: string
    startedAt: string | null
    timeLabel: string
    statusMeta: ProbeStatusMeta
    counts: ProbeSummaryCounts
    triggerLabel: string
    tradingDayLabel: string
    durationLabel: string
    /** 该次运行的总数相对最大值的高度百分比（0 = 无数据） */
    heightPct: number
}

/**
 * 把「最近若干次运行摘要」转成堆叠柱所需的数据。
 * 后端 runs[] 是最新在前，图表需要时间升序（最旧在左），因此这里反转。
 */
export function buildTrendBars(
    runs: readonly ProbeRunLike[] | null | undefined,
    limit = 14,
): ProbeTrendBar[] {
    const boundedLimit = toFiniteNumber(limit)
    const maxRuns = boundedLimit === null ? 14 : Math.max(0, Math.floor(boundedLimit))
    const recent = (runs ?? []).filter(Boolean).slice(0, maxRuns)
    const ascending = [...recent].reverse()
    const totals = ascending.map(run => resolveProbeCounts(run).total)
    const maxTotal = totals.reduce((acc, value) => (value > acc ? value : acc), 0)
    return ascending.map((run, index) => {
        const counts = resolveProbeCounts(run)
        return {
            id: trimmed(run.id) || `run-${index}`,
            startedAt: run.started_at ?? null,
            timeLabel: formatProbeTimestamp(run.started_at),
            statusMeta: probeStatusMeta(run.status),
            counts,
            triggerLabel: triggerLabel(run.trigger),
            tradingDayLabel: formatTradingDay(run.trading_day),
            durationLabel: formatLatency(run.duration_ms),
            heightPct: maxTotal > 0 ? Math.round((counts.total / maxTotal) * 100) : 0,
        }
    })
}

export type ProbeTrendSegmentKey = 'ok' | 'degraded' | 'unsupported' | 'timeout' | 'failed'

export interface ProbeTrendSegmentSpec {
    key: ProbeTrendSegmentKey
    label: string
    barClass: string
}

/**
 * 堆叠柱自下而上的顺序（健康 → 问题），与图例一致。
 * 超时排在「不可用」正下方：两者都是问题，但紫色提醒「慢」不等于「挂」；
 * 若不进图例，超时的柱子会被整段吞掉，看起来反而像一切正常。
 */
export const PROBE_TREND_SEGMENTS: readonly ProbeTrendSegmentSpec[] = [
    { key: 'ok', label: '可用', barClass: 'bg-emerald-500' },
    { key: 'degraded', label: '降级', barClass: 'bg-amber-500' },
    { key: 'unsupported', label: '不支持', barClass: 'bg-slate-400' },
    { key: 'timeout', label: '超时', barClass: 'bg-violet-500' },
    { key: 'failed', label: '不可用', barClass: 'bg-rose-500' },
]

export interface ProbeTrendSegment extends ProbeTrendSegmentSpec {
    count: number
    /** 该段占整根柱子的高度百分比（总数 > 0 时合计为 100） */
    pct: number
}

/** 单根柱子的分段高度：按本次运行的各状态计数按比例分配。 */
export function trendSegmentHeights(counts: ProbeSummaryCounts | null | undefined): ProbeTrendSegment[] {
    const total = toFiniteNumber(counts?.total) ?? 0
    return PROBE_TREND_SEGMENTS.map(segment => {
        const count = toFiniteNumber(counts?.[segment.key]) ?? 0
        return {
            ...segment,
            count,
            pct: total > 0 ? (count / total) * 100 : 0,
        }
    })
}

export interface ProbeTimelineCell {
    key: string
    checkedAt: string | null
    label: string
    statusMeta: ProbeStatusMeta
    latencyLabel: string
    rowCountLabel: string
}

export interface ProbeSourceTimeline extends ProbeSummaryCounts {
    source: string
    /** 时间升序（最旧在左），最多保留最近 limitPerSource 个点 */
    cells: ProbeTimelineCell[]
    worstMeta: ProbeStatusMeta
    lastCell: ProbeTimelineCell | null
}

/** 历史点按数据源分组，形成「每个数据源一条状态时间线」。 */
export function buildSourceTimelines(
    points: readonly ProbeHistoryPointLike[] | null | undefined,
    options: { limitPerSource?: number } = {},
): ProbeSourceTimeline[] {
    const requested = toFiniteNumber(options.limitPerSource)
    const limitPerSource = requested === null ? 30 : Math.max(1, Math.floor(requested))
    const grouped = new Map<string, ProbeTimelineCell[]>()
    let index = 0
    for (const point of points ?? []) {
        if (!point) continue
        const source = trimmed(point.source) || '未知数据源'
        const meta = probeStatusMeta(point.status)
        const cell: ProbeTimelineCell = {
            key: `${source}-${point.checked_at ?? index}-${index}`,
            checkedAt: point.checked_at ?? null,
            label: formatProbeTimestamp(point.checked_at, { seconds: true }),
            statusMeta: meta,
            latencyLabel: formatLatency(point.latency_ms),
            rowCountLabel: formatRowCount(point.row_count, point.row_unit),
        }
        index += 1
        const bucket = grouped.get(source)
        if (bucket) bucket.push(cell)
        else grouped.set(source, [cell])
    }

    const timelines: ProbeSourceTimeline[] = []
    for (const [source, cells] of grouped.entries()) {
        cells.sort((a, b) => {
            const aTime = parseProbeTime(a.checkedAt)
            const bTime = parseProbeTime(b.checkedAt)
            const aValue = Number.isFinite(aTime) ? aTime : Number.POSITIVE_INFINITY
            const bValue = Number.isFinite(bTime) ? bTime : Number.POSITIVE_INFINITY
            if (aValue !== bValue) return aValue < bValue ? -1 : 1
            return compareText(a.checkedAt, b.checkedAt)
        })
        const recentCells = cells.slice(-limitPerSource)
        const statuses = recentCells.map(cell => cell.statusMeta.status)
        timelines.push({
            source,
            cells: recentCells,
            ...countProbeResults(recentCells.map(cell => ({ status: cell.statusMeta.status }))),
            worstMeta: worstProbeStatusMeta(statuses),
            lastCell: recentCells.length > 0 ? recentCells[recentCells.length - 1] : null,
        })
    }

    // 最差状态在前，其次按数据源名，方便先看有问题的源
    return timelines.sort((a, b) => {
        const bySeverity = a.worstMeta.severity - b.worstMeta.severity
        if (bySeverity !== 0) return bySeverity
        return compareText(a.source, b.source)
    })
}

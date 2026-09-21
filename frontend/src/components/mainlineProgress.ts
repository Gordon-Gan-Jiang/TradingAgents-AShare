export const MAINLINE_PHASE_STEPS = [
    { id: 'collecting', label: '采集板块' },
    { id: 'analyst', label: '识别主线' },
    { id: 'selector', label: '主线选股' },
    { id: 'persisting', label: '生成报告' },
] as const

export type MainlinePhase = (typeof MAINLINE_PHASE_STEPS)[number]['id'] | 'pending' | 'started' | 'completed' | 'failed'

const PHASE_BASE: Record<string, number> = {
    pending: 8,
    started: 16,
    collecting: 28,
    analyst: 52,
    selector: 78,
    persisting: 94,
    completed: 100,
    failed: 100,
}

const PHASE_LABEL: Record<string, string> = {
    pending: '排队中',
    started: '启动中',
    collecting: '采集板块',
    analyst: '识别主线',
    selector: '主线选股',
    persisting: '生成报告',
    completed: '已完成',
    failed: '失败',
}

function clamp(value: number, min: number, max: number): number {
    return Math.min(max, Math.max(min, value))
}

function parseTimeOrNow(value?: string | null, fallback = Date.now()): number {
    if (!value) return fallback
    const parsed = Date.parse(value)
    return Number.isNaN(parsed) ? fallback : parsed
}

export function mainlinePhaseLabel(phase?: string | null): string {
    if (!phase) return '处理中'
    return PHASE_LABEL[phase] || phase
}

export function mainlinePhaseStepIndex(phase?: string | null): number {
    if (!phase || phase === 'pending' || phase === 'started') return 0
    if (phase === 'completed') return MAINLINE_PHASE_STEPS.length
    if (phase === 'failed') return -1
    const idx = MAINLINE_PHASE_STEPS.findIndex((step) => step.id === phase)
    return idx >= 0 ? idx : 0
}

export function getMainlineProgress(params: {
    status: 'pending' | 'running' | 'completed' | 'failed'
    phase?: string | null
    serverProgress?: number | null
    createdAt?: string | null
    nowMs?: number
}): { percent: number; label: string; detail: string } {
    if (params.status === 'completed') {
        return { percent: 100, label: '已完成', detail: '主线报告已生成' }
    }
    if (params.status === 'failed') {
        return { percent: 100, label: '失败', detail: '主线分析失败' }
    }

    const phase = params.status === 'pending' ? (params.phase || 'pending') : (params.phase || 'started')
    const base = PHASE_BASE[phase] ?? (params.status === 'pending' ? 8 : 22)
    const nowMs = params.nowMs ?? Date.now()
    const elapsedMs = Math.max(0, nowMs - parseTimeOrNow(params.createdAt, nowMs))
    const crawl = Math.round((Math.min(elapsedMs, 90_000) / 90_000) * 8)
    const fromServer = typeof params.serverProgress === 'number' && Number.isFinite(params.serverProgress)
        ? params.serverProgress
        : null
    const percent = clamp(Math.max(base, fromServer ?? 0) + crawl, 6, 96)

    return {
        percent,
        label: mainlinePhaseLabel(phase),
        detail: params.status === 'pending' ? '任务已进入队列，最多约 10 分钟' : '正在生成主线报告，最多约 10 分钟',
    }
}

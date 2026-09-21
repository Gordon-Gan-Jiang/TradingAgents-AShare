import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertCircle, CheckCircle2, Loader2 } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api } from '@/services/api'
import type { Mainline, MainlineBacktestResult, MainlineCandidate, MainlineReport, MainlineRuleCandidate, MainlineT1Outcome, MainlineT1Overview } from '@/types'
import TaskProgressBanner from '@/components/TaskProgressBanner'
import { MAINLINE_PHASE_STEPS, getMainlineProgress, mainlinePhaseStepIndex } from '@/components/mainlineProgress'
import CyclePanel from '@/components/CyclePanel'

const ACTIVE_JOB_KEY = 'ta-mainline-active-job'

const PHASE_STYLE: Record<string, string> = {
    '发酵': 'bg-cyan-100 text-cyan-700 dark:bg-cyan-500/10 dark:text-cyan-300',
    '主升': 'bg-red-100 text-red-700 dark:bg-red-500/10 dark:text-red-300',
    '高位分歧': 'bg-amber-100 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300',
    '退潮': 'bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300',
}
const STATUS_STYLE: Record<string, string> = {
    '延续': 'bg-emerald-100 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300',
    '扩散': 'bg-blue-100 text-blue-700 dark:bg-blue-500/10 dark:text-blue-300',
    '新发': 'bg-violet-100 text-violet-700 dark:bg-violet-500/10 dark:text-violet-300',
    '退潮': 'bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300',
}
const TIER_STYLE: Record<string, string> = {
    '龙头': 'bg-red-100 text-red-700 dark:bg-red-500/10 dark:text-red-300',
    '中军': 'bg-blue-100 text-blue-700 dark:bg-blue-500/10 dark:text-blue-300',
    '补涨': 'bg-amber-100 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300',
}

function todayStr(): string {
    const d = new Date()
    const m = String(d.getMonth() + 1).padStart(2, '0')
    const day = String(d.getDate()).padStart(2, '0')
    return `${d.getFullYear()}-${m}-${day}`
}

/** 候选股行唯一键（与表格 row key 保持一致） */
function candidateKey(c: MainlineCandidate): string {
    return `${c.symbol}-${c.mainline || ''}`
}

function cell(row: Record<string, unknown> | undefined, key: string): unknown {
    return row?.[key]
}

function num(v: unknown): number | null {
    if (v === null || v === undefined || v === '') return null
    const n = Number(v)
    return Number.isFinite(n) ? n : null
}

function fmtPct(v: unknown): string {
    const n = num(v)
    return n === null ? '-' : `${n > 0 ? '+' : ''}${n.toFixed(2)}%`
}

function fmtNum(v: unknown): string {
    const n = num(v)
    return n === null ? '-' : n.toLocaleString('zh-CN', { maximumFractionDigits: 1 })
}

export default function MainlinePanel() {
    const [perspective, setPerspective] = useState<'short' | 'medium'>('short')
    const [userFocus, setUserFocus] = useState('')
    const [tradeDate, setTradeDate] = useState(todayStr())
    const [activeTab, setActiveTab] = useState<'report' | 'candidates' | 'boards' | 't1' | 'cycle'>('report')

    const [report, setReport] = useState<MainlineReport | null>(null)
    const [runs, setRuns] = useState<MainlineReport[]>([])
    const [jobId, setJobId] = useState<string | null>(null)
    const [jobStatus, setJobStatus] = useState<string | null>(null)
    const [jobCreatedAt, setJobCreatedAt] = useState<string | null>(null)
    const [phase, setPhase] = useState<string | null>(null)
    const [serverProgress, setServerProgress] = useState<number | null>(null)
    const [progressDetail, setProgressDetail] = useState<string | null>(null)
    const [logs, setLogs] = useState<Array<{ at?: string; phase?: string; message: string }>>([])
    const [liveAnalyst, setLiveAnalyst] = useState('')
    const [liveSelector, setLiveSelector] = useState('')
    const [nowMs, setNowMs] = useState(() => Date.now())
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState('')
    const [notice, setNotice] = useState('')
    const [busyCandidate, setBusyCandidate] = useState<string | null>(null)

    const [t1Overview, setT1Overview] = useState<MainlineT1Overview | null>(null)
    const [t1Outcomes, setT1Outcomes] = useState<MainlineT1Outcome[]>([])
    const [t1Loading, setT1Loading] = useState(false)
    const [backtest, setBacktest] = useState<MainlineBacktestResult | null>(null)

    const [boardTab, setBoardTab] = useState<'industry' | 'concept'>('industry')
    const [boardSpot, setBoardSpot] = useState<{ industry: Array<Record<string, unknown>>; concept: Array<Record<string, unknown>>; sources: Record<string, string | null>; boardWarnings: string[] }>({
        industry: [],
        concept: [],
        sources: {},
        boardWarnings: [],
    })

    const [candidateFilter, setCandidateFilter] = useState('all')
    const pollRef = useRef<number | null>(null)
    const pollInFlightRef = useRef(false)

    const loadRuns = useCallback(async () => {
        try {
            const res = await api.listMainlineRuns(20)
            setRuns(res.runs || [])
        } catch {
            /* ignore */
        }
    }, [])

    const loadBoardSpot = useCallback(async () => {
        try {
            const [ind, con] = await Promise.all([
                api.getMainlineBoardSpot('industry'),
                api.getMainlineBoardSpot('concept'),
            ])
            setBoardSpot({
                industry: ind.boards || [],
                concept: con.boards || [],
                sources: { industry: ind.source ?? null, concept: con.source ?? null },
                boardWarnings: [...(ind.warnings || []), ...(con.warnings || [])],
            })
        } catch (e) {
            setError(`板块涨幅榜加载失败：${e instanceof Error ? e.message : String(e)}`)
        }
    }, [])

    const loadLatest = useCallback(async () => {
        try {
            const latest = await api.getMainlineLatest(perspective)
            setReport(latest)
        } catch {
            /* 404 = 尚无报告，忽略 */
        }
    }, [perspective])

    const loadT1 = useCallback(async () => {
        try {
            const [ov, items] = await Promise.all([api.getMainlineT1Overview(30), api.getMainlineT1Outcomes()])
            setT1Overview(ov)
            setT1Outcomes(items.items || [])
        } catch {
            /* 尚无 T+1 数据，忽略 */
        }
    }, [])

    const handleT1Refresh = async () => {
        setT1Loading(true)
        setError('')
        try {
            const res = await api.mainlineT1Refresh()
            setNotice(`T+1 兑现已更新：评估 ${res.evaluated_reports} 份报告${res.skipped.length ? `，跳过 ${res.skipped.length} 份（无前瞻数据）` : ''}`)
            await loadT1()
        } catch (e) {
            setError(`T+1 刷新失败：${e instanceof Error ? e.message : String(e)}`)
        } finally {
            setT1Loading(false)
        }
    }

    useEffect(() => {
        void loadRuns()
        void loadBoardSpot()
        void loadT1()
        api.getMainlineBacktestLatest().then(setBacktest).catch(() => setBacktest(null))
    }, [loadRuns, loadBoardSpot, loadT1])

    useEffect(() => {
        void loadLatest()
    }, [loadLatest])

    const running = jobId !== null && jobStatus !== null && jobStatus !== 'completed' && jobStatus !== 'failed'
    const progressView = getMainlineProgress({
        status: (jobStatus as 'pending' | 'running' | 'completed' | 'failed') || 'pending',
        phase,
        serverProgress,
        createdAt: jobCreatedAt,
        nowMs,
    })
    const activeStep = mainlinePhaseStepIndex(phase)

    const applyRunProgress = useCallback((run: MainlineReport) => {
        const snap = run.market_snapshot
        if (snap?.progress?.phase) setPhase(snap.progress.phase)
        if (typeof snap?.progress?.percent === 'number') setServerProgress(snap.progress.percent)
        if (snap?.progress?.detail) setProgressDetail(snap.progress.detail)
        if (snap?.progress_logs?.length) setLogs(snap.progress_logs)
        if (run.status === 'failed' && run.error) setError(`主线分析失败：${run.error}`)
    }, [])

    const attachJob = useCallback((id: string, status: string, createdAt?: string | null) => {
        setJobId(id)
        setJobStatus(status)
        setJobCreatedAt(createdAt || new Date().toISOString())
        try {
            sessionStorage.setItem(ACTIVE_JOB_KEY, id)
        } catch {
            /* ignore */
        }
    }, [])

    const clearActiveJob = useCallback(() => {
        try {
            sessionStorage.removeItem(ACTIVE_JOB_KEY)
        } catch {
            /* ignore */
        }
    }, [])

    useEffect(() => {
        if (!running) return
        const timer = window.setInterval(() => setNowMs(Date.now()), 1000)
        return () => window.clearInterval(timer)
    }, [running])

    // 刷新后从历史记录 / session 接回进行中的任务
    useEffect(() => {
        if (jobId) return
        let cancelled = false
        const resume = async () => {
            try {
                const res = await api.listMainlineRuns(20)
                if (cancelled) return
                setRuns(res.runs || [])
                let stored = ''
                try {
                    stored = sessionStorage.getItem(ACTIVE_JOB_KEY) || ''
                } catch {
                    stored = ''
                }
                const active = (res.runs || []).find((r) => r.status === 'pending' || r.status === 'running')
                const id = stored || active?.job_id || active?.id || ''
                if (!id) return
                const run = (res.runs || []).find((r) => r.id === id || r.job_id === id)
                if (run && (run.status === 'pending' || run.status === 'running')) {
                    attachJob(run.job_id || run.id, run.status, run.created_at)
                    applyRunProgress(run)
                    try {
                        const full = await api.getMainlineRun(run.id)
                        if (!cancelled) applyRunProgress(full)
                    } catch {
                        /* ignore */
                    }
                } else if (stored) {
                    clearActiveJob()
                }
            } catch {
                /* ignore */
            }
        }
        void resume()
        return () => {
            cancelled = true
        }
    }, [applyRunProgress, attachJob, clearActiveJob, jobId])

    // 任务轮询（带鉴权，刷新后仍能拿到状态 / 报告 / 错误日志）
    useEffect(() => {
        if (!jobId || jobStatus === 'completed' || jobStatus === 'failed') return
        const poll = async () => {
            if (pollInFlightRef.current) return
            pollInFlightRef.current = true
            try {
                let status = jobStatus
                let jobError = ''
                let jobMissing = false
                try {
                    const s = await api.getJobStatus(jobId)
                    status = s.status
                    setJobStatus(s.status)
                    if (s.phase) setPhase(s.phase)
                    if (typeof s.progress === 'number') setServerProgress(s.progress)
                    if (s.progress_detail) setProgressDetail(s.progress_detail)
                    if (s.created_at) setJobCreatedAt(s.created_at)
                    jobError = s.error || ''
                } catch {
                    jobMissing = true
                }
                const run = await api.getMainlineRun(jobId)
                if (run.status === 'pending' || run.status === 'running' || run.status === 'completed' || run.status === 'failed') {
                    status = run.status
                    setJobStatus(run.status)
                }
                applyRunProgress(run)
                const createdMs = Date.parse(run.created_at || jobCreatedAt || '') || Date.now()
                if (jobMissing && (run.status === 'pending' || run.status === 'running') && Date.now() - createdMs > 10 * 60 * 1000) {
                    status = 'failed'
                    jobError = '任务可能已中断（服务重启或超时），请重新生成'
                    setJobStatus('failed')
                }
                if (status === 'completed') {
                    setReport(run)
                    setError('')
                    setNotice('主线报告已生成')
                    clearActiveJob()
                    void loadRuns()
                    if (pollRef.current) window.clearInterval(pollRef.current)
                } else if (status === 'failed') {
                    setError(`主线分析失败：${jobError || run.error || '未知错误'}`)
                    clearActiveJob()
                    void loadRuns()
                    if (pollRef.current) window.clearInterval(pollRef.current)
                }
            } catch (e) {
                const message = e instanceof Error ? e.message : String(e)
                if (message.includes('超时') || message.includes('无法连接')) {
                    return
                }
                setError(`任务状态查询失败：${message}`)
            } finally {
                pollInFlightRef.current = false
            }
        }
        void poll()
        pollRef.current = window.setInterval(poll, 3000)
        return () => {
            if (pollRef.current) window.clearInterval(pollRef.current)
        }
    }, [applyRunProgress, clearActiveJob, jobId, jobStatus, loadRuns])

    // SSE 实时输出（fetch + Bearer，与智能分析同一套事件流）
    useEffect(() => {
        if (!jobId || !running) return
        const controller = new AbortController()
        let cancelled = false

        const consume = async () => {
            try {
                const response = await api.streamJobEvents(jobId, controller.signal)
                if (!response.body) return
                const reader = response.body.getReader()
                const decoder = new TextDecoder()
                let buffer = ''
                let currentEvent = 'message'
                while (!cancelled) {
                    const { value, done } = await reader.read()
                    if (done) break
                    buffer += decoder.decode(value, { stream: true })
                    const blocks = buffer.split('\n\n')
                    buffer = blocks.pop() || ''
                    for (const block of blocks) {
                        const lines = block.split('\n')
                        let dataLine = ''
                        for (const raw of lines) {
                            const line = raw.trim()
                            if (!line) continue
                            if (line.startsWith('event:')) currentEvent = line.slice(6).trim()
                            else if (line.startsWith('data:')) dataLine = line.slice(5).trim()
                        }
                        if (!dataLine || currentEvent === 'ping') continue
                        if (dataLine === '[DONE]' || currentEvent === 'done') return
                        let data: Record<string, unknown> = {}
                        try {
                            data = JSON.parse(dataLine) as Record<string, unknown>
                        } catch {
                            continue
                        }
                        if (currentEvent === 'mainline.phase') {
                            if (typeof data.phase === 'string') setPhase(data.phase)
                            if (typeof data.progress === 'number') setServerProgress(data.progress)
                            if (typeof data.detail === 'string') {
                                setProgressDetail(data.detail)
                                setLogs((prev) => [...prev, { at: new Date().toISOString(), phase: String(data.phase || ''), message: data.detail as string }].slice(-50))
                            }
                        } else if (currentEvent === 'agent.milestone') {
                            const title = String(data.title || data.agent || '')
                            if (title) {
                                setLogs((prev) => [...prev, { at: new Date().toISOString(), message: `${title} 开始输出` }].slice(-50))
                            }
                        } else if (currentEvent === 'agent.token') {
                            const content = String(data.content || '')
                            const agent = String(data.agent || '')
                            if (!content) continue
                            if (agent.includes('Selector')) setLiveSelector((prev) => (prev + content).slice(-12000))
                            else setLiveAnalyst((prev) => (prev + content).slice(-12000))
                        } else if (currentEvent === 'job.failed') {
                            const err = String(data.error || '未知错误')
                            setJobStatus('failed')
                            setError(`主线分析失败：${err}`)
                            setLogs((prev) => [...prev, { at: new Date().toISOString(), phase: 'failed', message: err }].slice(-50))
                        } else if (currentEvent === 'job.completed') {
                            setJobStatus('completed')
                        }
                    }
                }
            } catch (e) {
                if (controller.signal.aborted) return
                setLogs((prev) => [...prev, { at: new Date().toISOString(), message: `事件流中断，已改为轮询状态：${e instanceof Error ? e.message : String(e)}` }].slice(-50))
            }
        }
        void consume()
        return () => {
            cancelled = true
            controller.abort()
        }
    }, [jobId, running])

    const handleGenerate = async () => {
        // 一键发起整轮主线分析（大模型识别主线 + 选股，耗时数分钟、消耗模型额度），先二次确认再提交
        const confirmed = confirm(
            '确定生成主线报告吗？\n\n' +
            '将调用大模型识别市场主线并在主线内选股，通常耗时数分钟并消耗模型额度。'
        )
        if (!confirmed) return
        setError('')
        setNotice('')
        setLogs([])
        setLiveAnalyst('')
        setLiveSelector('')
        setPhase('pending')
        setServerProgress(8)
        setProgressDetail('任务已创建，等待执行')
        setLoading(true)
        try {
            const res = await api.analyzeMainline({
                trade_date: tradeDate || undefined,
                perspective,
                user_focus: userFocus.trim() || null,
            })
            attachJob(res.job_id, res.status || 'pending', res.created_at)
        } catch (e) {
            setError(`触发主线分析失败：${e instanceof Error ? e.message : String(e)}`)
        } finally {
            setLoading(false)
        }
    }

    const handleSelectRun = async (runId: string) => {
        try {
            const run = await api.getMainlineRun(runId)
            setReport(run)
            setActiveTab('report')
            applyRunProgress(run)
            if (run.status === 'pending' || run.status === 'running') {
                attachJob(run.job_id || run.id, run.status, run.created_at)
            }
        } catch (e) {
            setError(`加载报告失败：${e instanceof Error ? e.message : String(e)}`)
        }
    }

    const handleAddWatchlist = async (c: MainlineCandidate) => {
        if (!c.id) {
            setError(`候选股缺少 id，无法加入自选：${c.name}(${c.symbol})，请重新生成主线报告`)
            return
        }
        try {
            await api.mainlineCandidateToWatchlist(c.id)
            setNotice(`已把 ${c.name}(${c.symbol}) 加入自选`)
        } catch (e) {
            setError(`加入自选失败：${e instanceof Error ? e.message : String(e)}`)
        }
    }

    const handleAnalyzeCandidate = async (c: MainlineCandidate) => {
        if (!c.id) {
            setError(`候选股缺少 id，无法发起深度分析：${c.name}(${c.symbol})，请重新生成主线报告`)
            return
        }
        const key = candidateKey(c)
        if (busyCandidate === key) return
        // 深度分析会创建真实的多智能体任务（耗时数分钟、消耗模型额度），先二次确认再提交
        const confirmed = confirm(
            `确定为 ${c.name}(${c.symbol}) 发起深度分析吗？\n\n` +
            '将启动多智能体分析任务，通常耗时 1-5 分钟并消耗模型额度。'
        )
        if (!confirmed) return
        setBusyCandidate(key)
        setError('')
        setNotice('')
        try {
            const res = await api.mainlineCandidateAnalyze(c.id)
            setNotice(`已发起 ${c.name}(${c.symbol}) 深度分析，任务 ${res.job_id}，可在「报告」页查看进度`)
        } catch (e) {
            setError(`发起分析失败：${e instanceof Error ? e.message : String(e)}`)
        } finally {
            setBusyCandidate(null)
        }
    }

    const mainlines: Mainline[] = report?.mainlines || []
    const candidates: MainlineCandidate[] = report?.candidates || []
    const filteredCandidates = candidateFilter === 'all'
        ? candidates
        : candidates.filter((c) => c.mainline === candidateFilter)
    const snap = report?.market_snapshot || null
    const emotion = snap?.emotion || null
    const breadth = snap?.breadth || null
    const benchmark = snap?.benchmark || null

    return (
        <div className="space-y-5">
            {/* 头部：触发区 */}
            <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-700 dark:bg-slate-800">
                <div className="flex flex-wrap items-end justify-between gap-4">
                    <div>
                        <h2 className="text-lg font-bold text-slate-900 dark:text-slate-100">市场主线洞察</h2>
                        <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                            板块数据 → 大模型识别主线 → 主线内选股（数据源：东财/同花顺/新浪多源兜底）
                        </p>
                    </div>
                    <div className="flex flex-wrap items-end gap-3">
                        <label className="text-xs text-slate-500 dark:text-slate-400">
                            日期
                            <input
                                type="date"
                                value={tradeDate}
                                onChange={(e) => setTradeDate(e.target.value)}
                                className="mt-1 block rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-600 dark:bg-slate-900"
                            />
                        </label>
                        <div>
                            <span className="text-xs text-slate-500 dark:text-slate-400">视角</span>
                            <div className="mt-1 flex overflow-hidden rounded-lg border border-slate-300 dark:border-slate-600">
                                {(['short', 'medium'] as const).map((p) => (
                                    <button
                                        key={p}
                                        onClick={() => setPerspective(p)}
                                        className={`px-3 py-1.5 text-sm font-medium transition ${
                                            perspective === p
                                                ? 'bg-blue-600 text-white'
                                                : 'bg-white text-slate-600 hover:bg-slate-50 dark:bg-slate-900 dark:text-slate-300'
                                        }`}
                                    >
                                        {p === 'short' ? '短线题材' : '中期行业'}
                                    </button>
                                ))}
                            </div>
                        </div>
                        <label className="text-xs text-slate-500 dark:text-slate-400">
                            关注方向（可选）
                            <input
                                type="text"
                                value={userFocus}
                                onChange={(e) => setUserFocus(e.target.value)}
                                placeholder="如：只看 AI 相关"
                                className="mt-1 block w-44 rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-600 dark:bg-slate-900"
                            />
                        </label>
                        <button
                            onClick={handleGenerate}
                            disabled={loading || running}
                            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                            {running ? '生成中…' : '生成主线报告'}
                        </button>
                    </div>
                </div>

                {(running || jobStatus === 'failed') && (
                    <div className="mt-4 space-y-3">
                        <TaskProgressBanner
                            status={jobStatus === 'failed' ? 'error' : 'loading'}
                            progress={progressView.percent}
                            label={
                                jobStatus === 'failed'
                                    ? '主线分析失败'
                                    : jobStatus === 'pending'
                                      ? '主线任务排队中...'
                                      : '主线报告生成中...'
                            }
                            detail={
                                jobStatus === 'failed'
                                    ? (error || progressDetail || '任务执行失败')
                                    : (progressDetail || progressView.detail)
                            }
                        />
                        {running && (
                            <div className="flex flex-wrap gap-2">
                                {MAINLINE_PHASE_STEPS.map((step, idx) => {
                                    const done = activeStep > idx
                                    const current = activeStep === idx
                                    return (
                                        <span
                                            key={step.id}
                                            className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium ${
                                                done
                                                    ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300'
                                                    : current
                                                      ? 'bg-blue-50 text-blue-700 dark:bg-blue-500/10 dark:text-blue-300'
                                                      : 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400'
                                            }`}
                                        >
                                            {done ? <CheckCircle2 className="h-3 w-3" /> : current ? <Loader2 className="h-3 w-3 animate-spin" /> : <span className="h-3 w-3 rounded-full border border-current" />}
                                            {step.label}
                                        </span>
                                    )
                                })}
                                <span className="ml-auto text-[11px] text-slate-400">任务 {jobId?.slice(0, 8)}… · 超时 10 分钟</span>
                            </div>
                        )}
                    </div>
                )}
                {(jobStatus === 'failed' || (report?.status === 'failed' && !running)) && (
                    <div className="mt-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 dark:border-rose-500/30 dark:bg-rose-500/10">
                        <div className="flex items-start gap-2 text-sm text-rose-700 dark:text-rose-300">
                            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                            <div className="min-w-0">
                                <p className="font-medium">分析失败</p>
                                <pre className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap break-words font-sans text-xs leading-5">{error || report?.error || '未知错误'}</pre>
                            </div>
                        </div>
                        {logs.length > 0 && (
                            <details className="mt-2">
                                <summary className="cursor-pointer text-xs text-rose-600 dark:text-rose-300">查看任务日志（{logs.length}）</summary>
                                <ul className="mt-2 max-h-48 space-y-1 overflow-y-auto text-[11px] leading-5 text-rose-700/90 dark:text-rose-200/80">
                                    {logs.map((item, i) => (
                                        <li key={`${item.at || i}-${i}`}>
                                            {item.at ? `${item.at.slice(11, 19)} ` : ''}
                                            {item.phase ? `[${item.phase}] ` : ''}
                                            {item.message}
                                        </li>
                                    ))}
                                </ul>
                            </details>
                        )}
                    </div>
                )}
                {error && jobStatus !== 'failed' && report?.status !== 'failed' && <p className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600 dark:bg-red-500/10 dark:text-red-300">{error}</p>}
                {notice && <p className="mt-3 rounded-lg bg-emerald-50 px-3 py-2 text-xs text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-300">{notice}</p>}
            </div>

            {/* 历史报告选择 */}
            {runs.length > 0 && (
                <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs text-slate-500 dark:text-slate-400">历史报告：</span>
                    {runs.slice(0, 8).map((r) => (
                        <button
                            key={r.id}
                            onClick={() => handleSelectRun(r.id)}
                            className={`rounded-full border px-2.5 py-1 text-xs transition ${
                                report?.id === r.id
                                    ? 'border-blue-500 bg-blue-50 text-blue-700 dark:bg-blue-500/10 dark:text-blue-300'
                                    : 'border-slate-300 text-slate-600 hover:border-blue-400 dark:border-slate-600 dark:text-slate-300'
                            }`}
                        >
                            {r.trade_date} {r.perspective === 'short' ? '短线' : '中期'}
                            {r.status === 'pending' || r.status === 'running' ? ' · 生成中' : ''}
                            {r.status === 'failed' && ' ✗'}
                        </button>
                    ))}
                </div>
            )}

            {/* 情绪/宽度/基准 KPI */}
            {(emotion || breadth || benchmark) && (
                <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                    <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-800">
                        <p className="text-xs text-slate-500 dark:text-slate-400">情绪温度</p>
                        <p className="mt-1 text-xl font-bold text-slate-900 dark:text-slate-100">
                            {emotion?.temperature ?? '-'}
                            <span className="ml-2 text-xs font-normal text-slate-500">{emotion?.regime ?? ''}</span>
                        </p>
                        {emotion?.gate_reason && <p className="mt-1 text-[11px] text-amber-600 dark:text-amber-400">{emotion.gate_reason}</p>}
                    </div>
                    <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-800">
                        <p className="text-xs text-slate-500 dark:text-slate-400">
                            市场宽度{breadth?.approximate ? '（近似·THS聚合）' : ''}
                        </p>
                        <p className="mt-1 text-xl font-bold text-slate-900 dark:text-slate-100">
                            <span className="text-red-600 dark:text-red-400">{breadth?.up ?? '-'}</span>
                            <span className="mx-1.5 text-slate-400">/</span>
                            <span className="text-green-600 dark:text-green-400">{breadth?.down ?? '-'}</span>
                        </p>
                        <p className="mt-1 text-[11px] text-slate-400">涨/跌家数</p>
                    </div>
                    <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-800">
                        <p className="text-xs text-slate-500 dark:text-slate-400">沪深300</p>
                        <p className="mt-1 text-xl font-bold text-slate-900 dark:text-slate-100">
                            {benchmark?.chg_5d !== null && benchmark?.chg_5d !== undefined ? `${(benchmark.chg_5d * 100).toFixed(2)}%` : '-'}
                        </p>
                        <p className="mt-1 text-[11px] text-slate-400">5日涨幅</p>
                    </div>
                    <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-800">
                        <p className="text-xs text-slate-500 dark:text-slate-400">主线数</p>
                        <p className="mt-1 text-xl font-bold text-slate-900 dark:text-slate-100">{mainlines.length}</p>
                        <p className="mt-1 text-[11px] text-slate-400">+ 观察方向（如有）</p>
                    </div>
                </div>
            )}

            {/* 降级警告 */}
            {report?.warnings && report.warnings.length > 0 && (
                <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-xs text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300">
                    数据提示：{report.warnings.join('；')}
                </div>
            )}

            {/* Tabs */}
            <div className="flex gap-1 rounded-xl border border-slate-200 bg-white p-1 dark:border-slate-700 dark:bg-slate-800" style={{ width: 'fit-content' }}>
                {([
                    ['report', '主线报告'],
                    ['candidates', `候选股 (${candidates.length})`],
                    ['boards', '板块涨幅榜'],
                    ['t1', '兑现跟踪'],
                    ['cycle', '周期分析'],
                ] as const).map(([key, label]) => (
                    <button
                        key={key}
                        onClick={() => setActiveTab(key)}
                        className={`rounded-lg px-4 py-1.5 text-sm font-medium transition ${
                            activeTab === key
                                ? 'bg-blue-600 text-white'
                                : 'text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-700'
                        }`}
                    >
                        {label}
                    </button>
                ))}
            </div>

            {/* Tab1 主线报告 */}
            {activeTab === 'report' && (
                <div className="space-y-4">
                    {running && (liveAnalyst || liveSelector || logs.length > 0) && (
                        <div className="space-y-3">
                            {liveAnalyst && (
                                <div className="rounded-2xl border border-blue-200 bg-white p-4 shadow-sm dark:border-blue-500/20 dark:bg-slate-800">
                                    <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-slate-900 dark:text-slate-100">
                                        <Loader2 className="h-4 w-4 animate-spin text-blue-500" />
                                        主线分析师（实时输出）
                                    </div>
                                    <div className="max-h-72 overflow-y-auto text-sm leading-6 text-slate-700 dark:text-slate-300">
                                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{liveAnalyst}</ReactMarkdown>
                                    </div>
                                </div>
                            )}
                            {liveSelector && (
                                <div className="rounded-2xl border border-indigo-200 bg-white p-4 shadow-sm dark:border-indigo-500/20 dark:bg-slate-800">
                                    <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-slate-900 dark:text-slate-100">
                                        <Loader2 className="h-4 w-4 animate-spin text-indigo-500" />
                                        选股师（实时输出）
                                    </div>
                                    <div className="max-h-72 overflow-y-auto text-sm leading-6 text-slate-700 dark:text-slate-300">
                                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{liveSelector}</ReactMarkdown>
                                    </div>
                                </div>
                            )}
                            {logs.length > 0 && (
                                <details className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-800" open>
                                    <summary className="cursor-pointer text-xs font-medium text-slate-600 dark:text-slate-300">任务日志（{logs.length}）</summary>
                                    <ul className="mt-2 max-h-36 space-y-1 overflow-y-auto text-[11px] leading-5 text-slate-500 dark:text-slate-400">
                                        {logs.map((item, i) => (
                                            <li key={`${item.at || i}-${i}`}>
                                                {item.at ? `${item.at.slice(11, 19)} ` : ''}
                                                {item.phase ? `[${item.phase}] ` : ''}
                                                {item.message}
                                            </li>
                                        ))}
                                    </ul>
                                </details>
                            )}
                        </div>
                    )}
                    {report?.market_snapshot?.rule_candidates && report.market_snapshot.rule_candidates.length > 0 && (
                        <details className="rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
                            <summary className="cursor-pointer border-b border-slate-200 px-4 py-3 text-sm font-semibold text-slate-900 dark:border-slate-700 dark:text-slate-100">
                                规则层候选评分（LLM 判定依据，金融理论 v2）
                            </summary>
                            <div className="overflow-x-auto">
                                <table className="w-full text-left text-sm">
                                    <thead>
                                        <tr className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
                                            <th className="px-4 py-2 font-medium">板块</th>
                                            <th className="px-4 py-2 font-medium">RS20</th>
                                            <th className="px-4 py-2 font-medium">均线多头</th>
                                            <th className="px-4 py-2 font-medium">RSI14</th>
                                            <th className="px-4 py-2 font-medium">硬门槛</th>
                                            <th className="px-4 py-2 font-medium">阶段预判</th>
                                            <th className="px-4 py-2 font-medium">heat</th>
                                            <th className="px-4 py-2 font-medium">strength</th>
                                            <th className="px-4 py-2 font-medium">composite</th>
                                            <th className="px-4 py-2 font-medium">资金双周期</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {(report.market_snapshot.rule_candidates as MainlineRuleCandidate[]).map((b, i) => (
                                            <tr key={i} className="border-b border-slate-100 last:border-0 dark:border-slate-700/50">
                                                <td className="px-4 py-2 font-medium text-slate-900 dark:text-slate-100">{b.name}</td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{b.rs20 != null ? `${(b.rs20 * 100).toFixed(1)}%` : '-'}</td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{b.ma_bullish ? '是' : '否'}</td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{b.rsi14 != null ? b.rsi14.toFixed(1) : '-'}</td>
                                                <td className="px-4 py-2 text-xs">
                                                    <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${b.passes_gate ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300' : 'bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300'}`}>
                                                        {b.passes_gate ? '通过' : '未过'}
                                                    </span>
                                                </td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{b.phase_hint || '-'}</td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{b.heat_score}</td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{b.strength_score}</td>
                                                <td className="px-4 py-2 text-xs font-semibold text-slate-900 dark:text-slate-100">{b.composite_score}</td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">
                                                    {b.inflow_persistent_10 ? '是' : b.flow_data_missing ? '数据缺失' : '否'}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        </details>
                    )}
                    {report && report.status === 'completed' && mainlines.length === 0 && (
                        <p className="rounded-xl border border-slate-200 bg-white p-5 text-sm text-slate-500 dark:border-slate-700 dark:bg-slate-800">
                            本次未识别出高置信主线（情绪 gating 或数据不足），详见原始报告。
                        </p>
                    )}
                    {mainlines.map((m, idx) => (
                        <div key={`${m.name}-${idx}`} className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm dark:border-slate-700 dark:bg-slate-800">
                            <div className="flex flex-wrap items-center gap-2">
                                <span className="text-base font-bold text-slate-900 dark:text-slate-100">
                                    {idx + 1}. {m.name}
                                </span>
                                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600 dark:bg-slate-700 dark:text-slate-300">
                                    {m.type === 'industry' ? '行业' : '概念'}
                                </span>
                                {m.phase && (
                                    <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${PHASE_STYLE[m.phase] || 'bg-slate-100 text-slate-600'}`}>
                                        {m.phase}
                                    </span>
                                )}
                                {m.status_vs_yesterday && (
                                    <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${STATUS_STYLE[m.status_vs_yesterday] || 'bg-slate-100 text-slate-600'}`}>
                                        {m.status_vs_yesterday}
                                    </span>
                                )}
                                <span className="ml-auto text-xs text-slate-400">置信度 {m.confidence}</span>
                            </div>
                            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700">
                                <div
                                    className="h-full rounded-full bg-gradient-to-r from-blue-500 to-indigo-500"
                                    style={{ width: `${Math.min(100, Math.max(0, m.confidence))}%` }}
                                />
                            </div>
                            {m.logic && <p className="mt-3 text-sm leading-6 text-slate-700 dark:text-slate-300">{m.logic}</p>}
                            {m.drivers && m.drivers.length > 0 && (
                                <div className="mt-2 flex flex-wrap gap-1.5">
                                    {m.drivers.map((d, i) => (
                                        <span key={i} className="rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-[11px] text-blue-700 dark:border-blue-500/30 dark:bg-blue-500/10 dark:text-blue-300">
                                            {d}
                                        </span>
                                    ))}
                                </div>
                            )}
                            <div className="mt-3 grid gap-3 md:grid-cols-2">
                                {m.representative_boards && m.representative_boards.length > 0 && (
                                    <div>
                                        <p className="text-xs font-medium text-slate-500 dark:text-slate-400">代表板块</p>
                                        <p className="mt-1 text-sm text-slate-700 dark:text-slate-300">
                                            {m.representative_boards
                                                .map((b) => `${b.board}${b.chg_1d != null ? ` ${b.chg_1d > 0 ? '+' : ''}${b.chg_1d}%` : ''}`)
                                                .join('、')}
                                        </p>
                                    </div>
                                )}
                                {m.leading_stocks && m.leading_stocks.length > 0 && (
                                    <div>
                                        <p className="text-xs font-medium text-slate-500 dark:text-slate-400">领涨股</p>
                                        <p className="mt-1 text-sm text-slate-700 dark:text-slate-300">{m.leading_stocks.join('、')}</p>
                                    </div>
                                )}
                                {m.verify_conditions && m.verify_conditions.length > 0 && (
                                    <div className="md:col-span-2">
                                        <p className="text-xs font-medium text-amber-600 dark:text-amber-400">验证条件（可证伪）</p>
                                        <ul className="mt-1 list-inside list-disc space-y-0.5 text-sm text-slate-700 dark:text-slate-300">
                                            {m.verify_conditions.map((v, i) => (
                                                <li key={i}>{v}</li>
                                            ))}
                                        </ul>
                                    </div>
                                )}
                                {m.risks && m.risks.length > 0 && (
                                    <div className="md:col-span-2">
                                        <p className="text-xs font-medium text-red-600 dark:text-red-400">风险</p>
                                        <ul className="mt-1 list-inside list-disc space-y-0.5 text-sm text-slate-700 dark:text-slate-300">
                                            {m.risks.map((r, i) => (
                                                <li key={i}>{r}</li>
                                            ))}
                                        </ul>
                                    </div>
                                )}
                            </div>
                            {m.evidence && (
                                <p className="mt-3 rounded-lg bg-slate-50 px-3 py-2 text-[11px] text-slate-500 dark:bg-slate-900 dark:text-slate-400">
                                    依据：{m.evidence}
                                </p>
                            )}
                        </div>
                    ))}
                    {report?.gated_out && report.gated_out.length > 0 && (
                        <p className="text-xs text-slate-400">置信度不足未选股的主线：{report.gated_out.join('、')}</p>
                    )}
                    {report?.analyst_report && (
                        <details className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
                            <summary className="cursor-pointer text-sm font-medium text-slate-700 dark:text-slate-300">查看主线分析师原文</summary>
                            <div className="mt-3 text-sm leading-6 text-slate-600 dark:text-slate-400">
                                <ReactMarkdown remarkPlugins={[remarkGfm]}>{report.analyst_report}</ReactMarkdown>
                            </div>
                        </details>
                    )}
                    {report?.selector_report && (
                        <details className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-800">
                            <summary className="cursor-pointer text-sm font-medium text-slate-700 dark:text-slate-300">查看选股师原文</summary>
                            <div className="mt-3 text-sm leading-6 text-slate-600 dark:text-slate-400">
                                <ReactMarkdown remarkPlugins={[remarkGfm]}>{report.selector_report}</ReactMarkdown>
                            </div>
                        </details>
                    )}
                </div>
            )}

            {/* Tab2 候选股 */}
            {activeTab === 'candidates' && (
                <div className="rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
                    <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
                        <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">主线候选股</span>
                        <select
                            value={candidateFilter}
                            onChange={(e) => setCandidateFilter(e.target.value)}
                            className="ml-auto rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-600 dark:bg-slate-900"
                        >
                            <option value="all">全部主线</option>
                            {mainlines.map((m) => (
                                <option key={m.name} value={m.name}>
                                    {m.name}
                                </option>
                            ))}
                        </select>
                    </div>
                    {filteredCandidates.length === 0 ? (
                        <p className="px-4 py-8 text-center text-sm text-slate-400">
                            {report?.status === 'completed' ? '暂无候选股（主线置信度不足或候选池数据受限）' : '尚未生成报告'}
                        </p>
                    ) : (
                        <div className="overflow-x-auto">
                            <table className="w-full text-left text-sm">
                                <thead>
                                    <tr className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
                                        <th className="px-4 py-2.5 font-medium">代码</th>
                                        <th className="px-4 py-2.5 font-medium">名称</th>
                                        <th className="px-4 py-2.5 font-medium">主线</th>
                                        <th className="px-4 py-2.5 font-medium">层级</th>
                                        <th className="px-4 py-2.5 font-medium">评分</th>
                                        <th className="px-4 py-2.5 font-medium">理由</th>
                                        <th className="px-4 py-2.5 font-medium">介入建议</th>
                                        <th className="px-4 py-2.5 font-medium">风险</th>
                                        <th className="px-4 py-2.5 font-medium">操作</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {filteredCandidates.map((c) => (
                                        <tr key={`${c.symbol}-${c.mainline}`} className="border-b border-slate-100 last:border-0 dark:border-slate-700/50">
                                            <td className="px-4 py-2.5 font-mono text-xs text-slate-600 dark:text-slate-300">{c.symbol}</td>
                                            <td className="px-4 py-2.5 font-medium text-slate-900 dark:text-slate-100">{c.name}</td>
                                            <td className="px-4 py-2.5 text-xs text-slate-600 dark:text-slate-300">{c.mainline || '-'}</td>
                                            <td className="px-4 py-2.5">
                                                {c.tier ? (
                                                    <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${TIER_STYLE[c.tier] || 'bg-slate-100 text-slate-600'}`}>{c.tier}</span>
                                                ) : (
                                                    '-'
                                                )}
                                            </td>
                                            <td className="px-4 py-2.5 font-semibold text-slate-900 dark:text-slate-100">{c.score ?? '-'}</td>
                                            <td className="max-w-[220px] px-4 py-2.5 text-xs leading-5 text-slate-600 dark:text-slate-300">{(c.reasons || []).join('；')}</td>
                                            <td className="max-w-[180px] px-4 py-2.5 text-xs leading-5 text-amber-700 dark:text-amber-300">{c.entry_hint || '-'}</td>
                                            <td className="max-w-[140px] px-4 py-2.5 text-xs leading-5 text-red-600 dark:text-red-400">{c.risk || '-'}</td>
                                            <td className="px-4 py-2.5">
                                                <div className="flex gap-1.5">
                                                    <button
                                                        onClick={() => void handleAddWatchlist(c)}
                                                        className="rounded-md border border-slate-300 px-2 py-1 text-[11px] text-slate-600 hover:border-blue-400 hover:text-blue-600 dark:border-slate-600 dark:text-slate-300"
                                                    >
                                                        加自选
                                                    </button>
                                                    <button
                                                        onClick={() => void handleAnalyzeCandidate(c)}
                                                        disabled={busyCandidate === candidateKey(c)}
                                                        title={c.id ? '发起个股多智能体深度分析' : '该候选股缺少 id，请重新生成主线报告'}
                                                        className="inline-flex items-center gap-1 rounded-md bg-blue-600 px-2 py-1 text-[11px] text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-60"
                                                    >
                                                        {busyCandidate === candidateKey(c) && <Loader2 className="h-3 w-3 animate-spin" />}
                                                        {busyCandidate === candidateKey(c) ? '发起中…' : '深度分析'}
                                                    </button>
                                                </div>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                    {report?.selector_report && (
                        <details className="border-t border-slate-200 px-4 py-3 dark:border-slate-700">
                            <summary className="cursor-pointer text-xs font-medium text-slate-500 dark:text-slate-400">查看选股师原文</summary>
                            <pre className="mt-2 whitespace-pre-wrap text-xs leading-5 text-slate-600 dark:text-slate-400">{report.selector_report}</pre>
                        </details>
                    )}
                </div>
            )}

            {/* Tab3 板块涨幅榜 */}
            {activeTab === 'boards' && (
                <div className="space-y-4">
                    {boardSpot.boardWarnings.length > 0 && (
                        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-xs text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300">
                            数据提示：{boardSpot.boardWarnings.join('；')}
                        </div>
                    )}
                    <div className="rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
                        <div className="flex items-center gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
                            {(['industry', 'concept'] as const).map((t) => (
                                <button
                                    key={t}
                                    onClick={() => setBoardTab(t)}
                                    className={`rounded-lg px-3 py-1 text-xs font-medium transition ${
                                        boardTab === t ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-700 dark:text-slate-300'
                                    }`}
                                >
                                    {t === 'industry' ? '行业板块' : '概念板块'}
                                </button>
                            ))}
                            <span className="ml-auto text-[11px] text-slate-400">
                                数据源：{boardSpot.sources[boardTab] === 'ths' ? '同花顺' : boardSpot.sources[boardTab] === 'em' ? '东方财富' : boardSpot.sources[boardTab] === 'sina' ? '新浪' : '—'}
                            </span>
                        </div>
                        <div className="overflow-x-auto">
                            <table className="w-full text-left text-sm">
                                <thead>
                                    <tr className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
                                        <th className="px-4 py-2.5 font-medium">#</th>
                                        <th className="px-4 py-2.5 font-medium">板块</th>
                                        <th className="px-4 py-2.5 font-medium">涨跌幅</th>
                                        <th className="px-4 py-2.5 font-medium">领涨股</th>
                                        <th className="px-4 py-2.5 font-medium">涨/跌家数</th>
                                        <th className="px-4 py-2.5 font-medium">净流入(亿)</th>
                                        <th className="px-4 py-2.5 font-medium">换手率</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {(boardTab === 'industry' ? boardSpot.industry : boardSpot.concept).map((b, i) => {
                                        const chg = num(cell(b, 'chg_1d'))
                                        return (
                                            <tr key={i} className="border-b border-slate-100 last:border-0 dark:border-slate-700/50">
                                                <td className="px-4 py-2 text-xs text-slate-400">{i + 1}</td>
                                                <td className="px-4 py-2 font-medium text-slate-900 dark:text-slate-100">{String(cell(b, 'name') ?? '-')}</td>
                                                <td className={`px-4 py-2 font-semibold ${chg !== null && chg >= 0 ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'}`}>
                                                    {fmtPct(chg)}
                                                </td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{String(cell(b, 'leader') ?? '-')}</td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">
                                                    {fmtNum(cell(b, 'up_count'))}/{fmtNum(cell(b, 'down_count'))}
                                                </td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{fmtNum(cell(b, 'net_inflow'))}</td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{fmtNum(cell(b, 'turnover'))}%</td>
                                            </tr>
                                        )
                                    })}
                                    {(boardTab === 'industry' ? boardSpot.industry : boardSpot.concept).length === 0 && (
                                        <tr>
                                            <td colSpan={7} className="px-4 py-8 text-center text-sm text-slate-400">
                                                {boardTab === 'concept'
                                                    ? '概念板块涨幅榜数据源受限（东财接口网络不可用），题材信号见「涨停行业热度」'
                                                    : '该类型板块数据当前不可用（数据源受限，见上方提示）'}
                                            </td>
                                        </tr>
                                    )}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            )}

            {/* Tab4 兑现跟踪 */}
            {activeTab === 't1' && (
                <div className="space-y-4">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                        <p className="text-xs text-slate-500 dark:text-slate-400">
                            主线报告发布后，其代表板块在后续交易日的相对基准（沪深300）表现（数据源：板块历史，需新鲜）
                        </p>
                        <button
                            onClick={() => void handleT1Refresh()}
                            disabled={t1Loading}
                            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:border-blue-400 hover:text-blue-600 disabled:opacity-50 dark:border-slate-600 dark:text-slate-300"
                        >
                            {t1Loading ? '评估中…' : '刷新兑现评估'}
                        </button>
                    </div>

                    {t1Overview && t1Overview.total > 0 && (
                        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                            <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-800">
                                <p className="text-xs text-slate-500 dark:text-slate-400">已评估主线</p>
                                <p className="mt-1 text-xl font-bold text-slate-900 dark:text-slate-100">{t1Overview.total}</p>
                            </div>
                            <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-800">
                                <p className="text-xs text-slate-500 dark:text-slate-400">兑现率（超额&gt;0）</p>
                                <p className="mt-1 text-xl font-bold text-slate-900 dark:text-slate-100">
                                    {t1Overview.hit_rate != null ? `${(t1Overview.hit_rate * 100).toFixed(1)}%` : '-'}
                                </p>
                            </div>
                            <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-800">
                                <p className="text-xs text-slate-500 dark:text-slate-400">平均超额</p>
                                <p className="mt-1 text-xl font-bold text-slate-900 dark:text-slate-100">
                                    {t1Overview.avg_excess_ret != null ? `${(t1Overview.avg_excess_ret * 100).toFixed(2)}%` : '-'}
                                </p>
                            </div>
                            <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-800">
                                <p className="text-xs text-slate-500 dark:text-slate-400">结果分布</p>
                                <p className="mt-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
                                    {Object.entries(t1Overview.by_outcome || {}).map(([k, v]) => `${k} ${v}`).join(' · ') || '-'}
                                </p>
                            </div>
                        </div>
                    )}

                    <div className="rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
                        <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
                            <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">兑现明细</span>
                        </div>
                        {t1Outcomes.length === 0 ? (
                            <p className="px-4 py-8 text-center text-sm text-slate-400">
                                暂无兑现记录——生成主线报告后，点击"刷新兑现评估"（需报告日之后至少一个交易日）
                            </p>
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full text-left text-sm">
                                    <thead>
                                        <tr className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
                                            <th className="px-4 py-2.5 font-medium">报告日</th>
                                            <th className="px-4 py-2.5 font-medium">主线</th>
                                            <th className="px-4 py-2.5 font-medium">代表板块</th>
                                            <th className="px-4 py-2.5 font-medium">板块前瞻</th>
                                            <th className="px-4 py-2.5 font-medium">基准同期</th>
                                            <th className="px-4 py-2.5 font-medium">超额</th>
                                            <th className="px-4 py-2.5 font-medium">结果</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {t1Outcomes.map((o) => {
                                            const ex = o.excess_ret
                                            const outcomeCls =
                                                o.outcome === '兑现'
                                                    ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-300'
                                                    : o.outcome === '证伪'
                                                      ? 'bg-red-100 text-red-700 dark:bg-red-500/10 dark:text-red-300'
                                                      : 'bg-amber-100 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300'
                                            return (
                                                <tr key={`${o.report_id}-${o.mainline}`} className="border-b border-slate-100 last:border-0 dark:border-slate-700/50">
                                                    <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{o.trade_date}</td>
                                                    <td className="px-4 py-2 font-medium text-slate-900 dark:text-slate-100">{o.mainline || '-'}</td>
                                                    <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{o.board || '-'}</td>
                                                    <td className={`px-4 py-2 font-medium ${o.board_fwd_ret != null && o.board_fwd_ret >= 0 ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'}`}>
                                                        {o.board_fwd_ret != null ? `${(o.board_fwd_ret * 100).toFixed(2)}%` : '-'}
                                                    </td>
                                                    <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">
                                                        {o.benchmark_fwd_ret != null ? `${(o.benchmark_fwd_ret * 100).toFixed(2)}%` : '-'}
                                                    </td>
                                                    <td className={`px-4 py-2 text-xs font-medium ${ex != null && ex >= 0 ? 'text-red-600 dark:text-red-400' : 'text-green-600 dark:text-green-400'}`}>
                                                        {ex != null ? `${(ex * 100).toFixed(2)}%` : '-'}
                                                    </td>
                                                    <td className="px-4 py-2">
                                                        <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${outcomeCls}`}>{o.outcome || '未知'}</span>
                                                    </td>
                                                </tr>
                                            )
                                        })}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </div>

                    {/* 规则层回测摘要（CLI 生成，只读） */}
                    {backtest && backtest.found && backtest.comparison && (
                        <details className="rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
                            <summary className="cursor-pointer border-b border-slate-200 px-4 py-3 text-sm font-semibold text-slate-900 dark:border-slate-700 dark:text-slate-100">
                                规则层回测（v1 vs v2，{String(backtest.config?.effective_end || '')} 截止，top {backtest.top_n}）
                            </summary>
                            <div className="overflow-x-auto">
                                <table className="w-full text-left text-sm">
                                    <thead>
                                        <tr className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
                                            <th className="px-4 py-2 font-medium">前瞻窗口</th>
                                            <th className="px-4 py-2 font-medium">v1 Top 超额</th>
                                            <th className="px-4 py-2 font-medium">v1 胜率</th>
                                            <th className="px-4 py-2 font-medium">v2 Top 超额</th>
                                            <th className="px-4 py-2 font-medium">v2 胜率</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {Object.entries(backtest.comparison).map(([h, row]) => (
                                            <tr key={h} className="border-b border-slate-100 last:border-0 dark:border-slate-700/50">
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{h} 日</td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">
                                                    {row.v1?.top?.mean != null ? `${(row.v1.top.mean * 100).toFixed(2)}%` : '-'}
                                                </td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">
                                                    {row.v1?.top?.hit_rate != null ? `${(row.v1.top.hit_rate * 100).toFixed(0)}%` : '-'}
                                                </td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">
                                                    {row.v2?.top?.mean != null ? `${(row.v2.top.mean * 100).toFixed(2)}%` : '-'}
                                                </td>
                                                <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">
                                                    {row.v2?.top?.hit_rate != null ? `${(row.v2.top.hit_rate * 100).toFixed(0)}%` : '-'}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                            <p className="border-t border-slate-200 px-4 py-2 text-[11px] text-slate-400 dark:border-slate-700">
                                回测基于同花顺行业面板真实历史（2020-2024）。结论：规则层边际较薄，真主线依赖 LLM 的持续性与催化判断。重新回测：python -m tradingagents.dataflows.mainline_backtest --compare
                            </p>
                        </details>
                    )}
                </div>
            )}

            {/* Tab5 周期分析 */}
            {activeTab === 'cycle' && <CyclePanel tradeDate={tradeDate} />}
        </div>
    )
}

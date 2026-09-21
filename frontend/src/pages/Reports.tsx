import { FileText, Download, Trash2, Search, ChevronLeft, ChevronRight, Loader2, History, Clock3, Sparkles, Calendar } from 'lucide-react'
import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import TaskProgressBanner from '@/components/TaskProgressBanner'
import PromptTemplateSelector from '@/components/PromptTemplateSelector'
import { api } from '@/services/api'
import type { ModelProfile, Report, ReportDetail, PromptTemplate } from '@/types'
import DecisionCard from '@/components/DecisionCard'
import ConsensusCard from '@/components/ConsensusCard'
import ReportViewer from '@/components/ReportViewer'
import { FreshnessBanner, ReportAgeNote } from '@/components/FreshnessBadge'
import { FRESHNESS_COLUMN_TOOLTIP, REPORT_LIST_TABLE_HEADERS } from '@/components/freshnessListDisplay'
import { FreshnessStatusCell } from '@/components/FreshnessStatusCell'
import RiskRadar from '@/components/RiskRadar'
import KeyMetrics from '@/components/KeyMetrics'
import { useAuthStore } from '@/stores/authStore'
import { advanceProgress, getReportRunProgress } from '@/utils/progressFeedback'

type ProgressState = {
    status: 'idle' | 'loading' | 'success' | 'error'
    progress: number
    detail: string | null
}

const IDLE_PROGRESS: ProgressState = {
    status: 'idle',
    progress: 0,
    detail: null,
}

function toDateTimeLocalValue(date: Date): string {
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function nowDateTimeLocal(): string {
    const d = new Date()
    d.setSeconds(0, 0)
    return toDateTimeLocalValue(d)
}

function daysAgoDateTimeLocal(n: number): string {
    const d = new Date()
    d.setDate(d.getDate() - n)
    d.setHours(0, 0, 0, 0)
    return toDateTimeLocalValue(d)
}

function toApiStartDateTime(localValue: string): string | undefined {
    const raw = localValue.trim()
    if (!raw) return undefined
    const d = new Date(raw)
    if (Number.isNaN(d.getTime())) return undefined
    d.setSeconds(0, 0)
    return d.toISOString()
}

function toApiEndDateTime(localValue: string): string | undefined {
    const raw = localValue.trim()
    if (!raw) return undefined
    const d = new Date(raw)
    if (Number.isNaN(d.getTime())) return undefined
    d.setSeconds(59, 999)
    return d.toISOString()
}

function formatDateTimeLocalLabel(localValue: string): string {
    const d = new Date(localValue)
    if (Number.isNaN(d.getTime())) return localValue
    return d.toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
    })
}

function getStoredDefaultAnalysts(): string[] {
    try {
        const stored = localStorage.getItem('tradingagents-settings')
        if (!stored) return ['market', 'social', 'news', 'fundamentals', 'macro', 'smart_money', 'volume_price']
        const parsed = JSON.parse(stored) as { defaultAnalysts?: string[] }
        if (Array.isArray(parsed.defaultAnalysts) && parsed.defaultAnalysts.length > 0) {
            return parsed.defaultAnalysts
        }
    } catch { /* ignore */ }
    return ['market', 'social', 'news', 'fundamentals', 'macro', 'smart_money', 'volume_price']
}

function reportCanDeepAnalyze(report: Pick<Report, 'status'>): boolean {
    return report.status !== 'pending' && report.status !== 'running'
}

const DEEPSEEK_ALLOWED_MODELS = new Set([
    'deepseek-chat',
    'deepseek-reasoner',
    'deepseek-v4-flash',
    'deepseek-v4-pro',
])

function hostFromUrl(value?: string | null): string {
    if (!value) return ''
    try {
        return new URL(value).hostname.toLowerCase()
    } catch {
        return ''
    }
}

const parseDecision = (decisionText?: string): { action: 'add' | 'reduce' | 'hold'; label: string } => {
    if (!decisionText) return { action: 'hold', label: '观望' }
    const text = decisionText.toUpperCase()
    if (text.includes('BUY') || text.includes('增持') || text.includes('买入')) return { action: 'add', label: '增持' }
    if (text.includes('SELL') || text.includes('减持') || text.includes('卖出')) return { action: 'reduce', label: '减持' }
    return { action: 'hold', label: '持有' }
}

const getDecisionColor = (decision?: string) => {
    const { action } = parseDecision(decision)
    if (action === 'add') return 'text-red-600 dark:text-red-400'
    if (action === 'reduce') return 'text-green-600 dark:text-green-400'
    return 'text-slate-600 dark:text-slate-400'
}

function getQueueHint(report: Pick<Report, 'status' | 'waiting_ahead_count' | 'scheduled_running_count' | 'scheduled_concurrency_limit'>): string | null {
    if (report.status !== 'pending') return null

    const waitingAhead = report.waiting_ahead_count ?? 0
    const runningCount = report.scheduled_running_count
    const limit = report.scheduled_concurrency_limit

    if (runningCount != null && limit != null) {
        return `前方还有 ${waitingAhead} 项等待，当前 ${runningCount}/${limit} 个任务执行中`
    }

    return `前方还有 ${waitingAhead} 项等待`
}

function ActiveReportStatus({ report }: { report: Report }) {
    const progress = getReportRunProgress({
        status: report.status,
        createdAt: report.created_at,
    })
    const isPending = report.status === 'pending'
    const label = isPending ? '排队中' : '分析中'
    const toneCls = isPending
        ? 'text-slate-500 dark:text-slate-300'
        : 'text-blue-600 dark:text-blue-300'
    const barCls = isPending
        ? 'from-slate-400 via-slate-500 to-slate-600'
        : 'from-cyan-500 via-blue-500 to-violet-500'
    const queueHint = getQueueHint(report)

    return (
        <div className="min-w-[148px] space-y-2">
            <div className={`flex items-center gap-1.5 text-xs font-medium ${toneCls}`}>
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                <span>{label}</span>
                <span className="ml-auto tabular-nums">{progress}%</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700">
                <div
                    className={`h-full rounded-full bg-gradient-to-r ${barCls} transition-[width] duration-700 ease-out`}
                    style={{ width: `${progress}%` }}
                />
            </div>
            {queueHint ? (
                <p className="text-[11px] leading-4 text-slate-400">{queueHint}</p>
            ) : null}
        </div>
    )
}

function ActiveDetailStatusCard({ report }: { report: ReportDetail }) {
    const progress = getReportRunProgress({
        status: report.status,
        createdAt: report.created_at,
    })
    const isPending = report.status === 'pending'
    const title = isPending ? '排队处理中...' : '深度分析中...'
    const queueHint = getQueueHint(report)
    const detail = isPending
        ? (queueHint || '任务已进入队列，正在等待分析资源。')
        : '正在汇总各路 Agent 的观点，请稍后。'

    return (
        <div className="card h-full min-h-[320px] p-8">
            <div className="flex h-full flex-col justify-center">
                <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-full bg-blue-50 text-blue-500 dark:bg-blue-500/10 dark:text-blue-300">
                    <Clock3 className="h-7 w-7" />
                </div>
                <div className="mx-auto w-full max-w-[280px] text-center">
                    <h3 className="text-lg font-bold text-slate-900 dark:text-slate-100">{title}</h3>
                    <p className="mt-2 text-sm text-slate-500">{detail}</p>
                    <div className="mt-6 text-left">
                        <div className="mb-2 flex items-center justify-between text-sm">
                            <span className="font-medium text-slate-600 dark:text-slate-300">当前进度</span>
                            <span className="font-semibold tabular-nums text-blue-600 dark:text-blue-300">{progress}%</span>
                        </div>
                        <div className="h-2.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700">
                            <div
                                className="h-full rounded-full bg-gradient-to-r from-cyan-500 via-blue-500 to-violet-500 transition-[width] duration-700 ease-out"
                                style={{ width: `${progress}%` }}
                            />
                        </div>
                    </div>
                    <p className="mt-4 text-xs text-slate-400">
                        页面会自动刷新任务状态，完成后这里会直接切换为分析结果。
                    </p>
                </div>
            </div>
        </div>
    )
}

const renderStatusBadge = (report: Report) => {
    switch (report.status) {
        case 'pending':
            return <ActiveReportStatus report={report} />
        case 'running':
            return <ActiveReportStatus report={report} />
        case 'failed':
            return (
                <div className="group relative flex items-center gap-1.5 text-rose-500" title={report.error?.split('\n')[0]}>
                    <div className="w-1.5 h-1.5 rounded-full bg-rose-500 animate-pulse" />
                    <span className="text-xs font-medium">任务失败</span>
                </div>
            )
        default:
            const { label } = parseDecision(report.decision)
            return (
                <span className={`font-medium ${getDecisionColor(report.decision)}`}>
                    {label}
                </span>
            )
    }
}

function exportReport(report: ReportDetail) {
    const sections = [
        { key: 'market_report', title: '市场分析报告' },
        { key: 'sentiment_report', title: '舆情分析报告' },
        { key: 'news_report', title: '新闻分析报告' },
        { key: 'fundamentals_report', title: '基本面分析报告' },
        { key: 'investment_plan', title: '研究团队决策' },
        { key: 'trader_investment_plan', title: '交易团队计划' },
        { key: 'final_trade_decision', title: '最终交易决策' },
    ]
    const text = sections
        .filter(s => report[s.key as keyof ReportDetail])
        .map(s => `## ${s.title}\n\n${report[s.key as keyof ReportDetail]}`)
        .join('\n\n---\n\n')
    const blob = new Blob([text], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `analysis-${report.symbol}-${report.trade_date}.md`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
}

export default function Reports() {
    const { user } = useAuthStore()
    const [searchParams, setSearchParams] = useSearchParams()
    const setSearchParamsRef = useRef(setSearchParams)
    setSearchParamsRef.current = setSearchParams
    const PAGE_SIZE = 20
    const [searchQuery, setSearchQuery] = useState('')
    const [listSearchDebounced, setListSearchDebounced] = useState('')
    const [filterStartDate, setFilterStartDate] = useState('')
    const [filterEndDate, setFilterEndDate] = useState('')
    const [filterModelProfileId, setFilterModelProfileId] = useState('')
    const [filterFreshnessIssue, setFilterFreshnessIssue] = useState(false)
    const [page, setPage] = useState(0)
    const [reports, setReports] = useState<Report[]>([])
    const [total, setTotal] = useState(0)
    const [selectedReport, setSelectedReport] = useState<ReportDetail | null>(null)
    const [loading, setLoading] = useState(false)
    const [detailLoading, setDetailLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [deleting, setDeleting] = useState<string | null>(null)
    const [symbolHistory, setSymbolHistory] = useState<Report[]>([])
    const [listProgress, setListProgress] = useState<ProgressState>(IDLE_PROGRESS)
    const [detailProgress, setDetailProgress] = useState<ProgressState>(IDLE_PROGRESS)
    const [enhancedExporting, setEnhancedExporting] = useState(false)
    const [selectedIds, setSelectedIds] = useState<string[]>([])
    const [deepSubmitting, setDeepSubmitting] = useState(false)
    const [deepAnalyzingId, setDeepAnalyzingId] = useState<string | null>(null)
    const [promptTemplates, setPromptTemplates] = useState<PromptTemplate[]>([])
    const [promptTemplatesLoading, setPromptTemplatesLoading] = useState(false)
    const [selectedTemplateId, setSelectedTemplateId] = useState('')
    const [modelProfiles, setModelProfiles] = useState<ModelProfile[]>([])
    const [selectedModelProfileId, setSelectedModelProfileId] = useState('')
    const selectAllCheckboxRef = useRef<HTMLInputElement>(null)

    const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
    const eligibleReportsOnPage = useMemo(() => reports.filter(reportCanDeepAnalyze), [reports])
    const activeDeepTemplates = useMemo(
        () => promptTemplates.filter(t => t.scope === 'deep_analysis' && t.is_active),
        [promptTemplates],
    )
    const selectedModelProfile = useMemo(
        () => modelProfiles.find(item => item.id === selectedModelProfileId && item.is_active) || null,
        [modelProfiles, selectedModelProfileId],
    )
    const filterModelProfile = useMemo(
        () => modelProfiles.find(item => item.id === filterModelProfileId) || null,
        [filterModelProfileId, modelProfiles],
    )
    const selectedModelIncompatibleReason = useMemo(() => {
        if (!selectedModelProfile) return null
        const backendUrl = String(selectedModelProfile.backend_url || '').trim()
        if (!backendUrl) {
            return '当前模型配置未设置 Base URL，会沿用系统设置端点，可能导致模型与端点不匹配。请先到模型管理补全 Base URL。'
        }
        const provider = String(selectedModelProfile.llm_provider || '').trim().toLowerCase()
        const host = hostFromUrl(backendUrl)
        if (provider === 'openai' && host === 'api.deepseek.com') {
            const quick = String(selectedModelProfile.quick_think_llm || '').trim()
            const deep = String(selectedModelProfile.deep_think_llm || '').trim()
            const models = [quick, deep].filter(Boolean)
            const bad = models.find(m => !DEEPSEEK_ALLOWED_MODELS.has(m))
            if (bad) {
                return `当前配置包含 ${bad}，与 DeepSeek 端点不兼容。请先到模型管理修正该配置后再发起。`
            }
        }
        return null
    }, [selectedModelProfile])

    useEffect(() => {
        if (listProgress.status !== 'loading') return

        const timer = window.setInterval(() => {
            setListProgress((prev) => prev.status === 'loading'
                ? { ...prev, progress: advanceProgress(prev.progress) }
                : prev)
        }, 180)

        return () => window.clearInterval(timer)
    }, [listProgress.status])

    useEffect(() => {
        if (detailProgress.status !== 'loading') return

        const timer = window.setInterval(() => {
            setDetailProgress((prev) => prev.status === 'loading'
                ? { ...prev, progress: advanceProgress(prev.progress) }
                : prev)
        }, 180)

        return () => window.clearInterval(timer)
    }, [detailProgress.status])

    useEffect(() => {
        const handle = window.setTimeout(() => {
            const next = searchQuery.trim()
            setListSearchDebounced(prev => {
                if (prev !== next) {
                    setPage(0)
                }
                return next
            })
        }, 350)
        return () => window.clearTimeout(handle)
    }, [searchQuery])

    const fetchReports = useCallback(async (targetPage: number, options?: { silent?: boolean }) => {
        const silent = options?.silent === true
        if (!silent) {
            setLoading(true)
            setError(null)
            setListProgress({
                status: 'loading',
                progress: 12,
                detail: `正在加载第 ${targetPage + 1} 页报告列表...`,
            })
        }
        try {
            const response = await api.getReports(
                undefined,
                targetPage * PAGE_SIZE,
                PAGE_SIZE,
                listSearchDebounced || undefined,
                filterStartDate ? toApiStartDateTime(filterStartDate) : undefined,
                filterEndDate ? toApiEndDateTime(filterEndDate) : undefined,
                filterModelProfileId || undefined,
                filterFreshnessIssue || undefined,
            )
            setReports(response.reports)
            setTotal(response.total)
            if (!silent) {
                setListProgress({
                    status: 'success',
                    progress: 100,
                    detail: `已获取 ${response.reports.length} 条报告记录`,
                })
            }
        } catch (err) {
            const message = err instanceof Error ? err.message : '获取报告失败'
            if (!silent) {
                setError(message)
                setListProgress({
                    status: 'error',
                    progress: 100,
                    detail: message,
                })
            }
        } finally {
            if (!silent) {
                setLoading(false)
            }
        }
    }, [filterEndDate, filterFreshnessIssue, filterModelProfileId, filterStartDate, listSearchDebounced])

    useEffect(() => {
        setPage(0)
    }, [filterStartDate, filterEndDate, filterModelProfileId, filterFreshnessIssue])

    useEffect(() => { fetchReports(page) }, [fetchReports, page])

    useEffect(() => {
        setSelectedIds([])
    }, [page])

    useEffect(() => {
        const loadTemplates = async () => {
            setPromptTemplatesLoading(true)
            try {
                const result = await api.listPromptTemplates('deep_analysis')
                const active = result.templates.filter(item => item.scope === 'deep_analysis' && item.is_active)
                setPromptTemplates(result.templates)
                const fallback = result.defaults.manual_deep_analysis || active[0]?.id || ''
                setSelectedTemplateId(current => {
                    if (current && active.some(item => item.id === current)) return current
                    return fallback
                })
            } catch (error) {
                console.error('Failed to load prompt templates:', error)
            } finally {
                setPromptTemplatesLoading(false)
            }
        }
        void loadTemplates()
    }, [])

    useEffect(() => {
        const loadModelProfiles = async () => {
            try {
                const result = await api.listModelProfiles(true)
                const all = result.profiles || []
                const active = all.filter(p => p.is_active)
                setModelProfiles(all)
                const defaultId = active.find(p => p.is_default)?.id || ''
                setSelectedModelProfileId(current => {
                    if (current && active.some(item => item.id === current)) return current
                    const stored = localStorage.getItem('reports-deep-model-profile-id') || ''
                    if (stored && active.some(item => item.id === stored)) return stored
                    return defaultId
                })
            } catch (error) {
                console.error('Failed to load model profiles:', error)
            }
        }
        void loadModelProfiles()
    }, [])

    useEffect(() => {
        if (selectedModelProfileId) {
            localStorage.setItem('reports-deep-model-profile-id', selectedModelProfileId)
        } else {
            localStorage.removeItem('reports-deep-model-profile-id')
        }
    }, [selectedModelProfileId])

    useEffect(() => {
        const eligible = eligibleReportsOnPage
        const selectedEligible = eligible.filter(r => selectedIds.includes(r.id)).length
        const el = selectAllCheckboxRef.current
        if (!el) return
        el.indeterminate = selectedEligible > 0 && selectedEligible < eligible.length
    }, [eligibleReportsOnPage, selectedIds])

    const queueDeepAnalysis = async (items: Report[]): Promise<{
        ok: number
        fail: number
        skippedDup: number
        skippedBusy: number
        errors: string[]
    }> => {
        const eligible = items.filter(reportCanDeepAnalyze)
        const skippedBusy = items.length - eligible.length
        if (selectedModelIncompatibleReason) {
            alert(selectedModelIncompatibleReason)
            return { ok: 0, fail: 0, skippedDup: 0, skippedBusy, errors: [selectedModelIncompatibleReason] }
        }
        if (!eligible.length) {
            return { ok: 0, fail: 0, skippedDup: 0, skippedBusy, errors: [] }
        }
        const seenSyms = new Set<string>()
        const queue: Report[] = []
        for (const r of eligible) {
            const sym = r.symbol.trim().toUpperCase()
            if (seenSyms.has(sym)) continue
            seenSyms.add(sym)
            queue.push(r)
        }
        const skippedDup = eligible.length - queue.length
        let ok = 0
        let fail = 0
        const errors: string[] = []
        const analysts = getStoredDefaultAnalysts()
        for (let i = 0; i < queue.length; i++) {
            const r = queue[i]
            try {
                await api.startAnalysis({
                    symbol: r.symbol,
                    selected_analysts: analysts,
                    prompt_template_id: selectedTemplateId || undefined,
                    model_profile_id: selectedModelProfileId || undefined,
                })
                ok++
            } catch (e) {
                fail++
                errors.push(`${r.symbol}: ${e instanceof Error ? e.message : String(e)}`)
            }
            if (i < queue.length - 1) {
                await new Promise(resolve => window.setTimeout(resolve, 120))
            }
        }
        return { ok, fail, skippedDup, skippedBusy, errors }
    }

    const toggleReportSelected = (e: React.SyntheticEvent, reportId: string) => {
        e.stopPropagation()
        setSelectedIds(prev => (prev.includes(reportId) ? prev.filter(id => id !== reportId) : [...prev, reportId]))
    }

    const toggleSelectAllEligible = (e: React.SyntheticEvent) => {
        e.stopPropagation()
        const eligible = eligibleReportsOnPage
        const allSelected = eligible.length > 0 && eligible.every(r => selectedIds.includes(r.id))
        if (allSelected) {
            const drop = new Set(eligible.map(r => r.id))
            setSelectedIds(prev => prev.filter(id => !drop.has(id)))
        } else {
            setSelectedIds(prev => [...new Set([...prev, ...eligible.map(r => r.id)])])
        }
    }

    const handleDeepAnalyzeOne = async (e: React.MouseEvent, report: Report) => {
        e.stopPropagation()
        if (!reportCanDeepAnalyze(report)) return
        setDeepAnalyzingId(report.id)
        try {
            const r = await queueDeepAnalysis([report])
            if (r.skippedBusy) {
                alert('该报告正在排队或执行中，请稍后再试')
                return
            }
            if (r.fail) {
                alert(r.errors[0] || '提交失败')
                return
            }
            alert(`已为 ${report.symbol} 提交深度分析任务，新报告生成后将出现在列表中（默认按当前 A 股交易日）。`)
            void fetchReports(page, { silent: true })
        } finally {
            setDeepAnalyzingId(null)
        }
    }

    const handleBulkDeepAnalyze = async () => {
        const picked = reports.filter(r => selectedIds.includes(r.id))
        if (!picked.length) return
        const busy = picked.filter(r => !reportCanDeepAnalyze(r))
        const eligibleCount = picked.length - busy.length
        if (!eligibleCount) {
            alert('所选报告均在排队或执行中，请稍后再试')
            return
        }
        const dupSyms = new Set<string>()
        let uniq = 0
        for (const r of picked.filter(reportCanDeepAnalyze)) {
            const s = r.symbol.trim().toUpperCase()
            if (dupSyms.has(s)) continue
            dupSyms.add(s)
            uniq++
        }
        const msg = `确定为 ${eligibleCount} 份所选报告发起深度分析吗？${uniq < eligibleCount ? `其中相同标的将只排队 ${uniq} 次。` : ''}`
        if (!confirm(msg)) return
        setDeepSubmitting(true)
        try {
            const r = await queueDeepAnalysis(picked)
            const parts = [`成功提交 ${r.ok} 个任务`]
            if (r.fail) parts.push(`失败 ${r.fail} 个`)
            if (r.skippedDup) parts.push(`合并重复标的 ${r.skippedDup} 条`)
            if (r.skippedBusy) parts.push(`跳过进行中 ${r.skippedBusy} 条`)
            alert(parts.join('；') + (r.errors.length ? `\n\n${r.errors.slice(0, 5).join('\n')}` : ''))
            setSelectedIds([])
            void fetchReports(page, { silent: true })
        } finally {
            setDeepSubmitting(false)
        }
    }

    const handleDelete = async (e: React.MouseEvent, reportId: string) => {
        e.stopPropagation()
        if (!confirm('确定要删除这份报告吗？')) return
        setDeleting(reportId)
        try {
            await api.deleteReport(reportId)
            setReports(prev => prev.filter(r => r.id !== reportId))
            setSelectedIds(prev => prev.filter(id => id !== reportId))
            setTotal(prev => {
                const newTotal = prev - 1
                // Go to prev page if current page is now empty
                if (reports.length === 1 && page > 0) setPage(p => p - 1)
                return newTotal
            })
        } catch (err) {
            alert(err instanceof Error ? err.message : '删除失败')
        } finally {
            setDeleting(null)
        }
    }

    const loadReportDetail = useCallback(async (
        reportId: string,
        options?: { silent?: boolean; preserveHistory?: boolean },
    ) => {
        const silent = options?.silent === true
        const preserveHistory = options?.preserveHistory === true

        if (!silent) {
            setDetailLoading(true)
            if (!preserveHistory) {
                setSymbolHistory([])
            }
            setDetailProgress({
                status: 'loading',
                progress: 14,
                detail: preserveHistory ? '正在恢复你刚刚打开的报告...' : '正在打开报告详情...',
            })
        }

        try {
            const detail = await api.getReport(reportId)
            setSelectedReport(detail)
            setReports(prev => prev.map(report => report.id === detail.id ? { ...report, ...detail } : report))

            if (!silent || !preserveHistory) {
                const history = await api.getReports(detail.symbol, 0, 20)
                setSymbolHistory(history.reports)
            } else {
                setSymbolHistory(prev => prev.map(report => report.id === detail.id ? { ...report, ...detail } : report))
            }

            if (!silent) {
                setSearchParamsRef.current({ report: reportId })
                setDetailProgress({
                    status: 'success',
                    progress: 100,
                    detail: `${detail.name || detail.symbol} 报告已就绪`,
                })
            }
        } catch (err) {
            const message = err instanceof Error ? err.message : '获取报告详情失败'
            if (!silent) {
                setDetailProgress({
                    status: 'error',
                    progress: 100,
                    detail: message,
                })
                alert(message)
            }
            throw err
        } finally {
            if (!silent) {
                setDetailLoading(false)
            }
        }
    }, [])

    const handleSelectReport = async (report: Pick<Report, 'id' | 'symbol'>) => {
        try {
            await loadReportDetail(report.id)
        } catch {}
    }

    const exportEnhancedHtml = useCallback(async (report: ReportDetail) => {
        setEnhancedExporting(true)
        try {
            const html = await api.exportStockTeamEnhancedReportHtml({
                reportId: report.id,
                market: 'cn',
                period: '6mo',
                include_charts: true,
            })
            const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
            const url = URL.createObjectURL(blob)
            const a = document.createElement('a')
            a.href = url
            a.download = `analysis-${report.symbol}-${report.trade_date}-enhanced.html`
            document.body.appendChild(a)
            a.click()
            document.body.removeChild(a)
            URL.revokeObjectURL(url)
        } catch (err) {
            alert(err instanceof Error ? err.message : '导出增强版HTML失败')
        } finally {
            setEnhancedExporting(false)
        }
    }, [])

    // Only on mount: restore report from URL
    const initialReportId = useRef(searchParams.get('report'))
    useEffect(() => {
        const reportId = initialReportId.current
        if (reportId) {
            void loadReportDetail(reportId, { preserveHistory: true })
        }
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    const hasActiveReport = reports.some(report => report.status === 'pending' || report.status === 'running')

    useEffect(() => {
        if (loading || detailLoading || selectedReport || !hasActiveReport) return

        const timer = window.setInterval(() => {
            void fetchReports(page, { silent: true })
        }, 4000)

        return () => window.clearInterval(timer)
    }, [detailLoading, fetchReports, hasActiveReport, loading, page, selectedReport])

    const selectedReportRef = useRef(selectedReport)
    selectedReportRef.current = selectedReport

    useEffect(() => {
        if (!selectedReport || detailLoading) return
        if (selectedReport.status !== 'pending' && selectedReport.status !== 'running') return

        const timer = window.setInterval(() => {
            const current = selectedReportRef.current
            if (!current || (current.status !== 'pending' && current.status !== 'running')) return
            void loadReportDetail(current.id, { silent: true, preserveHistory: true })
        }, 4000)

        return () => window.clearInterval(timer)
    }, [detailLoading, loadReportDetail, selectedReport?.id, selectedReport?.status])

    // ─── 详情视图 ────────────────────────────────────────────────────────────
    if (detailLoading) {
        return (
            <div className="flex items-center justify-center py-24">
                <Loader2 className="w-8 h-8 animate-spin text-blue-500" />
            </div>
        )
    }

    if (selectedReport) {
        const { action } = parseDecision(selectedReport.decision)
        const selectedReportProgressStatus = selectedReport.status === 'pending' || selectedReport.status === 'running'
            ? 'loading'
            : selectedReport.status === 'failed'
                ? 'error'
                : 'success'
        const selectedReportProgressValue = getReportRunProgress({
            status: selectedReport.status,
            createdAt: selectedReport.created_at,
        })
        const selectedReportProgressDetail = selectedReport.status === 'failed'
            ? (selectedReport.error || '任务执行失败')
            : selectedReport.status === 'completed'
                ? `${selectedReport.name || selectedReport.symbol} 报告已完成`
                : selectedReport.status === 'pending'
                    ? (getQueueHint(selectedReport) || '任务排队中 · 进度会自动刷新')
                    : '多智能体正在协同分析 · 进度会自动刷新'

        return (
            <div className="space-y-6">
                {(selectedReport.status === 'pending' || selectedReport.status === 'running') && (
                    <TaskProgressBanner
                        status={selectedReportProgressStatus}
                        progress={selectedReportProgressValue}
                        label={selectedReport.status === 'pending' ? '报告任务排队中...' : '报告生成中...'}
                        detail={selectedReportProgressDetail}
                    />
                )}
                {/* 返回按钮 + 标题 */}
                <div className="flex items-center gap-4">

                    <button
                        onClick={() => {
                            setSelectedReport(null)
                            setSearchParams({})
                        }}
                        className="btn-secondary flex items-center gap-2"
                    >
                        <ChevronLeft className="w-4 h-4" />
                        返回列表
                    </button>
                    <h1 className="text-xl font-bold text-slate-900 dark:text-slate-100">
                        {selectedReport.name || selectedReport.symbol} 分析报告
                        {selectedReport.name && selectedReport.name !== selectedReport.symbol && (
                            <span className="ml-2 text-base font-normal text-slate-400">{selectedReport.symbol}</span>
                        )}
                    </h1>
                    <button
                        onClick={() => exportReport(selectedReport)}
                        className="btn-secondary ml-auto flex items-center gap-1.5 px-3 py-1.5 text-sm"
                    >
                        <Download className="w-4 h-4" />
                        导出 Markdown
                    </button>
                    <button
                        onClick={() => exportEnhancedHtml(selectedReport)}
                        disabled={enhancedExporting}
                        className="btn-primary flex items-center gap-1.5 px-3 py-1.5 text-sm"
                    >
                        {enhancedExporting ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                        导出增强HTML
                    </button>
                </div>

                {/* 元信息 */}
                <div className="flex flex-wrap items-center gap-4 text-sm text-slate-500">
                    <span>分析日期：{selectedReport.trade_date}</span>
                    <span>生成时间：{selectedReport.created_at ? new Date(selectedReport.created_at).toLocaleString('zh-CN') : '-'}</span>
                    <span>
                        模型：
                        {selectedReport.model_profile_name || selectedReport.llm_provider || selectedReport.quick_think_llm || selectedReport.deep_think_llm
                            ? `${selectedReport.model_profile_name || selectedReport.llm_provider || '-'} / ${selectedReport.quick_think_llm || '-'}${selectedReport.deep_think_llm ? ` / ${selectedReport.deep_think_llm}` : ''}`
                            : '-'}
                    </span>
                    {selectedReport.model_profile_id && <span>配置ID：{selectedReport.model_profile_id}</span>}
                </div>

                {/* 历史决策时间线 */}
                {symbolHistory.length > 1 && (
                    <div className="card">
                        <div className="flex items-center gap-2 mb-3">
                            <History className="w-4 h-4 text-slate-400" />
                            <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-300">{selectedReport.name || selectedReport.symbol} 历史决策</h3>
                        </div>
                        <div className="flex items-center gap-2 overflow-x-auto pb-1">
                            {symbolHistory.slice().reverse().map(r => {
                                const { action: a } = parseDecision(r.decision)
                                const color = a === 'add' ? 'bg-red-500' : a === 'reduce' ? 'bg-green-500' : 'bg-slate-400'
                                const isCurrent = r.id === selectedReport.id
                                return (
                                    <button
                                        key={r.id}
                                        onClick={() => !isCurrent && handleSelectReport(r)}
                                        className={`flex flex-col items-center gap-1 shrink-0 px-2 py-1.5 rounded-lg transition-colors ${isCurrent ? 'bg-blue-50 dark:bg-blue-500/10' : 'hover:bg-slate-50 dark:hover:bg-slate-800/50'}`}
                                    >
                                        <div className={`w-3 h-3 rounded-full ${color}`} />
                                        <span className="text-xs text-slate-500 dark:text-slate-400 whitespace-nowrap">{r.trade_date}</span>
                                        {r.confidence != null && <span className="text-xs text-slate-400">{r.confidence}%</span>}
                                    </button>
                                )
                            })}
                        </div>
                    </div>
                )}

                <FreshnessBanner summary={selectedReport.freshness_summary} />
                <ReportAgeNote note={selectedReport.freshness_summary?.report_age_note} />

                {/* 主体：概要卡片 + 报告全文 */}
                <div className="grid grid-cols-1 xl:grid-cols-3 gap-6 items-start">
                    {selectedReport.status === 'completed' ? (
                        <DecisionCard
                            symbol={selectedReport.symbol}
                            name={selectedReport.name}
                            decision={action}
                            direction={selectedReport.direction}
                            confidence={selectedReport.confidence ?? undefined}
                            targetPrice={selectedReport.target_price ?? undefined}
                            stopLoss={selectedReport.stop_loss_price ?? undefined}
                            reasoning={selectedReport.final_trade_decision?.slice(0, 300) ?? undefined}
                        />
                    ) : selectedReport.status === 'failed' ? (
                        <div className="card h-full flex flex-col items-center justify-center p-8 text-center min-h-[320px]">
                            <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-rose-50 text-rose-500 dark:bg-rose-500/10 dark:text-rose-300">
                                <Trash2 className="h-6 w-6" />
                            </div>
                            <h3 className="text-lg font-bold text-slate-900 dark:text-slate-100">分析失败</h3>
                            <p className="mt-2 max-w-[240px] text-sm text-slate-500">
                                {selectedReport.error?.slice(0, 80) || '未知错误'}
                            </p>
                        </div>
                    ) : (
                        <ActiveDetailStatusCard report={selectedReport} />
                    )}
                    <RiskRadar items={selectedReport.risk_items ?? undefined} />
                    <KeyMetrics items={selectedReport.key_metrics ?? undefined} />
                </div>

                <ConsensusCard summary={selectedReport.result_data?.consensus_summary} />

                <div className="card">
                    <ReportViewer reportData={selectedReport} />
                </div>
            </div>
        )
    }

    // ─── 列表视图 ────────────────────────────────────────────────────────────
    return (
        <div className="space-y-6">
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">历史报告</h1>
                    <p className="text-slate-500 dark:text-slate-400 mt-1">
                        {user?.email ? `${user.email} 的私有分析记录 · 共 ${total} 份` : `共 ${total} 份分析报告`}
                    </p>
                </div>
            </div>

            {/* 搜索与筛选 */}
            <div className="card">
                <div className="flex flex-col gap-4">
                    <div className="flex flex-col gap-3 lg:flex-row lg:flex-wrap lg:items-end">
                        <div className="relative max-w-md flex-1 min-w-[220px]">
                            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                            <input
                                type="text"
                                value={searchQuery}
                                onChange={e => setSearchQuery(e.target.value)}
                                placeholder="搜索股票代码或中文名称..."
                                className="input w-full pl-10"
                            />
                        </div>
                        <div className="flex flex-wrap items-end gap-2">
                            <div>
                                <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">生成时间起</label>
                                <input
                                    type="datetime-local"
                                    value={filterStartDate}
                                    max={filterEndDate || nowDateTimeLocal()}
                                    onChange={e => setFilterStartDate(e.target.value)}
                                    className="input h-9 text-sm min-w-[190px]"
                                />
                            </div>
                            <div>
                                <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">生成时间止</label>
                                <input
                                    type="datetime-local"
                                    value={filterEndDate}
                                    min={filterStartDate || undefined}
                                    max={nowDateTimeLocal()}
                                    onChange={e => setFilterEndDate(e.target.value)}
                                    className="input h-9 text-sm min-w-[190px]"
                                />
                            </div>
                            <div>
                                <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">分析模型</label>
                                <select
                                    value={filterModelProfileId}
                                    onChange={e => setFilterModelProfileId(e.target.value)}
                                    className="input h-9 min-w-[180px] text-sm"
                                >
                                    <option value="">全部模型</option>
                                    {modelProfiles.map(profile => (
                                        <option key={profile.id} value={profile.id}>
                                            {profile.name}
                                            {!profile.is_active ? '（已停用）' : profile.is_default ? '（默认）' : ''}
                                            {profile.deep_think_llm ? ` · ${profile.deep_think_llm}` : ''}
                                        </option>
                                    ))}
                                </select>
                            </div>
                            <label className="inline-flex h-9 items-center gap-2 rounded-lg border border-slate-300 px-2.5 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300">
                                <input
                                    type="checkbox"
                                    checked={filterFreshnessIssue}
                                    onChange={e => setFilterFreshnessIssue(e.target.checked)}
                                    className="rounded border-slate-300 dark:border-slate-600"
                                />
                                仅数据异常/过时
                            </label>
                            <button
                                type="button"
                                onClick={() => {
                                    setFilterStartDate(daysAgoDateTimeLocal(30))
                                    setFilterEndDate(nowDateTimeLocal())
                                }}
                                className="inline-flex h-9 items-center gap-1 rounded-lg border border-slate-300 px-2.5 text-xs text-slate-600 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                            >
                                <Calendar className="h-3.5 w-3.5" />
                                最近 30 天
                            </button>
                            {(filterStartDate || filterEndDate || filterModelProfileId || filterFreshnessIssue || listSearchDebounced) && (
                                <button
                                    type="button"
                                    onClick={() => {
                                        setSearchQuery('')
                                        setListSearchDebounced('')
                                        setFilterStartDate('')
                                        setFilterEndDate('')
                                        setFilterModelProfileId('')
                                        setFilterFreshnessIssue(false)
                                        setPage(0)
                                    }}
                                    className="inline-flex h-9 items-center rounded-lg border border-slate-300 px-2.5 text-xs text-slate-600 hover:bg-slate-50 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                                >
                                    清除筛选
                                </button>
                            )}
                        </div>
                    </div>
                    {(filterStartDate || filterEndDate || filterModelProfileId) && (
                        <p className="text-xs text-slate-500 dark:text-slate-400">
                            {filterStartDate || filterEndDate ? (
                                <>
                                    按报告<strong className="font-medium text-slate-600 dark:text-slate-300">生成时间</strong>筛选（精确到分钟）
                                    {filterStartDate && filterEndDate
                                        ? `：${formatDateTimeLocalLabel(filterStartDate)} 至 ${formatDateTimeLocalLabel(filterEndDate)}`
                                        : filterStartDate
                                            ? `：不早于 ${formatDateTimeLocalLabel(filterStartDate)}`
                                            : `：不晚于 ${formatDateTimeLocalLabel(filterEndDate)}`}
                                </>
                            ) : null}
                            {filterModelProfileId && (
                                <>
                                    {filterStartDate || filterEndDate ? ' · ' : ''}
                                    模型：
                                    <strong className="font-medium text-slate-600 dark:text-slate-300">
                                        {filterModelProfile?.name || filterModelProfileId}
                                    </strong>
                                    （含同 LLM 名但未绑定配置的历史报告）
                                </>
                            )}
                        </p>
                    )}
                    <PromptTemplateSelector
                        value={selectedTemplateId}
                        templates={activeDeepTemplates}
                        loading={promptTemplatesLoading}
                        onChange={setSelectedTemplateId}
                    />
                    <div className="max-w-sm">
                        <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">深度分析模型</label>
                        <select
                            value={selectedModelProfileId}
                            onChange={e => setSelectedModelProfileId(e.target.value)}
                            className="input h-9 w-full text-sm"
                        >
                            <option value="">默认模型（系统设置）</option>
                            {modelProfiles.filter(p => p.is_active).map(profile => (
                                <option key={profile.id} value={profile.id}>
                                    {profile.name}
                                </option>
                            ))}
                        </select>
                        {selectedModelIncompatibleReason && (
                            <p className="mt-1 text-xs text-rose-600">{selectedModelIncompatibleReason}</p>
                        )}
                    </div>
                </div>
            </div>

            {/* 加载中 */}
            {loading && (
                <div className="card py-12">
                    <div className="flex flex-col items-center gap-4">
                        <Loader2 className="w-8 h-8 text-blue-500 animate-spin" />
                        <p className="text-slate-500">加载报告中...</p>
                    </div>
                </div>
            )}

            {/* 错误 */}
            {error && !loading && (
                <div className="card py-12 text-center">
                    <p className="text-red-500 mb-4">{error}</p>
                    <button
                        onClick={() => fetchReports(page)}
                        className="btn-primary"
                    >
                        重试
                    </button>
                </div>
            )}

            {/* 报告表格 */}
            {!loading && !error && (
                <div className="card overflow-hidden">
                    {selectedIds.length > 0 && (
                        <div className="flex flex-wrap items-center gap-3 px-4 py-3 border-b border-slate-200 dark:border-slate-700 bg-blue-50/60 dark:bg-blue-950/25">
                            <span className="text-sm text-slate-700 dark:text-slate-300">
                                已选 <strong className="tabular-nums">{selectedIds.length}</strong> 项
                            </span>
                            <button
                                type="button"
                                disabled={deepSubmitting}
                                onClick={() => void handleBulkDeepAnalyze()}
                                className="btn-primary inline-flex items-center gap-1.5 px-3 py-1.5 text-sm"
                            >
                                {deepSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                                批量深度分析
                            </button>
                            <button
                                type="button"
                                disabled={deepSubmitting}
                                onClick={() => setSelectedIds([])}
                                className="text-sm text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200 disabled:opacity-50"
                            >
                                清除选择
                            </button>
                        </div>
                    )}
                    <div className="overflow-x-auto">
                        <table className="w-full">
                            <thead>
                                <tr className="border-b border-slate-200 dark:border-slate-700">
                                    <th className="w-11 py-3 pl-4 pr-2 text-left">
                                        <input
                                            ref={selectAllCheckboxRef}
                                            type="checkbox"
                                            disabled={eligibleReportsOnPage.length === 0}
                                            checked={
                                                eligibleReportsOnPage.length > 0
                                                && eligibleReportsOnPage.every(r => selectedIds.includes(r.id))
                                            }
                                            onChange={toggleSelectAllEligible}
                                            title="全选当前页（不含排队中）"
                                            className="rounded border-slate-300 dark:border-slate-600"
                                        />
                                    </th>
                                    {REPORT_LIST_TABLE_HEADERS.map(h => (
                                        <th
                                            key={h}
                                            title={h === '数据状态' ? FRESHNESS_COLUMN_TOOLTIP : undefined}
                                            className={`py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400 ${h === '操作' ? 'text-right' : 'text-left'}`}
                                        >
                                            {h}
                                        </th>
                                    ))}
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                                {reports.map((report) => {
                                    const canAnalyze = reportCanDeepAnalyze(report)
                                    return (
                                        <tr
                                            key={report.id}
                                            className="transition-colors cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/50"
                                            onClick={() => handleSelectReport(report)}
                                        >
                                            <td className="py-3 pl-4 pr-2 align-middle" onClick={e => e.stopPropagation()}>
                                                <input
                                                    type="checkbox"
                                                    disabled={!canAnalyze}
                                                    checked={selectedIds.includes(report.id)}
                                                    onChange={e => toggleReportSelected(e, report.id)}
                                                    title={canAnalyze ? '选择以批量操作' : '排队或执行中不可选'}
                                                    className="rounded border-slate-300 dark:border-slate-600 disabled:opacity-40"
                                                />
                                            </td>
                                            <td className="py-3 px-4">
                                                <div className="flex items-center gap-3">
                                                    <div className="w-8 h-8 rounded-lg bg-blue-100 dark:bg-blue-500/10 flex items-center justify-center">
                                                        <FileText className="w-4 h-4 text-blue-600 dark:text-blue-400" />
                                                    </div>
                                                    <div>
                                                        <p className="font-medium text-slate-900 dark:text-slate-100">{report.name || report.symbol}</p>
                                                        {report.name && report.name !== report.symbol && (
                                                            <p className="text-xs text-slate-400 dark:text-slate-500">{report.symbol}</p>
                                                        )}
                                                    </div>
                                                </div>
                                            </td>
                                            <td className="py-3 px-4 text-slate-600 dark:text-slate-400">{report.trade_date}</td>
                                            <td
                                                className="py-3 px-4 text-xs text-slate-600 dark:text-slate-400"
                                                title={report.deep_think_llm || report.quick_think_llm || '--'}
                                            >
                                                <span className="font-medium text-slate-700 dark:text-slate-200">
                                                    {report.deep_think_llm || report.quick_think_llm || '--'}
                                                </span>
                                            </td>
                                            <td className="py-3 px-4">
                                                <FreshnessStatusCell status={report.freshness_status} />
                                            </td>
                                            <td className="py-3 px-4">
                                                {renderStatusBadge(report)}
                                            </td>
                                            <td className="py-3 px-4">
                                                {report.confidence != null ? (
                                                    <div className="flex items-center gap-2">
                                                        <div className="w-16 h-1.5 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden">
                                                            <div
                                                                className="h-full bg-blue-500 rounded-full"
                                                                style={{ width: `${report.confidence}%` }}
                                                            />
                                                        </div>
                                                        <span className="text-sm text-slate-600 dark:text-slate-400">{report.confidence}%</span>
                                                    </div>
                                                ) : (
                                                    <span className="text-slate-400">-</span>
                                                )}
                                            </td>
                                            <td className="py-3 px-4 text-sm text-slate-600 dark:text-slate-400">
                                                {report.target_price != null ? `¥${report.target_price}` : '-'} / {report.stop_loss_price != null ? `¥${report.stop_loss_price}` : '-'}
                                            </td>
                                            <td className="py-3 px-4 text-sm text-slate-500 dark:text-slate-400">
                                                {report.created_at ? new Date(report.created_at).toLocaleString('zh-CN') : '-'}
                                            </td>
                                            <td className="py-3 px-4">
                                                <div className="flex items-center justify-end gap-2">
                                                    <button
                                                        type="button"
                                                        className={`p-2 transition-colors disabled:opacity-40 ${canAnalyze ? 'text-slate-400 hover:text-violet-600 dark:hover:text-violet-400' : 'text-slate-300 cursor-not-allowed'}`}
                                                        onClick={e => void handleDeepAnalyzeOne(e, report)}
                                                        disabled={!canAnalyze || deepAnalyzingId === report.id || deepSubmitting}
                                                        title={canAnalyze ? '再次发起深度分析（当前交易日）' : '排队或执行中不可用'}
                                                    >
                                                        {deepAnalyzingId === report.id
                                                            ? <Loader2 className="w-4 h-4 animate-spin" />
                                                            : <Sparkles className="w-4 h-4" />}
                                                    </button>
                                                    <button
                                                        className="p-2 text-slate-400 hover:text-blue-600 dark:hover:text-blue-400 transition-colors"
                                                        onClick={e => { e.stopPropagation(); handleSelectReport(report) }}
                                                        title="查看详情"
                                                    >
                                                        <FileText className="w-4 h-4" />
                                                    </button>
                                                    <button
                                                        className="p-2 text-slate-400 hover:text-red-600 dark:hover:text-red-400 transition-colors disabled:opacity-50"
                                                        onClick={e => handleDelete(e, report.id)}
                                                        disabled={deleting === report.id}
                                                        title="删除"
                                                    >
                                                        {deleting === report.id
                                                            ? <Loader2 className="w-4 h-4 animate-spin" />
                                                            : <Trash2 className="w-4 h-4" />
                                                        }
                                                    </button>
                                                </div>
                                            </td>
                                        </tr>
                                    )
                                })}
                            </tbody>
                        </table>
                    </div>

                    {reports.length === 0 && (
                        <div className="text-center py-12">
                            <FileText className="w-12 h-12 text-slate-300 dark:text-slate-600 mx-auto mb-4" />
                            <p className="text-slate-500 dark:text-slate-400">
                                {listSearchDebounced ? '没有匹配的报告' : '暂无报告'}
                            </p>
                            <p className="text-sm text-slate-400 dark:text-slate-500 mt-1">
                                在分析页面生成新的报告
                            </p>
                        </div>
                    )}

                    {/* Pagination */}
                    {totalPages > 1 && (
                        <div className="flex items-center justify-between px-4 py-3 border-t border-slate-200 dark:border-slate-700">
                            <span className="text-sm text-slate-500 dark:text-slate-400">
                                第 {page + 1} / {totalPages} 页，共 {total} 条
                            </span>
                            <div className="flex items-center gap-2">
                                <button
                                    onClick={() => setPage(p => p - 1)}
                                    disabled={page === 0}
                                    className="p-1.5 rounded-lg text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                                >
                                    <ChevronLeft className="w-4 h-4" />
                                </button>
                                <button
                                    onClick={() => setPage(p => p + 1)}
                                    disabled={page >= totalPages - 1}
                                    className="p-1.5 rounded-lg text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                                >
                                    <ChevronRight className="w-4 h-4" />
                                </button>
                            </div>
                        </div>
                    )}
                </div>
            )}
        </div>
    )
}

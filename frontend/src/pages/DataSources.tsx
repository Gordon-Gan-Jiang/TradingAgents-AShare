import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
    AlertTriangle,
    ChevronDown,
    Database,
    Layers,
    Loader2,
    Play,
    RefreshCw,
    Search,
    TrendingUp,
} from 'lucide-react'

import { api } from '@/services/api'
import type {
    DataSourceMatrixResponse,
    DataSourceProbeHistoryResponse,
    DataSourceProbeResult,
    DataSourceProbeStatusResponse,
} from '@/types'
import {
    buildCapabilityRows,
    buildSourceTimelines,
    buildTrendBars,
    EMPTY_PLACEHOLDER,
    FILTER_ALL,
    filterProbeResultRows,
    formatLatency,
    formatProbeTimestamp,
    formatRowCount,
    formatTradingDay,
    isApproxRowUnit,
    isNonTradingDay,
    listProbeFieldValues,
    probeErrorSummary,
    probeErrorText,
    probeStatusMeta,
    PROBE_SUMMARY_CARDS,
    PROBE_TIER_LEGEND,
    PROBE_TREND_SEGMENTS,
    resolveProbeCounts,
    sortProbeResultRows,
    summariseBySource,
    trendSegmentHeights,
    triggerLabel,
    type ProbeStatusMeta,
} from '@/pages/dataSourcesDisplay'

/** 历史区间可选值。 */
const HISTORY_DAY_OPTIONS = [7, 30, 90]

/**
 * 后台探测的轮询参数。
 * 一轮探测是 20+ 次真实网络请求（后端刻意串行，避免刺激免费源限流），
 * 十几秒到几十秒都正常，因此 POST 超时后会转后台跑，页面轮询 /status 直到 running 变为 false。
 */
const PROBE_POLL_INTERVAL_MS = 3000
const PROBE_POLL_MAX_ATTEMPTS = 40

function delay(ms: number): Promise<void> {
    return new Promise(resolve => {
        window.setTimeout(resolve, ms)
    })
}

function StatusBadge({ meta }: { meta: ProbeStatusMeta }) {
    return (
        <span
            className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full border px-2 py-0.5 text-xs font-medium ${meta.badgeClass}`}
        >
            <span className={`h-1.5 w-1.5 rounded-full ${meta.dotClass}`} />
            {meta.label}
        </span>
    )
}

function ErrorBanner({ message }: { message: string }) {
    return (
        <div className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-600 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{message}</span>
        </div>
    )
}

function LoadingRow({ label }: { label: string }) {
    return (
        <div className="flex items-center gap-2 px-4 py-6 text-sm text-slate-500 dark:text-slate-400">
            <Loader2 className="h-4 w-4 animate-spin" />
            {label}
        </div>
    )
}

export default function DataSources() {
    const [status, setStatus] = useState<DataSourceProbeStatusResponse | null>(null)
    const [statusLoading, setStatusLoading] = useState(true)
    const [statusError, setStatusError] = useState<string | null>(null)

    const [matrix, setMatrix] = useState<DataSourceMatrixResponse | null>(null)
    const [matrixLoading, setMatrixLoading] = useState(true)
    const [matrixError, setMatrixError] = useState<string | null>(null)
    const [showMatrix, setShowMatrix] = useState(true)

    const [history, setHistory] = useState<DataSourceProbeHistoryResponse | null>(null)
    const [historyLoading, setHistoryLoading] = useState(true)
    const [historyError, setHistoryError] = useState<string | null>(null)
    const [historyDays, setHistoryDays] = useState(30)

    const [probing, setProbing] = useState(false)
    const [probeError, setProbeError] = useState<string | null>(null)

    const [query, setQuery] = useState('')
    const [sourceFilter, setSourceFilter] = useState(FILTER_ALL)
    const [categoryFilter, setCategoryFilter] = useState(FILTER_ALL)

    // 轮询令牌：新一轮轮询会顶掉上一轮，组件卸载后也会作废（避免卸载后 setState）
    const pollTokenRef = useRef(0)

    useEffect(() => {
        return () => {
            pollTokenRef.current += 1
        }
    }, [])

    /** 轮询到 running 为 false 为止；返回 false 表示达到次数上限仍在跑。 */
    const pollUntilProbeDone = useCallback(async (): Promise<boolean> => {
        const token = pollTokenRef.current + 1
        pollTokenRef.current = token
        for (let attempt = 0; attempt < PROBE_POLL_MAX_ATTEMPTS; attempt += 1) {
            await delay(PROBE_POLL_INTERVAL_MS)
            if (pollTokenRef.current !== token) return true
            const next = await api.getDataSourceStatus()
            if (pollTokenRef.current !== token) return true
            setStatus(next)
            if (!next.running) return true
        }
        return false
    }, [])

    const loadStatus = useCallback(async () => {
        setStatusLoading(true)
        setStatusError(null)
        try {
            const payload = await api.getDataSourceStatus()
            setStatus(payload)
            setStatusLoading(false)
            // 可能是定时任务正在跑（或上一轮手动探测还在后台），继续轮询
            if (payload.running) await pollUntilProbeDone()
        } catch (e) {
            setStatusError(e instanceof Error ? e.message : '加载探测状态失败')
        } finally {
            setStatusLoading(false)
        }
    }, [pollUntilProbeDone])

    const loadMatrix = useCallback(async () => {
        setMatrixLoading(true)
        setMatrixError(null)
        try {
            setMatrix(await api.getDataSourceMatrix())
        } catch (e) {
            setMatrixError(e instanceof Error ? e.message : '加载能力矩阵失败')
        } finally {
            setMatrixLoading(false)
        }
    }, [])

    const loadHistory = useCallback(async (days: number) => {
        setHistoryLoading(true)
        setHistoryError(null)
        try {
            setHistory(await api.getDataSourceHistory({ days }))
        } catch (e) {
            setHistoryError(e instanceof Error ? e.message : '加载探测历史失败')
        } finally {
            setHistoryLoading(false)
        }
    }, [])

    useEffect(() => {
        void loadStatus()
    }, [loadStatus])

    useEffect(() => {
        void loadMatrix()
    }, [loadMatrix])

    useEffect(() => {
        void loadHistory(historyDays)
    }, [loadHistory, historyDays])

    const refreshAll = useCallback(() => {
        void loadStatus()
        void loadMatrix()
        void loadHistory(historyDays)
    }, [historyDays, loadHistory, loadMatrix, loadStatus])

    const runProbe = useCallback(async () => {
        setProbing(true)
        setProbeError(null)
        try {
            // POST 返回与 status 相同的形状，可以直接替换，无需再取一次
            const payload = await api.runDataSourceProbe()
            setStatus(payload)
            if (payload.running) {
                // 后端最多同步等 20s，超时就转后台：这里继续轮询直到这一轮跑完
                const finished = await pollUntilProbeDone()
                if (!finished) setProbeError('探测仍在后台运行，请稍后点「刷新」查看最新结果。')
            }
            void loadHistory(historyDays)
        } catch (e) {
            setProbeError(e instanceof Error ? `触发探测失败：${e.message}` : '触发探测失败')
            // 409（已有一轮在跑）等情况下，把当前状态取回来
            void loadStatus()
        } finally {
            setProbing(false)
        }
    }, [historyDays, loadHistory, loadStatus, pollUntilProbeDone])

    const latestRun = status?.latest_run ?? null
    const sortedRows = useMemo<DataSourceProbeResult[]>(
        () => sortProbeResultRows(status?.results ?? []),
        [status],
    )
    const sourceOptions = useMemo(() => listProbeFieldValues(sortedRows, 'source'), [sortedRows])
    const categoryOptions = useMemo(() => listProbeFieldValues(sortedRows, 'category'), [sortedRows])
    const visibleRows = useMemo(
        () => filterProbeResultRows(sortedRows, { query, source: sourceFilter, category: categoryFilter }),
        [sortedRows, query, sourceFilter, categoryFilter],
    )
    const counts = useMemo(
        () => resolveProbeCounts(latestRun, status?.results),
        [latestRun, status],
    )
    const sourceSummaries = useMemo(() => summariseBySource(sortedRows), [sortedRows])
    const capabilityRows = useMemo(
        () => buildCapabilityRows(matrix?.items, sortedRows),
        [matrix, sortedRows],
    )
    const trendBars = useMemo(() => buildTrendBars(status?.runs), [status])
    const timelines = useMemo(() => buildSourceTimelines(history?.points), [history])

    const hasResults = sortedRows.length > 0
    const hasFilters = Boolean(query) || sourceFilter !== FILTER_ALL || categoryFilter !== FILTER_ALL
    const nonTradingDay = isNonTradingDay(latestRun?.trading_day)
    // 自己点的探测，或后台（定时任务/上一轮）正在跑，都视为「进行中」
    const probeRunning = probing || status?.running === true

    return (
        <div className="space-y-4">
            {/* 标题 + 操作 */}
            <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">数据源探测报告</h1>
                    <p className="mt-1 max-w-3xl text-sm text-slate-500 dark:text-slate-400">
                        每个数据源都会用<b>真实最小请求</b>探测一次今天到底通不通，并记录返回的库版本指纹。
                        历史上出过「依赖库升级后域名/接口变了，没人发现」的事故，所以这里看的是实际结果，
                        而不是配置里写了什么。
                    </p>
                </div>
                <div className="flex items-center gap-2">
                    <button
                        type="button"
                        onClick={refreshAll}
                        disabled={statusLoading}
                        className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 disabled:opacity-60 dark:border-slate-700 dark:hover:bg-slate-800"
                    >
                        <RefreshCw className={`h-4 w-4 ${statusLoading ? 'animate-spin' : ''}`} />
                        刷新
                    </button>
                    <button
                        type="button"
                        onClick={runProbe}
                        disabled={probeRunning}
                        className="inline-flex items-center gap-1 rounded-lg bg-violet-600 px-3 py-1.5 text-sm text-white hover:bg-violet-700 disabled:opacity-60"
                    >
                        {probeRunning ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                        {probeRunning ? '探测中…' : '立即探测'}
                    </button>
                </div>
            </div>

            {status && (
                <p className="text-xs text-slate-500 dark:text-slate-400">
                    {status.enabled === false
                        ? '定时探测已关闭，只能手动触发。'
                        : status.scheduled_at
                          ? `定时探测：每个交易日 ${status.scheduled_at} 自动跑一轮。`
                          : '每个交易日开盘前会自动跑一轮。'}
                </p>
            )}

            {probeRunning && (
                <div className="rounded-lg border border-violet-200 bg-violet-50 px-3 py-2 text-sm text-violet-700 dark:border-violet-500/30 dark:bg-violet-500/10 dark:text-violet-300">
                    {probing
                        ? '正在逐个发起真实最小请求（后端串行探测，避免触发免费源限流），通常需要数十秒；跑完会自动刷新。'
                        : '后台有一轮探测正在运行（可能是定时任务），结果会自动刷新。'}
                </div>
            )}

            {probeError && <ErrorBanner message={probeError} />}
            {statusError && <ErrorBanner message={statusError} />}

            {/* 摘要 */}
            {statusLoading ? (
                <div className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
                    <LoadingRow label="正在加载探测状态…" />
                </div>
            ) : !latestRun && !hasResults ? (
                <div className="rounded-xl border border-dashed border-slate-300 bg-white px-4 py-10 text-center dark:border-slate-700 dark:bg-slate-900">
                    <Database className="mx-auto h-8 w-8 text-slate-400" />
                    <p className="mt-3 text-sm text-slate-600 dark:text-slate-300">
                        {statusError
                            ? '探测状态暂时取不到，请点击右上角「刷新」重试。'
                            : '还没有探测记录，点击「立即探测」先跑一次。'}
                    </p>
                    <p className="mt-1 text-xs text-slate-500">
                        探测会真实调用每个数据源，通常数十秒内完成；完成后这里会显示可用 / 降级 / 不可用 / 超时 / 不支持的分布。
                    </p>
                    <button
                        type="button"
                        onClick={runProbe}
                        disabled={probeRunning}
                        className="mt-4 inline-flex items-center gap-1 rounded-lg bg-violet-600 px-3 py-1.5 text-sm text-white hover:bg-violet-700 disabled:opacity-60"
                    >
                        {probeRunning ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                        {probeRunning ? '探测中…' : '立即探测'}
                    </button>
                </div>
            ) : (
                <div className="space-y-3">
                    {/* 卡片数由 PROBE_SUMMARY_CARDS 决定（现在含「超时」共 6 张），这里只负责栅格 */}
                    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
                        {PROBE_SUMMARY_CARDS.map(card => (
                            <div
                                key={card.key}
                                className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900"
                            >
                                <div className="text-xs text-slate-500 dark:text-slate-400">{card.label}</div>
                                <div className={`mt-1 text-2xl font-semibold ${card.valueClass}`}>
                                    {counts[card.key]}
                                </div>
                            </div>
                        ))}
                    </div>

                    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-slate-200 bg-white px-4 py-3 text-sm dark:border-slate-700 dark:bg-slate-900">
                        <span className="text-slate-500 dark:text-slate-400">
                            探测时间：
                            <span className="text-slate-700 dark:text-slate-200">
                                {latestRun
                                    ? formatProbeTimestamp(latestRun.started_at, { seconds: true })
                                    : EMPTY_PLACEHOLDER}
                            </span>
                        </span>
                        <span className="text-slate-500 dark:text-slate-400">
                            触发方式：
                            <span className="text-slate-700 dark:text-slate-200">
                                {latestRun ? triggerLabel(latestRun.trigger) : EMPTY_PLACEHOLDER}
                            </span>
                        </span>
                        <span className="text-slate-500 dark:text-slate-400">
                            耗时：
                            <span className="text-slate-700 dark:text-slate-200">
                                {formatLatency(latestRun?.duration_ms)}
                            </span>
                        </span>
                        <span className="text-slate-500 dark:text-slate-400">
                            交易日：
                            <span
                                className={
                                    nonTradingDay
                                        ? 'font-medium text-amber-600 dark:text-amber-400'
                                        : 'text-slate-700 dark:text-slate-200'
                                }
                            >
                                {latestRun ? formatTradingDay(latestRun.trading_day) : EMPTY_PLACEHOLDER}
                            </span>
                        </span>
                        {latestRun && <StatusBadge meta={probeStatusMeta(latestRun.status)} />}
                    </div>

                    {nonTradingDay && (
                        <p className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300">
                            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                            非交易日：行情类数据返回空属于预期，此时「降级（0 行）」不必按故障处理，重点看「不可用」。
                            另外「超时」只表示这一轮超过了硬截止（源慢），不等于源坏了，不必按故障处理。
                        </p>
                    )}

                    {!latestRun && hasResults && (
                        <p className="text-xs text-slate-500 dark:text-slate-400">
                            暂无运行记录（latest_run 为空），下面的数字按最新明细结果统计。
                        </p>
                    )}
                </div>
            )}

            {/* 明细表 */}
            <div className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
                    <div className="text-sm font-medium text-slate-700 dark:text-slate-200">
                        探测明细
                        <span className="ml-2 text-xs font-normal text-slate-500 dark:text-slate-400">
                            显示 {visibleRows.length} / {sortedRows.length} 条（问题在前）
                        </span>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                        <div className="relative">
                            <Search className="pointer-events-none absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                            <input
                                value={query}
                                onChange={e => setQuery(e.target.value)}
                                aria-label="搜索探测结果"
                                placeholder="搜索数据源 / 维度 / 错误…"
                                className="w-56 rounded-lg border border-slate-300 py-1.5 pl-8 pr-2 text-sm focus:outline-none focus:ring-1 focus:ring-violet-500 dark:border-slate-700 dark:bg-slate-900"
                            />
                        </div>
                        <select
                            value={sourceFilter}
                            onChange={e => setSourceFilter(e.target.value)}
                            aria-label="按数据源筛选"
                            className="rounded-lg border border-slate-300 px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
                        >
                            <option value={FILTER_ALL}>全部数据源</option>
                            {sourceOptions.map(source => (
                                <option key={source} value={source}>
                                    {source}
                                </option>
                            ))}
                        </select>
                        <select
                            value={categoryFilter}
                            onChange={e => setCategoryFilter(e.target.value)}
                            aria-label="按类别筛选"
                            className="rounded-lg border border-slate-300 px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
                        >
                            <option value={FILTER_ALL}>全部类别</option>
                            {categoryOptions.map(category => (
                                <option key={category} value={category}>
                                    {category}
                                </option>
                            ))}
                        </select>
                        {hasFilters && (
                            <button
                                type="button"
                                onClick={() => {
                                    setQuery('')
                                    setSourceFilter(FILTER_ALL)
                                    setCategoryFilter(FILTER_ALL)
                                }}
                                className="rounded-lg border border-slate-300 px-2 py-1.5 text-sm hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
                            >
                                清空筛选
                            </button>
                        )}
                    </div>
                </div>

                {/* 按数据源分组概览（点一下即筛选） */}
                {sourceSummaries.length > 0 && (
                    <div className="flex flex-wrap gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
                        {sourceSummaries.map(item => (
                            <button
                                key={item.source}
                                type="button"
                                onClick={() => setSourceFilter(item.source)}
                                title={`只看 ${item.source}`}
                                className={`inline-flex items-center gap-2 rounded-lg border px-2 py-1 text-xs transition-colors ${
                                    sourceFilter === item.source
                                        ? 'border-violet-300 bg-violet-50 dark:border-violet-500/40 dark:bg-violet-500/10'
                                        : 'border-slate-200 hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800'
                                }`}
                            >
                                <span className={`h-2 w-2 rounded-full ${item.worstMeta.dotClass}`} />
                                <span className="font-mono text-slate-600 dark:text-slate-300">{item.source}</span>
                                <span className="text-slate-500 dark:text-slate-400">
                                    可用 {item.ok}/{item.total}
                                </span>
                                {item.degraded > 0 && (
                                    <span className="text-amber-600 dark:text-amber-400">降级 {item.degraded}</span>
                                )}
                                {item.failed > 0 && (
                                    <span className="text-rose-600 dark:text-rose-400">不可用 {item.failed}</span>
                                )}
                                {item.timeout > 0 && (
                                    <span className="text-violet-600 dark:text-violet-400">超时 {item.timeout}</span>
                                )}
                            </button>
                        ))}
                    </div>
                )}

                <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                        <thead>
                            <tr className="text-left text-xs text-slate-500 dark:text-slate-400">
                                <th className="px-4 py-2 whitespace-nowrap">数据源</th>
                                <th className="px-4 py-2 whitespace-nowrap">维度</th>
                                <th className="px-4 py-2 whitespace-nowrap">状态</th>
                                <th className="px-4 py-2 text-right whitespace-nowrap">延迟</th>
                                <th className="px-4 py-2 text-right whitespace-nowrap">行数</th>
                                <th className="px-4 py-2 whitespace-nowrap">版本</th>
                                <th className="px-4 py-2">错误摘要</th>
                            </tr>
                        </thead>
                        <tbody>
                            {visibleRows.map((row, index) => {
                                const meta = probeStatusMeta(row.status)
                                const errorText = probeErrorText(row)
                                return (
                                    <tr
                                        key={`${row.source}-${row.tool}-${index}`}
                                        className={`border-t border-slate-100 dark:border-slate-800 ${meta.rowClass}`}
                                    >
                                        <td className="px-4 py-2 whitespace-nowrap font-mono text-xs text-slate-600 dark:text-slate-300">
                                            {row.source || EMPTY_PLACEHOLDER}
                                        </td>
                                        <td className="px-4 py-2">
                                            <div className="text-slate-900 dark:text-slate-100">
                                                {row.label || row.tool || EMPTY_PLACEHOLDER}
                                            </div>
                                            <div className="mt-0.5 font-mono text-xs text-slate-500 dark:text-slate-400">
                                                {row.tool || EMPTY_PLACEHOLDER} · {formatProbeTimestamp(row.checked_at)}
                                            </div>
                                        </td>
                                        <td className="px-4 py-2 whitespace-nowrap">
                                            <StatusBadge meta={meta} />
                                        </td>
                                        <td className="px-4 py-2 text-right whitespace-nowrap text-slate-600 dark:text-slate-300">
                                            {formatLatency(row.latency_ms)}
                                        </td>
                                        {/* lines 单位是预格式化文本的非空行数，只是体量代理，悬停说明一句避免被读成记录数 */}
                                        <td
                                            className={`px-4 py-2 text-right whitespace-nowrap ${
                                                row.row_count === 0
                                                    ? 'text-amber-600 dark:text-amber-400'
                                                    : 'text-slate-600 dark:text-slate-300'
                                            }`}
                                            title={
                                                isApproxRowUnit(row.row_unit)
                                                    ? '返回的是预格式化文本，这里是非空行数（体量代理），不是记录数'
                                                    : undefined
                                            }
                                        >
                                            {formatRowCount(row.row_count, row.row_unit)}
                                        </td>
                                        <td className="px-4 py-2 whitespace-nowrap font-mono text-xs text-slate-500 dark:text-slate-400">
                                            {row.version || EMPTY_PLACEHOLDER}
                                        </td>
                                        <td className="max-w-[22rem] px-4 py-2">
                                            {errorText ? (
                                                <span
                                                    className="block truncate text-xs text-rose-600 dark:text-rose-400"
                                                    title={errorText}
                                                >
                                                    {probeErrorSummary(row)}
                                                </span>
                                            ) : (
                                                <span className="text-slate-400">{EMPTY_PLACEHOLDER}</span>
                                            )}
                                        </td>
                                    </tr>
                                )
                            })}
                            {visibleRows.length === 0 && (
                                <tr>
                                    <td colSpan={7} className="px-4 py-8 text-center text-sm text-slate-500">
                                        {sortedRows.length === 0
                                            ? statusError
                                                ? '探测结果暂时取不到，请点击「刷新」重试。'
                                                : '还没有探测结果，点击「立即探测」先跑一次。'
                                            : '没有符合筛选条件的记录，试试清空筛选。'}
                                    </td>
                                </tr>
                            )}
                        </tbody>
                    </table>
                </div>
            </div>

            {/* 趋势 */}
            <div className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3 dark:border-slate-700">
                    <div className="flex items-center gap-2 text-sm font-medium text-slate-700 dark:text-slate-200">
                        <TrendingUp className="h-4 w-4 text-cyan-500" />
                        探测趋势
                    </div>
                    <label className="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
                        历史区间
                        <select
                            value={historyDays}
                            onChange={e => setHistoryDays(Number(e.target.value))}
                            className="rounded-lg border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900"
                        >
                            {HISTORY_DAY_OPTIONS.map(days => (
                                <option key={days} value={days}>
                                    近 {days} 天
                                </option>
                            ))}
                        </select>
                    </label>
                </div>

                <div className="space-y-4 px-4 py-3">
                    <div>
                        <div className="mb-2 flex flex-wrap items-center gap-3 text-xs text-slate-500 dark:text-slate-400">
                            <span>最近几次探测（柱高 = 探测总数）</span>
                            {PROBE_TREND_SEGMENTS.map(segment => (
                                <span key={segment.key} className="inline-flex items-center gap-1">
                                    <span className={`h-2 w-2 rounded-sm ${segment.barClass}`} />
                                    {segment.label}
                                </span>
                            ))}
                            <span className="ml-auto">共 {trendBars.length} 次</span>
                        </div>
                        {statusLoading ? (
                            <LoadingRow label="正在加载运行记录…" />
                        ) : trendBars.length === 0 ? (
                            <p className="py-6 text-center text-sm text-slate-500">
                                {statusError
                                    ? '运行记录暂时取不到，请点击「刷新」重试。'
                                    : '还没有运行记录，点击「立即探测」先跑一次。'}
                            </p>
                        ) : (
                            <>
                                <div className="flex h-32 items-end gap-1.5">
                                    {trendBars.map(bar => (
                                        <div
                                            key={bar.id}
                                            className="flex h-full flex-1 flex-col justify-end"
                                            title={`${bar.timeLabel} · ${bar.triggerLabel} · ${bar.tradingDayLabel} · 总数 ${bar.counts.total}（可用 ${bar.counts.ok} / 降级 ${bar.counts.degraded} / 不可用 ${bar.counts.failed} / 超时 ${bar.counts.timeout} / 不支持 ${bar.counts.unsupported}） · 耗时 ${bar.durationLabel}`}
                                        >
                                            <div
                                                className="flex w-full flex-col overflow-hidden rounded-t"
                                                style={{ height: `${Math.max(4, bar.heightPct)}%` }}
                                            >
                                                {trendSegmentHeights(bar.counts).map(segment =>
                                                    segment.count > 0 ? (
                                                        <div
                                                            key={segment.key}
                                                            className={segment.barClass}
                                                            style={{ flexGrow: segment.count, flexBasis: 0 }}
                                                        />
                                                    ) : null,
                                                )}
                                            </div>
                                        </div>
                                    ))}
                                </div>
                                <div className="mt-1 flex gap-1.5 text-[10px] text-slate-400">
                                    {trendBars.map(bar => (
                                        <span key={bar.id} className="flex-1 truncate text-center">
                                            {bar.timeLabel.slice(0, 5)}
                                        </span>
                                    ))}
                                </div>
                            </>
                        )}
                    </div>

                    <div className="border-t border-slate-100 pt-3 dark:border-slate-800">
                        <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500 dark:text-slate-400">
                            <span>按数据源的状态时间线（左旧右新，每格一次探测；鼠标悬停看详情）</span>
                            {history && <span>共 {history.points.length} 个历史点</span>}
                        </div>
                        {historyLoading ? (
                            <LoadingRow label="正在加载探测历史…" />
                        ) : historyError ? (
                            <ErrorBanner message={historyError} />
                        ) : timelines.length === 0 ? (
                            <p className="py-6 text-center text-sm text-slate-500">
                                近 {historyDays} 天没有探测历史记录，先跑一次探测，这里会累积成趋势。
                            </p>
                        ) : (
                            <div className="space-y-2">
                                {timelines.map(timeline => (
                                    <div key={timeline.source} className="flex flex-wrap items-center gap-2">
                                        <span
                                            className="w-40 shrink-0 truncate font-mono text-xs text-slate-600 dark:text-slate-300"
                                            title={timeline.source}
                                        >
                                            {timeline.source}
                                        </span>
                                        <span className="flex flex-wrap items-center gap-0.5">
                                            {timeline.cells.map(cell => (
                                                <span
                                                    key={cell.key}
                                                    className={`h-3 w-3 rounded-sm ${cell.statusMeta.barClass}`}
                                                    title={`${cell.label} · ${cell.statusMeta.label} · 延迟 ${cell.latencyLabel} · 行数 ${cell.rowCountLabel}`}
                                                />
                                            ))}
                                        </span>
                                        <StatusBadge meta={timeline.lastCell ? timeline.lastCell.statusMeta : timeline.worstMeta} />
                                        <span className="text-xs text-slate-500 dark:text-slate-400">
                                            可用 {timeline.ok}/{timeline.total}
                                        </span>
                                        {timeline.degraded > 0 && (
                                            <span className="text-xs text-amber-600 dark:text-amber-400">
                                                降级 {timeline.degraded}
                                            </span>
                                        )}
                                        {timeline.failed > 0 && (
                                            <span className="text-xs text-rose-600 dark:text-rose-400">
                                                不可用 {timeline.failed}
                                            </span>
                                        )}
                                        {timeline.timeout > 0 && (
                                            <span className="text-xs text-violet-600 dark:text-violet-400">
                                                超时 {timeline.timeout}
                                            </span>
                                        )}
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                </div>
            </div>

            {/* 能力矩阵 */}
            <div className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
                <button
                    type="button"
                    onClick={() => setShowMatrix(value => !value)}
                    aria-expanded={showMatrix}
                    className="flex w-full items-center justify-between gap-2 px-4 py-3 text-left"
                >
                    <span className="flex flex-wrap items-center gap-2 text-sm font-medium text-slate-700 dark:text-slate-200">
                        <Layers className="h-4 w-4 text-violet-500" />
                        能力矩阵（期望覆盖 vs 实际状态）
                        <span className="text-xs font-normal text-slate-500 dark:text-slate-400">
                            {capabilityRows.length} 个维度
                            {matrix?.generated_at
                                ? ` · 生成于 ${formatProbeTimestamp(matrix.generated_at)}`
                                : ''}
                        </span>
                    </span>
                    <ChevronDown
                        className={`h-4 w-4 shrink-0 text-slate-400 transition-transform ${
                            showMatrix ? '' : '-rotate-90'
                        }`}
                    />
                </button>

                {showMatrix && (
                    <div className="border-t border-slate-200 px-4 py-3 dark:border-slate-700">
                        <div className="mb-3 flex flex-wrap items-center gap-3 text-xs text-slate-500 dark:text-slate-400">
                            {PROBE_TIER_LEGEND.map(tier => (
                                <span key={tier.label} className="inline-flex items-center gap-1">
                                    <span className={`rounded border px-1.5 py-0.5 text-[11px] font-medium ${tier.className}`}>
                                        {tier.label}
                                    </span>
                                    {tier.description}
                                </span>
                            ))}
                        </div>

                        {matrixLoading ? (
                            <LoadingRow label="正在加载能力矩阵…" />
                        ) : matrixError ? (
                            <ErrorBanner message={matrixError} />
                        ) : capabilityRows.length === 0 ? (
                            <p className="py-6 text-center text-sm text-slate-500">暂无能力矩阵数据。</p>
                        ) : (
                            <div className="overflow-x-auto">
                                <table className="w-full text-sm">
                                    <thead>
                                        <tr className="text-left text-xs text-slate-500 dark:text-slate-400">
                                            <th className="px-4 py-2 whitespace-nowrap">维度</th>
                                            <th className="px-4 py-2 whitespace-nowrap">类别</th>
                                            <th className="px-4 py-2">期望数据源（含最新实际状态）</th>
                                            <th className="px-4 py-2">备注</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {capabilityRows.map(row => (
                                            <tr
                                                key={row.tool}
                                                className="border-t border-slate-100 align-top dark:border-slate-800"
                                            >
                                                <td className="px-4 py-3">
                                                    <div className="text-slate-900 dark:text-slate-100">{row.label}</div>
                                                    <div className="mt-0.5 font-mono text-xs text-slate-500 dark:text-slate-400">
                                                        {row.tool || EMPTY_PLACEHOLDER}
                                                    </div>
                                                </td>
                                                <td className="px-4 py-3 whitespace-nowrap">
                                                    <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                                                        {row.category || EMPTY_PLACEHOLDER}
                                                    </span>
                                                </td>
                                                <td className="px-4 py-3">
                                                    <div className="flex flex-wrap gap-2">
                                                        {row.sources.map(source => (
                                                            <span
                                                                key={source.source}
                                                                className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-2 py-1 text-xs dark:border-slate-700"
                                                            >
                                                                <span
                                                                    className={`rounded border px-1 py-0.5 text-[10px] font-medium ${source.tier.className}`}
                                                                    title={source.tier.description}
                                                                >
                                                                    {source.tier.label}
                                                                </span>
                                                                <span className="font-mono text-slate-600 dark:text-slate-300">
                                                                    {source.source}
                                                                </span>
                                                                <span
                                                                    className={`h-1.5 w-1.5 rounded-full ${source.statusMeta.dotClass}`}
                                                                    title={`实际状态：${source.statusMeta.label}`}
                                                                />
                                                                <span
                                                                    className={
                                                                        source.probed
                                                                            ? 'text-slate-500 dark:text-slate-400'
                                                                            : 'text-slate-400 dark:text-slate-500'
                                                                    }
                                                                >
                                                                    {source.statusMeta.label}
                                                                    {source.probed ? ` · ${source.latencyLabel}` : ''}
                                                                </span>
                                                                {source.batchable && (
                                                                    <span className="text-slate-400">可批量</span>
                                                                )}
                                                                {source.needs_credential && (
                                                                    <span className="text-amber-600 dark:text-amber-400">
                                                                        需凭证
                                                                    </span>
                                                                )}
                                                            </span>
                                                        ))}
                                                        {row.sources.length === 0 && (
                                                            <span className="text-xs text-slate-400">未登记数据源</span>
                                                        )}
                                                    </div>
                                                </td>
                                                <td className="px-4 py-3 text-xs text-slate-500 dark:text-slate-400">
                                                    {row.note ||
                                                        row.sources
                                                            .map(source => source.note)
                                                            .filter(Boolean)
                                                            .join(' / ') ||
                                                        EMPTY_PLACEHOLDER}
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                        )}
                    </div>
                )}
            </div>
        </div>
    )
}

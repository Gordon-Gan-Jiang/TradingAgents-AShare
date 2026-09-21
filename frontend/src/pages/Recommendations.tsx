import { BookmarkPlus, CheckSquare, Loader2, PlayCircle, PlusCircle, RefreshCw, Sparkles, Square, TrendingUp } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '@/services/api'
import type { DailyProductRunResponse, RecommendationProfile, RecommendationResponse } from '@/types'

// ── Strategy-hit label localisation ──────────────────────────────────────────
const STRATEGY_LABELS: Record<string, string> = {
    strong_momentum:    '强动量',
    trend_up:           '趋势上行',
    reversal_dip:       '回调反转',
    breakout_near_high: '日高突破',
    high_tight_range:   '高位密集',
    active_liquidity:   '成交活跃',
    sector_rotation:    '板块轮动',
    volume_surge:       '量比放大',
    intraday_bull_body: '盘中阳线',
    turnover_quality:   '换手健康',
    low_volatility:     '低波动',
    '52w_near_high':    '近52周高',
    '52w_middle':       '52周中段',
}
const RISK_LABELS: Record<string, string> = {
    weak_momentum:         '动量弱',
    far_from_intraday_high:'远离日高',
    borderline_liquidity:  '流动性偏低',
    volume_dry_up:         '缩量',
    hot_money_risk:        '换手率过高（热钱）',
    elevated_turnover:     '换手偏高',
    illiquid_turnover:     '换手率极低（流动性差）',
    high_volatility:       '振幅过大',
    excessive_turnover:    '换手率过高',
    missing_volume:        '无成交量',
}
function hitBadge(key: string) {
    return STRATEGY_LABELS[key] ?? key
}
function riskBadge(key: string) {
    return RISK_LABELS[key] ?? key
}

// ── Score-breakdown factor labels ─────────────────────────────────────────────
const FACTOR_LABELS: Record<string, string> = {
    momentum:     '动量',
    activity:     '活跃',
    near_high:    '逼高',
    sector:       '板块',
    volume_ratio: '量比',
    volatility:   '低波',
    turnover_q:   '换手质',
    bonus:        '加成',
    penalty:      '扣分',
}

export default function Recommendations() {
    const navigate = useNavigate()
    const [loading, setLoading] = useState(false)
    const [workingSymbol, setWorkingSymbol] = useState<string | null>(null)
    const [selectedSymbols, setSelectedSymbols] = useState<string[]>([])
    const [watchlistWorking, setWatchlistWorking] = useState<'batch' | string | null>(null)
    const [successMsg, setSuccessMsg] = useState<string | null>(null)
    const [result, setResult] = useState<RecommendationResponse | null>(null)
    const [error, setError] = useState<string | null>(null)
    const [topK, setTopK] = useState(5)
    const [market, setMarket] = useState<'cn' | 'us'>('cn')
    const [scoreProfile, setScoreProfile] = useState('ashare_balanced')
    const [profiles, setProfiles] = useState<RecommendationProfile[]>([])
    const [sourceMode, setSourceMode] = useState<'user_pool' | 'market_scan'>('market_scan')
    const [scanLimitInput, setScanLimitInput] = useState('2000')
    const [momentumWeight, setMomentumWeight] = useState('')
    const [activityWeight, setActivityWeight] = useState('')
    const [nearHighWeight, setNearHighWeight] = useState('')
    const [dailyRuns, setDailyRuns] = useState<DailyProductRunResponse[]>([])
    const [runningDailyProduct, setRunningDailyProduct] = useState(false)
    const [pushingCloseRec, setPushingCloseRec] = useState(false)
    const [useBacktestFeedback, setUseBacktestFeedback] = useState(true)
    const [strategyMode, setStrategyMode] = useState<'manual' | 'auto'>('auto')

    const customWeights = useMemo(() => {
        const toNum = (v: string) => {
            if (!v.trim()) return undefined
            const n = Number(v)
            return Number.isFinite(n) ? n : undefined
        }
        return {
            momentum_weight: toNum(momentumWeight),
            activity_weight: toNum(activityWeight),
            near_high_weight: toNum(nearHighWeight),
        }
    }, [activityWeight, momentumWeight, nearHighWeight])

    const resolvedScanLimit = useMemo(() => {
        const digitsOnly = scanLimitInput.replace(/[^\d]/g, '')
        const parsed = Number(digitsOnly)
        if (!Number.isFinite(parsed) || parsed <= 0) return 2000
        return Math.max(50, Math.min(5000, Math.trunc(parsed)))
    }, [scanLimitInput])

    // Fetch profiles once on mount
    useEffect(() => {
        api.listRecommendationProfiles()
            .then(r => setProfiles(r.profiles || []))
            .catch(() => {
                setProfiles([
                    { id: 'ashare_balanced',     name: 'A股均衡',   description: '价量综合均衡打分' },
                    { id: 'ashare_aggressive',   name: 'A股进攻',   description: '强动量优先' },
                    { id: 'sector_rotation',     name: '行业轮动',  description: '板块资金流向优先' },
                    { id: 'breakout',            name: '突破策略',  description: '量比+逼近日高双重确认' },
                    { id: 'low_volume_reversal', name: '缩量反转',  description: '低换手强板块底部信号' },
                    { id: 'us_balanced',         name: '美股均衡',  description: '适用于美股' },
                ])
            })
    }, [])

    const cnProfiles = profiles.filter(p => p.id !== 'us_balanced')
    const usProfiles = profiles.filter(p => p.id === 'us_balanced')

    const buildPayload = useCallback(() => ({
        top_k: topK,
        include_tracking: true,
        include_watchlist: true,
        source_mode: sourceMode,
        scan_limit: sourceMode === 'market_scan' ? resolvedScanLimit : undefined,
        min_change_pct: -1.0,
        market,
        min_price: 2.0,
        max_price: 10000.0,
        min_amount: 200000000,
        score_profile: scoreProfile,
        ...customWeights,
    }), [customWeights, market, resolvedScanLimit, scoreProfile, sourceMode, topK])

    const load = useCallback(async () => {
        setLoading(true)
        setError(null)
        setSuccessMsg(null)
        try {
            setResult(await api.getRecommendations(buildPayload()))
            setSelectedSymbols([])
        } catch (e) {
            setError(e instanceof Error ? e.message : '推荐加载失败')
        } finally {
            setLoading(false)
        }
    }, [buildPayload])

    const autoAnalyzeTop = useCallback(async () => {
        setLoading(true)
        setError(null)
        setSuccessMsg(null)
        try {
            setResult(await api.getRecommendations({
                ...buildPayload(),
                top_k: Math.max(10, topK),
                auto_start_analysis: true,
                auto_top_n: 10,
                horizons: ['short'],
            }))
            setSelectedSymbols([])
        } catch (e) {
            setError(e instanceof Error ? e.message : '自动分析触发失败')
        } finally {
            setLoading(false)
        }
    }, [buildPayload, topK])

    const addToTracking = useCallback(async (symbol: string, name: string) => {
        setWorkingSymbol(symbol)
        setError(null)
        try {
            await api.mergePortfolioImport({ positions: [{ symbol, name }], auto_apply_scheduled: true })
        } catch (e) {
            setError(e instanceof Error ? e.message : '加入跟踪失败')
        } finally {
            setWorkingSymbol(null)
        }
    }, [])

    const recommendationItems = result?.items || []
    const allSelected = recommendationItems.length > 0
        && recommendationItems.every(i => selectedSymbols.includes(i.symbol))

    const toggleSelectSymbol = useCallback((symbol: string) => {
        setSelectedSymbols(prev =>
            prev.includes(symbol) ? prev.filter(s => s !== symbol) : [...prev, symbol],
        )
    }, [])

    const toggleSelectAll = useCallback(() => {
        if (allSelected) {
            setSelectedSymbols([])
        } else {
            setSelectedSymbols(recommendationItems.map(i => i.symbol).filter(Boolean))
        }
    }, [allSelected, recommendationItems])

    const addSymbolsToWatchlist = useCallback(async (symbols: string[]) => {
        const uniq = [...new Set(symbols.map(s => s.trim()).filter(Boolean))]
        if (uniq.length === 0) return
        setError(null)
        setSuccessMsg(null)
        setWatchlistWorking(uniq.length > 1 ? 'batch' : uniq[0])
        try {
            const resp = await api.addToWatchlist(uniq.join('\n'))
            setSuccessMsg(resp.message || `已处理 ${uniq.length} 只`)
            setSelectedSymbols(prev => prev.filter(s => !uniq.includes(s)))
        } catch (e) {
            setError(e instanceof Error ? e.message : '加入自选失败')
        } finally {
            setWatchlistWorking(null)
        }
    }, [])

    const topJobs = useMemo(() => result?.analysis_jobs || [], [result])

    const loadDailyRuns = useCallback(async () => {
        try {
            const resp = await api.listDailyProductRuns(8)
            setDailyRuns(resp.runs || [])
        } catch {}
    }, [])

    const runDailyProduct = useCallback(async () => {
        setRunningDailyProduct(true)
        setError(null)
        setSuccessMsg(null)
        try {
            const resp = await api.runDailyProduct({
                mode: 'recommended',
                top_k: Math.min(5, topK),
                candidate_limit: 80,
                recommendation_source: sourceMode,
                scan_limit: sourceMode === 'market_scan' ? resolvedScanLimit : undefined,
                include_tracking: true,
                include_watchlist: true,
                market,
                min_price: 2.0,
                max_price: 10000.0,
                min_amount: 200000000,
                score_profile: scoreProfile,
                ...customWeights,
                use_backtest_feedback: useBacktestFeedback,
                strategy_mode: strategyMode,
                strategy_skills: strategyMode === 'manual' ? ['market_strategy', 'risk_scoring'] : [],
                horizons: ['short'],
                ensure_scheduled: true,
                schedule_horizon: 'short',
                schedule_trigger_time: '20:00',
            })
            setDailyRuns(prev => [resp, ...prev].slice(0, 8))
        } catch (e) {
            setError(e instanceof Error ? e.message : '批跑触发失败')
        } finally {
            setRunningDailyProduct(false)
        }
    }, [customWeights, market, resolvedScanLimit, scoreProfile, sourceMode, strategyMode, topK, useBacktestFeedback])

    const manualPushCloseRecommendation = useCallback(async () => {
        setPushingCloseRec(true)
        setError(null)
        setSuccessMsg(null)
        try {
            const r = await api.manualPushRecommendations('close')
            if (r.sent) {
                setSuccessMsg(`已手动推送收盘复盘推荐：候选 ${r.items_count} 只（企业微信: ${r.sent_wecom ? '成功' : '未发送'}；WPS: ${r.sent_wps ? '成功' : '未发送'}）`)
            } else {
                setSuccessMsg(`手动推送已执行，但未发送成功（status=${r.status}，候选 ${r.items_count} 只）。请检查 webhook 与推送开关。`)
            }
        } catch (e) {
            setError(e instanceof Error ? e.message : '手动推送失败')
        } finally {
            setPushingCloseRec(false)
        }
    }, [])

    const refreshRunStatus = useCallback(async (runId: string) => {
        try {
            const run = await api.getDailyProductRun(runId)
            setDailyRuns(prev => prev.map(item => (item.run_id === runId ? run : item)))
        } catch {}
    }, [])

    useEffect(() => { void loadDailyRuns() }, [loadDailyRuns])

    const sm = result?.scoring_model
    const weightSummary = sm?.weights
        ? Object.entries(sm.weights)
            .filter(([, v]) => v != null && v > 0)
            .map(([k, v]) => `${FACTOR_LABELS[k] ?? k}=${v}`)
            .join('  ')
        : null

    return (
        <div className="space-y-4">
            {/* ── Header bar ─────────────────────────────────────────────────── */}
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">股票推荐</h1>
                    <p className="text-sm text-slate-500 dark:text-slate-400">
                        全市场五因子扫描 · 板块资金流 · 量比 · 换手率
                    </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                    <label className="text-xs text-slate-500">数量</label>
                    <input
                        type="number" min={1} max={20} value={topK}
                        onChange={e => setTopK(Math.max(1, Math.min(20, Number(e.target.value) || 5)))}
                        className="w-16 rounded-lg border border-slate-200 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                    />

                    {/* Source mode */}
                    <select
                        value={sourceMode}
                        onChange={e => setSourceMode(e.target.value as 'user_pool' | 'market_scan')}
                        className="rounded-lg border border-slate-200 px-2 py-1 text-sm font-medium dark:border-slate-700 dark:bg-slate-900"
                    >
                        <option value="market_scan">🔍 全市场扫描</option>
                        <option value="user_pool">📋 自选/跟踪池</option>
                    </select>

                    {/* Scan limit (market_scan only) */}
                    {sourceMode === 'market_scan' && (
                        <>
                            <label className="text-xs text-slate-500">扫描上限</label>
                            <input
                                type="text"
                                inputMode="numeric"
                                pattern="[0-9]*"
                                value={scanLimitInput}
                                onChange={e => setScanLimitInput(e.target.value)}
                                onBlur={() => setScanLimitInput(String(resolvedScanLimit))}
                                className="w-20 rounded-lg border border-slate-200 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                                title="全市场扫描股票数量上限（50-5000）"
                            />
                        </>
                    )}

                    {/* Market */}
                    <select
                        value={market}
                        onChange={e => {
                            const v = e.target.value as 'cn' | 'us'
                            setMarket(v)
                            setScoreProfile(v === 'us' ? 'us_balanced' : 'ashare_balanced')
                        }}
                        className="rounded-lg border border-slate-200 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
                    >
                        <option value="cn">A 股</option>
                        <option value="us">美股</option>
                    </select>

                    <button type="button" onClick={load} disabled={loading}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800">
                        {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                        刷新推荐
                    </button>
                    <button type="button" onClick={autoAnalyzeTop} disabled={loading}
                        className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-700">
                        {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <PlayCircle className="h-4 w-4" />}
                        推荐并分析（10支）
                    </button>
                    <button
                        type="button"
                        onClick={manualPushCloseRecommendation}
                        disabled={pushingCloseRec}
                        className="inline-flex items-center gap-1.5 rounded-lg bg-violet-600 px-3 py-1.5 text-sm text-white hover:bg-violet-700 disabled:opacity-60"
                        title="立即手动触发“收盘复盘推荐”消息推送"
                    >
                        {pushingCloseRec ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                        手动推送收盘复盘
                    </button>
                </div>
            </div>

            {/* ── Strategy profile quick-pick ────────────────────────────────── */}
            <div className="rounded-xl border border-indigo-100 bg-indigo-50 p-3 dark:border-indigo-900 dark:bg-indigo-950/30">
                <div className="mb-2 text-xs font-semibold text-indigo-700 dark:text-indigo-300">选股策略模板</div>
                <div className="flex flex-wrap gap-2">
                    {(market === 'cn' ? cnProfiles : usProfiles).map(p => (
                        <button
                            key={p.id}
                            type="button"
                            onClick={() => setScoreProfile(p.id)}
                            title={p.description}
                            className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                                scoreProfile === p.id
                                    ? 'bg-indigo-600 text-white shadow'
                                    : 'border border-indigo-200 bg-white text-indigo-700 hover:bg-indigo-100 dark:border-indigo-700 dark:bg-slate-900 dark:text-indigo-300'
                            }`}
                        >
                            {p.name}
                        </button>
                    ))}
                </div>
                {profiles.find(p => p.id === scoreProfile) && (
                    <p className="mt-1.5 text-[11px] text-indigo-500 dark:text-indigo-400">
                        {profiles.find(p => p.id === scoreProfile)!.description}
                    </p>
                )}
            </div>

            {/* ── Daily batch run panel ──────────────────────────────────────── */}
            <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-900">
                <div className="mb-2 flex items-center justify-between">
                    <div className="text-sm font-medium text-slate-700 dark:text-slate-200">每日批跑看板</div>
                    <div className="flex items-center gap-2">
                        <label className="inline-flex items-center gap-1.5 text-xs text-slate-500">
                            <input type="checkbox" checked={useBacktestFeedback}
                                onChange={e => setUseBacktestFeedback(e.target.checked)} />
                            回测反哺权重
                        </label>
                        <select value={strategyMode} onChange={e => setStrategyMode(e.target.value as 'manual' | 'auto')}
                            className="rounded-md border border-slate-300 px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900">
                            <option value="auto">策略自动编排</option>
                            <option value="manual">策略手动编排</option>
                        </select>
                        <button type="button" onClick={loadDailyRuns}
                            className="rounded-md border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800">
                            刷新看板
                        </button>
                        <button type="button" onClick={runDailyProduct} disabled={runningDailyProduct}
                            className="inline-flex items-center gap-1 rounded-md bg-violet-600 px-2 py-1 text-xs text-white hover:bg-violet-700 disabled:opacity-60">
                            {runningDailyProduct ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <PlayCircle className="h-3.5 w-3.5" />}
                            启动每日批跑
                        </button>
                    </div>
                </div>
                <div className="space-y-1.5">
                    {dailyRuns.map(run => (
                        <div key={run.run_id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-100 px-2 py-2 dark:border-slate-800">
                            <div className="text-xs text-slate-600 dark:text-slate-300">
                                <span className="font-medium">{run.mode}</span>
                                <span className={`ml-2 rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${
                                    run.status === 'completed' ? 'bg-emerald-100 text-emerald-700' :
                                    run.status === 'failed'    ? 'bg-rose-100 text-rose-700' :
                                    'bg-amber-100 text-amber-700'
                                }`}>{run.status}</span>
                                <span className="ml-2 text-slate-400">{run.run_id.slice(0, 8)}</span>
                            </div>
                            <button type="button" onClick={() => refreshRunStatus(run.run_id)}
                                className="rounded-md border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800">
                                刷新状态
                            </button>
                        </div>
                    ))}
                    {dailyRuns.length === 0 && (
                        <div className="text-xs text-slate-500">暂无批跑记录，点击「启动每日批跑」创建。</div>
                    )}
                </div>
            </div>

            {error && (
                <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-600">
                    {error}
                </div>
            )}
            {successMsg && (
                <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700 dark:border-emerald-900/50 dark:bg-emerald-950/30 dark:text-emerald-300">
                    {successMsg}
                </div>
            )}
            {successMsg && (
                <div className="flex items-center justify-between gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800 dark:border-emerald-800/50 dark:bg-emerald-950/30 dark:text-emerald-200">
                    <span>{successMsg}</span>
                    <button type="button" onClick={() => setSuccessMsg(null)} className="shrink-0 text-emerald-600 hover:underline dark:text-emerald-300">
                        关闭
                    </button>
                </div>
            )}

            {/* ── Custom weight override (collapsed by default) ──────────────── */}
            <details className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
                <summary className="cursor-pointer px-4 py-2.5 text-sm font-medium text-slate-600 dark:text-slate-300">
                    ▸ 高级权重覆盖（可选）
                </summary>
                <div className="grid grid-cols-1 gap-2 px-4 pb-3 pt-2 md:grid-cols-3">
                    <input type="number" step="0.1" min={0} max={10} value={momentumWeight}
                        onChange={e => setMomentumWeight(e.target.value)} placeholder="动量权重"
                        className="rounded-lg border border-slate-200 px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900" />
                    <input type="number" step="0.1" min={0} max={10} value={activityWeight}
                        onChange={e => setActivityWeight(e.target.value)} placeholder="活跃度权重"
                        className="rounded-lg border border-slate-200 px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900" />
                    <input type="number" step="0.1" min={0} max={10} value={nearHighWeight}
                        onChange={e => setNearHighWeight(e.target.value)} placeholder="逼近日高权重"
                        className="rounded-lg border border-slate-200 px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900" />
                </div>
            </details>

            {/* ── Analysis jobs ──────────────────────────────────────────────── */}
            {topJobs.length > 0 && (
                <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-900">
                    <div className="mb-2 text-sm font-medium text-slate-700 dark:text-slate-200">已启动分析任务</div>
                    <div className="flex flex-wrap gap-2">
                        {topJobs.map(j => (
                            <button key={j.job_id} type="button"
                                onClick={() => navigate(`/analysis?job=${j.job_id}&symbol=${j.symbol}`)}
                                className="rounded-md bg-slate-100 px-2 py-1 text-xs text-slate-700 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-200">
                                {j.symbol} · {j.job_id.slice(0, 8)}
                            </button>
                        ))}
                    </div>
                </div>
            )}

            {/* ── Results list ───────────────────────────────────────────────── */}
            <div className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
                <div className="flex flex-col gap-2 border-b border-slate-200 px-4 py-2.5 dark:border-slate-700 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between">
                    <span className="text-sm text-slate-500 dark:text-slate-400">
                        候选池 <b>{result?.pool_size ?? '--'}</b> 只 · 完成打分 <b>{result?.scored_size ?? '--'}</b> 只
                    </span>
                    <div className="flex flex-wrap items-center gap-2">
                        {recommendationItems.length > 0 && (
                            <>
                                <button
                                    type="button"
                                    onClick={toggleSelectAll}
                                    className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-600 hover:bg-slate-50 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-800"
                                >
                                    {allSelected ? <CheckSquare className="h-3.5 w-3.5" /> : <Square className="h-3.5 w-3.5" />}
                                    {allSelected ? '取消全选' : '全选'}
                                </button>
                                <button
                                    type="button"
                                    disabled={selectedSymbols.length === 0 || watchlistWorking !== null}
                                    onClick={() => void addSymbolsToWatchlist(selectedSymbols)}
                                    className="inline-flex items-center gap-1 rounded-lg bg-violet-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-violet-700 disabled:opacity-50"
                                >
                                    {watchlistWorking === 'batch'
                                        ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                        : <BookmarkPlus className="h-3.5 w-3.5" />}
                                    添加到自选{selectedSymbols.length > 0 ? ` (${selectedSymbols.length})` : ''}
                                </button>
                            </>
                        )}
                    </div>
                    {sm && (
                        <span className="w-full text-xs text-slate-400 sm:w-auto sm:text-right">
                            模型 <b className="text-indigo-500">{sm.profile}</b>
                            {sm.engine && <span className="ml-1 opacity-60">{sm.engine}</span>}
                            {weightSummary && <span className="ml-2">{weightSummary}</span>}
                        </span>
                    )}
                </div>
                <div className="divide-y divide-slate-100 dark:divide-slate-800">
                    {(result?.items || []).map((item, idx) => (
                        <div key={item.symbol} className="flex flex-col gap-2 px-4 py-3 md:flex-row md:items-start md:justify-between">
                            {/* Left: info */}
                            <div className="flex-1 min-w-0">
                                <div className="flex items-center gap-2">
                                    <label className="flex cursor-pointer items-center gap-1.5">
                                        <input
                                            type="checkbox"
                                            checked={selectedSymbols.includes(item.symbol)}
                                            onChange={() => toggleSelectSymbol(item.symbol)}
                                            className="h-4 w-4 rounded border-slate-300 text-violet-600 focus:ring-violet-500 dark:border-slate-600 dark:bg-slate-900"
                                        />
                                        <span className="sr-only">选中 {item.name}</span>
                                    </label>
                                    <span className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-indigo-100 text-xs font-bold text-indigo-700 dark:bg-indigo-900 dark:text-indigo-300">
                                        {idx + 1}
                                    </span>
                                    <Sparkles className="h-4 w-4 flex-shrink-0 text-amber-500" />
                                    <span className="font-semibold text-slate-900 dark:text-slate-100">{item.name}</span>
                                    <span className="text-xs text-slate-400">{item.code || item.symbol}</span>
                                    {item.sector && (
                                        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] text-slate-500 dark:bg-slate-800">
                                            {item.sector}
                                        </span>
                                    )}
                                </div>

                                {/* Price row */}
                                <div className="mt-1 flex flex-wrap items-center gap-3 text-xs text-slate-500 dark:text-slate-400">
                                    <span>分数 <b className="text-slate-700 dark:text-slate-200">{item.score}</b></span>
                                    {item.live_price != null && (
                                        <span>现价 <b>{item.live_price.toFixed(2)}</b></span>
                                    )}
                                    {item.price_change_pct != null && (
                                        <span className={item.price_change_pct >= 0 ? 'text-rose-500 font-medium' : 'text-emerald-600 font-medium'}>
                                            {item.price_change_pct >= 0 ? '+' : ''}{item.price_change_pct.toFixed(2)}%
                                        </span>
                                    )}
                                    {item.volume_ratio != null && (
                                        <span className={item.volume_ratio >= 2.0 ? 'font-medium text-orange-500' : ''}>
                                            量比 {item.volume_ratio.toFixed(2)}
                                        </span>
                                    )}
                                    {item.turnover_rate != null && (
                                        <span>换手 {item.turnover_rate.toFixed(2)}%</span>
                                    )}
                                    {item.amount != null && (
                                        <span>成交 {(item.amount / 1e8).toFixed(1)} 亿</span>
                                    )}
                                </div>

                                {/* Reasons */}
                                <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                                    {item.reasons.join('；')}
                                </div>

                                {/* Strategy hits */}
                                {!!item.strategy_hits?.length && (
                                    <div className="mt-1.5 flex flex-wrap gap-1">
                                        {item.strategy_hits.map(h => (
                                            <span key={h} className="inline-flex items-center gap-0.5 rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-medium text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
                                                <TrendingUp className="h-2.5 w-2.5" />
                                                {hitBadge(h)}
                                            </span>
                                        ))}
                                    </div>
                                )}

                                {/* Risk flags */}
                                {!!item.risk_flags?.length && (
                                    <div className="mt-1 flex flex-wrap gap-1">
                                        {item.risk_flags.map(r => (
                                            <span key={r} className="rounded-full bg-amber-50 px-2 py-0.5 text-[11px] text-amber-600 dark:bg-amber-950 dark:text-amber-300">
                                                ⚠ {riskBadge(r)}
                                            </span>
                                        ))}
                                    </div>
                                )}

                                {/* Score breakdown bar */}
                                {item.score_breakdown && (
                                    <div className="mt-2 flex flex-wrap gap-2">
                                        {Object.entries(item.score_breakdown)
                                            .filter(([k]) => !['bonus', 'penalty'].includes(k))
                                            .map(([k, v]) => (
                                            <div key={k} className="flex flex-col items-center gap-0.5">
                                                <div className="text-[10px] text-slate-400">{FACTOR_LABELS[k] ?? k}</div>
                                                <div className="h-1.5 w-10 rounded-full bg-slate-100 dark:bg-slate-700">
                                                    <div
                                                        className="h-full rounded-full bg-indigo-400"
                                                        style={{ width: `${Math.min(100, Math.max(0, v))}%` }}
                                                    />
                                                </div>
                                                <div className="text-[10px] text-slate-500">{v.toFixed(0)}</div>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>

                            {/* Right: actions */}
                            <div className="flex flex-shrink-0 flex-wrap items-center gap-2">
                                <button type="button" onClick={() => navigate(`/analysis?symbol=${item.symbol}`)}
                                    className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800">
                                    立即分析
                                </button>
                                <button
                                    type="button"
                                    onClick={() => void addSymbolsToWatchlist([item.symbol])}
                                    disabled={watchlistWorking !== null}
                                    className="inline-flex items-center gap-1 rounded-lg border border-violet-300 bg-violet-50 px-2.5 py-1.5 text-xs font-medium text-violet-800 hover:bg-violet-100 disabled:opacity-50 dark:border-violet-700 dark:bg-violet-950/40 dark:text-violet-200 dark:hover:bg-violet-900/50"
                                >
                                    {watchlistWorking === item.symbol
                                        ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                        : <BookmarkPlus className="h-3.5 w-3.5" />}
                                    自选
                                </button>
                                <button type="button" onClick={() => addToTracking(item.symbol, item.name)}
                                    disabled={workingSymbol === item.symbol}
                                    className="inline-flex items-center gap-1 rounded-lg bg-emerald-500 px-2.5 py-1.5 text-xs text-white hover:bg-emerald-600 disabled:opacity-50">
                                    {workingSymbol === item.symbol
                                        ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                        : <PlusCircle className="h-3.5 w-3.5" />}
                                    加入跟踪
                                </button>
                            </div>
                        </div>
                    ))}
                    {(result?.items || []).length === 0 && (
                        <div className="px-4 py-8 text-center text-sm text-slate-500">
                            点击「刷新推荐」开始生成候选
                        </div>
                    )}
                </div>
            </div>
        </div>
    )
}

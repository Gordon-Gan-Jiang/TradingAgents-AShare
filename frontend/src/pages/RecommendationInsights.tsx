import { BarChart3, BrainCircuit, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

import { api } from '@/services/api'
import type {
    DailyProductRunResponse,
    RecommendationEvalRun,
    RecommendationHistoryItem,
    RecommendationStrategyStat,
} from '@/types'

export default function RecommendationInsights() {
    const [runs, setRuns] = useState<DailyProductRunResponse[]>([])
    const [history, setHistory] = useState<RecommendationHistoryItem[]>([])
    const [stats, setStats] = useState<RecommendationStrategyStat[]>([])
    const [learnedWeights, setLearnedWeights] = useState<Record<string, number> | null>(null)
    const [evalRun, setEvalRun] = useState<RecommendationEvalRun | null>(null)
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)

    const load = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const [runsResp, historyResp, statsResp] = await Promise.all([
                api.listDailyProductRuns(10),
                api.listRecommendationHistory(30),
                api.getRecommendationStrategyStats(30),
            ])
            const evalResp = await api.getLatestRecommendationEvalRun()
            setRuns(runsResp.runs || [])
            setHistory(historyResp.items || [])
            setStats(statsResp.stats || [])
            setLearnedWeights(statsResp.learned_weights || null)
            setEvalRun(evalResp.run || null)
        } catch (e) {
            setError(e instanceof Error ? e.message : '加载选股统计失败')
        } finally {
            setLoading(false)
        }
    }, [])

    const refreshStats = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            // Run sequentially to reduce SQLite write-lock contention.
            await api.refreshRecommendationStrategyStats()
            await api.refreshRecommendationEvalRun()
            await load()
        } catch (e) {
            setError(e instanceof Error ? e.message : '刷新统计失败')
        } finally {
            setLoading(false)
        }
    }, [load])

    useEffect(() => {
        void load()
    }, [load])

    return (
        <div className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">选股统计</h1>
                    <p className="text-sm text-slate-500 dark:text-slate-400">查看批跑历史、扫描结果与策略学习后的权重变化。</p>
                </div>
                <div className="flex items-center gap-2">
                    <button
                        type="button"
                        onClick={load}
                        disabled={loading}
                        className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
                    >
                        <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
                        刷新
                    </button>
                    <button
                        type="button"
                        onClick={refreshStats}
                        disabled={loading}
                        className="inline-flex items-center gap-1 rounded-lg bg-violet-600 px-3 py-1.5 text-sm text-white hover:bg-violet-700 disabled:opacity-60"
                    >
                        <BrainCircuit className="h-4 w-4" />
                        刷新学习统计
                    </button>
                </div>
            </div>

            {error && (
                <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-600">
                    {error}
                </div>
            )}

            <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
                <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
                    <div className="mb-2 flex items-center gap-2 text-sm font-medium text-slate-700 dark:text-slate-200">
                        <BarChart3 className="h-4 w-4 text-violet-500" />
                        策略学习后权重
                    </div>
                    {learnedWeights && Object.keys(learnedWeights).length > 0 ? (
                        <div className="space-y-2">
                            {Object.entries(learnedWeights).map(([k, v]) => {
                                const labels: Record<string, string> = {
                                    momentum: '动量', activity: '活跃', near_high: '逼高',
                                    sector: '板块', volume_ratio: '量比',
                                }
                                return (
                                    <div key={k} className="flex items-center gap-2">
                                        <span className="w-14 text-xs text-slate-500">{labels[k] ?? k}</span>
                                        <div className="flex-1 h-2 rounded-full bg-slate-100 dark:bg-slate-700">
                                            <div className="h-full rounded-full bg-violet-500"
                                                style={{ width: `${Math.min(100, (v * 100 / 0.5) * 100)}%` }} />
                                        </div>
                                        <span className="w-10 text-right text-xs text-slate-600 dark:text-slate-300">
                                            {(v * 100).toFixed(1)}%
                                        </span>
                                    </div>
                                )
                            })}
                        </div>
                    ) : (
                        <div className="text-sm text-slate-500">暂无学习权重（需积累回测样本）</div>
                    )}
                </div>
                <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
                    <div className="text-sm font-medium text-slate-700 dark:text-slate-200">历史批跑</div>
                    <div className="mt-2 text-2xl font-semibold text-slate-900 dark:text-slate-100">{runs.length}</div>
                </div>
                <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
                    <div className="text-sm font-medium text-slate-700 dark:text-slate-200">历史扫描样本</div>
                    <div className="mt-2 text-2xl font-semibold text-slate-900 dark:text-slate-100">{history.length}</div>
                </div>
            </div>

            <div className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
                <div className="border-b border-slate-200 px-4 py-3 text-sm font-medium text-slate-700 dark:border-slate-700 dark:text-slate-200">
                    A/B 实测对比（普通 vs 实验）
                </div>
                {evalRun?.summary?.groups ? (
                    <div className="grid grid-cols-1 gap-3 px-4 py-3 md:grid-cols-2">
                        {(['baseline', 'variant'] as const).map(group => {
                            const g = evalRun.summary.groups[group]
                            return (
                                <div key={group} className="rounded-lg border border-slate-200 p-3 text-sm dark:border-slate-700">
                                    <div className="font-medium text-slate-900 dark:text-slate-100">
                                        {group === 'baseline' ? `基线：${evalRun.baseline_profile}` : `实验：${evalRun.variant_profile}`}
                                    </div>
                                    <div className="mt-1 text-xs text-slate-500">
                                        样本 {g.sample_count} · hit@k {g.hit_at_k_pct == null ? '--' : `${g.hit_at_k_pct.toFixed(2)}%`}
                                    </div>
                                    <div className="mt-1 text-xs text-slate-500">
                                        平均收益 {g.avg_return_pct == null ? '--' : `${g.avg_return_pct.toFixed(2)}%`} · 超额 {g.avg_excess_return_pct == null ? '--' : `${g.avg_excess_return_pct.toFixed(2)}%`}
                                    </div>
                                    <div className="mt-1 text-xs text-slate-500">
                                        回撤 P95 {g.max_drawdown_p95_pct == null ? '--' : `${g.max_drawdown_p95_pct.toFixed(2)}%`}
                                    </div>
                                </div>
                            )
                        })}
                        <div className="md:col-span-2 rounded-lg border border-slate-200 p-3 text-xs dark:border-slate-700">
                            <div className="font-medium text-slate-700 dark:text-slate-200">
                                默认切换门控：{evalRun.gate?.allow_switch_default ? '允许' : '不允许'}
                            </div>
                            <div className="mt-1 text-slate-500">
                                原因：{(evalRun.gate?.reasons || []).join(' / ') || '通过所有门控'}
                            </div>
                        </div>
                    </div>
                ) : (
                    <div className="px-4 py-8 text-center text-sm text-slate-500">
                        暂无 A/B 评估数据，点击「刷新学习统计」将自动生成最新快照。
                    </div>
                )}
            </div>

            <div className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
                <div className="border-b border-slate-200 px-4 py-3 text-sm font-medium text-slate-700 dark:border-slate-700 dark:text-slate-200">
                    历史批跑看板
                </div>
                <div className="divide-y divide-slate-100 dark:divide-slate-800">
                    {runs.map(run => (
                        <div key={run.run_id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-sm">
                            <div>
                                <div className="font-medium text-slate-900 dark:text-slate-100">{run.mode} · {run.status}</div>
                                <div className="text-xs text-slate-500">
                                    {run.run_id.slice(0, 8)} · 来源 {String(run.summary?.recommendation_source || '-')}
                                </div>
                            </div>
                            <div className="text-xs text-slate-500">
                                目标 {String(run.summary?.total_targets || 0)} · 完成 {String(run.summary?.completed_jobs || 0)}
                            </div>
                        </div>
                    ))}
                    {runs.length === 0 && <div className="px-4 py-8 text-center text-sm text-slate-500">暂无批跑历史</div>}
                </div>
            </div>

            <div className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
                <div className="border-b border-slate-200 px-4 py-3 text-sm font-medium text-slate-700 dark:border-slate-700 dark:text-slate-200">
                    历史扫描结果
                </div>
                <div className="divide-y divide-slate-100 dark:divide-slate-800">
                    {history.map(item => (
                        <div key={item.id} className="px-4 py-3 text-sm">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                                <div className="font-medium text-slate-900 dark:text-slate-100">
                                    {item.name} <span className="text-xs text-slate-400">{item.symbol}</span>
                                </div>
                                <div className="text-xs text-slate-500">
                                    rank {item.rank ?? '-'} · score {item.score ?? '-'} · {item.feedback_status}
                                </div>
                            </div>
                            <div className="mt-1 text-xs text-slate-500">
                                {item.strategy_hits.join(' / ') || '无策略标签'}
                            </div>
                            <div className="mt-1 text-xs text-slate-500">
                                收益 {item.realized_return_pct == null ? '--' : `${item.realized_return_pct.toFixed(2)}%`} ·
                                入场 {item.entry_price ?? '--'} · 出场 {item.exit_price ?? '--'}
                            </div>
                        </div>
                    ))}
                    {history.length === 0 && <div className="px-4 py-8 text-center text-sm text-slate-500">暂无扫描历史</div>}
                </div>
            </div>

            <div className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
                <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
                    <div className="text-sm font-medium text-slate-700 dark:text-slate-200">
                        策略效果统计（仅用于扫描器候选排序）
                    </div>
                    <div className="mt-1 text-xs text-slate-500">
                        权重增量只调整扫描器的因子权重（上限 ±0.08），影响的是候选排序。
                        它不参与深度分析的次日方向判断，因此这里的胜率不能当作 T+1 研判能力的证据。
                    </div>
                </div>
                <div className="divide-y divide-slate-100 dark:divide-slate-800">
                    {stats.map(item => (
                        <div key={item.strategy_key} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-sm">
                            <div>
                                <div className="font-medium text-slate-900 dark:text-slate-100">{item.strategy_key}</div>
                                <div className="text-xs text-slate-500">归属因子：{item.factor_bucket}</div>
                            </div>
                            <div className="text-xs text-slate-500">
                                样本 {item.sample_count} · 胜率 {item.win_rate ?? '--'}% · 收益 {item.avg_return_pct ?? '--'}% · 权重增量 {item.weight_delta ?? '--'}
                            </div>
                        </div>
                    ))}
                    {stats.length === 0 && <div className="px-4 py-8 text-center text-sm text-slate-500">暂无策略统计</div>}
                </div>
            </div>
        </div>
    )
}

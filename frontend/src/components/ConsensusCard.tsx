import { useMemo, useState } from 'react'
import { ChevronDown, ChevronUp, Scale, ShieldAlert, Swords, Waves } from 'lucide-react'
import type { ConsensusSummary } from '@/types'

interface ConsensusCardProps {
    summary?: ConsensusSummary | null
}

function metricTone(value: number): string {
    if (value >= 75) return 'text-emerald-600 dark:text-emerald-400'
    if (value >= 45) return 'text-amber-600 dark:text-amber-400'
    return 'text-rose-600 dark:text-rose-400'
}

export default function ConsensusCard({ summary }: ConsensusCardProps) {
    const [expanded, setExpanded] = useState(false)

    const topAgents = useMemo(
        () => (summary?.agent_breakdown || []).slice(0, 4),
        [summary?.agent_breakdown],
    )

    if (!summary) return null

    const executionTone = summary.execution_mode === 'direct'
        ? 'bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-500/10 dark:text-emerald-300 dark:border-emerald-500/20'
        : summary.execution_mode === 'conditional'
            ? 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/20'
            : 'bg-slate-50 text-slate-700 border-slate-200 dark:bg-slate-800/70 dark:text-slate-300 dark:border-slate-700'

    return (
        <div className="card overflow-hidden">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                <div className="space-y-2">
                    <div className="flex items-center gap-2">
                        <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-violet-100 text-violet-600 dark:bg-violet-500/15 dark:text-violet-300">
                            <Scale className="h-5 w-5" />
                        </div>
                        <div>
                            <h3 className="text-base font-semibold text-slate-900 dark:text-slate-100">显式共识层</h3>
                            <p className="text-sm text-slate-500 dark:text-slate-400">
                                {summary.summary || `${summary.consensus_direction}，建议${summary.execution_mode_label}`}
                            </p>
                        </div>
                    </div>
                    <div className="flex flex-wrap gap-2">
                        <span className="inline-flex items-center rounded-full border border-slate-200 px-3 py-1 text-xs font-medium text-slate-600 dark:border-slate-700 dark:text-slate-300">
                            共识方向：{summary.consensus_direction}
                        </span>
                        <span className={`inline-flex items-center rounded-full border px-3 py-1 text-xs font-medium ${executionTone}`}>
                            执行模式：{summary.execution_mode_label}
                        </span>
                        {summary.horizon_conflict && (
                            <span className="inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-3 py-1 text-xs font-medium text-amber-700 dark:border-amber-500/20 dark:bg-amber-500/10 dark:text-amber-300">
                                短中线存在分歧
                            </span>
                        )}
                        {(summary.unparsed_analyst_n ?? 0) > 0 && (
                            <span className="inline-flex items-center rounded-full border border-rose-200 bg-rose-50 px-3 py-1 text-xs font-medium text-rose-700 dark:border-rose-500/20 dark:bg-rose-500/10 dark:text-rose-300">
                                {summary.unparsed_analyst_n} 份报告解析失败，未计入共识
                            </span>
                        )}
                    </div>
                </div>

                <div className="grid min-w-[280px] grid-cols-2 gap-3 lg:w-[360px]">
                    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-700 dark:bg-slate-800/60">
                        <div className="flex items-center gap-1 text-xs text-slate-500 dark:text-slate-400">
                            <Waves className="h-3.5 w-3.5" />
                            共识强度
                        </div>
                        <div className={`mt-1 text-2xl font-bold ${metricTone(summary.consensus_strength)}`}>{summary.consensus_strength}</div>
                    </div>
                    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-700 dark:bg-slate-800/60">
                        <div className="flex items-center gap-1 text-xs text-slate-500 dark:text-slate-400">
                            <Swords className="h-3.5 w-3.5" />
                            分歧等级
                        </div>
                        <div className={`mt-1 text-2xl font-bold ${metricTone(100 - summary.disagreement_score)}`}>{summary.disagreement_score}</div>
                    </div>
                    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-700 dark:bg-slate-800/60">
                        <div className="flex items-center gap-1 text-xs text-slate-500 dark:text-slate-400">
                            <ShieldAlert className="h-3.5 w-3.5" />
                            稳定性
                        </div>
                        <div className={`mt-1 text-2xl font-bold ${metricTone(summary.stability_score)}`}>{summary.stability_score}</div>
                    </div>
                    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 dark:border-slate-700 dark:bg-slate-800/60">
                        <div className="text-xs text-slate-500 dark:text-slate-400">主导期限</div>
                        <div className="mt-1 text-2xl font-bold text-slate-800 dark:text-slate-100">
                            {summary.dominant_horizon === 'medium' ? '中线' : '短线'}
                        </div>
                    </div>
                </div>
            </div>

            <div className="mt-4 grid gap-3 lg:grid-cols-[1.1fr_0.9fr]">
                <div className="rounded-xl border border-slate-200 p-4 dark:border-slate-700">
                    <div className="mb-2 text-sm font-medium text-slate-700 dark:text-slate-200">主导观点</div>
                    <div className="flex flex-wrap gap-2">
                        {topAgents.length > 0 ? topAgents.map((item) => (
                            <span
                                key={`${item.agent}-${item.horizon}`}
                                className="inline-flex items-center rounded-full border border-slate-200 px-3 py-1 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300"
                            >
                                {item.label} · {item.verdict}
                            </span>
                        )) : (
                            <span className="text-sm text-slate-500 dark:text-slate-400">暂无可计算的分析师分解</span>
                        )}
                    </div>
                </div>
                <div className="rounded-xl border border-slate-200 p-4 dark:border-slate-700">
                    <div className="mb-2 text-sm font-medium text-slate-700 dark:text-slate-200">关键翻转条件</div>
                    <div className="flex flex-wrap gap-2">
                        {(summary.flip_conditions || []).length > 0 ? (summary.flip_conditions || []).map((item) => (
                            <span
                                key={item}
                                className="inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-3 py-1 text-xs text-amber-700 dark:border-amber-500/20 dark:bg-amber-500/10 dark:text-amber-300"
                            >
                                {item}
                            </span>
                        )) : (
                            <span className="text-sm text-slate-500 dark:text-slate-400">暂无额外触发条件</span>
                        )}
                    </div>
                </div>
            </div>

            {!!summary.agent_breakdown?.length && (
                <div className="mt-4">
                    <button
                        onClick={() => setExpanded(prev => !prev)}
                        className="inline-flex items-center gap-1 rounded-lg bg-slate-100 px-3 py-1.5 text-sm text-slate-600 transition-colors hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
                    >
                        {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
                        {expanded ? '收起分解' : '查看分析师分解'}
                    </button>

                    {expanded && (
                        <div className="mt-3 overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-700">
                            <table className="w-full text-sm">
                                <thead className="bg-slate-50 dark:bg-slate-800/60">
                                    <tr className="text-left text-xs text-slate-500 dark:text-slate-400">
                                        <th className="px-4 py-3 font-medium">维度</th>
                                        <th className="px-4 py-3 font-medium">期限</th>
                                        <th className="px-4 py-3 font-medium">方向</th>
                                        <th className="px-4 py-3 font-medium">置信度</th>
                                        <th className="px-4 py-3 font-medium">贡献</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {summary.agent_breakdown.map((item) => (
                                        <tr key={`${item.agent}-${item.horizon}-${item.verdict}`} className="border-t border-slate-200 dark:border-slate-700">
                                            <td className="px-4 py-3 text-slate-700 dark:text-slate-200">{item.label}</td>
                                            <td className="px-4 py-3 text-slate-500 dark:text-slate-400">{item.horizon === 'medium' ? '中线' : '短线'}</td>
                                            <td className="px-4 py-3 text-slate-700 dark:text-slate-200">{item.verdict}</td>
                                            <td className="px-4 py-3 text-slate-500 dark:text-slate-400">{item.confidence}%</td>
                                            <td className="px-4 py-3 text-slate-500 dark:text-slate-400">{item.contribution.toFixed(2)}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </div>
            )}
        </div>
    )
}

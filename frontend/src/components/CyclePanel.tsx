import { useCallback, useEffect, useState } from 'react'
import { api } from '@/services/api'
import type { MainlineCapability, MainlineCycle, MainlineDailyLog, MainlineDecision, MainlineRotationDay, MainlineTradeCandidate } from '@/types'

const STAGE_STYLE: Record<string, string> = {
    '发酵': 'bg-cyan-100 text-cyan-700 dark:bg-cyan-500/10 dark:text-cyan-300',
    '主升': 'bg-red-100 text-red-700 dark:bg-red-500/10 dark:text-red-300',
    '高位分歧': 'bg-amber-100 text-amber-700 dark:bg-amber-500/10 dark:text-amber-300',
    '退潮': 'bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300',
    '已终结': 'bg-slate-200 text-slate-500 dark:bg-slate-700 dark:text-slate-300',
    '数据不足': 'bg-slate-100 text-slate-500 dark:bg-slate-700 dark:text-slate-300',
    '发酵(待确认)': 'bg-cyan-100 text-cyan-700 dark:bg-cyan-500/10 dark:text-cyan-300',
}
const ACTION_STYLE: Record<string, string> = {
    '布局': 'text-cyan-700 dark:text-cyan-300',
    '持有': 'text-emerald-700 dark:text-emerald-300',
    '减仓': 'text-amber-700 dark:text-amber-300',
    '规避': 'text-red-700 dark:text-red-300',
    '观察': 'text-slate-500 dark:text-slate-400',
}

function u(v: unknown): string {
    if (v === null || v === undefined) return '-'
    if (typeof v === 'number') return String(Math.round(v * 100) / 100)
    return String(v)
}

export default function CyclePanel({ tradeDate }: { tradeDate: string }) {
    const [cycles, setCycles] = useState<MainlineCycle[]>([])
    const [decisions, setDecisions] = useState<MainlineDecision[]>([])
    const [candidates, setCandidates] = useState<MainlineTradeCandidate[]>([])
    const [capability, setCapability] = useState<MainlineCapability | null>(null)
    const [rotation, setRotation] = useState<MainlineRotationDay[]>([])
    const [dailyLog, setDailyLog] = useState<MainlineDailyLog | null>(null)
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState('')
    const [notice, setNotice] = useState('')
    const [selectedKey, setSelectedKey] = useState<string | null>(null)
    const [track, setTrack] = useState<MainlineCycle | null>(null)

    const load = useCallback(async () => {
        try {
            const [cy, de, ca, cap, rot, log] = await Promise.all([
                api.getMainlineCycles('active', 50),
                api.getMainlineDecisions(30, 100),
                api.getMainlineAutodiveRuns(tradeDate || undefined),
                api.getMainlineCapability(90),
                api.getMainlineRotation(30),
                api.getMainlineDailyLog(tradeDate || undefined),
            ])
            setCycles(cy.items || [])
            setDecisions(de.items || [])
            setCandidates(ca.items || [])
            setCapability(cap)
            setRotation(rot.timeline || [])
            setDailyLog(log)
        } catch (e) {
            setError(`周期数据加载失败：${e instanceof Error ? e.message : String(e)}`)
        }
    }, [tradeDate])

    useEffect(() => {
        void load()
    }, [load])

    const handleAutodive = async () => {
        setLoading(true)
        setError('')
        try {
            const res = await api.runMainlineAutodive(tradeDate || undefined)
            setNotice(`自动深挖完成：可买入 ${res.buyable_count ?? '?'} 只，创建任务 ${(res.created_tasks as unknown[] | undefined)?.length ?? 0} 个`)
            await load()
        } catch (e) {
            setError(`自动深挖失败：${e instanceof Error ? e.message : String(e)}`)
        } finally {
            setLoading(false)
        }
    }

    const handleShowTrack = async (key: string) => {
        try {
            const t = await api.getMainlineCycleTrack(key)
            setTrack(t)
            setSelectedKey(key)
        } catch (e) {
            setError(`轨迹加载失败：${e instanceof Error ? e.message : String(e)}`)
        }
    }

    const deepDivePending = candidates.filter((c) => c.deep_dive_status === 'queued' || c.deep_dive_status === 'running').length

    return (
        <div className="space-y-4">
            {error && <p className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600 dark:bg-red-500/10 dark:text-red-300">{error}</p>}
            {notice && <p className="rounded-lg bg-emerald-50 px-3 py-2 text-xs text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-300">{notice}</p>}

            {/* 能力曲线 KPI */}
            {capability && (
                <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                    <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-800">
                        <p className="text-xs text-slate-500 dark:text-slate-400">已验证决策</p>
                        <p className="mt-1 text-xl font-bold text-slate-900 dark:text-slate-100">{capability.evaluated}</p>
                    </div>
                    <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-800">
                        <p className="text-xs text-slate-500 dark:text-slate-400">决策命中率</p>
                        <p className="mt-1 text-xl font-bold text-slate-900 dark:text-slate-100">
                            {capability.hit_rate != null ? `${(capability.hit_rate * 100).toFixed(1)}%` : '-'}
                        </p>
                    </div>
                    <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-800">
                        <p className="text-xs text-slate-500 dark:text-slate-400">深挖任务进行中</p>
                        <p className="mt-1 text-xl font-bold text-slate-900 dark:text-slate-100">{deepDivePending}</p>
                    </div>
                    <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-800">
                        <p className="text-xs text-slate-500 dark:text-slate-400">可买入标的</p>
                        <p className="mt-1 text-xl font-bold text-slate-900 dark:text-slate-100">{candidates.filter((c) => c.buyable).length}</p>
                    </div>
                </div>
            )}

            {/* 操作栏 */}
            <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs text-slate-500 dark:text-slate-400">
                    主线周期 = 历史轨迹分析（持续天数/强度动量/拥挤度/退潮三因子）
                </p>
                <button
                    onClick={() => void handleAutodive()}
                    disabled={loading}
                    className="rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-blue-700 disabled:opacity-50"
                >
                    {loading ? '执行中…' : '执行自动深挖'}
                </button>
            </div>

            {/* 活跃主线周期决策卡 */}
            <div className="grid gap-3 md:grid-cols-2">
                {cycles.map((c) => (
                    <div key={c.mainline_key} className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-800">
                        <div className="flex flex-wrap items-center gap-2">
                            <button
                                onClick={() => void handleShowTrack(c.mainline_key)}
                                className="text-sm font-bold text-slate-900 hover:text-blue-600 dark:text-slate-100"
                                title="点击查看轨迹"
                            >
                                {c.mainline_key}
                            </button>
                            <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${STAGE_STYLE[c.cycle_position || ''] || 'bg-slate-100 text-slate-600'}`}>
                                {c.cycle_position || '未知'}
                            </span>
                            <span className={`text-[11px] font-semibold ${ACTION_STYLE[c.action || ''] || ''}`}>
                                {c.action} {c.position_pct ? `${(c.position_pct * 100).toFixed(0)}%` : ''}
                            </span>
                            <span className="ml-auto text-[11px] text-slate-400">进度 {c.progress ?? '-'}% · 已{c.daily_track?.length ?? 0}天</span>
                        </div>
                        {c.cycle_reason && <p className="mt-2 text-xs leading-5 text-slate-500 dark:text-slate-400">{c.cycle_reason}</p>}
                        {c.alerts && c.alerts.length > 0 && (
                            <div className="mt-2 space-y-1">
                                {c.alerts.map((a, i) => (
                                    <p key={i} className="rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-700 dark:bg-amber-500/10 dark:text-amber-300">
                                        ⚠ {a}
                                    </p>
                                ))}
                            </div>
                        )}
                        {c.peak_strength != null && (
                            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700">
                                <div className="h-full rounded-full bg-gradient-to-r from-blue-500 to-indigo-500" style={{ width: `${Math.min(100, Math.max(0, c.peak_strength))}%` }} />
                            </div>
                        )}
                    </div>
                ))}
                {cycles.length === 0 && (
                    <p className="rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-400 dark:border-slate-700 dark:bg-slate-800">
                        暂无活跃主线档案——生成主线报告并运行周期分析后出现
                    </p>
                )}
            </div>

            {/* 选中主线的轨迹 */}
            {selectedKey && track && (
                <details open className="rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
                    <summary className="cursor-pointer border-b border-slate-200 px-4 py-3 text-sm font-semibold text-slate-900 dark:border-slate-700 dark:text-slate-100">
                        {selectedKey} 历史轨迹（{track.daily_track?.length ?? 0} 天）
                    </summary>
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm">
                            <thead>
                                <tr className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
                                    <th className="px-4 py-2 font-medium">日期</th>
                                    <th className="px-4 py-2 font-medium">强度</th>
                                    <th className="px-4 py-2 font-medium">热度</th>
                                    <th className="px-4 py-2 font-medium">RS20</th>
                                    <th className="px-4 py-2 font-medium">5日资金</th>
                                    <th className="px-4 py-2 font-medium">RSI</th>
                                    <th className="px-4 py-2 font-medium">规则预判</th>
                                </tr>
                            </thead>
                            <tbody>
                                {(track.daily_track || []).map((s: Record<string, unknown>, i: number) => (
                                    <tr key={i} className="border-b border-slate-100 last:border-0 dark:border-slate-700/50">
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{String(s.date || '')}</td>
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{u(s.strength)}</td>
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{u(s.heat)}</td>
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{s.rs20 != null ? `${(Number(s.rs20) * 100).toFixed(1)}%` : '-'}</td>
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{u(s.net_inflow_5d)}</td>
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{u(s.rsi14)}</td>
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{String(s.phase_hint || '')}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </details>
            )}

            {/* 决策卡列表 */}
            {decisions.length > 0 && (
                <details className="rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
                    <summary className="cursor-pointer border-b border-slate-200 px-4 py-3 text-sm font-semibold text-slate-900 dark:border-slate-700 dark:text-slate-100">
                        决策卡（近 30 日）
                    </summary>
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm">
                            <thead>
                                <tr className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
                                    <th className="px-4 py-2 font-medium">日期</th>
                                    <th className="px-4 py-2 font-medium">主线</th>
                                    <th className="px-4 py-2 font-medium">周期</th>
                                    <th className="px-4 py-2 font-medium">动作</th>
                                    <th className="px-4 py-2 font-medium">结果</th>
                                    <th className="px-4 py-2 font-medium">验证条件</th>
                                </tr>
                            </thead>
                            <tbody>
                                {decisions.slice(0, 20).map((d) => (
                                    <tr key={`${d.trade_date}-${d.mainline_key}`} className="border-b border-slate-100 last:border-0 dark:border-slate-700/50">
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{d.trade_date}</td>
                                        <td className="px-4 py-2 text-xs font-medium text-slate-900 dark:text-slate-100">{d.mainline_key}</td>
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{d.stage}</td>
                                        <td className={`px-4 py-2 text-xs font-medium ${ACTION_STYLE[d.action || ''] || ''}`}>{d.action}</td>
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">
                                            {d.outcome === 'verified' ? '✅' : d.outcome === 'falsified' ? '❌' : '-'}
                                            {d.outcome_note ? ` ${d.outcome_note}` : ''}
                                        </td>
                                        <td className="max-w-[220px] px-4 py-2 text-[11px] leading-4 text-slate-400">{(d.verify_conditions || []).slice(0, 2).join('；')}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </details>
            )}

            {/* 自动深挖任务 */}
            <div className="rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
                <div className="border-b border-slate-200 px-4 py-3 dark:border-slate-700">
                    <span className="text-sm font-semibold text-slate-900 dark:text-slate-100">自动深挖（可买入 → 个股深度分析）</span>
                </div>
                {candidates.length === 0 ? (
                    <p className="px-4 py-6 text-center text-sm text-slate-400">暂无候选记录——执行自动深挖后出现</p>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="w-full text-left text-sm">
                            <thead>
                                <tr className="border-b border-slate-200 text-xs text-slate-500 dark:border-slate-700 dark:text-slate-400">
                                    <th className="px-4 py-2 font-medium">标的</th>
                                    <th className="px-4 py-2 font-medium">主线</th>
                                    <th className="px-4 py-2 font-medium">可买入</th>
                                    <th className="px-4 py-2 font-medium">档位</th>
                                    <th className="px-4 py-2 font-medium">介入时机</th>
                                    <th className="px-4 py-2 font-medium">深挖状态</th>
                                </tr>
                            </thead>
                            <tbody>
                                {candidates.slice(0, 20).map((c) => (
                                    <tr key={c.id || `${c.symbol}-${c.mainline_key}`} className="border-b border-slate-100 last:border-0 dark:border-slate-700/50">
                                        <td className="px-4 py-2 text-xs font-medium text-slate-900 dark:text-slate-100">{c.name}({c.symbol})</td>
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{c.mainline_key}</td>
                                        <td className="px-4 py-2 text-xs">{c.buyable ? '✅' : '❌'}</td>
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">{c.position_tier || '-'}</td>
                                        <td className="max-w-[200px] px-4 py-2 text-[11px] text-amber-700 dark:text-amber-300">{c.timing || '-'}</td>
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-300">
                                            {c.deep_dive_status === 'completed' ? `✅ ${c.deep_dive_report_id ? '报告已生成' : ''}` : c.deep_dive_status}
                                            {c.deep_dive_error ? `（${c.deep_dive_error.slice(0, 40)}）` : ''}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </div>

            {/* 主线轮动时间线 */}
            {rotation.length > 0 && (
                <details className="rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
                    <summary className="cursor-pointer border-b border-slate-200 px-4 py-3 text-sm font-semibold text-slate-900 dark:border-slate-700 dark:text-slate-100">
                        主线轮动回顾（近 30 日）
                    </summary>
                    <div className="space-y-2 px-4 py-3">
                        {rotation.slice(0, 15).map((day) => (
                            <div key={day.date} className="flex flex-wrap items-center gap-2 text-xs">
                                <span className="w-24 shrink-0 font-medium text-slate-500 dark:text-slate-400">{day.date}</span>
                                {day.mainlines.map((m) => (
                                    <span key={`${day.date}-${m.key}`} className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600 dark:bg-slate-700 dark:text-slate-300">
                                        {m.key} <span className={ACTION_STYLE[m.action || ''] || ''}>{m.stage}</span>
                                    </span>
                                ))}
                            </div>
                        ))}
                    </div>
                </details>
            )}

            {/* 作战日志 */}
            {dailyLog && dailyLog.report_id && (
                <details className="rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-700 dark:bg-slate-800">
                    <summary className="cursor-pointer border-b border-slate-200 px-4 py-3 text-sm font-semibold text-slate-900 dark:border-slate-700 dark:text-slate-100">
                        📋 主线作战日志（{dailyLog.trade_date}）
                    </summary>
                    <div className="space-y-3 px-4 py-3 text-sm">
                        <div>
                            <p className="text-xs font-medium text-slate-500 dark:text-slate-400">今日主线决策</p>
                            {dailyLog.mainlines.map((m, i) => (
                                <p key={i} className="mt-1 text-xs text-slate-700 dark:text-slate-300">
                                    {m.key} [{m.stage}] 动作:{m.action} {m.position_pct ? `仓位${(m.position_pct * 100).toFixed(0)}%` : ''}
                                </p>
                            ))}
                        </div>
                        {dailyLog.retreat_alerts.length > 0 && (
                            <div>
                                <p className="text-xs font-medium text-amber-600 dark:text-amber-400">退潮预警</p>
                                {dailyLog.retreat_alerts.map((r, i) => (
                                    <p key={i} className="mt-1 text-xs text-amber-700 dark:text-amber-300">
                                        ⚠ {r.key}: {r.alerts.join('；')}
                                    </p>
                                ))}
                            </div>
                        )}
                        <div>
                            <p className="text-xs font-medium text-emerald-600 dark:text-emerald-400">可买入（自动深挖）</p>
                            {dailyLog.buyable.map((b, i) => (
                                <p key={i} className="mt-1 text-xs text-slate-700 dark:text-slate-300">
                                    {b.name}({b.symbol}) [{b.tier || '-'}] 深挖:{b.deep_dive_status}
                                </p>
                            ))}
                        </div>
                    </div>
                </details>
            )}
        </div>
    )
}

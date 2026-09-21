/**
 * 判定历史与后续走势回看。
 *
 * ## 为什么必须有这个视图（审计 9.5）
 *
 * 后端 `/v1/signals/verdicts` 返回判定及其 T+3/T+5 兑现结果，是校准闭环的对外
 * 窗口；但**前端没有任何组件消费它**——`api.listVerdicts` 写了却无调用方。
 * 于是「判定可回看后续实际走势」在产品上等于不存在，校准也就没人看得见。
 *
 * ## 展示上的两条纪律
 *
 * 1. **不可评估 ≠ 未兑现**：停牌导致的窗口不完整既不算命中也不算未命中，
 *    必须显示为「不可评估」并给出原因，否则用户会把它读成"模型错了"。
 * 2. **待回填 ≠ 失败**：T+3/T+5 还没到时间时显示「待回填」。
 */

import { History, Loader2, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

import { outcomeRows } from '@/components/verdictPresentation'
import { api } from '@/services/api'
import type { VerdictHistoryItem } from '@/types'

const TONE_CLASS: Record<string, string> = {
    bullish: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-500/20 dark:text-emerald-300',
    bearish: 'bg-rose-100 text-rose-700 dark:bg-rose-500/20 dark:text-rose-300',
    warning: 'bg-amber-100 text-amber-700 dark:bg-amber-500/20 dark:text-amber-300',
    neutral: 'bg-slate-100 text-slate-700 dark:bg-slate-500/20 dark:text-slate-300',
    muted: 'bg-slate-100 text-slate-500 dark:bg-slate-600/20 dark:text-slate-400',
}

interface Props {
    symbol?: string
    limit?: number
    className?: string
}

export default function VerdictHistoryPanel({ symbol, limit = 20, className = '' }: Props) {
    const [items, setItems] = useState<VerdictHistoryItem[]>([])
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [backfilling, setBackfilling] = useState(false)

    const load = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const payload = await api.listVerdicts({ symbol, limit })
            setItems(payload.items ?? [])
        } catch (err) {
            setError(err instanceof Error ? err.message : '加载失败')
        } finally {
            setLoading(false)
        }
    }, [symbol, limit])

    useEffect(() => {
        void load()
    }, [load])

    const handleBackfill = async () => {
        setBackfilling(true)
        try {
            await api.triggerBackfill()
            await load()
        } catch (err) {
            setError(err instanceof Error ? err.message : '回填失败')
        } finally {
            setBackfilling(false)
        }
    }

    return (
        <section
            className={`rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900 ${className}`}
            data-testid="verdict-history"
        >
            <header className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <History className="h-4 w-4 text-slate-500" />
                    <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">
                        判定历史与后续走势
                    </h2>
                    <span className="text-xs text-slate-500 dark:text-slate-400">
                        判定不可变；下方为判定后 T+3 / T+5 的实际兑现情况
                    </span>
                </div>
                <div className="flex items-center gap-2">
                    <button
                        type="button"
                        onClick={() => void load()}
                        className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700 dark:hover:text-slate-300"
                    >
                        {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
                        刷新
                    </button>
                    <button
                        type="button"
                        onClick={handleBackfill}
                        disabled={backfilling}
                        className="text-xs text-blue-600 hover:text-blue-700 disabled:opacity-50 dark:text-blue-400"
                    >
                        {backfilling ? '回填中…' : '触发回填'}
                    </button>
                </div>
            </header>

            {error && (
                <p className="mt-2 rounded bg-rose-50 p-2 text-xs text-rose-700 dark:bg-rose-500/10 dark:text-rose-300">
                    {error}
                </p>
            )}

            {!loading && items.length === 0 && !error && (
                <p className="mt-3 text-xs text-slate-500 dark:text-slate-400">
                    暂无已落库的判定。在判定卡片上点「记录以跟踪后续走势」后，这里会显示它的兑现情况。
                </p>
            )}

            <ul className="mt-3 space-y-2">
                {items.map((item) => (
                    <li
                        key={item.verdict_id}
                        className="rounded-lg border border-slate-100 p-2 dark:border-slate-800"
                        data-testid="verdict-history-item"
                    >
                        <div className="flex flex-wrap items-center gap-2 text-xs">
                            <span className="font-medium text-slate-800 dark:text-slate-100">{item.symbol}</span>
                            <span className="text-slate-500 dark:text-slate-400">{item.as_of}</span>
                            <span className={`rounded px-1.5 py-0.5 ${TONE_CLASS[item.state === 'insufficient_evidence' ? 'muted' : 'neutral']}`}>
                                {item.state_label}
                            </span>
                            <span className="text-slate-500 dark:text-slate-400">置信 {item.confidence}</span>
                            {item.daily_only && (
                                <span className="text-amber-600 dark:text-amber-400">仅日线</span>
                            )}
                            <span className="font-mono text-slate-400 dark:text-slate-500">
                                {item.signal_version}
                            </span>
                        </div>
                        <div className="mt-1 flex flex-wrap gap-2">
                            {outcomeRows(item).map((row, index) => (
                                <span
                                    key={`${row.horizonLabel}-${index}`}
                                    className={`rounded px-1.5 py-0.5 text-xs ${TONE_CLASS[row.tone]}`}
                                    title={row.refDate ? `参照日 ${row.refDate}` : undefined}
                                >
                                    {row.horizonLabel}：{row.label}
                                    {row.retPct !== null && `（${row.retPct > 0 ? '+' : ''}${row.retPct.toFixed(2)}%）`}
                                </span>
                            ))}
                        </div>
                    </li>
                ))}
            </ul>
        </section>
    )
}

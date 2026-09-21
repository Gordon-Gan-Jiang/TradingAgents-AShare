import { useEffect, useState } from 'react'
import { api } from '@/services/api'
import type { ExitAdviceDecision, ExitAdviceResponse } from '@/types'
import { AlertTriangle, Loader2, RefreshCw, ShieldAlert, TrendingDown, TrendingUp, Eye } from 'lucide-react'

/**
 * 持仓退出纪律面板（P0-3）。
 *
 * 产品的决策链一直是"买入导向"：研报给出方向与目标价，然后整条售后生命周期就断了——
 * 何时卖、卖多少、跌到哪算错，全部没有出口。这个面板把每份研报自己的交易计划
 * （硬止损 / 分批止盈 / 移动止盈 / 时间止损）变成可执行的提示。
 *
 * 注意：判断依据是该标的自己那份计划，而不是全局写死的 -6%/+10%；同时已考虑
 * A 股 T+1 可卖数量、涨跌停不可成交与停牌。文案明确标注"仅为纪律提示，不构成投资建议"。
 */

const ACTION_STYLE: Record<string, { cls: string; Icon: typeof TrendingDown }> = {
    清仓: { cls: 'bg-rose-50 text-rose-700 border-rose-200 dark:bg-rose-950/30 dark:text-rose-300 dark:border-rose-900/50', Icon: ShieldAlert },
    减仓: { cls: 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/30 dark:text-amber-300 dark:border-amber-900/50', Icon: TrendingDown },
    持有: { cls: 'bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/30 dark:text-emerald-300 dark:border-emerald-900/50', Icon: TrendingUp },
    观察: { cls: 'bg-slate-100 text-slate-600 border-slate-200 dark:bg-slate-800/60 dark:text-slate-300 dark:border-slate-700', Icon: Eye },
}

const PRIORITY_ZH: Record<string, string> = { high: '高', medium: '中', low: '低' }

function formatPrice(value?: number | null): string {
    return value == null ? '—' : String(value)
}

function DecisionRow({ item }: { item: ExitAdviceDecision }) {
    const style = ACTION_STYLE[item.action] || ACTION_STYLE['观察']
    const { Icon } = style
    // T+1 / 涨跌停 / 停牌 限制：这些是"信号对了但今天执行不了"的现实约束
    const blocking = item.constraints.filter(c => c.includes('T+1') || c.includes('停牌') || c.includes('涨跌停') || c.includes('涨停') || c.includes('跌停'))

    return (
        <div className="rounded-xl border border-slate-200 dark:border-slate-700 p-3 space-y-2">
            <div className="flex items-start justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2">
                    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full border text-xs font-medium ${style.cls}`}>
                        <Icon className="w-3 h-3" />
                        {item.action}
                    </span>
                    <span className="font-medium text-sm text-slate-800 dark:text-slate-100">
                        {item.name || item.symbol}
                    </span>
                    <span className="text-xs text-slate-400">{item.symbol}</span>
                    {(item.priority === 'high' || item.priority === 'medium') && (
                        <span className={`text-[10px] px-1.5 py-0.5 rounded ${
                            item.priority === 'high'
                                ? 'bg-rose-100 text-rose-600 dark:bg-rose-950/40 dark:text-rose-300'
                                : 'bg-amber-100 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300'
                        }`}>
                            {PRIORITY_ZH[item.priority]}优先级
                        </span>
                    )}
                </div>
                <div className="text-xs text-slate-500 dark:text-slate-400 tabular-nums text-right">
                    <div>现价 {formatPrice(item.price)}</div>
                    {item.pnl_pct != null && (
                        <div className={item.pnl_pct >= 0 ? 'text-rose-500' : 'text-emerald-500'}>
                            浮动 {item.pnl_pct >= 0 ? '+' : ''}{item.pnl_pct.toFixed(2)}%
                        </div>
                    )}
                </div>
            </div>

            {item.reasons.length > 0 && (
                <ul className="text-xs text-slate-600 dark:text-slate-300 space-y-0.5 list-disc list-inside">
                    {item.reasons.map((r, i) => <li key={i}>{r}</li>)}
                </ul>
            )}

            {item.action === '减仓' && item.suggested_shares > 0 && (
                <div className="text-xs text-slate-700 dark:text-slate-200">
                    建议减仓 <span className="font-semibold">{item.suggested_shares}</span> 股（约 {item.suggested_pct}% 仓位）
                </div>
            )}

            {blocking.length > 0 && (
                <div className="flex items-start gap-1 text-[11px] text-amber-600 dark:text-amber-400">
                    <AlertTriangle className="w-3 h-3 mt-0.5 flex-shrink-0" />
                    <span>{blocking.join('；')}</span>
                </div>
            )}

            {!item.plan_available && (
                <div className="text-[11px] text-slate-500 dark:text-slate-400">
                    该持仓暂无可用交易计划，先做一次深度分析即可得到止损/止盈锚点
                </div>
            )}
        </div>
    )
}

export default function ExitAdvicePanel() {
    const [data, setData] = useState<ExitAdviceResponse | null>(null)
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState<string | null>(null)

    const load = async () => {
        setLoading(true)
        setError(null)
        try {
            setData(await api.getExitAdvice())
        } catch (e) {
            setError(e instanceof Error ? e.message : '加载失败')
        } finally {
            setLoading(false)
        }
    }

    useEffect(() => { void load() }, [])

    const decisions = data?.decisions || []
    // 只把需要动作的排前面，持有/观察沉底，避免噪音
    const actionable = decisions.filter(d => d.action === '清仓' || d.action === '减仓')
    const others = decisions.filter(d => d.action !== '清仓' && d.action !== '减仓')
    const summary = data?.summary

    return (
        <div className="card space-y-3">
            <div className="flex items-center justify-between gap-2 flex-wrap">
                <div className="flex items-center gap-2">
                    <ShieldAlert className="w-5 h-5 text-rose-500" />
                    <h2 className="font-semibold text-slate-900 dark:text-slate-100">持仓退出纪律</h2>
                    {summary && summary.total > 0 && (
                        <span className="text-xs text-slate-500 dark:text-slate-400">
                            共 {summary.total} 只 · 需动作 {actionable.length} 只
                            {typeof summary.without_plan === 'number' && summary.without_plan > 0 && (
                                <> · {summary.without_plan} 只无计划</>
                            )}
                        </span>
                    )}
                </div>
                <button
                    type="button"
                    onClick={() => void load()}
                    disabled={loading}
                    className="inline-flex items-center gap-1 text-xs px-2.5 py-1.5 rounded-lg border border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-800 disabled:opacity-50"
                >
                    {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                    刷新
                </button>
            </div>

            <p className="text-xs text-slate-500 dark:text-slate-400">
                依据每份研报自己的交易计划（硬止损 / 分批止盈 / 移动止盈 / 时间止损）判断，并已考虑 T+1 可卖数量、涨跌停与停牌。
            </p>

            {error && (
                <div className="text-sm text-rose-600 dark:text-rose-300">{error}</div>
            )}

            {loading && !data && (
                <div className="flex items-center gap-2 text-sm text-slate-500 dark:text-slate-400">
                    <Loader2 className="w-4 h-4 animate-spin" /> 正在按交易计划评估持仓…
                </div>
            )}

            {!loading && !error && decisions.length === 0 && (
                <div className="text-sm text-slate-500 dark:text-slate-400">
                    暂无持仓。先导入持仓并完成一次深度分析，这里会给出持有 / 减仓 / 清仓的纪律化提示。
                </div>
            )}

            {actionable.length > 0 && (
                <div className="space-y-2">
                    {actionable.map(item => <DecisionRow key={`${item.symbol}-a`} item={item} />)}
                </div>
            )}

            {others.length > 0 && (
                <details className="pt-1">
                    <summary className="cursor-pointer text-xs text-slate-500 dark:text-slate-400">
                        其余 {others.length} 只持仓（持有 / 观察）
                    </summary>
                    <div className="space-y-2 mt-2">
                        {others.map(item => <DecisionRow key={`${item.symbol}-o`} item={item} />)}
                    </div>
                </details>
            )}

            {data?.summary?.label && (
                <p className="text-[11px] text-slate-400 dark:text-slate-500 border-t border-slate-100 dark:border-slate-800 pt-2">
                    {data.summary.label}
                </p>
            )}
        </div>
    )
}

/**
 * 判定卡片：把确定性判定的结构化结果渲染出来。
 *
 * ## 这个组件刻意做得很「薄」
 *
 * 所有判断（状态徽章、置信度档位、组件行、门控解释、调制器说明）都在
 * `verdictPresentation.ts` 里，且被单测覆盖。组件只负责把视图模型映射成
 * DOM，因此不存在「样式改了语义也变了」的风险。
 *
 * ## 三条必须让用户看到的东西
 *
 * 1. **弃权是一个结果**，不是弱化版观点：`证据不足` 的呈现与方向性判定显著不同
 * 2. **失效条件**：什么情况发生这个判定就错了——这是研究结论与观点的分界线
 * 3. **没覆盖到什么**：缺分时/筹码、门控撤回加分，都必须显式说明，
 *    否则用户会以为结论建立在他以为存在的证据上
 */

import { AlertTriangle, CheckCircle2, Info, Loader2, RefreshCw, ShieldAlert } from 'lucide-react'
import { useState } from 'react'

import { api } from '@/services/api'
import type { WashoutVerdict } from '@/types'
import { buildVerdictView } from '@/components/verdictPresentation'

interface VerdictCardProps {
    verdict: WashoutVerdict
    /** 触发重新评估（可选）。传入时显示刷新按钮。 */
    onRefresh?: () => void
    className?: string
}

const TONE_CLASS: Record<string, string> = {
    bullish: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-500/20 dark:text-emerald-300',
    bearish: 'bg-rose-100 text-rose-700 dark:bg-rose-500/20 dark:text-rose-300',
    warning: 'bg-amber-100 text-amber-700 dark:bg-amber-500/20 dark:text-amber-300',
    neutral: 'bg-slate-100 text-slate-700 dark:bg-slate-500/20 dark:text-slate-300',
    muted: 'bg-slate-100 text-slate-500 dark:bg-slate-600/20 dark:text-slate-400',
}

const DIRECTION_LABEL: Record<string, string> = {
    washout: '洗盘侧',
    distribution: '派发侧',
    trend_break: '结构破坏',
    neutral: '中性',
    unavailable: '不可得',
}

const DIRECTION_CLASS: Record<string, string> = {
    washout: 'text-emerald-600 dark:text-emerald-400',
    distribution: 'text-rose-600 dark:text-rose-400',
    trend_break: 'text-amber-600 dark:text-amber-400',
    neutral: 'text-slate-500 dark:text-slate-400',
    unavailable: 'text-slate-400 dark:text-slate-500',
}

export default function VerdictCard({ verdict, onRefresh, className = '' }: VerdictCardProps) {
    const view = buildVerdictView(verdict)
    const [persisting, setPersisting] = useState(false)
    const [persistError, setPersistError] = useState<string | null>(null)

    const handlePersist = async () => {
        setPersisting(true)
        setPersistError(null)
        try {
            await api.getWashoutVerdict(verdict.symbol, {
                asOf: verdict.as_of,
                persist: true,
            })
            onRefresh?.()
        } catch (err) {
            setPersistError(err instanceof Error ? err.message : '落库失败')
        } finally {
            setPersisting(false)
        }
    }

    return (
        <section
            className={`rounded-xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-700 dark:bg-slate-900 ${className}`}
            data-testid="verdict-card"
            data-verdict-state={verdict.state}
        >
            {/* 头部：状态 + 置信度 */}
            <header className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                    <span
                        className={`rounded-md px-2 py-0.5 text-sm font-medium ${TONE_CLASS[view.badge.tone]}`}
                        data-testid="verdict-state-badge"
                    >
                        {view.badge.label}
                    </span>
                    <span className={`text-xs ${TONE_CLASS[view.band.tone]} rounded px-1.5 py-0.5`}>
                        {view.band.label}
                    </span>
                </div>
                <div className="flex items-center gap-2">
                    {onRefresh && (
                        <button
                            type="button"
                            onClick={onRefresh}
                            className="text-xs text-slate-500 hover:text-slate-700 dark:hover:text-slate-300"
                        >
                            重新评估
                        </button>
                    )}
                    {!verdict.verdict_id && (
                        <button
                            type="button"
                            onClick={handlePersist}
                            disabled={persisting}
                            className="flex items-center gap-1 text-xs text-blue-600 hover:text-blue-700 disabled:opacity-50 dark:text-blue-400"
                        >
                            {persisting ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
                            记录以跟踪后续走势
                        </button>
                    )}
                </div>
            </header>

            <p className="mt-2 text-sm font-medium text-slate-800 dark:text-slate-100">{view.summaryLine}</p>
            <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">{view.badge.description}</p>

            {persistError && <p className="mt-1 text-xs text-rose-600 dark:text-rose-400">{persistError}</p>}

            {/* 弃权时的显式说明：不能让它看起来像一个弱化的方向性观点 */}
            {view.badge.isAbstention && (
                <div className="mt-3 flex items-start gap-2 rounded-lg bg-slate-50 p-2 text-xs text-slate-600 dark:bg-slate-800/50 dark:text-slate-300">
                    <Info className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
                    <span>
                        本次未给出方向性结论。这是**合法结果**：证据缺失或两侧证据过于接近时，
                        系统选择少说而不是猜。
                    </span>
                </div>
            )}

            {/* 分数分解：让判定可检验 */}
            <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
                <div className="rounded bg-emerald-50 p-2 dark:bg-emerald-500/10">
                    <div className="text-slate-500 dark:text-slate-400">洗盘侧质量</div>
                    <div className="font-mono text-emerald-700 dark:text-emerald-300">
                        {view.scores.washout.toFixed(2)}
                    </div>
                </div>
                <div className="rounded bg-rose-50 p-2 dark:bg-rose-500/10">
                    <div className="text-slate-500 dark:text-slate-400">派发侧质量</div>
                    <div className="font-mono text-rose-700 dark:text-rose-300">
                        {view.scores.distribution.toFixed(2)}
                    </div>
                </div>
                <div className="rounded bg-amber-50 p-2 dark:bg-amber-500/10">
                    <div className="text-slate-500 dark:text-slate-400">结构破坏质量</div>
                    <div className="font-mono text-amber-700 dark:text-amber-300">
                        {view.scores.trend_break.toFixed(2)}
                    </div>
                </div>
            </div>

            {/* 未覆盖与门控：必须显式，不能藏进数字里 */}
            {view.coverage.text && (
                <div className="mt-3 flex items-start gap-2 rounded-lg bg-amber-50 p-2 text-xs text-amber-800 dark:bg-amber-500/10 dark:text-amber-200">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
                    <span>{view.coverage.text}</span>
                </div>
            )}

            {/* 逐维度证据 */}
            <details className="mt-3" open>
                <summary className="cursor-pointer text-xs font-medium text-slate-600 dark:text-slate-300">
                    证据分解（{view.components.length} 个维度）
                </summary>
                <div className="mt-2 overflow-x-auto">
                    <table className="w-full text-xs">
                        <thead>
                            <tr className="text-left text-slate-500 dark:text-slate-400">
                                <th className="py-1 pr-2 font-normal">维度</th>
                                <th className="py-1 pr-2 font-normal">方向</th>
                                <th className="py-1 pr-2 font-normal">强度</th>
                                <th className="py-1 font-normal">观测</th>
                            </tr>
                        </thead>
                        <tbody>
                            {view.components.map((row) => (
                                <tr key={row.key} className="border-t border-slate-100 dark:border-slate-800">
                                    <td className="py-1 pr-2 text-slate-700 dark:text-slate-200">{row.label}</td>
                                    <td className={`py-1 pr-2 ${DIRECTION_CLASS[row.direction]}`}>
                                        {DIRECTION_LABEL[row.direction]}
                                    </td>
                                    <td className="py-1 pr-2 font-mono text-slate-600 dark:text-slate-300">
                                        {row.available ? row.magnitude.toFixed(2) : '—'}
                                    </td>
                                    <td className="py-1 text-slate-500 dark:text-slate-400">
                                        {row.note
                                            ? row.note
                                            : row.observables
                                                  .map((o) => `${o.field}=${o.value}`)
                                                  .join('，') || '—'}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </details>

            {/* 位置调制器：与加法贡献分开展示 */}
            {view.modulator && (
                <div className="mt-3 rounded-lg bg-slate-50 p-2 text-xs dark:bg-slate-800/50">
                    <div className="flex items-center gap-2">
                        <span className="text-slate-500 dark:text-slate-400">位置调制因子</span>
                        <span className="font-mono text-slate-800 dark:text-slate-100">
                            {view.modulator.factor.toFixed(2)}
                        </span>
                    </div>
                    <p className="mt-1 text-slate-500 dark:text-slate-400">{view.modulator.explanation}</p>
                </div>
            )}

            {/* 持仓上下文：已知则给出成本/可卖数量，未知则**明确说不知道**。
                修复审计 D1——后端早已返回该字段，但类型与卡片都没消费它，
                于是「还能不能拿」的回答在界面上看不出有没有考虑持仓。 */}
            {view.position && (
                <div
                    className={`mt-3 rounded-lg p-2 text-xs ${
                        view.position.known
                            ? 'bg-slate-50 dark:bg-slate-800/50'
                            : 'bg-amber-50 dark:bg-amber-500/10'
                    }`}
                    data-testid="verdict-position"
                    data-position-known={view.position.known}
                >
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                        <span className="text-slate-500 dark:text-slate-400">
                            {view.position.sourceLabel}
                        </span>
                        {view.position.known ? (
                            <>
                                <span className="text-slate-700 dark:text-slate-200">
                                    持仓 {view.position.quantity ?? '—'}
                                </span>
                                <span className="text-slate-700 dark:text-slate-200">
                                    可卖 {view.position.sellable ?? '—'}
                                </span>
                                <span className="text-slate-700 dark:text-slate-200">
                                    成本 {view.position.averageCost ?? '未记录'}
                                </span>
                                {view.position.flat && <span className="text-slate-500">已清仓</span>}
                            </>
                        ) : (
                            <span className="text-amber-800 dark:text-amber-200">
                                系统不知道你是否持有该标的，因此与持仓相关的建议未纳入本次判定
                            </span>
                        )}
                    </div>
                    {view.position.note && (
                        <p className="mt-1 text-slate-500 dark:text-slate-400">{view.position.note}</p>
                    )}
                </div>
            )}

            {/* 失效条件：研究结论与观点的分界线 */}
            {view.falsifiers.length > 0 && (
                <div className="mt-3">
                    <div className="flex items-center gap-1 text-xs font-medium text-slate-600 dark:text-slate-300">
                        <ShieldAlert className="h-3.5 w-3.5" />
                        失效条件（触发即需重新评估）
                    </div>
                    <ul className="mt-1 space-y-1">
                        {view.falsifiers.map((f, index) => (
                            <li key={`${f.observable}-${index}`} className="text-xs text-slate-600 dark:text-slate-300">
                                <span className="font-mono text-slate-500 dark:text-slate-400">{f.observable}</span>{' '}
                                {f.threshold} · {f.window}
                                <div className="text-slate-500 dark:text-slate-400">{f.description}</div>
                            </li>
                        ))}
                    </ul>
                </div>
            )}

            {/* 免责声明与版本：任何判定出口都必须带 */}
            <footer className="mt-3 flex items-start gap-2 border-t border-slate-100 pt-2 text-xs text-slate-500 dark:border-slate-800 dark:text-slate-400">
                <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
                <div>
                    <div>{view.disclaimer}</div>
                    <div className="mt-0.5 font-mono opacity-70">
                        {verdict.probe} · {view.signalVersion} · 基准日 {verdict.as_of}
                    </div>
                </div>
            </footer>
        </section>
    )
}

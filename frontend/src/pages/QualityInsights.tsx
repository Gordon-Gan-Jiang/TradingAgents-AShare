import { Activity, Calendar, ChevronDown, ChevronUp, RefreshCw, TrendingUp, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
    CartesianGrid,
    Line,
    LineChart,
    ResponsiveContainer,
    Tooltip,
    XAxis,
    YAxis,
} from 'recharts'

import { api } from '@/services/api'
import type {
    InsightsT1RecommendationDetail,
    InsightsT1RecommendationPoint,
    InsightsT1ReportAccuracyPoint,
    InsightsT1ReportAccuracySummary,
    InsightsT1MultiModelConsensusPoint,
    InsightsT1MultiModelConsensusDetail,
    InsightsT1ReportDetail,
    ModelArenaLeaderboardItem,
    ModelArenaModelDetailRow,
    ModelArenaModelTrendSeries,
    ModelArenaDriftAlert,
    ModelArenaSymbolCompareRow,
    ModelProfile,
} from '@/types'

function todayStr(): string {
    return new Date().toISOString().slice(0, 10)
}

function daysAgoStr(n: number): string {
    const d = new Date()
    d.setDate(d.getDate() - n)
    return d.toISOString().slice(0, 10)
}

const PRESET_RANGES = [
    { label: '最近 3 天', days: 3 },
    { label: '最近 7 天', days: 7 },
    { label: '最近 14 天', days: 14 },
    { label: '最近 30 天', days: 30 },
    { label: '最近 60 天', days: 60 },
    { label: '最近 90 天', days: 90 },
]

/** 左图：每日推荐质量 — 与 Line dataKey 一致 */
const REC_LINE = {
    avg_return_pct: { label: '平均 T+1 收益 %', color: '#9333ea' },
    win_rate_pct: { label: 'T+1 上涨占比 %', color: '#ea580c' },
} as const
type RecLineKey = keyof typeof REC_LINE
const REC_LINE_KEYS = Object.keys(REC_LINE) as RecLineKey[]

/** 右图：深度分析方向正确率 — 与 Line dataKey 一致 */
const REP_LINE = {
    accuracy_portfolio_pct: { label: '持仓（当前导入）', color: '#047857' },
    accuracy_all_pct: { label: '全部深度报告', color: '#4338ca' },
} as const
type RepLineKey = keyof typeof REP_LINE
const REP_LINE_KEYS = Object.keys(REP_LINE) as RepLineKey[]

/** 多模型同日共识正确率 */
const CONS_LINE = {
    unanimous_bullish_accuracy_portfolio_pct: { label: '共识看涨（持仓）', color: '#047857' },
    unanimous_bullish_accuracy_all_pct: { label: '共识看涨（全部）', color: '#34d399' },
    unanimous_bearish_accuracy_portfolio_pct: { label: '共识看跌（持仓）', color: '#be123c' },
    unanimous_bearish_accuracy_all_pct: { label: '共识看跌（全部）', color: '#fb7185' },
} as const
type ConsLineKey = keyof typeof CONS_LINE
const CONS_LINE_KEYS = Object.keys(CONS_LINE) as ConsLineKey[]

function directionLabel(bucket: string | null | undefined): { text: string; cls: string } {
    switch (bucket) {
        case 'bullish': return { text: '看多 ↑', cls: 'text-emerald-600 dark:text-emerald-400' }
        case 'bearish': return { text: '看空 ↓', cls: 'text-rose-600 dark:text-rose-400' }
        case 'neutral': return { text: '中性 →', cls: 'text-slate-500' }
        default: return { text: bucket ?? '—', cls: 'text-slate-400' }
    }
}

function returnBadge(v: number | null | undefined): JSX.Element {
    if (v == null) return <span className="text-slate-400">—</span>
    const pos = v >= 0
    return (
        <span className={`font-mono font-semibold ${pos ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'}`}>
            {pos ? '+' : ''}{v.toFixed(2)}%
        </span>
    )
}

function correctBadge(correct: boolean | null | undefined): JSX.Element {
    if (correct == null) return <span className="text-slate-400">—</span>
    return correct
        ? <span className="inline-flex items-center rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">正确 ✓</span>
        : <span className="inline-flex items-center rounded-full bg-rose-100 px-2 py-0.5 text-xs font-medium text-rose-700 dark:bg-rose-900/40 dark:text-rose-300">错误 ✗</span>
}

function percentText(value: number | null | undefined, digits = 2): string {
    if (value == null || Number.isNaN(Number(value))) return '—'
    return `${Number(value).toFixed(digits)}%`
}

function t1EvalStatusLabel(status: string | null | undefined): string {
    switch (status) {
        case 'evaluated': return '已评估'
        case 'pending': return '待 T1 收盘'
        case 'insufficient_data': return '行情不足'
        default: return status ?? '—'
    }
}

/**
 * 命中率的诚实性提示（D1/D2/D3）。
 *
 * 这段存在的唯一理由是：在**去重后的有效样本量**下，现有数据的 Wilson 区间
 * 跨过 50%，也就是说这些百分比**无法**证明系统具备方向预测能力。只画一条
 * 围绕 50% 上下波动的折线而不说明这一点，就是在误导使用者。
 */
function AccuracyHonestyBanner({
    summary,
    note,
}: {
    summary: InsightsT1ReportAccuracySummary | null | undefined
    note?: string
}) {
    if (!summary || !summary.effective_n) return null
    const {
        effective_n: n,
        accuracy_pct: acc,
        ci_low_pct: lo,
        ci_high_pct: hi,
        row_count: rows,
        abstain_count: abstain,
        coverage_pct: coverage,
        conflict_n: conflicts,
        required_n: required,
        underpowered,
        verdict,
        clustered,
        excess,
        excess_spread: spread,
        plan_horizon: planHorizon,
    } = summary

    const tone = verdict === 'beats_coin_flip'
        ? 'border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-200'
        : verdict === 'worse_than_coin_flip'
            ? 'border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-700 dark:bg-rose-950/50 dark:text-rose-200'
            : 'border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-700 dark:bg-amber-950/50 dark:text-amber-200'

    const headline = verdict === 'beats_coin_flip'
        ? '区间下界高于 50%，具备统计显著性'
        : verdict === 'worse_than_coin_flip'
            ? '区间上界低于 50%，显著劣于掷硬币'
            : '与掷硬币不可区分'

    return (
        <div className={`mb-3 rounded-lg border px-3 py-2 text-xs leading-relaxed ${tone}`}>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-medium">
                <span>有效样本 n={n}</span>
                {acc != null && <span>命中率 {percentText(acc)}</span>}
                {lo != null && hi != null && <span>95% 区间 [{percentText(lo, 1)}, {percentText(hi, 1)}]</span>}
                <span>· {headline}</span>
            </div>
            <div className="mt-1 space-y-0.5 opacity-90">
                <div>
                    分母说明：{rows != null ? `研报行数 ${rows}` : '—'} → 去重后唯一价格窗口 <strong>{n}</strong>
                    {rows != null && rows > n && (
                        <> （同一标的同一信号日的多份研报共享同一次 T+1 收益，只算一次；虚增 {((rows / Math.max(n, 1))).toFixed(1)}×）</>
                    )}
                    {conflicts ? <>；另有 {conflicts} 个窗口被给出相反方向，已排除出分母</> : null}
                </div>
                {(abstain || coverage != null) && (
                    <div>
                        弃权 {abstain ?? 0} 次
                        {coverage != null && <>，覆盖率 {percentText(coverage, 1)}</>}
                        （中性/无方向不计入命中率，但也不应靠多弃权来抬升命中率）
                    </div>
                )}
                {underpowered && required ? (
                    <div>
                        要在 80% 功效下把这个命中率与 50% 区分开，需要约 <strong>{required}</strong> 个独立样本，
                        当前只有 {n} —— 因此上面的百分比<b>不足以证明预测能力</b>。
                    </div>
                ) : null}
                {clustered?.accuracy_pct != null && (
                    <div>
                        按日等权（每个交易日一票，对少数高产日更稳健）：
                        <strong>{percentText(clustered.accuracy_pct)}</strong>
                        {clustered.ci_low_pct != null && clustered.ci_high_pct != null && (
                            <> ，区间 [{percentText(clustered.ci_low_pct, 1)}, {percentText(clustered.ci_high_pct, 1)}]</>
                        )}
                        {clustered.t != null && <> ，t={clustered.t.toFixed(2)}</>}
                        <>（共 {clustered.n_days ?? '—'} 个交易日）</>
                        {clustered.bootstrap_ci_low_pct != null && clustered.bootstrap_ci_high_pct != null && (
                            <> ；按日 bootstrap 区间 [{percentText(clustered.bootstrap_ci_low_pct, 1)}, {percentText(clustered.bootstrap_ci_high_pct, 1)}]</>
                        )}
                    </div>
                )}
                {note ? <div className="opacity-80">{note}</div> : null}
            </div>
            {excess && excess.effective_n > 0 && (
                <div className="mt-2 border-t border-current/20 pt-2">
                    <div className="font-medium">
                        去市场 beta（相对口径，{excess.effective_n} 个可比窗口）：
                        超额命中率 <strong>{percentText(excess.accuracy_pct)}</strong>
                        {excess.ci_low_pct != null && excess.ci_high_pct != null && (
                            <> ，95% 区间 [{percentText(excess.ci_low_pct, 1)}, {percentText(excess.ci_high_pct, 1)}]</>
                        )}
                        {excess.verdict === 'not_significant' && <> —— 仍与掷硬币不可区分</>}
                    </div>
                    <div className="opacity-90">
                        绝对口径的百分比会被大盘涨跌抬高，不能单独作为「研判能力」的证据。
                        基准 = {excess.benchmark ?? '同日其它标的收益均值'}
                        {excess.days_without_peers ? <>；{excess.days_without_peers} 个交易日只有单只标的，无同侪可比，未计入</> : null}。
                    </div>
                    {spread?.long_short_spread_pct != null && (
                        <div className="opacity-90">
                            多空价差 <strong>{spread.long_short_spread_pct.toFixed(3)}</strong> 百分点/日
                            （看多 {spread.bullish_n ?? 0} 个、看空 {spread.bearish_n ?? 0} 个），
                            一个来回成本约 {spread.cost_round_trip_pct ?? 0.25}%
                            {spread.covers_cost
                                ? <> —— 价差高于成本</>
                                : <> —— <b>低于成本，不可交易</b></>}
                        </div>
                    )}
                </div>
            )}
            {planHorizon && (
                <div className="mt-2 border-t border-current/20 pt-2 opacity-90">
                    <div>
                        口径范围：本条命中率只评<b>次日方向</b>
                        {planHorizon.measured_horizon ? <>（horizon={planHorizon.measured_horizon}）</> : null}
                        —— 研报里的目标价／止损／时间止损属于更长期限，<b>其达成情况不在此指标内</b>。
                    </div>
                    {(planHorizon.multi_day_plan_windows ?? 0) > 0 && (
                        <div>
                            期限错配：{planHorizon.multi_day_plan_windows} 个窗口承载的是多日计划
                            {planHorizon.median_plan_days != null && <>（持有期中位数 {planHorizon.median_plan_days} 自然日）</>}
                            ，却由一日收益打分 —— 这类窗口越多，本命中率的解释力越弱。
                        </div>
                    )}
                    {(planHorizon.windows_with_unrecorded_plan_horizon ?? 0) > 0 && (
                        <div>
                            另有 {planHorizon.windows_with_unrecorded_plan_horizon} 个窗口附有交易计划，
                            但它们在记录计划持有期的列存在之前就已打分，因此无法判断是否错配
                            （不做回填，以免把猜出来的持有期当成记录值；重新评估后即可度量）。
                        </div>
                    )}
                    {(planHorizon.foreign_horizon_values?.length ?? 0) > 0 && (
                        <div className="font-medium">
                            口径告警：本指标只应包含「次日」打分，却出现了
                            {' '}{planHorizon.foreign_horizon_values!.join('、')}
                            {' '}周期的行 —— 口径声明已失效，此处的命中率不可直接采信。
                        </div>
                    )}
                    {planHorizon.graded_report_horizon_values &&
                        Object.entries(planHorizon.graded_report_horizon_values).some(([k]) => k === 'dual') && (
                            <div>
                                被评报告周期：其中 {planHorizon.graded_report_horizon_values['dual']} 份当时同时给出了
                                短期与中期两套结论，而此处持久化的方向取自短期那一套 ——
                                按次日方向打分是合理的，但不应读成「这些报告只对次日负责」。
                            </div>
                        )}
                </div>
            )}
        </div>
    )
}

/** 图例与曲线解耦：始终可点选恢复隐藏线；与 Line 的 hide 联动。 */
function SeriesLegendBar<K extends string>({
    series,
    visible,
    onToggle,
}: {
    series: Record<K, { label: string; color: string }>
    visible: Record<K, boolean>
    onToggle: (key: string) => void
}) {
    const keys = Object.keys(series) as K[]
    return (
        <div className="mt-3 flex flex-wrap justify-center gap-x-4 gap-y-2 border-t border-slate-100 px-1 pt-3 text-xs leading-snug dark:border-slate-800">
            {keys.map(key => {
                const meta = series[key]
                const on = visible[key] ?? true
                return (
                    <button
                        key={key}
                        type="button"
                        onClick={e => {
                            e.stopPropagation()
                            onToggle(key)
                        }}
                        className={`inline-flex max-w-full min-w-0 items-start gap-2 rounded-md px-2 py-1.5 text-left transition-all hover:bg-slate-100 dark:hover:bg-slate-800 sm:max-w-[calc(50%-0.5rem)] ${
                            on ? 'text-slate-800 opacity-100 dark:text-slate-100' : 'text-slate-400 opacity-50 line-through'
                        }`}
                    >
                        <span className="mt-1.5 h-0.5 w-5 shrink-0 rounded-full" style={{ background: meta.color }} />
                        <span className="min-w-0 break-words">{meta.label}</span>
                    </button>
                )
            })}
        </div>
    )
}

interface RecDetailPanelProps {
    date: string
    items: InsightsT1RecommendationDetail[]
    loading: boolean
    onClose: () => void
}

function RecDetailPanel({ date, items, loading, onClose }: RecDetailPanelProps) {
    return (
        <div className="mt-3 rounded-xl border border-violet-200 bg-violet-50/60 dark:border-violet-800/50 dark:bg-violet-950/20">
            <div className="flex items-center justify-between border-b border-violet-200 px-4 py-2.5 dark:border-violet-800/50">
                <span className="text-sm font-semibold text-violet-800 dark:text-violet-200">
                    {date} · 推荐明细（{loading ? '…' : items.length} 只）
                </span>
                <button onClick={onClose} className="rounded p-0.5 hover:bg-violet-200 dark:hover:bg-violet-800">
                    <X className="h-3.5 w-3.5 text-violet-600 dark:text-violet-300" />
                </button>
            </div>
            {loading ? (
                <div className="flex justify-center py-6 text-sm text-slate-400">加载中…</div>
            ) : items.length === 0 ? (
                <div className="py-6 text-center text-sm text-slate-400">该日暂无已评估数据</div>
            ) : (
                <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                        <thead>
                            <tr className="text-left text-xs text-slate-500 dark:text-slate-400">
                                <th className="px-4 py-2 whitespace-nowrap">股票代码</th>
                                <th className="px-4 py-2">名称</th>
                                <th className="px-4 py-2">评分</th>
                                <th className="px-4 py-2">排名</th>
                                <th className="px-4 py-2 whitespace-nowrap">扫描生成时间</th>
                                <th className="px-4 py-2 whitespace-nowrap">信号日 (T0)</th>
                                <th className="px-4 py-2 whitespace-nowrap">T+1 日期</th>
                                <th className="px-4 py-2 text-right whitespace-nowrap">T+1 收益</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                            {items.map(item => (
                                <tr key={item.id} className="hover:bg-violet-50 dark:hover:bg-violet-950/30">
                                    <td className="px-4 py-2 font-mono font-medium text-slate-800 dark:text-slate-200">{item.symbol}</td>
                                    <td className="px-4 py-2 text-slate-600 dark:text-slate-400">{item.name ?? '—'}</td>
                                    <td className="px-4 py-2 text-slate-600 dark:text-slate-400">{item.score != null ? item.score.toFixed(1) : '—'}</td>
                                    <td className="px-4 py-2 text-slate-600 dark:text-slate-400">{item.rank ?? '—'}</td>
                                    <td className="px-4 py-2 font-mono text-xs text-slate-500 whitespace-nowrap">{item.scan_created_at ?? '—'}</td>
                                    <td className="px-4 py-2 font-mono text-slate-500 whitespace-nowrap">{item.t1_signal_date}</td>
                                    <td className="px-4 py-2 font-mono text-slate-500 whitespace-nowrap">{item.t1_trade_date ?? '—'}</td>
                                    <td className="px-4 py-2 text-right">{returnBadge(item.t1_return_pct)}</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    )
}

interface RepDetailPanelProps {
    date: string
    items: InsightsT1ReportDetail[]
    loading: boolean
    onClose: () => void
    detailScope: 'portfolio' | 'all'
    onDetailScopeChange: (s: 'portfolio' | 'all') => void
}

function RepDetailPanel({ date, items, loading, onClose, detailScope, onDetailScopeChange }: RepDetailPanelProps) {
    const scopeLabel = detailScope === 'portfolio' ? '仅当前持仓' : '全部深度报告'
    return (
        <div className="mt-3 rounded-xl border border-emerald-200 bg-emerald-50/60 dark:border-emerald-800/50 dark:bg-emerald-950/20">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-emerald-200 px-4 py-2.5 dark:border-emerald-800/50">
                <span className="text-sm font-semibold text-emerald-800 dark:text-emerald-200">
                    {date} · 深度报告明细 · {scopeLabel}（{loading ? '…' : items.length} 只）
                </span>
                <div className="flex items-center gap-2">
                    <div className="inline-flex rounded-lg border border-emerald-200/80 bg-white/80 p-0.5 text-xs dark:border-emerald-800/60 dark:bg-emerald-950/40">
                        <button
                            type="button"
                            onClick={() => onDetailScopeChange('portfolio')}
                            className={`rounded-md px-2 py-1 font-medium transition-colors ${
                                detailScope === 'portfolio'
                                    ? 'bg-emerald-600 text-white'
                                    : 'text-emerald-800 hover:bg-emerald-100 dark:text-emerald-200 dark:hover:bg-emerald-900/50'
                            }`}
                        >
                            仅持仓
                        </button>
                        <button
                            type="button"
                            onClick={() => onDetailScopeChange('all')}
                            className={`rounded-md px-2 py-1 font-medium transition-colors ${
                                detailScope === 'all'
                                    ? 'bg-emerald-600 text-white'
                                    : 'text-emerald-800 hover:bg-emerald-100 dark:text-emerald-200 dark:hover:bg-emerald-900/50'
                            }`}
                        >
                            全部
                        </button>
                    </div>
                    <button onClick={onClose} className="rounded p-0.5 hover:bg-emerald-200 dark:hover:bg-emerald-800">
                        <X className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-300" />
                    </button>
                </div>
            </div>
            {loading ? (
                <div className="flex justify-center py-6 text-sm text-slate-400">加载中…</div>
            ) : items.length === 0 ? (
                <div className="px-4 py-6 text-center text-sm text-slate-400">
                    {detailScope === 'all' ? (
                        <>
                            该日尚无 T+1 结果记录。请先点击页面右上角「手动刷新 T+1 评估」拉取全部深度报告；若仍为空，说明该信号日尚无报告或评估尚未落库。
                        </>
                    ) : (
                        <>该日暂无<strong className="text-slate-600 dark:text-slate-300">持仓标的</strong>的已评估数据；可切换到「全部」查看账户内所有标的。</>
                    )}
                </div>
            ) : (
                <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                        <thead>
                            <tr className="text-left text-xs text-slate-500 dark:text-slate-400">
                                <th className="px-4 py-2 whitespace-nowrap">股票代码</th>
                                <th className="px-4 py-2">名称</th>
                                {detailScope === 'all' && (
                                    <th className="px-4 py-2 whitespace-nowrap">T+1 评估</th>
                                )}
                                <th className="px-4 py-2 whitespace-nowrap">报告生成时间</th>
                                <th className="px-4 py-2 whitespace-nowrap">信号日 (T0)</th>
                                <th className="px-4 py-2 whitespace-nowrap">T+1 日期</th>
                                <th className="px-4 py-2 whitespace-nowrap">模型</th>
                                <th className="px-4 py-2 whitespace-nowrap">分析方向</th>
                                <th className="px-4 py-2 whitespace-nowrap">买卖决策</th>
                                <th className="px-4 py-2 whitespace-nowrap">T0 收盘</th>
                                <th className="px-4 py-2 whitespace-nowrap">T+1 收盘</th>
                                <th className="px-4 py-2 text-right whitespace-nowrap">T+1 收益</th>
                                <th className="px-4 py-2 text-center whitespace-nowrap">方向正确</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                            {items.map((item, i) => {
                                const dir = directionLabel(item.direction_bucket)
                                return (
                                    <tr key={`${item.symbol}-${i}`} className="hover:bg-emerald-50 dark:hover:bg-emerald-950/30">
                                        <td className="px-4 py-2 font-mono font-medium text-slate-800 dark:text-slate-200">{item.symbol}</td>
                                        <td className="px-4 py-2 text-slate-600 dark:text-slate-400">{item.name ?? '—'}</td>
                                        {detailScope === 'all' && (
                                            <td className="whitespace-nowrap px-4 py-2 text-slate-600 dark:text-slate-400" title={item.evaluation_reason ?? undefined}>
                                                {t1EvalStatusLabel(item.evaluation_status)}
                                            </td>
                                        )}
                                        <td className="px-4 py-2 font-mono text-xs text-slate-500 whitespace-nowrap"
                                            title={item.report_trade_date ? `报告标注交易日: ${item.report_trade_date}` : undefined}>
                                            {item.report_created_at ?? '—'}
                                        </td>
                                        <td className="px-4 py-2 font-mono text-slate-500 whitespace-nowrap">{item.signal_trade_date}</td>
                                        <td className="px-4 py-2 font-mono text-slate-500 whitespace-nowrap">{item.t1_trade_date ?? '—'}</td>
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-400">
                                            <div className="space-y-0.5">
                                                <div className="font-medium text-slate-700 dark:text-slate-200">
                                                    {item.model_profile_name || item.llm_provider || '—'}
                                                </div>
                                                <div className="truncate max-w-[160px]" title={item.deep_think_llm || item.quick_think_llm || ''}>
                                                    {item.deep_think_llm ? `D: ${item.deep_think_llm}` : item.quick_think_llm ? `Q: ${item.quick_think_llm}` : '—'}
                                                </div>
                                            </div>
                                        </td>
                                        <td className={`px-4 py-2 font-medium whitespace-nowrap ${dir.cls}`}
                                            title={item.direction ?? undefined}>
                                            {dir.text}
                                        </td>
                                        <td className="px-4 py-2 text-slate-600 dark:text-slate-400 whitespace-nowrap">{item.decision ?? '—'}</td>
                                        <td className="px-4 py-2 font-mono text-slate-600 dark:text-slate-400">
                                            {item.p0 != null ? item.p0.toFixed(2) : '—'}
                                        </td>
                                        <td className="px-4 py-2 font-mono text-slate-600 dark:text-slate-400">
                                            {item.p1 != null ? item.p1.toFixed(2) : '—'}
                                        </td>
                                        <td className="px-4 py-2 text-right">{returnBadge(item.return_t1_pct)}</td>
                                        <td className="px-4 py-2 text-center">{correctBadge(item.label_correct)}</td>
                                    </tr>
                                )
                            })}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    )
}

function ConsensusDetailPanel({
    date,
    items,
    loading,
    onClose,
}: {
    date: string
    items: InsightsT1MultiModelConsensusDetail[]
    loading: boolean
    onClose: () => void
}) {
    return (
        <div className="mt-3 rounded-xl border border-violet-200 bg-violet-50/60 dark:border-violet-800/50 dark:bg-violet-950/20">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-violet-200 px-4 py-2.5 dark:border-violet-800/50">
                <span className="text-sm font-semibold text-violet-800 dark:text-violet-200">
                    {date} · 多模型共识明细（{loading ? '…' : items.length} 条）
                </span>
                <button onClick={onClose} className="rounded p-0.5 hover:bg-violet-200 dark:hover:bg-violet-800">
                    <X className="h-3.5 w-3.5 text-violet-600 dark:text-violet-300" />
                </button>
            </div>
            {loading ? (
                <div className="flex justify-center py-6 text-sm text-slate-400">加载中…</div>
            ) : items.length === 0 ? (
                <div className="px-4 py-6 text-center text-sm text-slate-400">
                    该日暂无多模型同向共识样本。需在同一信号日对同一股票用 ≥2 个不同模型完成深度分析并完成 T+1 评估。
                </div>
            ) : (
                <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                        <thead>
                            <tr className="text-left text-xs text-slate-500 dark:text-slate-400">
                                <th className="px-4 py-2 whitespace-nowrap">股票代码</th>
                                <th className="px-4 py-2 whitespace-nowrap">名称</th>
                                <th className="px-4 py-2 whitespace-nowrap">共识方向</th>
                                <th className="px-4 py-2 whitespace-nowrap">模型数</th>
                                <th className="px-4 py-2">参与模型</th>
                                <th className="px-4 py-2 whitespace-nowrap">T+1 日期</th>
                                <th className="px-4 py-2 text-right whitespace-nowrap">T+1 收益</th>
                                <th className="px-4 py-2 text-center whitespace-nowrap">方向正确</th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                            {items.map((item, i) => {
                                const dir = directionLabel(item.consensus_direction)
                                const modelNames = (item.models || [])
                                    .map(m => m.model_display_name || m.model_profile_name || m.model_key)
                                    .filter(Boolean)
                                    .join('、')
                                return (
                                    <tr key={`${item.symbol}-${item.consensus_direction}-${i}`} className="hover:bg-violet-50 dark:hover:bg-violet-950/30">
                                        <td className="px-4 py-2 font-mono font-medium text-slate-800 dark:text-slate-200">{item.symbol}</td>
                                        <td className="px-4 py-2 text-slate-600 dark:text-slate-400">{item.name ?? '—'}</td>
                                        <td className={`px-4 py-2 font-medium whitespace-nowrap ${dir.cls}`}>{dir.text}</td>
                                        <td className="px-4 py-2 text-slate-600 dark:text-slate-400">{item.model_count}</td>
                                        <td className="px-4 py-2 text-xs text-slate-600 dark:text-slate-400 max-w-xs truncate" title={modelNames}>{modelNames || '—'}</td>
                                        <td className="px-4 py-2 font-mono text-slate-500 whitespace-nowrap">{item.t1_trade_date ?? '—'}</td>
                                        <td className="px-4 py-2 text-right">{returnBadge(item.return_t1_pct)}</td>
                                        <td className="px-4 py-2 text-center">{correctBadge(item.label_correct)}</td>
                                    </tr>
                                )
                            })}
                        </tbody>
                    </table>
                </div>
            )}
        </div>
    )
}

export default function QualityInsights() {
    const [startDate, setStartDate] = useState(() => daysAgoStr(30))
    const [endDate, setEndDate] = useState(() => todayStr())
    const [customRange, setCustomRange] = useState(false)

    const [recSeries, setRecSeries] = useState<InsightsT1RecommendationPoint[]>([])
    const [repSeries, setRepSeries] = useState<InsightsT1ReportAccuracyPoint[]>([])
    const [repSeriesAll, setRepSeriesAll] = useState<InsightsT1ReportAccuracyPoint[]>([])
    const [repSummary, setRepSummary] = useState<InsightsT1ReportAccuracySummary | null>(null)
    const [repSummaryAll, setRepSummaryAll] = useState<InsightsT1ReportAccuracySummary | null>(null)
    const [repDenominatorNote, setRepDenominatorNote] = useState('')
    const [recDesc, setRecDesc] = useState('')
    const [repDesc, setRepDesc] = useState('')
    const [repDescAll, setRepDescAll] = useState('')
    const [consensusSeries, setConsensusSeries] = useState<InsightsT1MultiModelConsensusPoint[]>([])
    const [consensusSeriesAll, setConsensusSeriesAll] = useState<InsightsT1MultiModelConsensusPoint[]>([])
    const [consensusDesc, setConsensusDesc] = useState('')
    const [consensusDescAll, setConsensusDescAll] = useState('')
    const [loading, setLoading] = useState(false)
    const [refreshing, setRefreshing] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [refreshHint, setRefreshHint] = useState<string | null>(null)

    const [selectedRecDate, setSelectedRecDate] = useState<string | null>(null)
    const [recDetail, setRecDetail] = useState<InsightsT1RecommendationDetail[]>([])
    const [recDetailLoading, setRecDetailLoading] = useState(false)

    const [selectedRepDate, setSelectedRepDate] = useState<string | null>(null)
    /** 默认「全部」：与右侧「全部深度报告」曲线对齐，避免误以为没有明细。 */
    const [repDetailScope, setRepDetailScope] = useState<'portfolio' | 'all'>('all')
    const [repDetail, setRepDetail] = useState<InsightsT1ReportDetail[]>([])
    const [repDetailLoading, setRepDetailLoading] = useState(false)
    const [selectedConsensusDate, setSelectedConsensusDate] = useState<string | null>(null)
    const [consensusDetail, setConsensusDetail] = useState<InsightsT1MultiModelConsensusDetail[]>([])
    const [consensusDetailLoading, setConsensusDetailLoading] = useState(false)
    const [arenaScope, setArenaScope] = useState<'portfolio' | 'all'>('all')
    const [arenaRows, setArenaRows] = useState<ModelArenaLeaderboardItem[]>([])
    const [arenaSamples, setArenaSamples] = useState(0)
    const [arenaLoading, setArenaLoading] = useState(false)
    const [arenaDriftAlerts, setArenaDriftAlerts] = useState<ModelArenaDriftAlert[]>([])
    const [arenaDriftLoading, setArenaDriftLoading] = useState(false)
    const [arenaTrendSeries, setArenaTrendSeries] = useState<ModelArenaModelTrendSeries[]>([])
    const [arenaTrendLoading, setArenaTrendLoading] = useState(false)
    /** 空字符串 = 显示 Top 模型对比；否则只显示该模型曲线 */
    const [arenaSelectedModelKey, setArenaSelectedModelKey] = useState('')
    const [arenaModelDetail, setArenaModelDetail] = useState<ModelArenaModelDetailRow[]>([])
    const [arenaModelDetailDrift, setArenaModelDetailDrift] = useState<ModelArenaModelDetailRow[]>([])
    const [arenaModelDetailLoading, setArenaModelDetailLoading] = useState(false)
    const [arenaModelDetailTitle, setArenaModelDetailTitle] = useState('')
    const [arenaModelDetailSummary, setArenaModelDetailSummary] = useState<{ total: number; accuracy: number | null; driftCount: number }>({ total: 0, accuracy: null, driftCount: 0 })
    const [arenaSymbol, setArenaSymbol] = useState('600519.SH')
    const [arenaSymbolRows, setArenaSymbolRows] = useState<ModelArenaSymbolCompareRow[]>([])
    const [arenaSymbolLoading, setArenaSymbolLoading] = useState(false)
    const [modelProfiles, setModelProfiles] = useState<ModelProfile[]>([])
    const [promotingProfileId, setPromotingProfileId] = useState<string | null>(null)

    const [recLineVisible, setRecLineVisible] = useState<Record<RecLineKey, boolean>>({
        avg_return_pct: true,
        win_rate_pct: true,
    })
    const [repLineVisible, setRepLineVisible] = useState<Record<RepLineKey, boolean>>({
        accuracy_portfolio_pct: true,
        accuracy_all_pct: true,
    })
    const [consLineVisible, setConsLineVisible] = useState<Record<ConsLineKey, boolean>>({
        unanimous_bullish_accuracy_portfolio_pct: true,
        unanimous_bullish_accuracy_all_pct: true,
        unanimous_bearish_accuracy_portfolio_pct: true,
        unanimous_bearish_accuracy_all_pct: true,
    })

    const toggleRecLine = useCallback((key: string) => {
        const k = key as RecLineKey
        if (!REC_LINE_KEYS.includes(k)) return
        setRecLineVisible(prev => {
            const next = { ...prev, [k]: !prev[k] }
            if (!REC_LINE_KEYS.some(x => next[x])) return prev
            return next
        })
    }, [])

    const toggleRepLine = useCallback((key: string) => {
        const k = key as RepLineKey
        if (!REP_LINE_KEYS.includes(k)) return
        setRepLineVisible(prev => {
            const next = { ...prev, [k]: !prev[k] }
            if (!REP_LINE_KEYS.some(x => next[x])) return prev
            return next
        })
    }, [])

    const toggleConsLine = useCallback((key: string) => {
        const k = key as ConsLineKey
        if (!CONS_LINE_KEYS.includes(k)) return
        setConsLineVisible(prev => {
            const next = { ...prev, [k]: !prev[k] }
            if (!CONS_LINE_KEYS.some(x => next[x])) return prev
            return next
        })
    }, [])

    const loadCharts = useCallback(async (sd: string, ed: string) => {
        setLoading(true)
        setError(null)
        try {
            const [rec, rep, consensus] = await Promise.all([
                api.getInsightsT1RecommendationsTrend({ startDate: sd, endDate: ed }),
                api.getInsightsT1ReportsAccuracyTrend({ startDate: sd, endDate: ed }),
                api.getInsightsT1MultiModelConsensusTrend({ startDate: sd, endDate: ed }),
            ])
            setRecSeries(rec.series || [])
            setRepSeries(rep.series || [])
            setRepSeriesAll(rep.series_all || [])
            setRepSummary(rep.summary || null)
            setRepSummaryAll(rep.summary_all || null)
            setRepDenominatorNote(rep.denominator_note || '')
            setRecDesc(rec.description || '')
            setRepDesc(rep.description || '')
            setRepDescAll(rep.description_all || '')
            setConsensusSeries(consensus.series || [])
            setConsensusSeriesAll(consensus.series_all || [])
            setConsensusDesc(consensus.description || '')
            setConsensusDescAll(consensus.description_all || '')
        } catch (e) {
            setError(e instanceof Error ? e.message : '加载失败')
        } finally {
            setLoading(false)
        }
    }, [])

    const loadArenaLeaderboard = useCallback(async (sd: string, ed: string, scope: 'portfolio' | 'all') => {
        setArenaLoading(true)
        try {
            const board = await api.getModelArenaLeaderboard({ startDate: sd, endDate: ed, scope, minSamples: 1 })
            setArenaRows(board.leaderboard || [])
            setArenaSamples(Number(board.total_samples || 0))
        } catch {
            setArenaRows([])
            setArenaSamples(0)
        } finally {
            setArenaLoading(false)
        }
    }, [])

    const loadArenaDriftAlerts = useCallback(async (scope: 'portfolio' | 'all') => {
        setArenaDriftLoading(true)
        try {
            const payload = await api.getModelArenaDriftAlerts({ scope, lookbackDays: 120, recentDays: 7, baselineDays: 30 })
            setArenaDriftAlerts(payload.alerts || [])
        } catch {
            setArenaDriftAlerts([])
        } finally {
            setArenaDriftLoading(false)
        }
    }, [])

    const loadArenaTrend = useCallback(async (
        sd: string,
        ed: string,
        scope: 'portfolio' | 'all',
        focusModelKey?: string,
    ) => {
        setArenaTrendLoading(true)
        try {
            const payload = await api.getModelArenaModelTrend({
                startDate: sd,
                endDate: ed,
                scope,
                topN: focusModelKey ? 1 : 6,
                minSamples: focusModelKey ? 1 : 8,
                modelKeys: focusModelKey ? [focusModelKey] : undefined,
            })
            setArenaTrendSeries(payload.series || [])
        } catch {
            setArenaTrendSeries([])
        } finally {
            setArenaTrendLoading(false)
        }
    }, [])

    const resolveArenaModelLabel = useCallback((modelKey: string) => {
        const row = arenaRows.find(r => r.model_key === modelKey)
        if (row) {
            return row.deep_think_llm || row.quick_think_llm || row.model_display_name || modelKey
        }
        const series = arenaTrendSeries.find(s => s.model_key === modelKey)
        return series?.deep_think_llm || series?.quick_think_llm || series?.model_display_name || modelKey
    }, [arenaRows, arenaTrendSeries])

    const loadArenaModelDetailForPoint = useCallback(async (modelKey: string, signalDate: string) => {
        setArenaModelDetailLoading(true)
        setArenaModelDetail([])
        setArenaModelDetailDrift([])
        setArenaModelDetailTitle(`${resolveArenaModelLabel(modelKey)} · ${signalDate}`)
        setArenaModelDetailSummary({ total: 0, accuracy: null, driftCount: 0 })
        try {
            const payload = await api.getModelArenaModelDetail({
                modelKey,
                startDate: signalDate,
                endDate: signalDate,
                scope: arenaScope,
                limit: 300,
            })
            setArenaModelDetail(payload.rows || [])
            setArenaModelDetailDrift(payload.drift_rows || [])
            setArenaModelDetailSummary({
                total: Number(payload.total_samples || 0),
                accuracy: payload.accuracy_pct == null ? null : Number(payload.accuracy_pct),
                driftCount: Number(payload.drift_warning_count || 0),
            })
        } catch {
            setArenaModelDetail([])
            setArenaModelDetailDrift([])
            setArenaModelDetailSummary({ total: 0, accuracy: null, driftCount: 0 })
        } finally {
            setArenaModelDetailLoading(false)
        }
    }, [arenaScope, resolveArenaModelLabel])

    const loadArenaModelDetail = useCallback(async (item: ModelArenaLeaderboardItem) => {
        setArenaModelDetailLoading(true)
        setArenaModelDetail([])
        setArenaModelDetailDrift([])
        setArenaModelDetailTitle(item.deep_think_llm || item.quick_think_llm || item.model_display_name)
        setArenaModelDetailSummary({ total: 0, accuracy: null, driftCount: 0 })
        try {
            const payload = await api.getModelArenaModelDetail({
                // Use model_key (model:xxx) as canonical filter so merged groups
                // (profile-based + ad-hoc runs of same model) are shown together.
                modelKey: item.model_key,
                startDate: startDate,
                endDate: endDate,
                scope: arenaScope,
                limit: 300,
            })
            setArenaModelDetail(payload.rows || [])
            setArenaModelDetailDrift(payload.drift_rows || [])
            setArenaModelDetailSummary({
                total: Number(payload.total_samples || 0),
                accuracy: payload.accuracy_pct == null ? null : Number(payload.accuracy_pct),
                driftCount: Number(payload.drift_warning_count || 0),
            })
        } catch {
            setArenaModelDetail([])
            setArenaModelDetailDrift([])
            setArenaModelDetailSummary({ total: 0, accuracy: null, driftCount: 0 })
        } finally {
            setArenaModelDetailLoading(false)
        }
    }, [arenaScope, endDate, startDate])

    const loadArenaSymbolCompare = useCallback(async (symbolRaw: string, sd: string, ed: string, scope: 'portfolio' | 'all') => {
        const symbol = symbolRaw.trim().toUpperCase()
        if (!symbol) {
            setArenaSymbolRows([])
            return
        }
        setArenaSymbolLoading(true)
        try {
            const payload = await api.getModelArenaSymbolCompare(symbol, { startDate: sd, endDate: ed, scope })
            setArenaSymbolRows(payload.rows || [])
        } catch {
            setArenaSymbolRows([])
        } finally {
            setArenaSymbolLoading(false)
        }
    }, [])

    useEffect(() => {
        void loadCharts(startDate, endDate)
    }, [loadCharts, startDate, endDate])

    useEffect(() => {
        void loadArenaLeaderboard(startDate, endDate, arenaScope)
    }, [loadArenaLeaderboard, startDate, endDate, arenaScope])

    useEffect(() => {
        void loadArenaSymbolCompare(arenaSymbol, startDate, endDate, arenaScope)
    }, [loadArenaSymbolCompare, arenaSymbol, startDate, endDate, arenaScope])

    useEffect(() => {
        void loadArenaDriftAlerts(arenaScope)
    }, [loadArenaDriftAlerts, arenaScope])

    useEffect(() => {
        void loadArenaTrend(startDate, endDate, arenaScope, arenaSelectedModelKey || undefined)
    }, [loadArenaTrend, startDate, endDate, arenaScope, arenaSelectedModelKey])

    useEffect(() => {
        api.listModelProfiles(true)
            .then(res => setModelProfiles((res.profiles || []).filter(p => p.is_active)))
            .catch(() => setModelProfiles([]))
    }, [])

    useEffect(() => {
        if (!selectedRecDate) { setRecDetail([]); return }
        setRecDetailLoading(true)
        api.getInsightsT1RecommendationsDetail(selectedRecDate)
            .then(r => setRecDetail(r.items || []))
            .catch(() => setRecDetail([]))
            .finally(() => setRecDetailLoading(false))
    }, [selectedRecDate])

    useEffect(() => {
        if (!selectedRepDate) { setRepDetail([]); return }
        setRepDetailLoading(true)
        api.getInsightsT1ReportsDetail(selectedRepDate, { scope: repDetailScope })
            .then(r => setRepDetail(r.items || []))
            .catch(() => setRepDetail([]))
            .finally(() => setRepDetailLoading(false))
    }, [selectedRepDate, repDetailScope])

    useEffect(() => {
        if (!selectedConsensusDate) { setConsensusDetail([]); return }
        setConsensusDetailLoading(true)
        api.getInsightsT1MultiModelConsensusDetail(selectedConsensusDate, { scope: 'all' })
            .then(r => setConsensusDetail(r.items || []))
            .catch(() => setConsensusDetail([]))
            .finally(() => setConsensusDetailLoading(false))
    }, [selectedConsensusDate])

    const chartRec = useMemo(
        () => recSeries.map(p => ({ ...p, label: p.date.slice(5) })),
        [recSeries],
    )
    const chartRep = useMemo(() => {
        const pmap = new Map(repSeries.map(p => [p.date, p]))
        const amap = new Map(repSeriesAll.map(p => [p.date, p]))
        const dates = new Set<string>([...pmap.keys(), ...amap.keys()])
        return [...dates]
            .sort((a, b) => a.localeCompare(b))
            .map(date => ({
                date,
                label: date.slice(5),
                accuracy_portfolio_pct: pmap.get(date)?.accuracy_pct ?? null,
                sample_count_portfolio: pmap.get(date)?.sample_count ?? null,
                accuracy_all_pct: amap.get(date)?.accuracy_pct ?? null,
                sample_count_all: amap.get(date)?.sample_count ?? null,
            }))
    }, [repSeries, repSeriesAll])

    const chartConsensus = useMemo(() => {
        const pmap = new Map(consensusSeries.map(p => [p.date, p]))
        const amap = new Map(consensusSeriesAll.map(p => [p.date, p]))
        const dates = new Set<string>([...pmap.keys(), ...amap.keys()])
        return [...dates]
            .sort((a, b) => a.localeCompare(b))
            .map(date => ({
                date,
                label: date.slice(5),
                unanimous_bullish_accuracy_portfolio_pct: pmap.get(date)?.unanimous_bullish_accuracy_pct ?? null,
                unanimous_bullish_count_portfolio: pmap.get(date)?.unanimous_bullish_count ?? null,
                unanimous_bearish_accuracy_portfolio_pct: pmap.get(date)?.unanimous_bearish_accuracy_pct ?? null,
                unanimous_bearish_count_portfolio: pmap.get(date)?.unanimous_bearish_count ?? null,
                unanimous_bullish_accuracy_all_pct: amap.get(date)?.unanimous_bullish_accuracy_pct ?? null,
                unanimous_bullish_count_all: amap.get(date)?.unanimous_bullish_count ?? null,
                unanimous_bearish_accuracy_all_pct: amap.get(date)?.unanimous_bearish_accuracy_pct ?? null,
                unanimous_bearish_count_all: amap.get(date)?.unanimous_bearish_count ?? null,
            }))
    }, [consensusSeries, consensusSeriesAll])

    const arenaTrendData = useMemo(() => {
        const byDate = new Map<string, Record<string, unknown>>()
        arenaTrendSeries.forEach(series => {
            series.points.forEach(p => {
                const row = byDate.get(p.date) || { date: p.date, label: p.date.slice(5) }
                row[series.model_key] = p.accuracy_pct ?? null
                row[`${series.model_key}__count`] = p.sample_count
                byDate.set(p.date, row)
            })
        })
        return Array.from(byDate.values()).sort((a, b) => String(a.date).localeCompare(String(b.date)))
    }, [arenaTrendSeries])

    const arenaTrendColor = useCallback((idx: number) => {
        const palette = ['#2563eb', '#ea580c', '#9333ea', '#0f766e', '#dc2626', '#4f46e5', '#0891b2', '#65a30d']
        return palette[idx % palette.length]
    }, [])

    const reloadAll = useCallback(async () => {
        await Promise.all([
            loadCharts(startDate, endDate),
            loadArenaLeaderboard(startDate, endDate, arenaScope),
            loadArenaSymbolCompare(arenaSymbol, startDate, endDate, arenaScope),
            loadArenaDriftAlerts(arenaScope),
            loadArenaTrend(startDate, endDate, arenaScope, arenaSelectedModelKey || undefined),
        ])
    }, [arenaScope, arenaSelectedModelKey, arenaSymbol, endDate, loadArenaDriftAlerts, loadArenaLeaderboard, loadArenaSymbolCompare, loadArenaTrend, loadCharts, startDate])

    const runRefresh = async () => {
        setRefreshing(true)
        setError(null)
        setRefreshHint(null)
        try {
            // Do NOT pass windowStart/windowEnd: the display date range is for charts
            // only.  Reports that signal on a given date were *created* up to one
            // trading day earlier (after 15:00 cut-off), so a created_at window
            // anchored on startDate 00:00 would silently exclude those records.
            // The backend processes all pending records and _can_evaluate_t1() gates
            // evaluation to T+1 dates whose close has already passed.
            const raw = await api.refreshInsightsT1({})
            const r = raw as Record<string, unknown>
            const ms = (r.market_scan || {}) as Record<string, number>
            const rp = (r.reports || {}) as Record<string, number>
            const pScan = Number(ms.processed ?? 0)
            const eScan = Number(ms.evaluated ?? 0)
            const insScan = Number(ms.insufficient ?? 0)
            const pRep = Number(rp.processed ?? 0)
            const eRep = Number(rp.evaluated ?? 0)

            if (pScan === 0 && pRep === 0) {
                setRefreshHint(
                    '本次没有可处理的记录。左图需要「选股推荐」里点一次生成推荐（会写入 T+1 样本）或跑「每日产品」；右图需要至少完成一次深度分析报告（不限持仓）；「仅持仓」曲线另需导入持仓标的。',
                )
            } else if (pScan > 0 && eScan === 0 && insScan >= pScan) {
                setRefreshHint(
                    '有推荐扫描记录，但未能取到 T0/T1 收盘价（数据源或网络问题）。请确认行情接口可用后重试。',
                )
            } else {
                setRefreshHint(
                    `推荐扫描：处理 ${pScan} 条，新评估 ${eScan} 条；深度报告：处理 ${pRep} 条，新评估 ${eRep} 条。曲线只显示已成功评估的交易日，T1 尚未收盘前不会有点。`,
                )
            }
            await reloadAll()
        } catch (e) {
            setError(e instanceof Error ? e.message : '刷新失败')
        } finally {
            setRefreshing(false)
        }
    }

    const runPromoteDefault = async (profileId: string) => {
        setPromotingProfileId(profileId)
        setError(null)
        setRefreshHint(null)
        try {
            await api.promoteModelArenaDefault({
                profile_id: profileId,
                lookback_days: 90,
                scope: arenaScope,
                min_samples: 20,
                min_accuracy_improvement_pct: 0,
                max_return_drop_pct: 0.5,
            })
            setRefreshHint('默认模型已更新，后续未显式选择模型的分析将使用新默认模型。')
            const profiles = await api.listModelProfiles(true)
            setModelProfiles((profiles.profiles || []).filter(p => p.is_active))
            await reloadAll()
        } catch (e) {
            setError(e instanceof Error ? e.message : '晋升默认模型失败')
        } finally {
            setPromotingProfileId(null)
        }
    }

    const runRollbackDefault = async (profileId: string) => {
        setPromotingProfileId(profileId)
        setError(null)
        setRefreshHint(null)
        try {
            await api.rollbackModelArenaDefault(profileId, 'manual_rollback_from_quality_insights')
            setRefreshHint('默认模型已回滚。')
            const profiles = await api.listModelProfiles(true)
            setModelProfiles((profiles.profiles || []).filter(p => p.is_active))
            await reloadAll()
        } catch (e) {
            setError(e instanceof Error ? e.message : '回滚默认模型失败')
        } finally {
            setPromotingProfileId(null)
        }
    }

    const applyPreset = (days: number) => {
        setCustomRange(false)
        setStartDate(daysAgoStr(days))
        setEndDate(todayStr())
        setSelectedRecDate(null)
        setSelectedRepDate(null)
        setSelectedConsensusDate(null)
    }

    const handleArenaModelSelect = (modelKey: string) => {
        setArenaSelectedModelKey(modelKey)
        if (!modelKey) {
            setArenaModelDetail([])
            setArenaModelDetailDrift([])
            setArenaModelDetailTitle('')
            setArenaModelDetailSummary({ total: 0, accuracy: null, driftCount: 0 })
            return
        }
        const item = arenaRows.find(r => r.model_key === modelKey)
        if (item) {
            void loadArenaModelDetail(item)
        }
    }

    const handleRecChartClick = (data: { activePayload?: { payload: { date?: string } }[] } | null) => {
        if (!data?.activePayload?.[0]?.payload?.date) return
        const d = data.activePayload[0].payload.date
        setSelectedRecDate(prev => prev === d ? null : d)
    }

    const handleRepChartClick = (data: { activePayload?: { payload: { date?: string } }[] } | null) => {
        if (!data?.activePayload?.[0]?.payload?.date) return
        const d = data.activePayload[0].payload.date
        setSelectedRepDate(prev => {
            if (prev === d) return null
            setRepDetailScope('all')
            return d
        })
    }

    const handleConsensusChartClick = (data: { activePayload?: { payload: { date?: string } }[] } | null) => {
        if (!data?.activePayload?.[0]?.payload?.date) return
        const d = data.activePayload[0].payload.date
        setSelectedConsensusDate(prev => prev === d ? null : d)
    }

    const currentDefaultProfileId = useMemo(
        () => (modelProfiles.find(p => p.is_default)?.id ?? null),
        [modelProfiles],
    )

    return (
        <div className="space-y-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">T+1 质量曲线</h1>
                    <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                        信号日 T0 收盘 → 下一交易日 T1 收盘；每个交易日 <strong className="text-slate-600 dark:text-slate-300">15:10 后自动评估</strong>，也可手动触发。仅供复盘，不构成投资建议。
                    </p>
                </div>
                <div className="flex items-center gap-2">
                    <button
                        type="button"
                        onClick={() => void reloadAll()}
                        disabled={loading}
                        className="inline-flex items-center gap-1 rounded-lg border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800"
                    >
                        <Activity className={`h-4 w-4 ${loading ? 'animate-pulse' : ''}`} />
                        重新加载
                    </button>
                    <button
                        type="button"
                        onClick={() => void runRefresh()}
                        disabled={refreshing}
                        className="inline-flex items-center gap-1 rounded-lg bg-violet-600 px-3 py-1.5 text-sm text-white hover:bg-violet-700 disabled:opacity-60"
                        title="每个交易日 15:10 后系统会自动执行；也可手动触发立即评估"
                    >
                        <RefreshCw className={`h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
                        手动刷新 T+1 评估
                    </button>
                </div>
            </div>

            {error && (
                <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-600 dark:border-rose-900/50 dark:bg-rose-950/30 dark:text-rose-300">
                    {error}
                </div>
            )}
            {refreshHint && (
                <div className="flex items-start justify-between gap-2 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2 text-sm text-sky-900 dark:border-sky-800/50 dark:bg-sky-950/30 dark:text-sky-200">
                    <span>{refreshHint}</span>
                    <button
                        type="button"
                        onClick={() => setRefreshHint(null)}
                        className="shrink-0 rounded px-1 text-sky-600 hover:bg-sky-100 dark:text-sky-300 dark:hover:bg-sky-900/50"
                    >
                        关闭
                    </button>
                </div>
            )}

            {/* Date Range Control */}
            <div className="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 py-3 dark:border-slate-700 dark:bg-slate-900">
                <Calendar className="h-4 w-4 shrink-0 text-slate-500" />
                <span className="text-sm font-medium text-slate-600 dark:text-slate-400">时间范围：</span>
                <div className="flex flex-wrap gap-1.5">
                    {PRESET_RANGES.map(({ label, days }) => {
                        const active = !customRange && startDate === daysAgoStr(days) && endDate === todayStr()
                        return (
                            <button
                                key={days}
                                type="button"
                                onClick={() => applyPreset(days)}
                                className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                                    active
                                        ? 'bg-violet-600 text-white'
                                        : 'border border-slate-300 text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-400 dark:hover:bg-slate-800'
                                }`}
                            >
                                {label}
                            </button>
                        )
                    })}
                    <button
                        type="button"
                        onClick={() => setCustomRange(v => !v)}
                        className={`inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                            customRange
                                ? 'bg-violet-600 text-white'
                                : 'border border-slate-300 text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-400 dark:hover:bg-slate-800'
                        }`}
                    >
                        自定义
                        {customRange ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                    </button>
                </div>
                {customRange && (
                    <div className="flex flex-wrap items-center gap-2">
                        <input
                            type="date"
                            value={startDate}
                            max={endDate}
                            onChange={e => { setStartDate(e.target.value); setSelectedRecDate(null); setSelectedRepDate(null); setSelectedConsensusDate(null) }}
                            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
                        />
                        <span className="text-xs text-slate-400">至</span>
                        <input
                            type="date"
                            value={endDate}
                            min={startDate}
                            max={todayStr()}
                            onChange={e => { setEndDate(e.target.value); setSelectedRecDate(null); setSelectedRepDate(null); setSelectedConsensusDate(null) }}
                            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
                        />
                    </div>
                )}
            </div>

                <div className="rounded-xl border border-slate-200 bg-amber-50/80 px-4 py-3 text-sm text-amber-900 dark:border-amber-900/40 dark:bg-amber-950/20 dark:text-amber-200/90">
                <strong className="font-medium">说明：</strong>
                左图为推荐 T+1 质量（紫线=平均收益、橙线=上涨占比）；右图为深度分析方向正确率（深绿=当前导入持仓、靛蓝=全部深度报告）。图表横轴为<strong>被预测交易日</strong>：盘前报告测「上一收→当日收」，盘中/盘后报告测「当日收→次日收」（A 股 15:00 收盘价，close→close）。下方「多模型共识」统计同一预测日、同一股票上 ≥2 个不同模型同向看多看空的正确率。中性观点不计入统计。<strong>点击图表区数据点</strong>可查看当日明细；曲线下方图例可<strong>多选切换</strong>显示/隐藏单条线，默认全显示。
            </div>

            <div className="space-y-3 rounded-xl border border-indigo-200 bg-indigo-50/40 p-4 dark:border-indigo-900/40 dark:bg-indigo-950/20">
                <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                        <h2 className="text-base font-semibold text-indigo-900 dark:text-indigo-100">Model Arena（T+1）</h2>
                        <p className="text-xs text-indigo-700/80 dark:text-indigo-200/80">
                            基于同一时间窗的深度分析结果，比较模型准确率、收益与稳定性，并支持门禁晋升/回滚默认模型。
                        </p>
                    </div>
                    <div className="inline-flex rounded-lg border border-indigo-200/80 bg-white/80 p-0.5 text-xs dark:border-indigo-800/60 dark:bg-indigo-950/40">
                        <button
                            type="button"
                            onClick={() => setArenaScope('portfolio')}
                            className={`rounded-md px-2 py-1 font-medium transition-colors ${
                                arenaScope === 'portfolio'
                                    ? 'bg-indigo-600 text-white'
                                    : 'text-indigo-800 hover:bg-indigo-100 dark:text-indigo-200 dark:hover:bg-indigo-900/50'
                            }`}
                        >
                            仅持仓
                        </button>
                        <button
                            type="button"
                            onClick={() => setArenaScope('all')}
                            className={`rounded-md px-2 py-1 font-medium transition-colors ${
                                arenaScope === 'all'
                                    ? 'bg-indigo-600 text-white'
                                    : 'text-indigo-800 hover:bg-indigo-100 dark:text-indigo-200 dark:hover:bg-indigo-900/50'
                            }`}
                        >
                            全部
                        </button>
                    </div>
                </div>

                <div className="rounded-lg border border-indigo-200/70 bg-white/70 p-3 dark:border-indigo-800/50 dark:bg-indigo-950/30">
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                        <div className="flex flex-wrap items-center gap-3">
                            <div className="text-sm font-medium text-slate-800 dark:text-slate-100">
                                模型正确率曲线（点击点位看该模型当日明细）
                            </div>
                            <label className="inline-flex items-center gap-1.5 text-xs text-slate-600 dark:text-slate-300">
                                <span>选择模型</span>
                                <select
                                    value={arenaSelectedModelKey}
                                    onChange={e => handleArenaModelSelect(e.target.value)}
                                    className="max-w-[220px] rounded-md border border-indigo-200 bg-white px-2 py-1 text-xs dark:border-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-100"
                                >
                                    <option value="">Top 模型对比（默认）</option>
                                    {arenaRows.map(row => (
                                        <option key={row.model_key} value={row.model_key}>
                                            {row.deep_think_llm || row.quick_think_llm || row.model_display_name}
                                        </option>
                                    ))}
                                </select>
                            </label>
                        </div>
                        <button
                            type="button"
                            className="rounded-md border border-indigo-300 px-2 py-1 text-xs text-indigo-700 hover:bg-indigo-100 dark:border-indigo-700 dark:text-indigo-200 dark:hover:bg-indigo-900/50"
                            onClick={() => void loadArenaTrend(startDate, endDate, arenaScope, arenaSelectedModelKey || undefined)}
                            disabled={arenaTrendLoading}
                        >
                            刷新曲线
                        </button>
                    </div>
                    {arenaTrendData.length === 0 && !arenaTrendLoading ? (
                        <div className="flex h-52 items-center justify-center text-sm text-slate-400">暂无模型曲线样本</div>
                    ) : (
                        <div className="h-56 w-full">
                            <ResponsiveContainer width="100%" height="100%">
                                <LineChart
                                    data={arenaTrendData}
                                    margin={{ top: 8, right: 8, left: 0, bottom: 0 }}
                                >
                                    <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-700" />
                                    <XAxis dataKey="label" tick={{ fontSize: 11 }} className="text-slate-500" />
                                    <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} className="text-slate-500" />
                                    <Tooltip
                                        contentStyle={{ fontSize: 12 }}
                                        formatter={(value: unknown, name: string, item: { payload?: Record<string, unknown> }) => {
                                            const n = typeof value === 'number' ? value : Number(value)
                                            const accText = Number.isNaN(n) ? '—' : `${n.toFixed(2)}%`
                                            const count = Number(item?.payload?.[`${name}__count`] ?? 0)
                                            const series = arenaTrendSeries.find(s => s.model_key === name)
                                            const label = series?.deep_think_llm || series?.quick_think_llm || series?.model_display_name || name
                                            return [`${accText}（样本${count}）`, label]
                                        }}
                                        labelFormatter={(label, payload) => {
                                            const date = payload?.[0]?.payload?.date ?? label
                                            return `${date} · 点击查看该模型当日明细`
                                        }}
                                    />
                                    {arenaTrendSeries.map((s, idx) => {
                                        const color = arenaTrendColor(idx)
                                        const renderDot = (radius: number) => (props: {
                                            cx?: number
                                            cy?: number
                                            payload?: { date?: string }
                                        }) => {
                                            const { cx, cy, payload } = props
                                            const signalDate = payload?.date ? String(payload.date) : ''
                                            const visible = typeof cx === 'number' && typeof cy === 'number' && Boolean(signalDate)
                                            return (
                                                <circle
                                                    cx={visible ? cx : 0}
                                                    cy={visible ? cy : 0}
                                                    r={visible ? radius : 0}
                                                    fill={color}
                                                    stroke="#fff"
                                                    strokeWidth={1.5}
                                                    style={{ cursor: visible ? 'pointer' : 'default' }}
                                                    onClick={(ev) => {
                                                        if (!visible) return
                                                        ev.stopPropagation()
                                                        void loadArenaModelDetailForPoint(s.model_key, signalDate)
                                                    }}
                                                />
                                            )
                                        }
                                        return (
                                        <Line
                                            key={s.model_key}
                                            type="monotone"
                                            dataKey={s.model_key}
                                            name={s.model_key}
                                            stroke={color}
                                            strokeWidth={2}
                                            strokeDasharray="6 4"
                                            dot={renderDot(3)}
                                            activeDot={renderDot(5)}
                                            connectNulls
                                        />
                                        )
                                    })}
                                </LineChart>
                            </ResponsiveContainer>
                        </div>
                    )}
                    <div className="mt-2 flex flex-wrap gap-2">
                        {arenaTrendSeries.map((s, idx) => (
                            <div key={s.model_key} className="inline-flex items-center gap-1 text-[11px] text-slate-600 dark:text-slate-300">
                                <span className="h-0.5 w-4 rounded-full" style={{ backgroundColor: arenaTrendColor(idx) }} />
                                {s.deep_think_llm || s.quick_think_llm || s.model_display_name}
                            </div>
                        ))}
                    </div>
                </div>

                <div className="rounded-lg border border-indigo-200/70 bg-white/70 p-3 dark:border-indigo-800/50 dark:bg-indigo-950/30">
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                        <div className="text-sm font-medium text-slate-800 dark:text-slate-100">
                            模型榜单（样本总数：{arenaLoading ? '...' : arenaSamples}）
                        </div>
                        <button
                            type="button"
                            className="rounded-md border border-indigo-300 px-2 py-1 text-xs text-indigo-700 hover:bg-indigo-100 dark:border-indigo-700 dark:text-indigo-200 dark:hover:bg-indigo-900/50"
                            onClick={() => void loadArenaLeaderboard(startDate, endDate, arenaScope)}
                            disabled={arenaLoading}
                        >
                            刷新榜单
                        </button>
                    </div>
                    <div className="overflow-x-auto">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="text-left text-xs text-slate-500 dark:text-slate-400">
                                    <th className="px-2 py-1.5">排名</th>
                                    <th className="px-2 py-1.5">模型</th>
                                    <th className="px-2 py-1.5">样本</th>
                                    <th className="px-2 py-1.5">准确率</th>
                                    <th className="px-2 py-1.5">平均收益</th>
                                    <th className="px-2 py-1.5">波动</th>
                                    <th className="px-2 py-1.5">综合分</th>
                                    <th className="px-2 py-1.5">操作</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                                {arenaRows.map(item => {
                                    const isDefault = currentDefaultProfileId != null && item.model_profile_id === currentDefaultProfileId
                                    return (
                                        <tr key={item.model_key} className="hover:bg-indigo-50/60 dark:hover:bg-indigo-950/30">
                                            <td className="px-2 py-1.5 font-medium text-slate-700 dark:text-slate-200">#{item.rank}</td>
                                            <td className="px-2 py-1.5">
                                                <div className="space-y-0.5">
                                                    <div className="font-medium text-slate-800 dark:text-slate-100">
                                                        {item.deep_think_llm || item.quick_think_llm || item.model_display_name}
                                                        {isDefault && (
                                                            <span className="ml-1 rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">
                                                                默认
                                                            </span>
                                                        )}
                                                    </div>
                                                    <div className="text-xs text-slate-500 dark:text-slate-400">{item.model_profile_name || item.llm_provider || '—'}</div>
                                                </div>
                                            </td>
                                            <td className="px-2 py-1.5 font-mono text-slate-600 dark:text-slate-300">{item.sample_count}</td>
                                            <td className="px-2 py-1.5 font-mono text-slate-600 dark:text-slate-300">{percentText(item.accuracy_pct)}</td>
                                            <td className="px-2 py-1.5 font-mono text-slate-600 dark:text-slate-300">{percentText(item.avg_return_t1_pct, 4)}</td>
                                            <td className="px-2 py-1.5 font-mono text-slate-600 dark:text-slate-300">{item.return_volatility.toFixed(4)}</td>
                                            <td className="px-2 py-1.5 font-mono text-slate-700 dark:text-slate-200">{item.composite_score.toFixed(2)}</td>
                                            <td className="px-2 py-1.5">
                                                {item.model_profile_id ? (
                                                    <div className="flex items-center gap-1">
                                                        <button
                                                            type="button"
                                                            className="rounded border border-sky-300 px-1.5 py-0.5 text-xs text-sky-700 hover:bg-sky-100 dark:border-sky-700 dark:text-sky-200 dark:hover:bg-sky-900/50"
                                                            onClick={() => {
                                                                setArenaSelectedModelKey(item.model_key)
                                                                void loadArenaModelDetail(item)
                                                            }}
                                                        >
                                                            查看明细
                                                        </button>
                                                        <button
                                                            type="button"
                                                            disabled={promotingProfileId === item.model_profile_id || isDefault}
                                                            className="rounded border border-indigo-300 px-1.5 py-0.5 text-xs text-indigo-700 hover:bg-indigo-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-indigo-700 dark:text-indigo-200 dark:hover:bg-indigo-900/50"
                                                            onClick={() => void runPromoteDefault(item.model_profile_id as string)}
                                                        >
                                                            晋升默认
                                                        </button>
                                                        <button
                                                            type="button"
                                                            disabled={promotingProfileId === item.model_profile_id}
                                                            className="rounded border border-slate-300 px-1.5 py-0.5 text-xs text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
                                                            onClick={() => void runRollbackDefault(item.model_profile_id as string)}
                                                        >
                                                            回滚到此
                                                        </button>
                                                    </div>
                                                ) : (
                                                    <span className="text-xs text-slate-400">仅可晋升已保存配置</span>
                                                )}
                                            </td>
                                        </tr>
                                    )
                                })}
                                {!arenaLoading && arenaRows.length === 0 && (
                                    <tr>
                                        <td colSpan={8} className="px-2 py-4 text-center text-sm text-slate-400">当前窗口暂无可比较模型样本</td>
                                    </tr>
                                )}
                            </tbody>
                        </table>
                    </div>
                    {(arenaModelDetailLoading || arenaModelDetail.length > 0) && (
                        <div className="mt-3 rounded-md border border-sky-200 bg-sky-50/60 p-2 dark:border-sky-900/50 dark:bg-sky-950/20">
                            <div className="mb-2 flex items-center justify-between gap-2">
                                <div className="flex flex-wrap items-center gap-2">
                                    <div className="text-xs font-medium text-sky-900 dark:text-sky-200">
                                        {arenaModelDetailTitle || '模型'} · T+1 明细（样本 {arenaModelDetailSummary.total}，正确率 {percentText(arenaModelDetailSummary.accuracy)}）
                                    </div>
                                    {arenaModelDetailSummary.driftCount > 0 && (
                                        <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
                                            ⚠ {arenaModelDetailSummary.driftCount} 条疑似日期漂移（T+1 收益=0，请刷新 T+1 评估）
                                        </span>
                                    )}
                                </div>
                                <button
                                    type="button"
                                    className="rounded border border-sky-300 px-1.5 py-0.5 text-[11px] text-sky-700 hover:bg-sky-100 dark:border-sky-700 dark:text-sky-200 dark:hover:bg-sky-900/50"
                                    onClick={() => { setArenaModelDetail([]); setArenaModelDetailDrift([]); setArenaModelDetailTitle('') }}
                                >
                                    收起
                                </button>
                            </div>
                            {arenaModelDetailLoading ? (
                                <div className="py-4 text-center text-xs text-slate-400">加载中...</div>
                            ) : (
                                <div className="max-h-72 overflow-auto">
                                    <table className="w-full text-xs">
                                        <thead>
                                            <tr className="text-left text-slate-500 dark:text-slate-400">
                                                <th className="px-2 py-1">信号日</th>
                                                <th className="px-2 py-1">股票代码</th>
                                                <th className="px-2 py-1">名称</th>
                                                <th className="px-2 py-1">决策</th>
                                                <th className="px-2 py-1">正确</th>
                                                <th className="px-2 py-1 text-right">T+1收益</th>
                                                <th className="px-2 py-1">报告生成时间</th>
                                            </tr>
                                        </thead>
                                        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                                            {arenaModelDetail.map((row, idx) => (
                                                <tr
                                                    key={`${row.report_id || row.symbol}-${row.signal_trade_date}-${idx}`}
                                                >
                                                    <td className="px-2 py-1 font-mono text-slate-600 dark:text-slate-300">
                                                        {row.signal_trade_date}
                                                    </td>
                                                    <td className="px-2 py-1 font-mono text-slate-700 dark:text-slate-200">{row.symbol}</td>
                                                    <td className="px-2 py-1 text-slate-600 dark:text-slate-300">{row.name ?? '—'}</td>
                                                    <td className="px-2 py-1 text-slate-600 dark:text-slate-300">{row.decision || '—'}</td>
                                                    <td className="px-2 py-1">{correctBadge(row.label_correct)}</td>
                                                    <td className="px-2 py-1 text-right">{returnBadge(row.return_t1_pct)}</td>
                                                    <td className="px-2 py-1 font-mono text-slate-400 dark:text-slate-500">
                                                        {row.report_created_at
                                                            ? new Date(row.report_created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
                                                            : '—'}
                                                    </td>
                                                </tr>
                                            ))}
                                            {arenaModelDetailDrift.length > 0 && (
                                                <tr className="bg-amber-50/40 dark:bg-amber-900/10">
                                                    <td colSpan={7} className="px-2 py-1.5 text-[11px] font-medium text-amber-700 dark:text-amber-300">
                                                        以下 {arenaModelDetailDrift.length} 条疑似日期漂移，未计入正确率
                                                    </td>
                                                </tr>
                                            )}
                                            {arenaModelDetailDrift.map((row, idx) => (
                                                <tr
                                                    key={`drift-${row.report_id || row.symbol}-${row.signal_trade_date}-${idx}`}
                                                    className="bg-amber-50/60 dark:bg-amber-900/10"
                                                >
                                                    <td className="px-2 py-1 font-mono text-slate-600 dark:text-slate-300">
                                                        <span title={`疑似日期漂移：T+1 收益为 0，信号日 ${row.signal_trade_date} T+1日 ${row.t1_trade_date ?? '—'}`} className="mr-1 text-amber-500">⚠</span>
                                                        {row.signal_trade_date}
                                                    </td>
                                                    <td className="px-2 py-1 font-mono text-slate-700 dark:text-slate-200">{row.symbol}</td>
                                                    <td className="px-2 py-1 text-slate-600 dark:text-slate-300">{row.name ?? '—'}</td>
                                                    <td className="px-2 py-1 text-slate-600 dark:text-slate-300">{row.decision || '—'}</td>
                                                    <td className="px-2 py-1">{correctBadge(row.label_correct)}</td>
                                                    <td className="px-2 py-1 text-right">{returnBadge(row.return_t1_pct)}</td>
                                                    <td className="px-2 py-1 font-mono text-slate-400 dark:text-slate-500">
                                                        {row.report_created_at
                                                            ? new Date(row.report_created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
                                                            : '—'}
                                                    </td>
                                                </tr>
                                            ))}
                                            {arenaModelDetail.length === 0 && arenaModelDetailDrift.length === 0 && (
                                                <tr>
                                                    <td colSpan={7} className="px-2 py-3 text-center text-slate-400">该模型在窗口内暂无明细样本</td>
                                                </tr>
                                            )}
                                        </tbody>
                                    </table>
                                </div>
                            )}
                        </div>
                    )}
                </div>

                <div className="rounded-lg border border-indigo-200/70 bg-white/70 p-3 dark:border-indigo-800/50 dark:bg-indigo-950/30">
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                        <div className="text-sm font-medium text-slate-800 dark:text-slate-100">同一股票跨模型对比（T+1）</div>
                        <div className="flex items-center gap-2">
                            <input
                                type="text"
                                value={arenaSymbol}
                                onChange={e => setArenaSymbol(e.target.value.toUpperCase())}
                                placeholder="如 600519.SH"
                                className="h-8 rounded-md border border-indigo-300 bg-white px-2 text-xs text-slate-700 outline-none ring-indigo-300 focus:ring-2 dark:border-indigo-700 dark:bg-slate-900 dark:text-slate-200"
                            />
                            <button
                                type="button"
                                className="rounded-md border border-indigo-300 px-2 py-1 text-xs text-indigo-700 hover:bg-indigo-100 dark:border-indigo-700 dark:text-indigo-200 dark:hover:bg-indigo-900/50"
                                onClick={() => void loadArenaSymbolCompare(arenaSymbol, startDate, endDate, arenaScope)}
                                disabled={arenaSymbolLoading}
                            >
                                查询
                            </button>
                        </div>
                    </div>
                    <div className="overflow-x-auto">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="text-left text-xs text-slate-500 dark:text-slate-400">
                                    <th className="px-2 py-1.5">信号日</th>
                                    <th className="px-2 py-1.5">模型</th>
                                    <th className="px-2 py-1.5">决策</th>
                                    <th className="px-2 py-1.5">方向正确</th>
                                    <th className="px-2 py-1.5">T+1 收益</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                                {arenaSymbolRows.map((row, idx) => (
                                    <tr key={`${row.report_id || row.model_key}-${row.signal_trade_date}-${idx}`} className="hover:bg-indigo-50/60 dark:hover:bg-indigo-950/30">
                                        <td className="px-2 py-1.5 font-mono text-slate-600 dark:text-slate-300">{row.signal_trade_date}</td>
                                        <td className="px-2 py-1.5 text-slate-700 dark:text-slate-200">{row.deep_think_llm || row.quick_think_llm || row.model_display_name}</td>
                                        <td className="px-2 py-1.5 text-slate-600 dark:text-slate-300">{row.decision || '—'}</td>
                                        <td className="px-2 py-1.5">{correctBadge(row.label_correct)}</td>
                                        <td className="px-2 py-1.5">{returnBadge(row.return_t1_pct)}</td>
                                    </tr>
                                ))}
                                {!arenaSymbolLoading && arenaSymbolRows.length === 0 && (
                                    <tr>
                                        <td colSpan={5} className="px-2 py-4 text-center text-sm text-slate-400">
                                            当前股票在所选时间窗无可比较样本（请先用多个模型对同一股票发起分析并完成 T+1 评估）
                                        </td>
                                    </tr>
                                )}
                            </tbody>
                        </table>
                    </div>
                </div>

                <div className="rounded-lg border border-indigo-200/70 bg-white/70 p-3 dark:border-indigo-800/50 dark:bg-indigo-950/30">
                    <div className="mb-2 flex items-center justify-between gap-2">
                        <div className="text-sm font-medium text-slate-800 dark:text-slate-100">模型漂移告警（近7天 vs 前30天）</div>
                        <button
                            type="button"
                            className="rounded-md border border-indigo-300 px-2 py-1 text-xs text-indigo-700 hover:bg-indigo-100 dark:border-indigo-700 dark:text-indigo-200 dark:hover:bg-indigo-900/50"
                            onClick={() => void loadArenaDriftAlerts(arenaScope)}
                            disabled={arenaDriftLoading}
                        >
                            刷新告警
                        </button>
                    </div>
                    <div className="overflow-x-auto">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="text-left text-xs text-slate-500 dark:text-slate-400">
                                    <th className="px-2 py-1.5">模型</th>
                                    <th className="px-2 py-1.5">前30天准确率</th>
                                    <th className="px-2 py-1.5">近7天准确率</th>
                                    <th className="px-2 py-1.5">下降幅度</th>
                                    <th className="px-2 py-1.5">样本(近/前)</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                                {arenaDriftAlerts.map(item => (
                                    <tr key={item.model_key} className="hover:bg-indigo-50/60 dark:hover:bg-indigo-950/30">
                                        <td className="px-2 py-1.5 text-slate-700 dark:text-slate-200">{item.model_display_name || item.model_profile_name || item.model_key}</td>
                                        <td className="px-2 py-1.5 font-mono text-slate-600 dark:text-slate-300">{percentText(item.baseline_accuracy_pct)}</td>
                                        <td className="px-2 py-1.5 font-mono text-slate-600 dark:text-slate-300">{percentText(item.recent_accuracy_pct)}</td>
                                        <td className="px-2 py-1.5 font-mono text-rose-600 dark:text-rose-300">-{percentText(item.drop_pct)}</td>
                                        <td className="px-2 py-1.5 font-mono text-slate-600 dark:text-slate-300">
                                            {item.recent_sample_count}/{item.baseline_sample_count}
                                        </td>
                                    </tr>
                                ))}
                                {!arenaDriftLoading && arenaDriftAlerts.length === 0 && (
                                    <tr>
                                        <td colSpan={5} className="px-2 py-4 text-center text-sm text-slate-400">
                                            暂无漂移告警（当前模型近期表现未显著劣化）
                                        </td>
                                    </tr>
                                )}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
                {/* Recommendation Quality Chart */}
                <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
                    <div className="mb-3 flex items-center gap-2 text-sm font-medium text-slate-800 dark:text-slate-100">
                        <TrendingUp className="h-4 w-4 text-violet-500" />
                        每日推荐质量（T+1 收益）
                    </div>
                    {recDesc && <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">{recDesc}</p>}
                    <div className="w-full min-w-0">
                        {chartRec.length === 0 && !loading ? (
                            <div className="flex h-72 items-center justify-center text-sm text-slate-400">
                                暂无已评估样本。请使用股票推荐/每日批跑产生扫描记录，并于 T1 收盘后刷新评估。
                            </div>
                        ) : (
                            <>
                                <div className="h-72 w-full min-w-0">
                                    <ResponsiveContainer width="100%" height="100%">
                                    <LineChart
                                        data={chartRec}
                                        margin={{ top: 8, right: 8, left: 0, bottom: 0 }}
                                        onClick={handleRecChartClick}
                                        style={{ cursor: 'pointer' }}
                                    >
                                        <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-700" />
                                        <XAxis dataKey="label" tick={{ fontSize: 11 }} className="text-slate-500" />
                                        <YAxis yAxisId="l" tick={{ fontSize: 11 }} className="text-slate-500" />
                                        <YAxis yAxisId="r" orientation="right" domain={[0, 100]} tick={{ fontSize: 11 }} className="text-slate-500" />
                                        <Tooltip
                                            contentStyle={{ fontSize: 12 }}
                                            formatter={(value: number, name: string) => [
                                                name === REC_LINE.win_rate_pct.label ? `${value?.toFixed?.(2) ?? value}%` : value,
                                                name === REC_LINE.avg_return_pct.label ? REC_LINE.avg_return_pct.label : REC_LINE.win_rate_pct.label,
                                            ]}
                                            labelFormatter={(label, payload) => {
                                                const date = payload?.[0]?.payload?.date ?? label
                                                const count = payload?.[0]?.payload?.sample_count
                                                return `${date}${count != null ? `（${count} 只）` : ''} · 点击查看明细`
                                            }}
                                        />
                                        <Line
                                            yAxisId="l"
                                            type="monotone"
                                            dataKey="avg_return_pct"
                                            name={REC_LINE.avg_return_pct.label}
                                            stroke={REC_LINE.avg_return_pct.color}
                                            strokeWidth={2}
                                            dot={{ r: 3, strokeWidth: 2, fill: REC_LINE.avg_return_pct.color }}
                                            activeDot={{ r: 5 }}
                                            connectNulls
                                            hide={!recLineVisible.avg_return_pct}
                                        />
                                        <Line
                                            yAxisId="r"
                                            type="monotone"
                                            dataKey="win_rate_pct"
                                            name={REC_LINE.win_rate_pct.label}
                                            stroke={REC_LINE.win_rate_pct.color}
                                            strokeWidth={2}
                                            dot={{ r: 3, strokeWidth: 2, fill: REC_LINE.win_rate_pct.color }}
                                            activeDot={{ r: 5 }}
                                            connectNulls
                                            hide={!recLineVisible.win_rate_pct}
                                        />
                                    </LineChart>
                                    </ResponsiveContainer>
                                </div>
                                <SeriesLegendBar series={REC_LINE} visible={recLineVisible} onToggle={toggleRecLine} />
                            </>
                        )}
                    </div>
                    <p className="mt-3 text-xs leading-relaxed text-slate-400">横轴为信号日（T0）。点击下方图例可隐藏/显示单条曲线（至少保留一条）；点击数据点查看当日推荐明细。</p>
                    {selectedRecDate && (
                        <RecDetailPanel
                            date={selectedRecDate}
                            items={recDetail}
                            loading={recDetailLoading}
                            onClose={() => setSelectedRecDate(null)}
                        />
                    )}
                </div>

                {/* Report Accuracy Chart */}
                <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
                    <div className="mb-3 flex items-center gap-2 text-sm font-medium text-slate-800 dark:text-slate-100">
                        <Activity className="h-4 w-4 text-emerald-500" />
                        深度分析方向正确率（T+1）
                    </div>
                    {repDesc && <p className="mb-1 text-xs text-slate-500 dark:text-slate-400">{repDesc}</p>}
                    {repDescAll && <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">{repDescAll}</p>}
                    <AccuracyHonestyBanner summary={repSummaryAll ?? repSummary} note={repDenominatorNote} />
                    <div className="w-full min-w-0">
                        {chartRep.length === 0 && !loading ? (
                            <div className="flex h-72 items-center justify-center text-sm text-slate-400">
                                暂无数据。请完成至少一只标的的深度分析报告，并于 T1 收盘后刷新评估；「仅持仓」曲线另需导入持仓。
                            </div>
                        ) : (
                            <>
                                <div className="h-72 w-full min-w-0">
                                    <ResponsiveContainer width="100%" height="100%">
                                    <LineChart
                                        data={chartRep}
                                        margin={{ top: 8, right: 8, left: 0, bottom: 0 }}
                                        onClick={handleRepChartClick}
                                        style={{ cursor: 'pointer' }}
                                    >
                                        <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-700" />
                                        <XAxis dataKey="label" tick={{ fontSize: 11 }} className="text-slate-500" />
                                        <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} className="text-slate-500" />
                                        <Tooltip
                                            contentStyle={{ fontSize: 12 }}
                                            formatter={(value, name) => {
                                                const n = typeof value === 'number' ? value : Number(value)
                                                const text = value != null && value !== '' && !Number.isNaN(n)
                                                    ? `${n.toFixed(2)}%`
                                                    : '—'
                                                return [text, name]
                                            }}
                                            labelFormatter={(_label, payload) => {
                                                const row = payload?.[0]?.payload as {
                                                    date?: string
                                                    sample_count_portfolio?: number | null
                                                    sample_count_all?: number | null
                                                } | undefined
                                                const date = row?.date ?? _label
                                                const c1 = row?.sample_count_portfolio
                                                const c2 = row?.sample_count_all
                                                const bits: string[] = []
                                                if (c1 != null) bits.push(`持仓样本 ${c1}`)
                                                if (c2 != null) bits.push(`全部样本 ${c2}`)
                                                const suf = bits.length ? `（${bits.join('；')}）` : ''
                                                return `${date}${suf} · 点击查看明细`
                                            }}
                                        />
                                        <Line
                                            type="monotone"
                                            dataKey="accuracy_portfolio_pct"
                                            name={REP_LINE.accuracy_portfolio_pct.label}
                                            stroke={REP_LINE.accuracy_portfolio_pct.color}
                                            strokeWidth={2}
                                            dot={{ r: 3, strokeWidth: 2, fill: REP_LINE.accuracy_portfolio_pct.color }}
                                            activeDot={{ r: 5 }}
                                            connectNulls
                                            hide={!repLineVisible.accuracy_portfolio_pct}
                                        />
                                        <Line
                                            type="monotone"
                                            dataKey="accuracy_all_pct"
                                            name={REP_LINE.accuracy_all_pct.label}
                                            stroke={REP_LINE.accuracy_all_pct.color}
                                            strokeWidth={2}
                                            dot={{ r: 3, strokeWidth: 2, fill: REP_LINE.accuracy_all_pct.color }}
                                            activeDot={{ r: 5 }}
                                            connectNulls
                                            hide={!repLineVisible.accuracy_all_pct}
                                        />
                                    </LineChart>
                                    </ResponsiveContainer>
                                </div>
                                <SeriesLegendBar series={REP_LINE} visible={repLineVisible} onToggle={toggleRepLine} />
                            </>
                        )}
                    </div>
                    <p className="mt-3 text-xs leading-relaxed text-slate-400">仅统计非中性观点。点击下方图例可隐藏/显示单条曲线（至少保留一条）；点击数据点查看当日深度报告明细，可在明细中切换「仅持仓 / 全部」。</p>
                    {selectedRepDate && (
                        <RepDetailPanel
                            date={selectedRepDate}
                            items={repDetail}
                            loading={repDetailLoading}
                            onClose={() => setSelectedRepDate(null)}
                            detailScope={repDetailScope}
                            onDetailScopeChange={setRepDetailScope}
                        />
                    )}
                </div>
            </div>

            <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
                <div className="mb-3 flex items-center gap-2 text-sm font-medium text-slate-800 dark:text-slate-100">
                    <Activity className="h-4 w-4 text-violet-500" />
                    多模型同日共识正确率（T+1）
                </div>
                {consensusDesc && <p className="mb-1 text-xs text-slate-500 dark:text-slate-400">{consensusDesc}</p>}
                {consensusDescAll && <p className="mb-2 text-xs text-slate-500 dark:text-slate-400">{consensusDescAll}</p>}
                <div className="w-full min-w-0">
                    {chartConsensus.length === 0 && !loading ? (
                        <div className="flex h-72 items-center justify-center text-sm text-slate-400">
                            暂无数据。需在同一信号日对同一股票用 ≥2 个不同模型完成深度分析并完成 T+1 评估。
                        </div>
                    ) : (
                        <>
                            <div className="h-72 w-full min-w-0">
                                <ResponsiveContainer width="100%" height="100%">
                                    <LineChart
                                        data={chartConsensus}
                                        margin={{ top: 8, right: 8, left: 0, bottom: 0 }}
                                        onClick={handleConsensusChartClick}
                                        style={{ cursor: 'pointer' }}
                                    >
                                        <CartesianGrid strokeDasharray="3 3" className="stroke-slate-200 dark:stroke-slate-700" />
                                        <XAxis dataKey="label" tick={{ fontSize: 11 }} className="text-slate-500" />
                                        <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} className="text-slate-500" />
                                        <Tooltip
                                            contentStyle={{ fontSize: 12 }}
                                            formatter={(value, name) => {
                                                const n = typeof value === 'number' ? value : Number(value)
                                                const text = value != null && value !== '' && !Number.isNaN(n)
                                                    ? `${n.toFixed(2)}%`
                                                    : '—'
                                                return [text, name]
                                            }}
                                            labelFormatter={(_label, payload) => {
                                                const row = payload?.[0]?.payload as {
                                                    date?: string
                                                    unanimous_bullish_count_portfolio?: number | null
                                                    unanimous_bullish_count_all?: number | null
                                                    unanimous_bearish_count_portfolio?: number | null
                                                    unanimous_bearish_count_all?: number | null
                                                } | undefined
                                                const date = row?.date ?? _label
                                                const bits: string[] = []
                                                if (row?.unanimous_bullish_count_portfolio) bits.push(`看涨共识(持仓) ${row.unanimous_bullish_count_portfolio}`)
                                                if (row?.unanimous_bullish_count_all) bits.push(`看涨共识(全部) ${row.unanimous_bullish_count_all}`)
                                                if (row?.unanimous_bearish_count_portfolio) bits.push(`看跌共识(持仓) ${row.unanimous_bearish_count_portfolio}`)
                                                if (row?.unanimous_bearish_count_all) bits.push(`看跌共识(全部) ${row.unanimous_bearish_count_all}`)
                                                const suf = bits.length ? `（${bits.join('；')}）` : ''
                                                return `${date}${suf} · 点击查看明细`
                                            }}
                                        />
                                        {(Object.keys(CONS_LINE) as ConsLineKey[]).map(key => (
                                            <Line
                                                key={key}
                                                type="monotone"
                                                dataKey={key}
                                                name={CONS_LINE[key].label}
                                                stroke={CONS_LINE[key].color}
                                                strokeWidth={2}
                                                dot={{ r: 3, strokeWidth: 2, fill: CONS_LINE[key].color }}
                                                activeDot={{ r: 5 }}
                                                connectNulls
                                                hide={!consLineVisible[key]}
                                            />
                                        ))}
                                    </LineChart>
                                </ResponsiveContainer>
                            </div>
                            <SeriesLegendBar series={CONS_LINE} visible={consLineVisible} onToggle={toggleConsLine} />
                        </>
                    )}
                </div>
                <p className="mt-3 text-xs leading-relaxed text-slate-400">样本单位为「同一信号日 + 同一股票 + ≥2 模型同向」。点击下方图例可隐藏/显示单条曲线；点击数据点查看当日共识明细。</p>
                {selectedConsensusDate && (
                    <ConsensusDetailPanel
                        date={selectedConsensusDate}
                        items={consensusDetail}
                        loading={consensusDetailLoading}
                        onClose={() => setSelectedConsensusDate(null)}
                    />
                )}
            </div>
        </div>
    )
}

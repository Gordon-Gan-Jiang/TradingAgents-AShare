import { Loader2, PlayCircle, RefreshCw, Search, Wallet } from 'lucide-react'
import { type ReactNode, useCallback, useEffect, useMemo, useState } from 'react'

import { api } from '@/services/api'
import type {
    DailyOperationResult,
    PaperDailyReviewSummary,
    PaperPortfolioSnapshot,
    StockSearchResult,
} from '@/types'

type TradeSide = 'BUY' | 'SELL'

type TradeFormState = {
    symbol: string
    name: string
    side: TradeSide
    quantity: string
    price: string
    reason: string
}

function emptyTradeForm(): TradeFormState {
    return {
        symbol: '',
        name: '',
        side: 'BUY',
        quantity: '',
        price: '',
        reason: '',
    }
}

function extractSymbolFromInput(value: string): string {
    const trimmed = value.trim().toUpperCase()
    const match = trimmed.match(/\b\d{6}(?:\.(?:SZ|SH|BJ))?\b/)
    return match ? match[0] : ''
}

function formatMoney(value?: number | null): string {
    if (value == null || !Number.isFinite(value)) return '--'
    return value.toFixed(2)
}

function formatShares(value?: number | null): string {
    if (value == null || !Number.isFinite(value)) return '--'
    return `${value}`
}

function sideLabel(side: TradeSide): string {
    return side === 'BUY' ? '买入' : '卖出'
}

function actionToneClasses(action: 'BUY' | 'SELL' | 'HOLD'): string {
    if (action === 'BUY') return 'bg-rose-50 text-rose-600 dark:bg-rose-500/10 dark:text-rose-300'
    if (action === 'SELL') return 'bg-emerald-50 text-emerald-600 dark:bg-emerald-500/10 dark:text-emerald-300'
    return 'bg-slate-100 text-slate-500 dark:bg-slate-700 dark:text-slate-300'
}

export default function PaperTrading() {
    const [portfolio, setPortfolio] = useState<PaperPortfolioSnapshot | null>(null)
    const [dailyResult, setDailyResult] = useState<DailyOperationResult | null>(null)
    const [reviews, setReviews] = useState<Array<{ trade_date: string; summary: PaperDailyReviewSummary }>>([])
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [tradeSubmitting, setTradeSubmitting] = useState(false)
    const [tradeFeedback, setTradeFeedback] = useState<{ tone: 'success' | 'error'; message: string } | null>(null)
    const [tradeForm, setTradeForm] = useState<TradeFormState>(() => emptyTradeForm())
    const [tradeQuery, setTradeQuery] = useState('')
    const [searchResults, setSearchResults] = useState<StockSearchResult[]>([])
    const [searchLoading, setSearchLoading] = useState(false)

    const positions = portfolio?.positions || []

    const load = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const [portfolioResp, reviewsResp] = await Promise.all([
                api.getPaperPortfolio(),
                api.listDailyReviews(10),
            ])
            setPortfolio(portfolioResp)
            setReviews(reviewsResp.items || [])
        } catch (e) {
            setError(e instanceof Error ? e.message : '加载模拟账户失败')
        } finally {
            setLoading(false)
        }
    }, [])

    useEffect(() => {
        void load()
    }, [load])

    useEffect(() => {
        const keyword = tradeQuery.trim()
        const manualSymbol = extractSymbolFromInput(keyword)
        if (!keyword || tradeForm.symbol === manualSymbol) {
            setSearchResults([])
            setSearchLoading(false)
            return
        }
        let cancelled = false
        setSearchLoading(true)
        const timer = window.setTimeout(async () => {
            try {
                const resp = await api.searchStocks(keyword)
                if (!cancelled) {
                    setSearchResults(resp.results || [])
                }
            } catch {
                if (!cancelled) {
                    setSearchResults([])
                }
            } finally {
                if (!cancelled) {
                    setSearchLoading(false)
                }
            }
        }, 250)
        return () => {
            cancelled = true
            window.clearTimeout(timer)
        }
    }, [tradeForm.symbol, tradeQuery])

    const bootstrap = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const resp = await api.bootstrapPaperPortfolio({
                reset_existing: true,
            })
            setPortfolio(resp)
            await load()
        } catch (e) {
            setError(e instanceof Error ? e.message : '初始化模拟账户失败')
        } finally {
            setLoading(false)
        }
    }, [load])

    const runDailyOps = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const resp = await api.runDailyOperation({
                include_recommendations: true,
                recommendation_top_k: 3,
                auto_execute: true,
            })
            setDailyResult(resp)
            await load()
        } catch (e) {
            setError(e instanceof Error ? e.message : '执行每日操盘失败')
        } finally {
            setLoading(false)
        }
    }, [load])

    const focusTradeForm = useCallback((input: { symbol: string; name?: string | null; side: TradeSide }) => {
        const display = input.name && input.name !== input.symbol ? `${input.name} ${input.symbol}` : input.symbol
        setTradeQuery(display)
        setTradeForm(prev => ({
            ...prev,
            symbol: input.symbol,
            name: input.name || '',
            side: input.side,
            quantity: input.side === 'SELL'
                ? positions.find(item => item.symbol === input.symbol)?.quantity?.toString() || prev.quantity
                : prev.quantity,
        }))
    }, [positions])

    const displayedSearchResults = useMemo(() => {
        const pickedSymbol = tradeForm.symbol
        return searchResults.filter(item => item.symbol !== pickedSymbol)
    }, [searchResults, tradeForm.symbol])

    const handleSearchChange = useCallback((value: string) => {
        const extracted = extractSymbolFromInput(value)
        setTradeQuery(value)
        setTradeFeedback(null)
        setTradeForm(prev => ({
            ...prev,
            symbol: extracted,
            name: extracted && prev.symbol === extracted ? prev.name : '',
        }))
    }, [])

    const pickStock = useCallback((item: StockSearchResult) => {
        setTradeQuery(`${item.name} ${item.symbol}`)
        setSearchResults([])
        setTradeForm(prev => ({
            ...prev,
            symbol: item.symbol,
            name: item.name,
        }))
    }, [])

    const submitManualTrade = useCallback(async () => {
        const symbol = tradeForm.symbol || extractSymbolFromInput(tradeQuery)
        const quantity = Number(tradeForm.quantity)
        const price = tradeForm.price.trim() ? Number(tradeForm.price) : undefined

        if (!symbol) {
            setTradeFeedback({ tone: 'error', message: '请先选择或输入股票代码' })
            return
        }
        if (!Number.isFinite(quantity) || quantity <= 0) {
            setTradeFeedback({ tone: 'error', message: '请输入有效的买卖数量' })
            return
        }
        if (price != null && (!Number.isFinite(price) || price <= 0)) {
            setTradeFeedback({ tone: 'error', message: '成交价必须大于 0' })
            return
        }

        setTradeSubmitting(true)
        setTradeFeedback(null)
        try {
            await api.createPaperTrade({
                symbol,
                name: tradeForm.name || undefined,
                side: tradeForm.side,
                quantity,
                price,
                reason: tradeForm.reason.trim() || undefined,
            })
            const display = tradeForm.name && tradeForm.name !== symbol ? `${tradeForm.name}（${symbol}）` : symbol
            setTradeFeedback({
                tone: 'success',
                message: `已模拟${sideLabel(tradeForm.side)} ${display} ${quantity} 股`,
            })
            setTradeForm(emptyTradeForm())
            setTradeQuery('')
            setSearchResults([])
            await load()
        } catch (e) {
            setTradeFeedback({ tone: 'error', message: e instanceof Error ? e.message : '模拟交易失败' })
        } finally {
            setTradeSubmitting(false)
        }
    }, [load, tradeForm, tradeQuery])

    return (
        <div className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">每日操盘闭环</h1>
                    <p className="text-sm text-slate-500 dark:text-slate-400">显示股票名称与代号，支持手动模拟买入卖出，并结合今日计划完成操盘。</p>
                </div>
                <div className="flex gap-2">
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
                        onClick={bootstrap}
                        disabled={loading}
                        className="inline-flex items-center gap-1 rounded-lg border border-violet-300 px-3 py-1.5 text-sm text-violet-700 hover:bg-violet-50 dark:border-violet-600 dark:text-violet-300"
                    >
                        <Wallet className="h-4 w-4" />
                        同步导入持仓
                    </button>
                    <button
                        type="button"
                        onClick={runDailyOps}
                        disabled={loading}
                        className="inline-flex items-center gap-1 rounded-lg bg-violet-600 px-3 py-1.5 text-sm text-white hover:bg-violet-700 disabled:opacity-60"
                    >
                        <PlayCircle className="h-4 w-4" />
                        执行今日操盘
                    </button>
                </div>
            </div>

            {error && (
                <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-600">
                    {error}
                </div>
            )}

            {tradeFeedback && (
                <div
                    className={`rounded-lg border px-3 py-2 text-sm ${
                        tradeFeedback.tone === 'success'
                            ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                            : 'border-rose-200 bg-rose-50 text-rose-600'
                    }`}
                >
                    {tradeFeedback.message}
                </div>
            )}

            <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
                <MetricCard label="总资产" value={portfolio ? formatMoney(portfolio.equity) : '--'} />
                <MetricCard label="现金" value={portfolio ? formatMoney(portfolio.cash_balance) : '--'} />
                <MetricCard label="持仓市值" value={portfolio ? formatMoney(portfolio.market_value) : '--'} />
                <MetricCard label="累计收益率" value={portfolio?.total_return_pct == null ? '--' : `${portfolio.total_return_pct.toFixed(2)}%`} />
            </div>

            <Panel title="手动模拟交易">
                <div className="space-y-4 px-4 py-4">
                    <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1.6fr_0.9fr_0.9fr_0.9fr]">
                        <div className="relative">
                            <div className="flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2.5 dark:border-slate-700 dark:bg-slate-900">
                                <Search className="h-4 w-4 text-slate-400" />
                                <input
                                    value={tradeQuery}
                                    onChange={e => handleSearchChange(e.target.value)}
                                    placeholder="搜索股票名称或代码，例如 贵州茅台 / 600519"
                                    className="w-full border-0 bg-transparent text-sm text-slate-900 outline-none placeholder:text-slate-400 dark:text-slate-100"
                                />
                                {searchLoading && <Loader2 className="h-4 w-4 animate-spin text-slate-400" />}
                            </div>
                            {displayedSearchResults.length > 0 && (
                                <div className="absolute z-20 mt-1 max-h-56 w-full overflow-auto rounded-xl border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-900">
                                    {displayedSearchResults.map(item => (
                                        <button
                                            key={item.symbol}
                                            type="button"
                                            onClick={() => pickStock(item)}
                                            className="flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-slate-50 dark:hover:bg-slate-800"
                                        >
                                            <span className="font-medium text-slate-900 dark:text-slate-100">{item.name}</span>
                                            <span className="text-xs text-slate-500">{item.symbol}</span>
                                        </button>
                                    ))}
                                </div>
                            )}
                        </div>

                        <div className="inline-flex rounded-xl bg-slate-100 p-1 dark:bg-slate-800">
                            {(['BUY', 'SELL'] as const).map(side => (
                                <button
                                    key={side}
                                    type="button"
                                    onClick={() => setTradeForm(prev => ({ ...prev, side }))}
                                    className={`flex-1 rounded-lg px-3 py-2 text-sm font-medium ${
                                        tradeForm.side === side
                                            ? side === 'BUY'
                                                ? 'bg-rose-500 text-white'
                                                : 'bg-emerald-500 text-white'
                                            : 'text-slate-500'
                                    }`}
                                >
                                    {sideLabel(side)}
                                </button>
                            ))}
                        </div>

                        <input
                            type="number"
                            min="1"
                            step="1"
                            value={tradeForm.quantity}
                            onChange={e => setTradeForm(prev => ({ ...prev, quantity: e.target.value }))}
                            placeholder="数量"
                            className="rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none placeholder:text-slate-400 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
                        />

                        <input
                            type="number"
                            min="0"
                            step="0.01"
                            value={tradeForm.price}
                            onChange={e => setTradeForm(prev => ({ ...prev, price: e.target.value }))}
                            placeholder="成交价(可选)"
                            className="rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none placeholder:text-slate-400 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
                        />
                    </div>

                    <div className="grid grid-cols-1 gap-3 lg:grid-cols-[1fr_auto]">
                        <input
                            type="text"
                            value={tradeForm.reason}
                            onChange={e => setTradeForm(prev => ({ ...prev, reason: e.target.value }))}
                            placeholder="交易原因，例如 减仓止盈 / 试探性建仓"
                            className="rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none placeholder:text-slate-400 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
                        />
                        <button
                            type="button"
                            onClick={submitManualTrade}
                            disabled={tradeSubmitting}
                            className="inline-flex items-center justify-center gap-2 rounded-xl bg-slate-900 px-4 py-2.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-60 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white"
                        >
                            {tradeSubmitting && <Loader2 className="h-4 w-4 animate-spin" />}
                            确认模拟{sideLabel(tradeForm.side)}
                        </button>
                    </div>

                    {positions.length > 0 && (
                        <div className="flex flex-wrap gap-2">
                            {positions.map(item => (
                                <button
                                    key={`${item.symbol}-sell`}
                                    type="button"
                                    onClick={() => focusTradeForm({ symbol: item.symbol, name: item.name, side: 'SELL' })}
                                    className="rounded-full border border-slate-200 px-3 py-1.5 text-xs text-slate-600 hover:border-emerald-400 hover:text-emerald-600 dark:border-slate-700 dark:text-slate-300"
                                >
                                    快速卖出 {item.name || item.symbol}
                                </button>
                            ))}
                            {positions.map(item => (
                                <button
                                    key={`${item.symbol}-buy`}
                                    type="button"
                                    onClick={() => focusTradeForm({ symbol: item.symbol, name: item.name, side: 'BUY' })}
                                    className="rounded-full border border-slate-200 px-3 py-1.5 text-xs text-slate-600 hover:border-rose-400 hover:text-rose-600 dark:border-slate-700 dark:text-slate-300"
                                >
                                    快速加仓 {item.name || item.symbol}
                                </button>
                            ))}
                        </div>
                    )}
                </div>
            </Panel>

            <Panel title="当前模拟持仓">
                {positions.length === 0 ? (
                    <div className="px-4 py-8 text-center text-sm text-slate-500">暂无模拟持仓，可先点击“同步导入持仓”</div>
                ) : (
                    <div className="divide-y divide-slate-100 dark:divide-slate-800">
                        {positions.map(item => (
                            <div key={item.symbol} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 text-sm">
                                <div>
                                    <div className="font-medium text-slate-900 dark:text-slate-100">{item.name || item.symbol}</div>
                                    <div className="mt-1 text-xs text-slate-500">{item.symbol}</div>
                                </div>
                                <div className="text-xs text-slate-500">
                                    持仓 {formatShares(item.quantity)} 股 · 成本 {formatMoney(item.avg_cost)} · 现价 {formatMoney(item.last_price)}
                                </div>
                            </div>
                        ))}
                    </div>
                )}
            </Panel>

            <Panel title="今日3点前操作建议">
                {dailyResult?.plan?.actions?.length ? (
                    <div className="divide-y divide-slate-100 dark:divide-slate-800">
                        {dailyResult.plan.actions.map((action, idx) => (
                            <div key={`${action.symbol}-${idx}`} className="px-4 py-3 text-sm">
                                <div className="flex flex-wrap items-center justify-between gap-3">
                                    <div>
                                        <div className="flex items-center gap-2">
                                            <span className="font-medium text-slate-900 dark:text-slate-100">
                                                {action.name || action.symbol}
                                            </span>
                                            <span className="text-xs text-slate-400">{action.symbol}</span>
                                        </div>
                                        <div className="mt-1 text-xs text-slate-500">{action.reason}</div>
                                    </div>
                                    <div className="flex items-center gap-2">
                                        <span className={`rounded-full px-2 py-1 text-xs font-semibold ${actionToneClasses(action.action)}`}>
                                            {action.action}
                                        </span>
                                        <span className="text-xs text-slate-500">
                                            数量 {action.quantity} · 价格 {formatMoney(action.price)}
                                        </span>
                                    </div>
                                </div>
                            </div>
                        ))}
                    </div>
                ) : (
                    <div className="px-4 py-8 text-center text-sm text-slate-500">点击“执行今日操盘”生成建议</div>
                )}
            </Panel>

            <Panel title="每日复盘">
                {reviews.length === 0 ? (
                    <div className="px-4 py-8 text-center text-sm text-slate-500">暂无复盘记录</div>
                ) : (
                    <div className="divide-y divide-slate-100 dark:divide-slate-800">
                        {reviews.map((item) => (
                            <div key={item.trade_date} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-sm">
                                <div className="font-medium text-slate-900 dark:text-slate-100">{item.trade_date}</div>
                                <div className="text-xs text-slate-500">
                                    权益 {item.summary.equity} · 当日已实现 {item.summary.day_realized_pnl} · 交易数 {item.summary.trade_count}
                                </div>
                            </div>
                        ))}
                    </div>
                )}
            </Panel>
        </div>
    )
}

function MetricCard({ label, value }: { label: string; value: string }) {
    return (
        <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
            <div className="text-sm text-slate-500">{label}</div>
            <div className="mt-2 text-xl font-semibold text-slate-900 dark:text-slate-100">{value}</div>
        </div>
    )
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
    return (
        <div className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
            <div className="border-b border-slate-200 px-4 py-3 text-sm font-medium text-slate-700 dark:border-slate-700 dark:text-slate-200">
                {title}
            </div>
            {children}
        </div>
    )
}

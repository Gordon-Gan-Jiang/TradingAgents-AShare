import { useEffect, useMemo, useRef, useState } from 'react'
import {
    BusinessDay,
    CandlestickData,
    CandlestickSeries,
    ColorType,
    IChartApi,
    ISeriesApi,
    createChart,
} from 'lightweight-charts'
import { Activity, CandlestickChart } from 'lucide-react'
import { api } from '@/services/api'

interface KlinePanelProps {
    symbol: string
}

function toDateText(date: Date): string {
    const y = date.getFullYear()
    const m = String(date.getMonth() + 1).padStart(2, '0')
    const d = String(date.getDate()).padStart(2, '0')
    return `${y}-${m}-${d}`
}

function toBusinessDay(value: string): BusinessDay | null {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
    if (!m) return null
    const year = Number(m[1])
    const month = Number(m[2])
    const day = Number(m[3])
    if (!Number.isFinite(year) || !Number.isFinite(month) || !Number.isFinite(day)) return null
    return { year, month, day }
}

export default function KlinePanel({ symbol }: KlinePanelProps) {
    const containerRef = useRef<HTMLDivElement | null>(null)
    const chartRef = useRef<IChartApi | null>(null)
    const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
    const [bars, setBars] = useState(0)
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)

    const range = useMemo(() => {
        const end = new Date()
        const start = new Date(end.getTime() - 180 * 24 * 60 * 60 * 1000)
        return {
            start: toDateText(start),
            end: toDateText(end),
        }
    }, [])

    useEffect(() => {
        if (!containerRef.current) return
        const chart = createChart(containerRef.current, {
            layout: {
                background: { type: ColorType.Solid, color: 'transparent' },
                textColor: '#8B949E',
                attributionLogo: false,
            },
            width: containerRef.current.clientWidth,
            height: containerRef.current.clientHeight,
            grid: {
                vertLines: { color: 'rgba(48, 54, 61, 0.6)' },
                horzLines: { color: 'rgba(48, 54, 61, 0.6)' },
            },
            rightPriceScale: {
                borderColor: '#30363D',
            },
            timeScale: {
                borderColor: '#30363D',
                timeVisible: true,
                rightOffset: 6,
            },
            crosshair: {
                vertLine: { color: 'rgba(88, 166, 255, 0.35)' },
                horzLine: { color: 'rgba(88, 166, 255, 0.35)' },
            },
        })
        const series = chart.addSeries(CandlestickSeries, {
            upColor: '#DA3633',
            downColor: '#238636',
            wickUpColor: '#DA3633',
            wickDownColor: '#238636',
            borderVisible: false,
        })

        chartRef.current = chart
        seriesRef.current = series

        const onResize = () => {
            if (!containerRef.current || !chartRef.current) return
            chartRef.current.applyOptions({
                width: containerRef.current.clientWidth,
                height: containerRef.current.clientHeight,
            })
        }

        window.addEventListener('resize', onResize)
        return () => {
            window.removeEventListener('resize', onResize)
            chartRef.current?.remove()
            chartRef.current = null
            seriesRef.current = null
        }
    }, [])

    useEffect(() => {
        let cancelled = false

        const load = async () => {
            if (!seriesRef.current) return
            setLoading(true)
            setError(null)
            try {
                const resp = await api.getKline(symbol, range.start, range.end)
                const data: CandlestickData[] = resp.candles.flatMap((c) => {
                    const time = toBusinessDay((c.date || '').slice(0, 10))
                    const open = Number(c.open)
                    const high = Number(c.high)
                    const low = Number(c.low)
                    const close = Number(c.close)
                    if (!time) return []
                    if (![open, high, low, close].every(Number.isFinite)) return []
                    return [{ time, open, high, low, close }]
                })

                if (cancelled) return
                seriesRef.current?.setData(data)
                chartRef.current?.timeScale().fitContent()
                setBars(data.length)
                if (!data.length) {
                    setError('暂无可用K线数据')
                }
            } catch (e) {
                if (cancelled) return
                setError(e instanceof Error ? e.message : '加载K线失败')
                setBars(0)
                seriesRef.current?.setData([])
            } finally {
                if (!cancelled) setLoading(false)
            }
        }

        load()
        return () => {
            cancelled = true
        }
    }, [range.end, range.start, symbol])

    return (
        <section className="card h-[340px] flex flex-col min-h-0 overflow-hidden">
            <div className="flex items-center justify-between mb-3 shrink-0">
                <div className="flex items-center gap-2">
                    <CandlestickChart className="w-5 h-5 text-trading-accent-cyan" />
                    <h2 className="text-lg font-semibold text-trading-text-primary">TradingView K线</h2>
                </div>
                <div className="text-xs text-trading-text-muted">
                    {symbol} · {bars} bars
                </div>
            </div>
            <div className="relative flex-1 min-h-0 rounded-md border border-trading-border bg-trading-bg-primary/30 overflow-hidden">
                <div ref={containerRef} className="absolute inset-0" />
                {loading && (
                    <div className="absolute right-3 top-3 text-xs px-2 py-1 rounded bg-trading-bg-secondary/90 text-trading-text-secondary flex items-center gap-1">
                        <Activity className="w-3 h-3 animate-pulse" />
                        加载中
                    </div>
                )}
                {error && (
                    <div className="absolute left-3 top-3 text-xs px-2 py-1 rounded bg-trading-bg-secondary/90 text-trading-accent-orange">
                        {error}
                    </div>
                )}
            </div>
        </section>
    )
}

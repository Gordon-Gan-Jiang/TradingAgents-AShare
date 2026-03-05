import { BarChart3 } from 'lucide-react'
import { useAnalysisStore } from '@/stores/analysisStore'
import type { KeyMetric } from '@/types'

const STATUS_COLOR = {
    good: 'text-emerald-400',
    neutral: 'text-slate-200',
    bad: 'text-rose-400',
}

/** Fallback: extract metrics from text when structured data unavailable */
function extractMetricsFromText(
    fundamentals?: string,
    finalDecision?: string,
): KeyMetric[] {
    const metrics: KeyMetric[] = []

    if (finalDecision) {
        const confMatch = finalDecision.match(/置信度[:：]\s*(\d+)%/i) ||
            finalDecision.match(/confidence[:：]\s*(\d+)%/i)
        if (confMatch) {
            const val = parseInt(confMatch[1])
            metrics.push({
                name: '分析置信度',
                value: `${val}%`,
                status: val >= 70 ? 'good' : val >= 50 ? 'neutral' : 'bad',
            })
        }
    }

    if (fundamentals) {
        const peMatch = fundamentals.match(/市盈率[^：:\d]*[:：]?\s*([\d.]+)/i) ||
            fundamentals.match(/PE[^：:\d]*[:：]?\s*([\d.]+)/i)
        if (peMatch) metrics.push({ name: '市盈率(PE)', value: `${peMatch[1]}x`, status: 'neutral' })

        const pbMatch = fundamentals.match(/市净率[^：:\d]*[:：]?\s*([\d.]+)/i) ||
            fundamentals.match(/PB[^：:\d]*[:：]?\s*([\d.]+)/i)
        if (pbMatch) metrics.push({ name: '市净率(PB)', value: `${pbMatch[1]}x`, status: 'neutral' })

        const roeMatch = fundamentals.match(/ROE[^：:\d]*[:：]?\s*([\d.]+)%/i)
        if (roeMatch) metrics.push({ name: 'ROE', value: `${roeMatch[1]}%`, status: 'good' })
    }

    return metrics.slice(0, 4)
}

export default function KeyMetrics() {
    const { keyMetrics, report } = useAnalysisStore()

    const metrics: KeyMetric[] = keyMetrics.length > 0
        ? keyMetrics
        : (report ? extractMetricsFromText(report.fundamentals_report, report.final_trade_decision) : [])

    return (
        <div className="card bg-slate-900/50 border-slate-700/50 p-4">
            <div className="flex items-center gap-2 mb-4">
                <div className="p-1.5 rounded-lg bg-blue-500/20">
                    <BarChart3 className="w-4 h-4 text-blue-400" />
                </div>
                <h3 className="text-sm font-semibold text-slate-100">关键指标速览</h3>
            </div>

            {metrics.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-6 text-center">
                    <BarChart3 className="w-8 h-8 text-slate-600 mb-2" />
                    <p className="text-xs text-slate-500">分析完成后展示关键指标</p>
                </div>
            ) : (
                <div className="space-y-2">
                    {metrics.map((metric) => (
                        <div
                            key={metric.name}
                            className="flex items-center justify-between py-2 border-b border-slate-700/30 last:border-0"
                        >
                            <span className="text-sm text-slate-400">{metric.name}</span>
                            <span className={`text-sm font-medium ${STATUS_COLOR[metric.status]}`}>
                                {metric.value}
                            </span>
                        </div>
                    ))}
                </div>
            )}
        </div>
    )
}

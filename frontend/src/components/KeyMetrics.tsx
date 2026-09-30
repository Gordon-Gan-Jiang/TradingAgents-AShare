import { BarChart3 } from 'lucide-react'
import type { KeyMetric } from '@/types'
import { metricsEmptyText, statusTone } from './structuredDisplay'

export default function KeyMetrics({ items }: { items?: KeyMetric[] | null }) {
    // `null`/`undefined` 与 `[]` 是两种不同状态（审计 §4.7），末态文案不能合并。
    const metrics: KeyMetric[] = items ?? []

    return (
        <div className="card p-4">
            <div className="flex items-center gap-2 mb-4">
                <div className="p-1.5 rounded-lg bg-blue-500/20">
                    <BarChart3 className="w-4 h-4 text-blue-400" />
                </div>
                <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">关键指标速览</h3>
            </div>

            {metrics.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-6 text-center">
                    <BarChart3 className="w-8 h-8 text-slate-600 mb-2" />
                    <p className="text-xs text-slate-500">{metricsEmptyText(items)}</p>
                </div>
            ) : (
                <div className="space-y-2">
                    {metrics.map((metric) => (
                        <div
                            key={metric.name}
                            className="flex items-center justify-between py-2 border-b border-slate-100 dark:border-slate-700/30 last:border-0"
                        >
                            <span className="text-sm text-slate-400">{metric.name}</span>
                            {/* 用 statusTone 而不是 `STATUS_COLOR[metric.status]`：后者在
                                `status='unknown'`（后端修复后会出现）或历史脏数据下取到
                                undefined，className 里会拼进字面量 "undefined"。 */}
                            <span className={`text-sm font-medium ${statusTone(metric.status).color}`}>
                                {metric.value}
                            </span>
                        </div>
                    ))}
                </div>
            )}
        </div>
    )
}

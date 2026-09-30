import { AlertTriangle, Shield, AlertCircle } from 'lucide-react'
import type { RiskItem } from '@/types'
import { levelTone, risksEmptyText } from './structuredDisplay'

// 图标按 key 在这里映射：`structuredDisplay` 只返回可序列化的样式数据，
// 才能在没有 jsdom 的 vitest（node 环境）里被测到。
const LEVEL_ICON = {
    high: AlertTriangle,
    medium: AlertCircle,
    low: Shield,
    unknown: AlertCircle,
} as const

export default function RiskRadar({ items }: { items?: RiskItem[] | null }) {
    // `null`/`undefined`（没抽到）与 `[]`（确认没有）必须分开（审计 §4.7）。
    const risks: RiskItem[] = items ?? []

    return (
        <div className="card p-4">
            <div className="flex items-center gap-2 mb-4">
                <div className="p-1.5 rounded-lg bg-amber-500/20">
                    <AlertTriangle className="w-4 h-4 text-amber-400" />
                </div>
                <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">风险雷达</h3>
            </div>

            {risks.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-6 text-center">
                    <Shield className="w-8 h-8 text-slate-600 mb-2" />
                    <p className="text-xs text-slate-500">{risksEmptyText(items)}</p>
                </div>
            ) : (
                <div className="space-y-2">
                    {risks.map((risk, i) => {
                        // 必须用 levelTone 兜底：后端修复后会出现 `level='unknown'`，
                        // 且库里还有修复前的中文脏数据（"高"、"严重"）。
                        // 旧的 `LEVEL_CONFIG[risk.level]` 在这些取值下是 undefined，
                        // 下一行的 config.color 会抛 TypeError 把整个报告页打崩。
                        const config = levelTone(risk.level)
                        const Icon = LEVEL_ICON[config.key]
                        return (
                            <div
                                key={i}
                                className="flex items-start justify-between p-2.5 rounded-lg bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-700/50 gap-2"
                            >
                                <div className="flex-1 min-w-0">
                                    <span className="text-sm text-slate-700 dark:text-slate-300 block truncate">{risk.name}</span>
                                    {risk.description && (
                                        <span className="text-xs text-slate-500 mt-0.5 block line-clamp-2">{risk.description}</span>
                                    )}
                                </div>
                                <span className={`text-xs px-2 py-0.5 rounded-full font-medium shrink-0 inline-flex items-center gap-1 ${config.color} ${config.bg}`}>
                                    <Icon className="w-3 h-3" />
                                    {config.label}
                                </span>
                            </div>
                        )
                    })}
                </div>
            )}
        </div>
    )
}

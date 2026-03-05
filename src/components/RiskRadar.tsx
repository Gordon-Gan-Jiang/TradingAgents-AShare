import { AlertTriangle, Shield, AlertCircle } from 'lucide-react'
import { useAnalysisStore } from '@/stores/analysisStore'

interface RiskItem {
    name: string
    level: 'high' | 'medium' | 'low'
}

const LEVEL_CONFIG = {
    high: { color: 'text-rose-400', bg: 'bg-rose-500/20', label: '高风险', icon: AlertTriangle },
    medium: { color: 'text-amber-400', bg: 'bg-amber-500/20', label: '中风险', icon: AlertCircle },
    low: { color: 'text-emerald-400', bg: 'bg-emerald-500/20', label: '低风险', icon: Shield },
}

/** 从最终决策文本中简单提取风险条目 */
function parseRisks(text: string): RiskItem[] {
    const risks: RiskItem[] = []
    const lines = text.split(/[。\n]/)
    for (const line of lines) {
        const l = line.trim()
        if (!l) continue
        if (/高风险|重大风险|严重风险|高度风险/.test(l)) {
            risks.push({ name: l.slice(0, 20), level: 'high' })
        } else if (/风险|注意|警示|下行|波动|不确定/.test(l)) {
            risks.push({ name: l.slice(0, 20), level: 'medium' })
        }
        if (risks.length >= 3) break
    }
    return risks
}

export default function RiskRadar() {
    const { report } = useAnalysisStore()

    const risks: RiskItem[] = report?.final_trade_decision
        ? parseRisks(report.final_trade_decision)
        : []

    return (
        <div className="card bg-slate-900/50 border-slate-700/50 p-4">
            <div className="flex items-center gap-2 mb-4">
                <div className="p-1.5 rounded-lg bg-amber-500/20">
                    <AlertTriangle className="w-4 h-4 text-amber-400" />
                </div>
                <h3 className="text-sm font-semibold text-slate-100">风险雷达</h3>
            </div>

            {risks.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-6 text-center">
                    <Shield className="w-8 h-8 text-slate-600 mb-2" />
                    <p className="text-xs text-slate-500">分析完成后展示风险评估</p>
                </div>
            ) : (
                <div className="space-y-2">
                    {risks.map((risk, i) => {
                        const config = LEVEL_CONFIG[risk.level]
                        return (
                            <div
                                key={i}
                                className="flex items-center justify-between p-2.5 rounded-lg bg-slate-800/50 border border-slate-700/50"
                            >
                                <span className="text-sm text-slate-300 truncate mr-2">{risk.name}</span>
                                <span className={`text-xs px-2 py-0.5 rounded-full font-medium shrink-0 ${config.color} ${config.bg}`}>
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

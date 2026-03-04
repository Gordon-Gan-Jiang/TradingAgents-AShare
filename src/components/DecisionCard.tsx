import { useState } from 'react'
import { TrendingUp, TrendingDown, Target, Shield, ChevronDown, ChevronUp, Info } from 'lucide-react'

interface DecisionCardProps {
    symbol: string
    name?: string
    decision?: 'buy' | 'sell' | 'hold' | 'add' | 'reduce' | 'watch'
    confidence?: number
    targetPrice?: number
    targetChange?: number
    stopLoss?: number
    stopLossChange?: number
    reasoning?: string
    riskLevel?: 'low' | 'medium' | 'high'
}

const decisionConfig: Record<string, { label: string; color: string; icon: typeof TrendingUp }> = {
    buy: { label: '买入', color: 'bg-green-100 dark:bg-green-500/20 text-green-700 dark:text-green-400 border-green-200 dark:border-green-500/30', icon: TrendingUp },
    sell: { label: '卖出', color: 'bg-red-100 dark:bg-red-500/20 text-red-700 dark:text-red-400 border-red-200 dark:border-red-500/30', icon: TrendingDown },
    hold: { label: '持有', color: 'bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-400 border-blue-200 dark:border-blue-500/30', icon: Shield },
    add: { label: '增持', color: 'bg-green-100 dark:bg-green-500/20 text-green-700 dark:text-green-400 border-green-200 dark:border-green-500/30', icon: TrendingUp },
    reduce: { label: '减持', color: 'bg-orange-100 dark:bg-orange-500/20 text-orange-700 dark:text-orange-400 border-orange-200 dark:border-orange-500/30', icon: TrendingDown },
    watch: { label: '观望', color: 'bg-slate-100 dark:bg-slate-700/50 text-slate-700 dark:text-slate-400 border-slate-200 dark:border-slate-600', icon: Info },
}

export default function DecisionCard({
    symbol,
    name = symbol,
    decision = 'hold',
    confidence = 75,
    targetPrice = 185,
    targetChange = 12.5,
    stopLoss = 145,
    stopLossChange = -11.8,
    reasoning = '技术面突破+估值修复',
    riskLevel = 'medium',
}: DecisionCardProps) {
    const [expanded, setExpanded] = useState(false)
    const config = decisionConfig[decision] || decisionConfig.hold
    const DecisionIcon = config.icon

    const riskLabels: Record<string, string> = {
        low: '低',
        medium: '中等',
        high: '高',
    }

    const riskColors: Record<string, string> = {
        low: 'text-green-600 dark:text-green-400',
        medium: 'text-yellow-600 dark:text-yellow-400',
        high: 'text-red-600 dark:text-red-400',
    }

    return (
        <div className="card overflow-hidden">
            {/* 头部 */}
            <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-blue-500 to-cyan-400 flex items-center justify-center">
                        <TrendingUp className="w-5 h-5 text-white" />
                    </div>
                    <div>
                        <h3 className="font-semibold text-slate-900 dark:text-slate-100">{name}</h3>
                        <p className="text-sm text-slate-500">{symbol}</p>
                    </div>
                </div>
                <div className={`px-4 py-2 rounded-full border font-medium flex items-center gap-1.5 ${config.color}`}>
                    <DecisionIcon className="w-4 h-4" />
                    {config.label}
                </div>
            </div>

            {/* 置信度 */}
            <div className="mb-4">
                <div className="flex items-center justify-between mb-2">
                    <span className="text-sm text-slate-500">置信度</span>
                    <span className="text-sm font-medium text-slate-700 dark:text-slate-300">{confidence}%</span>
                </div>
                <div className="h-2 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden">
                    <div
                        className="h-full bg-gradient-to-r from-blue-500 to-cyan-400 rounded-full transition-all duration-1000"
                        style={{ width: `${confidence}%` }}
                    />
                </div>
            </div>

            {/* 目标价和止损价 */}
            <div className="grid grid-cols-2 gap-3 mb-4">
                <div className="p-3 rounded-xl bg-green-50 dark:bg-green-500/10 border border-green-100 dark:border-green-500/20">
                    <div className="flex items-center gap-1.5 mb-1">
                        <Target className="w-4 h-4 text-green-600 dark:text-green-400" />
                        <span className="text-xs text-slate-500">目标价</span>
                    </div>
                    <p className="text-xl font-bold text-green-600 dark:text-green-400">¥{targetPrice}</p>
                    <p className="text-sm text-green-600 dark:text-green-400">+{targetChange}%</p>
                </div>
                <div className="p-3 rounded-xl bg-red-50 dark:bg-red-500/10 border border-red-100 dark:border-red-500/20">
                    <div className="flex items-center gap-1.5 mb-1">
                        <Shield className="w-4 h-4 text-red-600 dark:text-red-400" />
                        <span className="text-xs text-slate-500">止损价</span>
                    </div>
                    <p className="text-xl font-bold text-red-600 dark:text-red-400">¥{stopLoss}</p>
                    <p className="text-sm text-red-600 dark:text-red-400">{stopLossChange}%</p>
                </div>
            </div>

            {/* 展开详情 */}
            {expanded && (
                <div className="mb-4 p-3 rounded-lg bg-slate-50 dark:bg-slate-800/50 space-y-2">
                    <div className="flex justify-between">
                        <span className="text-sm text-slate-500">核心逻辑</span>
                        <span className="text-sm text-slate-700 dark:text-slate-300">{reasoning}</span>
                    </div>
                    <div className="flex justify-between">
                        <span className="text-sm text-slate-500">风险等级</span>
                        <span className={`text-sm font-medium ${riskColors[riskLevel]}`}>{riskLabels[riskLevel]}</span>
                    </div>
                </div>
            )}

            {/* 操作按钮 */}
            <div className="flex flex-wrap gap-2">
                <button
                    onClick={() => setExpanded(!expanded)}
                    className="flex items-center gap-1 px-3 py-1.5 text-sm rounded-lg bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors"
                >
                    {expanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                    {expanded ? '收起' : '详情'}
                </button>
                <button className="px-3 py-1.5 text-sm rounded-lg bg-blue-50 dark:bg-blue-500/10 text-blue-600 dark:text-blue-400 hover:bg-blue-100 dark:hover:bg-blue-500/20 transition-colors">
                    查看分析过程
                </button>
                <button className="px-3 py-1.5 text-sm rounded-lg bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors">
                    调整参数
                </button>
            </div>
        </div>
    )
}

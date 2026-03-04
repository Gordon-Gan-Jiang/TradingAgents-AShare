import { useMemo } from 'react'
import { CheckCircle2, Circle, Loader2, AlertCircle } from 'lucide-react'
import { useAnalysisStore } from '@/stores/analysisStore'
import type { AgentStatus } from '@/types'

const TEAMS = [
    { name: 'Analyst Team', color: 'blue', label: '分析团队' },
    { name: 'Research Team', color: 'purple', label: '研究团队' },
    { name: 'Trading Team', color: 'green', label: '交易团队' },
    { name: 'Risk Management', color: 'orange', label: '风控团队' },
    { name: 'Portfolio Management', color: 'cyan', label: '组合管理' },
] as const

// Agent 中文名称映射
const AGENT_NAME_MAP: Record<string, string> = {
    'Market Analyst': '技术分析师',
    'Social Analyst': '舆情分析师',
    'News Analyst': '新闻分析师',
    'Fundamentals Analyst': '基本面分析师',
    'Bull Researcher': '看多研究员',
    'Bear Researcher': '看空研究员',
    'Research Manager': '研究经理',
    'Trader': '交易员',
    'Aggressive Analyst': '激进风控',
    'Conservative Analyst': '保守风控',
    'Neutral Analyst': '中性风控',
    'Portfolio Manager': '投资组合经理',
}

const COLORS = {
    blue: { bg: 'bg-blue-500', text: 'text-blue-500', border: 'border-blue-500', light: 'bg-blue-50 dark:bg-blue-500/10' },
    purple: { bg: 'bg-purple-500', text: 'text-purple-500', border: 'border-purple-500', light: 'bg-purple-50 dark:bg-purple-500/10' },
    green: { bg: 'bg-green-500', text: 'text-green-500', border: 'border-green-500', light: 'bg-green-50 dark:bg-green-500/10' },
    orange: { bg: 'bg-orange-500', text: 'text-orange-500', border: 'border-orange-500', light: 'bg-orange-50 dark:bg-orange-500/10' },
    cyan: { bg: 'bg-cyan-500', text: 'text-cyan-500', border: 'border-cyan-500', light: 'bg-cyan-50 dark:bg-cyan-500/10' },
}

export default function AgentPipeline() {
    const { agents, isAnalyzing } = useAnalysisStore()

    const groupedAgents = useMemo(() => {
        const groups: Record<string, typeof agents> = {}
        TEAMS.forEach(team => {
            groups[team.name] = agents.filter(a => a.team === team.name)
        })
        return groups
    }, [agents])

    const completedCount = agents.filter(a => a.status === 'completed').length
    const totalCount = agents.length
    const progress = totalCount > 0 ? (completedCount / totalCount) * 100 : 0

    return (
        <div className="card flex flex-col min-h-[320px] max-h-[380px] overflow-hidden">
            <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2">
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">Agent 流水线</h2>
                    {isAnalyzing && (
                        <span className="badge-blue animate-pulse">分析中</span>
                    )}
                </div>
                <div className="text-sm text-slate-500 dark:text-slate-400">
                    {completedCount}/{totalCount} 完成
                </div>
            </div>

            {/* Progress Bar */}
            <div className="w-full h-2 bg-slate-200 dark:bg-slate-700 rounded-full mb-4 overflow-hidden">
                <div
                    className="h-full bg-gradient-to-r from-blue-500 to-cyan-400 transition-all duration-500"
                    style={{ width: `${progress}%` }}
                />
            </div>

            {/* Agent Grid */}
            <div className="flex-1 overflow-y-auto space-y-4 min-h-0">
                {TEAMS.map((team) => {
                    const teamAgents = groupedAgents[team.name] || []
                    if (teamAgents.length === 0) return null
                    const colors = COLORS[team.color]

                    return (
                        <div key={team.name} className="space-y-2">
                            <h3 className={`text-xs font-medium uppercase tracking-wider ${colors.text}`}>
                                {team.label}
                            </h3>
                            <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-2">
                                {teamAgents.map((agent) => (
                                    <AgentCard key={agent.id} agent={agent} colors={colors} />
                                ))}
                            </div>
                        </div>
                    )
                })}
            </div>
        </div>
    )
}

interface AgentCardProps {
    agent: {
        id: string
        name: string
        status: AgentStatus
    }
    colors: typeof COLORS['blue']
}

function AgentCard({ agent, colors }: AgentCardProps) {
    const StatusIcon = {
        pending: Circle,
        in_progress: Loader2,
        completed: CheckCircle2,
        error: AlertCircle,
        skipped: Circle,
    }[agent.status]

    const statusConfig = {
        pending: {
            cardClass: 'bg-slate-100 dark:bg-slate-800/50 border-slate-200 dark:border-slate-700',
            textClass: 'text-slate-500 dark:text-slate-400',
        },
        in_progress: {
            cardClass: `${colors.light} ${colors.border} border-2`,
            textClass: `${colors.text} animate-spin`,
        },
        completed: {
            cardClass: 'bg-green-50 dark:bg-green-500/10 border-green-200 dark:border-green-500/30',
            textClass: 'text-green-500',
        },
        error: {
            cardClass: 'bg-red-50 dark:bg-red-500/10 border-red-200 dark:border-red-500/30',
            textClass: 'text-red-500',
        },
        skipped: {
            cardClass: 'bg-slate-50 dark:bg-slate-800/30 border-slate-200 dark:border-slate-700 opacity-60',
            textClass: 'text-slate-400',
        },
    }[agent.status]

    const statusLabels = {
        pending: '等待中',
        in_progress: '运行中',
        completed: '已完成',
        error: '错误',
        skipped: '已跳过',
    }

    return (
        <div className={`p-3 rounded-lg border transition-all duration-300 ${statusConfig.cardClass}`}>
            <div className="flex items-start gap-2">
                <StatusIcon className={`w-4 h-4 mt-0.5 flex-shrink-0 ${statusConfig.textClass}`} />
                <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-slate-900 dark:text-slate-100 truncate">
                        {AGENT_NAME_MAP[agent.name] || agent.name}
                    </p>
                    <p className={`text-xs ${agent.status === 'in_progress' ? colors.text : 'text-slate-500 dark:text-slate-400'}`}>
                        {statusLabels[agent.status]}
                    </p>
                </div>
            </div>
        </div>
    )
}

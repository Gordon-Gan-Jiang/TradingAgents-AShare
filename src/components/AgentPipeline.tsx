import { useMemo } from 'react'
import { CheckCircle2, Circle, Loader2, AlertCircle } from 'lucide-react'
import { useAnalysisStore } from '@/stores/analysisStore'
import type { AgentStatus } from '@/types'

const TEAMS = [
    { name: 'Analyst Team', color: 'blue' },
    { name: 'Research Team', color: 'purple' },
    { name: 'Trading Team', color: 'green' },
    { name: 'Risk Management', color: 'orange' },
    { name: 'Portfolio Management', color: 'cyan' },
] as const

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
        <div className="card h-full flex flex-col min-h-0 overflow-hidden">
            <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2">
                    <h2 className="text-lg font-semibold text-trading-text-primary">Agent 流水线</h2>
                    {isAnalyzing && (
                        <span className="badge-blue animate-pulse">分析中</span>
                    )}
                </div>
                <div className="text-sm text-trading-text-secondary">
                    {completedCount}/{totalCount} 完成
                </div>
            </div>

            {/* Progress Bar */}
            <div className="w-full h-1 bg-trading-bg-tertiary rounded-full mb-4 overflow-hidden">
                <div
                    className="h-full bg-gradient-to-r from-trading-accent-blue to-trading-accent-cyan transition-all duration-500"
                    style={{ width: `${progress}%` }}
                />
            </div>

            {/* Agent Grid */}
            <div className="flex-1 overflow-y-auto space-y-4 min-h-0">
                {TEAMS.map((team) => {
                    const teamAgents = groupedAgents[team.name] || []
                    if (teamAgents.length === 0) return null

                    return (
                        <div key={team.name} className="space-y-2">
                            <h3 className="text-xs font-medium text-trading-text-muted uppercase tracking-wider">
                                {team.name}
                            </h3>
                            <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-2">
                                {teamAgents.map((agent) => (
                                    <AgentCard key={agent.id} agent={agent} teamColor={team.color} />
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
        description?: string
    }
    teamColor: string
}

function AgentCard({ agent, teamColor }: AgentCardProps) {
    const StatusIcon = {
        pending: Circle,
        in_progress: Loader2,
        completed: CheckCircle2,
        error: AlertCircle,
        skipped: Circle,
    }[agent.status]

    const accentColorClassByTeam = {
        blue: 'text-trading-accent-blue',
        purple: 'text-trading-accent-purple',
        green: 'text-trading-accent-green',
        orange: 'text-trading-accent-orange',
        cyan: 'text-trading-accent-cyan',
    } as const

    const accentBorderClassByTeam = {
        blue: 'border-trading-accent-blue bg-trading-accent-blue/10',
        purple: 'border-trading-accent-purple bg-trading-accent-purple/10',
        green: 'border-trading-accent-green bg-trading-accent-green/10',
        orange: 'border-trading-accent-orange bg-trading-accent-orange/10',
        cyan: 'border-trading-accent-cyan bg-trading-accent-cyan/10',
    } as const

    const accentTextClass = accentColorClassByTeam[teamColor as keyof typeof accentColorClassByTeam] || 'text-trading-accent-blue'
    const accentBorderClass = accentBorderClassByTeam[teamColor as keyof typeof accentBorderClassByTeam] || 'border-trading-accent-blue bg-trading-accent-blue/10'
    const teamAccentHexByTeam = {
        blue: '#58A6FF',
        purple: '#8957E5',
        green: '#238636',
        orange: '#F0883E',
        cyan: '#39D0D8',
    } as const
    const teamLabelByColor = {
        blue: '分析团队',
        purple: '研究团队',
        green: '交易团队',
        orange: '风控团队',
        cyan: '组合管理',
    } as const
    const teamAccentHex = teamAccentHexByTeam[teamColor as keyof typeof teamAccentHexByTeam] || '#58A6FF'
    const teamLabel = teamLabelByColor[teamColor as keyof typeof teamLabelByColor] || '团队'

    const statusColors = {
        pending: 'text-trading-text-muted',
        in_progress: `${accentTextClass} animate-spin`,
        completed: 'text-trading-accent-green',
        error: 'text-trading-accent-red',
        skipped: 'text-trading-text-muted opacity-50',
    }

    const cardColors = {
        pending: 'border-trading-border bg-trading-bg-tertiary/30',
        in_progress: accentBorderClass,
        completed: 'border-trading-accent-green/50 bg-trading-accent-green/5',
        error: 'border-trading-accent-red/50 bg-trading-accent-red/5',
        skipped: 'border-trading-border bg-trading-bg-tertiary/10 opacity-50',
    }

    const statusLabels = {
        pending: '等待中',
        in_progress: '运行中',
        completed: '已完成',
        error: '错误',
        skipped: '已跳过',
    }

    return (
        <div
            className={`p-3 rounded-lg border transition-all duration-300 ${cardColors[agent.status]}`}
            style={{ borderLeftWidth: 3, borderLeftColor: teamAccentHex }}
        >
            <div className="flex items-start gap-2">
                <StatusIcon className={`w-4 h-4 mt-0.5 flex-shrink-0 ${statusColors[agent.status]}`} />
                <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-trading-text-primary truncate">
                        {agent.name}
                    </p>
                    <p className="text-[11px] truncate" style={{ color: teamAccentHex, opacity: 0.85 }}>
                        {teamLabel}
                    </p>
                    <p className={`text-xs ${agent.status === 'in_progress' ? accentTextClass : 'text-trading-text-muted'}`}>
                        {statusLabels[agent.status]}
                    </p>
                </div>
            </div>
        </div>
    )
}

import { useEffect, useState } from 'react'
import { useAnalysisStore } from '@/stores/analysisStore'
import type { AgentStatus } from '@/types'
import { 
    Database, 
    TrendingUp, 
    MessageCircle, 
    Newspaper, 
    Calculator,
    Scale,
    ArrowRight,
    Shield,
    Briefcase,
    CheckCircle2,
    Loader2,
} from 'lucide-react'

interface AgentNode {
    id: string
    name: string
    icon: React.ReactNode
    team: string
}

// 定义流水线的阶段 - Dify 风格横向布局
const PIPELINE_STAGES: AgentNode[][] = [
    // 阶段 1: 数据获取
    [
        { id: '数据获取', name: '数据获取', icon: <Database className="w-4 h-4" />, team: 'data' },
    ],
    // 阶段 2: 分析师团队（并行）
    [
        { id: 'Market Analyst', name: '市场分析', icon: <TrendingUp className="w-4 h-4" />, team: 'analyst' },
        { id: 'Social Analyst', name: '舆情分析', icon: <MessageCircle className="w-4 h-4" />, team: 'analyst' },
        { id: 'News Analyst', name: '新闻分析', icon: <Newspaper className="w-4 h-4" />, team: 'analyst' },
        { id: 'Fundamentals Analyst', name: '基本面', icon: <Calculator className="w-4 h-4" />, team: 'analyst' },
    ],
    // 阶段 3: 研究团队
    [
        { id: 'Research Manager', name: '研究决策', icon: <Scale className="w-4 h-4" />, team: 'research' },
    ],
    // 阶段 4: 交易员
    [
        { id: 'Trader', name: '交易计划', icon: <Briefcase className="w-4 h-4" />, team: 'trading' },
    ],
    // 阶段 5: 风控团队
    [
        { id: 'Risk', name: '风险评估', icon: <Shield className="w-4 h-4" />, team: 'risk' },
    ],
    // 阶段 6: 组合经理
    [
        { id: 'Portfolio Manager', name: '最终决策', icon: <CheckCircle2 className="w-4 h-4" />, team: 'portfolio' },
    ],
]

const TEAM_COLORS: Record<string, { bg: string; border: string; glow: string; text: string }> = {
    data: { bg: 'bg-slate-100', border: 'border-slate-400', glow: 'shadow-slate-400/50', text: 'text-slate-700' },
    analyst: { bg: 'bg-blue-50', border: 'border-blue-400', glow: 'shadow-blue-400/50', text: 'text-blue-700' },
    research: { bg: 'bg-purple-50', border: 'border-purple-400', glow: 'shadow-purple-400/50', text: 'text-purple-700' },
    trading: { bg: 'bg-orange-50', border: 'border-orange-400', glow: 'shadow-orange-400/50', text: 'text-orange-700' },
    risk: { bg: 'bg-rose-50', border: 'border-rose-400', glow: 'shadow-rose-400/50', text: 'text-rose-700' },
    portfolio: { bg: 'bg-emerald-50', border: 'border-emerald-400', glow: 'shadow-emerald-400/50', text: 'text-emerald-700' },
}

export default function AgentFlow() {
    const { agents, isAnalyzing } = useAnalysisStore()
    const [completedStages, setCompletedStages] = useState<Set<number>>(new Set())

    // 计算已完成的阶段
    useEffect(() => {
        const completed = new Set<number>()
        PIPELINE_STAGES.forEach((stage, index) => {
            const allCompleted = stage.every(node => {
                const agent = agents.find((a) => a.name === node.id)
                return agent?.status === 'completed' || agent?.status === 'skipped'
            })
            if (allCompleted && stage.length > 0) {
                completed.add(index)
            }
        })
        setCompletedStages(completed)
    }, [agents])

    const getAgentStatus = (agentId: string): AgentStatus => {
        // Risk 是多个 agent 的组合状态
        if (agentId === 'Risk') {
            const riskAgents = ['Aggressive Analyst', 'Neutral Analyst', 'Conservative Analyst']
            const statuses = riskAgents.map(id => {
                const agent = agents.find(a => a.name === id)
                return agent?.status || 'pending'
            })
            if (statuses.some(s => s === 'in_progress')) return 'in_progress'
            if (statuses.every(s => s === 'completed' || s === 'skipped')) return 'completed'
            return 'pending'
        }
        const agent = agents.find((a) => a.name === agentId)
        return agent?.status || 'pending'
    }

    const getNodeStyles = (status: AgentStatus, team: string) => {
        const colors = TEAM_COLORS[team] || TEAM_COLORS.data
        
        switch (status) {
            case 'in_progress':
                return `bg-white ${colors.border} ${colors.glow} shadow-lg ring-2 ring-offset-1 animate-pulse`
            case 'completed':
                return `${colors.bg} ${colors.border} shadow-md`
            case 'skipped':
                return 'bg-slate-50 border-slate-200 opacity-50'
            default:
                return 'bg-slate-100 border-slate-300 opacity-60'
        }
    }

    return (
        <div className="card">
            {/* 标题 */}
            <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                    <div className="w-2 h-2 rounded-full bg-gradient-to-r from-blue-500 to-purple-500" />
                    <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                        Agent 协作流程
                    </h3>
                </div>
                {isAnalyzing && (
                    <span className="text-xs text-blue-500 animate-pulse flex items-center gap-1">
                        <Loader2 className="w-3 h-3 animate-spin" />
                        分析中
                    </span>
                )}
            </div>

            {/* 横向流水线 */}
            <div className="relative overflow-x-auto pb-2 scrollbar-thin scrollbar-thumb-slate-300 scrollbar-track-transparent">
                <div className="flex items-start gap-2 min-w-max">
                    {PIPELINE_STAGES.map((stage, stageIndex) => (
                        <div key={stageIndex} className="flex items-center">
                            {/* 阶段节点组 */}
                            <div className={`
                                flex flex-col gap-2 p-2 rounded-xl border transition-all duration-300
                                ${completedStages.has(stageIndex)
                                    ? 'bg-slate-50/50 dark:bg-slate-800/30 border-slate-200 dark:border-slate-700'
                                    : 'bg-transparent border-transparent'
                                }
                            `}>
                                {/* 阶段标签 */}
                                <div className="text-[9px] uppercase tracking-wider text-slate-400 text-center font-medium">
                                    {stageIndex === 0 && 'Data'}
                                    {stageIndex === 1 && 'Analysis'}
                                    {stageIndex === 2 && 'Research'}
                                    {stageIndex === 3 && 'Trading'}
                                    {stageIndex === 4 && 'Risk'}
                                    {stageIndex === 5 && 'Decision'}
                                </div>
                                
                                {/* 节点网格 */}
                                <div className={`flex gap-2 ${stage.length > 2 ? 'flex-wrap max-w-[140px]' : 'flex-col'}`}>
                                    {stage.map((node) => {
                                        const status = getAgentStatus(node.id)
                                        const isActive = status === 'in_progress'
                                        const isCompleted = status === 'completed' || status === 'skipped'
                                        
                                        return (
                                            <div
                                                key={node.id}
                                                className={`
                                                    relative flex flex-col items-center gap-1 p-2 rounded-lg border-2 
                                                    transition-all duration-300 min-w-[60px]
                                                    ${getNodeStyles(status, node.team)}
                                                `}
                                            >
                                                {/* 图标 */}
                                                <div className={`p-1.5 rounded-md ${isActive ? 'bg-blue-100 text-blue-600' : isCompleted ? 'bg-white/50' : 'bg-white/30'}`}>
                                                    {isActive ? (
                                                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                                                    ) : (
                                                        node.icon
                                                    )}
                                                </div>

                                                {/* 名称 */}
                                                <span className={`text-[9px] font-medium text-center leading-tight ${isActive || isCompleted ? 'text-slate-700' : 'text-slate-500'}`}>
                                                    {node.name}
                                                </span>

                                                {/* 状态指示点 */}
                                                {isActive && (
                                                    <div className="absolute -top-1 -right-1 w-2 h-2 bg-blue-500 rounded-full animate-ping" />
                                                )}
                                                {isCompleted && (
                                                    <div className="absolute -top-1 -right-1 w-2 h-2 bg-emerald-500 rounded-full flex items-center justify-center">
                                                        <CheckCircle2 className="w-1.5 h-1.5 text-white" />
                                                    </div>
                                                )}
                                            </div>
                                        )
                                    })}
                                </div>
                            </div>

                            {/* 箭头连接 */}
                            {stageIndex < PIPELINE_STAGES.length - 1 && (
                                <div className="flex items-center px-1">
                                    <div className="relative">
                                        <div className={`w-4 h-0.5 ${completedStages.has(stageIndex) ? 'bg-blue-400' : 'bg-slate-200'}`} />
                                        <ArrowRight className={`absolute -right-1 -top-[5px] w-2.5 h-2.5 ${completedStages.has(stageIndex) ? 'text-blue-400' : 'text-slate-300'}`} />
                                    </div>
                                </div>
                            )}
                        </div>
                    ))}
                </div>
            </div>
        </div>
    )
}

import { useEffect, useState } from 'react'
import { useAnalysisStore } from '@/stores/analysisStore'
import type { AgentStatus } from '@/types'
import {
    TrendingUp,
    MessageCircle,
    Newspaper,
    Calculator,
    Scale,
    Shield,
    Briefcase,
    CheckCircle2,
    Loader2,
    Users
} from 'lucide-react'

interface AgentInfo {
    id: string
    name: string
    title: string
    icon: React.ReactNode
    color: string
}

const AGENTS: AgentInfo[] = [
    { id: 'Market Analyst', name: '技术分析师', title: '市场技术分析', icon: <TrendingUp className="w-4 h-4" />, color: 'bg-blue-500' },
    { id: 'Social Analyst', name: '情绪分析师', title: '舆情情绪监测', icon: <MessageCircle className="w-4 h-4" />, color: 'bg-purple-500' },
    { id: 'News Analyst', name: '新闻分析师', title: '新闻事件分析', icon: <Newspaper className="w-4 h-4" />, color: 'bg-cyan-500' },
    { id: 'Fundamentals Analyst', name: '估值分析师', title: '基本面估值分析', icon: <Calculator className="w-4 h-4" />, color: 'bg-emerald-500' },
    { id: 'Research Manager', name: '研究经理', title: '多空观点汇总', icon: <Scale className="w-4 h-4" />, color: 'bg-indigo-500' },
    { id: 'Risk Analyst', name: '风险分析师', title: '风险评估预警', icon: <Shield className="w-4 h-4" />, color: 'bg-amber-500' },
    { id: 'Trader', name: '交易员', title: '交易策略制定', icon: <Briefcase className="w-4 h-4" />, color: 'bg-orange-500' },
    { id: 'Portfolio Manager', name: '组合经理', title: '最终投资决策', icon: <CheckCircle2 className="w-4 h-4" />, color: 'bg-rose-500' },
]

export default function AgentCollaboration() {
    const { agents, isAnalyzing, streamingSections } = useAnalysisStore()
    const [completedCount, setCompletedCount] = useState(0)

    useEffect(() => {
        const analystAgents = agents.filter(a =>
            ['Market Analyst', 'Social Analyst', 'News Analyst', 'Fundamentals Analyst'].includes(a.name)
        )
        setCompletedCount(analystAgents.filter(a => a.status === 'completed').length)
    }, [agents])

    const getAgentStatus = (agentId: string): AgentStatus => {
        if (agentId === 'Risk Analyst') {
            const riskAgents = ['Aggressive Analyst', 'Neutral Analyst', 'Conservative Analyst']
            const statuses = riskAgents.map(id => agents.find(a => a.name === id)?.status || 'pending')
            if (statuses.some(s => s === 'in_progress')) return 'in_progress'
            if (statuses.every(s => s === 'completed' || s === 'skipped')) return 'completed'
            return 'pending'
        }
        return agents.find(a => a.name === agentId)?.status || 'pending'
    }

    const getAgentContent = (agentId: string): string => {
        const reportMap: Record<string, string> = {
            'Market Analyst': streamingSections['market_report']?.buffer || '',
            'Social Analyst': streamingSections['sentiment_report']?.buffer || '',
            'News Analyst': streamingSections['news_report']?.buffer || '',
            'Fundamentals Analyst': streamingSections['fundamentals_report']?.buffer || '',
        }
        return reportMap[agentId] || ''
    }

    const hasAnyActive = agents.some(a => a.status === 'in_progress' || a.status === 'completed')

    if (!hasAnyActive && !isAnalyzing) {
        return (
            <div className="card bg-slate-900/50 border-slate-700/50 flex items-center justify-center py-12">
                <div className="text-center">
                    <Users className="w-10 h-10 text-slate-600 mx-auto mb-3" />
                    <p className="text-sm text-slate-400">智能体委员会待命中</p>
                    <p className="text-xs text-slate-500 mt-1">发起分析后将在此显示协作过程</p>
                </div>
            </div>
        )
    }

    return (
        <div className="card bg-slate-900/50 border-slate-700/50">
            {/* 头部 */}
            <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2">
                    <div className="p-1.5 rounded-lg bg-gradient-to-br from-blue-500 to-purple-500">
                        <Users className="w-4 h-4 text-white" />
                    </div>
                    <h3 className="text-sm font-semibold text-slate-100">智能体委员会讨论记录</h3>
                </div>
                {isAnalyzing && (
                    <div className="flex items-center gap-1.5 text-xs text-blue-400">
                        <Loader2 className="w-3 h-3 animate-spin" />
                        实时协作
                    </div>
                )}
            </div>

            {/* Agent 讨论列表 */}
            <div className="space-y-3 max-h-[280px] overflow-y-auto pr-1">
                {AGENTS.map((agent) => {
                    const status = getAgentStatus(agent.id)
                    const content = getAgentContent(agent.id)
                    const isActive = status === 'in_progress'
                    const isCompleted = status === 'completed'

                    if (status === 'pending') return null

                    return (
                        <div key={agent.id} className="flex gap-3">
                            {/* 头像 */}
                            <div className={`
                                shrink-0 w-9 h-9 rounded-full flex items-center justify-center
                                ${isActive ? 'ring-2 ring-blue-400 ring-offset-2 ring-offset-slate-900' : ''}
                                ${isCompleted ? agent.color : 'bg-slate-700'}
                            `}>
                                {isActive ? (
                                    <Loader2 className="w-4 h-4 text-white animate-spin" />
                                ) : (
                                    <span className="text-white">{agent.icon}</span>
                                )}
                            </div>

                            {/* 内容卡片 */}
                            <div className="flex-1 min-w-0">
                                <div className={`
                                    p-3 rounded-xl border transition-all duration-300
                                    ${isActive
                                        ? 'bg-slate-800/80 border-blue-500/30'
                                        : isCompleted
                                            ? 'bg-slate-800/50 border-slate-700/50'
                                            : 'bg-slate-800/30 border-slate-800'
                                    }
                                `}>
                                    <div className="flex items-center gap-2 mb-1">
                                        <span className="text-sm font-medium text-slate-200">{agent.name}</span>
                                        {isCompleted && <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />}
                                    </div>
                                    <p className={`text-xs leading-relaxed ${isActive ? 'text-blue-300' : 'text-slate-400'}`}>
                                        {isActive
                                            ? '正在分析中...'
                                            : content
                                                ? content.slice(0, 80) + (content.length > 80 ? '...' : '')
                                                : agent.title
                                        }
                                    </p>
                                </div>
                            </div>
                        </div>
                    )
                })}
            </div>

            {/* 分析师共识进度 */}
            {completedCount > 0 && (
                <div className="mt-4 pt-3 border-t border-slate-700/50">
                    <div className="flex items-center justify-between text-xs">
                        <span className="text-slate-400">分析师完成进度</span>
                        <span className="text-emerald-400 font-medium">{completedCount}/4</span>
                    </div>
                    <div className="mt-1.5 h-1 bg-slate-700 rounded-full overflow-hidden">
                        <div
                            className="h-full bg-emerald-500 rounded-full transition-all duration-500"
                            style={{ width: `${(completedCount / 4) * 100}%` }}
                        />
                    </div>
                </div>
            )}
        </div>
    )
}

import { useState } from 'react'
import {
    BarChart3,
    Users,
    Newspaper,
    Calculator,
    TrendingUp,
    TrendingDown,
    AlertTriangle,
    CheckCircle2,
    Clock,
    Play,
    Pause,
} from 'lucide-react'

interface AgentOpinion {
    id: string
    name: string
    team?: string
    avatar?: string
    status: 'pending' | 'in_progress' | 'completed' | 'error'
    opinion: string
    time?: string
    isWarning?: boolean
    icon?: typeof BarChart3
}

const defaultAgents: AgentOpinion[] = [
    {
        id: 'market',
        name: '技术分析师',
        team: 'Analyst Team',
        avatar: '📊',
        status: 'completed',
        opinion: '突破20日均线，MACD金叉确认，RSI处于强势区（62），量价配合良好',
        time: '2分钟前',
        icon: BarChart3,
    },
    {
        id: 'sentiment',
        name: '舆情分析师',
        team: 'Analyst Team',
        avatar: '📰',
        status: 'completed',
        opinion: '机构调研频次+40%，舆情正面，无重大负面新闻，社交媒体情绪偏乐观',
        time: '2分钟前',
        icon: Newspaper,
    },
    {
        id: 'fundamentals',
        name: '估值分析师',
        team: 'Analyst Team',
        avatar: '💰',
        status: 'completed',
        opinion: 'PE处于历史30%分位，低于行业平均35%，处于低估区间，安全边际充足',
        time: '1分钟前',
        icon: Calculator,
    },
    {
        id: 'bull',
        name: '看多研究员',
        team: 'Research Team',
        avatar: '🐂',
        status: 'completed',
        opinion: 'Q4业绩有望超预期，新产能释放将带来15%营收增长，目标价上调至¥185',
        time: '1分钟前',
        icon: TrendingUp,
    },
    {
        id: 'bear',
        name: '看空研究员',
        team: 'Research Team',
        avatar: '🐻',
        status: 'completed',
        opinion: '短期涨幅过大，存在回调压力，建议等待回踩20日均线后再介入',
        time: '45秒前',
        icon: TrendingDown,
    },
    {
        id: 'risk',
        name: '风险分析师',
        team: 'Risk Management',
        avatar: '⚠️',
        status: 'completed',
        opinion: '警告：Q3业绩可能不及预期，需关注财报发布。建议仓位控制在15%以内',
        time: '30秒前',
        isWarning: true,
        icon: AlertTriangle,
    },
    {
        id: 'portfolio',
        name: '投资组合经理',
        team: 'Portfolio Management',
        avatar: '🎯',
        status: 'in_progress',
        opinion: '正在综合各方观点...',
        time: '进行中',
        icon: Users,
    },
]

interface AgentCollaborationProps {
    agents?: AgentOpinion[]
}

export default function AgentCollaboration({ agents }: AgentCollaborationProps) {
    const [isPlaying, setIsPlaying] = useState(false)
    const displayAgents = agents || defaultAgents

    const completedCount = displayAgents.filter((a) => a.status === 'completed').length
    const progress = (completedCount / displayAgents.length) * 100

    return (
        <div className="card">
            {/* 头部 */}
            <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2">
                    <Users className="w-5 h-5 text-purple-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">智能体委员会讨论记录</h2>
                </div>
                <div className="flex items-center gap-2">
                    <span className="text-sm text-slate-500">
                        {completedCount}/{displayAgents.length} 完成
                    </span>
                    <button
                        onClick={() => setIsPlaying(!isPlaying)}
                        className="p-2 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
                    >
                        {isPlaying ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4" />}
                    </button>
                </div>
            </div>

            {/* 进度条 */}
            <div className="h-1.5 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden mb-4">
                <div
                    className="h-full bg-gradient-to-r from-blue-500 via-purple-500 to-cyan-400 rounded-full transition-all duration-500"
                    style={{ width: `${progress}%` }}
                />
            </div>

            {/* Agent列表 */}
            <div className="space-y-3 max-h-[400px] overflow-y-auto pr-1">
                {displayAgents.map((agent) => (
                    <AgentCard key={agent.id} agent={agent} />
                ))}
            </div>

            {/* 底部操作 */}
            <div className="mt-4 pt-3 border-t border-slate-200 dark:border-slate-700 flex gap-2">
                <button className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors">
                    <Play className="w-3.5 h-3.5" />
                    回放讨论过程
                </button>
                <button className="px-3 py-1.5 text-sm rounded-lg bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors">
                    查看原始数据
                </button>
            </div>
        </div>
    )
}

function AgentCard({ agent }: { agent: AgentOpinion }) {
    const StatusIcon = {
        pending: Clock,
        in_progress: Clock,
        completed: CheckCircle2,
        error: AlertTriangle,
    }[agent.status]

    const statusColors = {
        pending: 'text-slate-400',
        in_progress: 'text-blue-500 animate-pulse',
        completed: agent.isWarning ? 'text-orange-500' : 'text-green-500',
        error: 'text-red-500',
    }

    const cardColors = agent.isWarning
        ? 'bg-orange-50 dark:bg-orange-500/10 border-orange-200 dark:border-orange-500/30'
        : agent.status === 'in_progress'
            ? 'bg-blue-50 dark:bg-blue-500/10 border-blue-200 dark:border-blue-500/30'
            : agent.status === 'completed'
                ? 'bg-slate-50 dark:bg-slate-800/50 border-slate-100 dark:border-slate-700'
                : 'bg-slate-50 dark:bg-slate-800/30 border-slate-100 dark:border-slate-700 opacity-60'

    return (
        <div className={`p-3 rounded-xl border ${cardColors} transition-all duration-300`}>
            <div className="flex items-start gap-3">
                {/* 头像 */}
                <div className="relative">
                    <div className="w-10 h-10 rounded-full bg-white dark:bg-slate-800 border-2 border-slate-200 dark:border-slate-700 flex items-center justify-center text-lg">
                        {agent.avatar}
                    </div>
                    <div className={`absolute -bottom-0.5 -right-0.5 w-4 h-4 rounded-full flex items-center justify-center ${statusColors[agent.status]}`}>
                        <StatusIcon className="w-3 h-3 bg-white dark:bg-slate-900 rounded-full" />
                    </div>
                </div>

                {/* 内容 */}
                <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between">
                        <div>
                            <h4 className="font-medium text-slate-900 dark:text-slate-100">{agent.name}</h4>
                            <p className="text-xs text-slate-400">{agent.team}</p>
                        </div>
                        <span className="text-xs text-slate-400">{agent.time}</span>
                    </div>

                    {agent.status !== 'pending' && (
                        <div className="mt-2">
                            <p className={`text-sm ${agent.isWarning ? 'text-orange-700 dark:text-orange-300' : 'text-slate-600 dark:text-slate-400'}`}>
                                <span className="opacity-50">└─&gt;</span> {agent.opinion}
                            </p>
                        </div>
                    )}
                </div>
            </div>
        </div>
    )
}

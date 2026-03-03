import { useState } from 'react'
import { TrendingUp, Activity, Clock, CheckCircle } from 'lucide-react'
import { useAnalysisStore } from '@/stores/analysisStore'

export default function Dashboard() {
    const { agents, logs, isAnalyzing, isConnected } = useAnalysisStore()

    const completedAgents = agents.filter(a => a.status === 'completed').length
    const inProgressAgents = agents.filter(a => a.status === 'in_progress').length

    return (
        <div className="space-y-6">
            {/* Welcome Section */}
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-trading-text-primary">控制台</h1>
                    <p className="text-trading-text-secondary mt-1">
                        欢迎使用 TradingAgents 智能分析系统
                    </p>
                </div>
                <div className="flex items-center gap-2">
                    <div className={`w-2 h-2 rounded-full ${isConnected ? 'bg-trading-accent-green animate-pulse' : 'bg-trading-text-muted'}`} />
                    <span className="text-sm text-trading-text-secondary">
                        {isConnected ? '已连接' : '未连接'}
                    </span>
                </div>
            </div>

            {/* Stats Cards */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                <StatCard
                    icon={Activity}
                    label="Agent 状态"
                    value={`${inProgressAgents} 进行中`}
                    subValue={`${completedAgents} 已完成`}
                    color="blue"
                />
                <StatCard
                    icon={CheckCircle}
                    label="分析任务"
                    value={isAnalyzing ? '分析中' : '空闲'}
                    subValue={isAnalyzing ? '请稍候...' : '准备就绪'}
                    color={isAnalyzing ? 'orange' : 'green'}
                />
                <StatCard
                    icon={Clock}
                    label="系统状态"
                    value="正常"
                    subValue="所有服务运行中"
                    color="green"
                />
                <StatCard
                    icon={TrendingUp}
                    label="今日分析"
                    value="0"
                    subValue="只股票"
                    color="purple"
                />
            </div>

            {/* Quick Actions */}
            <div className="card">
                <h2 className="text-lg font-semibold text-trading-text-primary mb-4">快速开始</h2>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                    <QuickActionCard
                        title="开始新分析"
                        description="输入股票代码，启动多 Agent 智能分析"
                        action="开始分析"
                        href="/analysis"
                    />
                    <QuickActionCard
                        title="查看历史报告"
                        description="浏览已完成的分析报告"
                        action="查看报告"
                        href="/reports"
                    />
                    <QuickActionCard
                        title="系统设置"
                        description="配置 API 和分析参数"
                        action="打开设置"
                        href="/settings"
                    />
                </div>
            </div>

            {/* Recent Activity */}
            <div className="card">
                <h2 className="text-lg font-semibold text-trading-text-primary mb-4">最近活动</h2>
                <div className="space-y-2 max-h-64 overflow-y-auto">
                    {logs.length === 0 ? (
                        <p className="text-trading-text-muted text-center py-8">暂无活动记录</p>
                    ) : (
                        logs.slice(0, 20).map((log) => (
                            <div
                                key={log.id}
                                className="flex items-start gap-3 p-3 rounded-lg bg-trading-bg-tertiary/50"
                            >
                                <div className={`w-2 h-2 rounded-full mt-2 flex-shrink-0 ${log.type === 'error' ? 'bg-trading-accent-red' :
                                        log.type === 'system' ? 'bg-trading-accent-blue' :
                                            log.type === 'tool' ? 'bg-trading-accent-orange' :
                                                'bg-trading-accent-green'
                                    }`} />
                                <div className="flex-1 min-w-0">
                                    <p className="text-sm text-trading-text-primary">{log.content}</p>
                                    <p className="text-xs text-trading-text-muted mt-1">
                                        {new Date(log.timestamp).toLocaleTimeString()}
                                        {log.agent && ` · ${log.agent}`}
                                    </p>
                                </div>
                            </div>
                        ))
                    )}
                </div>
            </div>
        </div>
    )
}

interface StatCardProps {
    icon: React.ComponentType<{ className?: string }>
    label: string
    value: string
    subValue: string
    color: 'blue' | 'green' | 'orange' | 'purple' | 'red'
}

function StatCard({ icon: Icon, label, value, subValue, color }: StatCardProps) {
    const colorClasses = {
        blue: 'bg-trading-accent-blue/10 text-trading-accent-blue',
        green: 'bg-trading-accent-green/10 text-trading-accent-green',
        orange: 'bg-trading-accent-orange/10 text-trading-accent-orange',
        purple: 'bg-trading-accent-purple/10 text-trading-accent-purple',
        red: 'bg-trading-accent-red/10 text-trading-accent-red',
    }

    return (
        <div className="card card-hover">
            <div className="flex items-start justify-between">
                <div>
                    <p className="text-sm text-trading-text-secondary">{label}</p>
                    <p className="text-2xl font-bold text-trading-text-primary mt-1">{value}</p>
                    <p className="text-xs text-trading-text-muted mt-1">{subValue}</p>
                </div>
                <div className={`p-3 rounded-lg ${colorClasses[color]}`}>
                    <Icon className="w-5 h-5" />
                </div>
            </div>
        </div>
    )
}

interface QuickActionCardProps {
    title: string
    description: string
    action: string
    href: string
}

function QuickActionCard({ title, description, action, href }: QuickActionCardProps) {
    return (
        <a
            href={href}
            className="block p-4 rounded-lg border border-trading-border bg-trading-bg-tertiary/30 hover:border-trading-accent-blue/50 hover:bg-trading-bg-tertiary/50 transition-all duration-200"
        >
            <h3 className="font-medium text-trading-text-primary">{title}</h3>
            <p className="text-sm text-trading-text-secondary mt-1">{description}</p>
            <span className="inline-block mt-3 text-sm text-trading-accent-blue hover:underline">
                {action} →
            </span>
        </a>
    )
}
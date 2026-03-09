import { useMemo } from 'react'
import { useAnalysisStore } from '@/stores/analysisStore'
import type { AgentStatus } from '@/types'
import {
    TrendingUp,
    MessageCircle,
    Newspaper,
    Calculator,
    ArrowBigUp,
    ArrowBigDown,
    Brain,
    Briefcase,
    Flame,
    Scale,
    Shield,
    CheckCircle2,
    Loader2,
} from 'lucide-react'

interface AgentCardMeta {
    name: string
    label: string
    short: string
    section?: string
    icon: React.ReactNode
    tint: string
    activeTint: string
}

const META: AgentCardMeta[] = [
    { name: 'Market Analyst', label: '市场分析师', short: '技术', section: 'market_report', icon: <TrendingUp className="w-3.5 h-3.5" />, tint: 'text-blue-500', activeTint: 'ring-blue-500/30 bg-blue-50 dark:bg-blue-500/10' },
    { name: 'Social Analyst', label: '舆情分析师', short: '情绪', section: 'sentiment_report', icon: <MessageCircle className="w-3.5 h-3.5" />, tint: 'text-fuchsia-500', activeTint: 'ring-fuchsia-500/30 bg-fuchsia-50 dark:bg-fuchsia-500/10' },
    { name: 'News Analyst', label: '新闻分析师', short: '新闻', section: 'news_report', icon: <Newspaper className="w-3.5 h-3.5" />, tint: 'text-cyan-500', activeTint: 'ring-cyan-500/30 bg-cyan-50 dark:bg-cyan-500/10' },
    { name: 'Fundamentals Analyst', label: '基本面分析师', short: '基本面', section: 'fundamentals_report', icon: <Calculator className="w-3.5 h-3.5" />, tint: 'text-emerald-500', activeTint: 'ring-emerald-500/30 bg-emerald-50 dark:bg-emerald-500/10' },
    { name: 'Bull Researcher', label: '多头研究员', short: '多头', section: 'investment_plan', icon: <ArrowBigUp className="w-3.5 h-3.5" />, tint: 'text-emerald-500', activeTint: 'ring-emerald-500/30 bg-emerald-50 dark:bg-emerald-500/10' },
    { name: 'Bear Researcher', label: '空头研究员', short: '空头', section: 'investment_plan', icon: <ArrowBigDown className="w-3.5 h-3.5" />, tint: 'text-rose-500', activeTint: 'ring-rose-500/30 bg-rose-50 dark:bg-rose-500/10' },
    { name: 'Research Manager', label: '研究经理', short: '研究', section: 'investment_plan', icon: <Brain className="w-3.5 h-3.5" />, tint: 'text-indigo-500', activeTint: 'ring-indigo-500/30 bg-indigo-50 dark:bg-indigo-500/10' },
    { name: 'Trader', label: '交易员', short: '交易', section: 'trader_investment_plan', icon: <Briefcase className="w-3.5 h-3.5" />, tint: 'text-orange-500', activeTint: 'ring-orange-500/30 bg-orange-50 dark:bg-orange-500/10' },
    { name: 'Aggressive Analyst', label: '激进风控', short: '激进', section: 'final_trade_decision', icon: <Flame className="w-3.5 h-3.5" />, tint: 'text-red-500', activeTint: 'ring-red-500/30 bg-red-50 dark:bg-red-500/10' },
    { name: 'Neutral Analyst', label: '中性风控', short: '中性', section: 'final_trade_decision', icon: <Scale className="w-3.5 h-3.5" />, tint: 'text-slate-500 dark:text-slate-300', activeTint: 'ring-slate-400/30 bg-slate-100 dark:bg-slate-700/40' },
    { name: 'Conservative Analyst', label: '稳健风控', short: '稳健', section: 'final_trade_decision', icon: <Shield className="w-3.5 h-3.5" />, tint: 'text-amber-500', activeTint: 'ring-amber-500/30 bg-amber-50 dark:bg-amber-500/10' },
    { name: 'Portfolio Manager', label: '组合经理', short: '决策', section: 'final_trade_decision', icon: <CheckCircle2 className="w-3.5 h-3.5" />, tint: 'text-rose-500', activeTint: 'ring-rose-500/30 bg-rose-50 dark:bg-rose-500/10' },
]

const STATUS_TEXT: Record<AgentStatus, string> = {
    pending: '待命',
    in_progress: '运行中',
    completed: '完成',
    skipped: '跳过',
    error: '异常',
}

interface AgentCollaborationProps {
    onSelectSection?: (section?: string) => void
}

export default function AgentCollaboration({ onSelectSection }: AgentCollaborationProps) {
    const { agents, isAnalyzing, streamingSections, report } = useAnalysisStore()

    const cards = useMemo(() => {
        return META.map((meta) => {
            const agent = agents.find(item => item.name === meta.name)
            const streamingState = meta.section ? streamingSections[meta.section] : undefined
            const reportContent = meta.section ? (report?.[meta.section as keyof typeof report] as string | undefined) : undefined
            const previewSource = streamingState?.displayed || reportContent || ''
            const preview = previewSource
                .replace(/^#+\s*/gm, '')
                .replace(/\*\*/g, '')
                .replace(/\s+/g, ' ')
                .trim()
                .slice(0, 52)
            return {
                ...meta,
                status: agent?.status || 'pending',
                isSectionStreaming: !!streamingState?.isTyping,
                hasSectionContent: !!previewSource,
                preview,
            }
        })
    }, [agents, report, streamingSections])

    const completedCount = cards.filter(card => card.status === 'completed' || card.status === 'skipped').length

    return (
        <section className="card">
            <div className="flex items-center justify-between mb-3">
                <h3 className="text-lg font-semibold text-slate-900 dark:text-slate-100">研究进程</h3>
                <div className="flex items-center gap-3">
                    <span className="text-sm text-slate-500 dark:text-slate-400">{completedCount}/12</span>
                    {isAnalyzing && (
                        <span className="inline-flex items-center gap-1 rounded-full px-2 py-1 text-xs bg-blue-100 text-blue-600 dark:bg-blue-500/15 dark:text-blue-300">
                            <Loader2 className="w-3 h-3 animate-spin" />
                            运行中
                        </span>
                    )}
                </div>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-2.5">
                {cards.map((card) => {
                    const clickable = !!card.section
                    const active = card.status === 'in_progress'
                    const done = card.status === 'completed' || card.status === 'skipped'

                    return (
                        <button
                            key={card.name}
                            type="button"
                            disabled={!clickable}
                            onClick={() => onSelectSection?.(card.section)}
                            className={`rounded-2xl border p-3 text-left transition-all ${active ? `ring-2 ${card.activeTint}` : 'bg-white dark:bg-slate-900/70'} ${done ? 'border-emerald-200 dark:border-emerald-500/20' : 'border-slate-200 dark:border-slate-700'} ${clickable ? 'hover:border-slate-400 dark:hover:border-slate-500' : 'cursor-default'}`}
                        >
                            <div className="flex items-center justify-between mb-2">
                                <div className={`${card.tint}`}>{active ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : card.icon}</div>
                                <span className={`text-[11px] px-1.5 py-0.5 rounded-full ${done ? 'bg-emerald-100 text-emerald-600 dark:bg-emerald-500/15 dark:text-emerald-400' : active ? 'bg-blue-100 text-blue-600 dark:bg-blue-500/15 dark:text-blue-300' : 'bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400'}`}>
                                    {STATUS_TEXT[card.status]}
                                </span>
                            </div>
                            <div className="text-sm font-medium text-slate-900 dark:text-slate-100">{card.label}</div>
                            <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{card.short}</div>

                            {(card.isSectionStreaming || card.hasSectionContent) && (
                                <div className={`mt-3 rounded-xl border px-2.5 py-2 text-[11px] leading-5 ${
                                    card.isSectionStreaming
                                        ? 'border-blue-200 bg-blue-50/80 text-blue-700 dark:border-blue-500/20 dark:bg-blue-500/10 dark:text-blue-300'
                                        : 'border-slate-200 bg-slate-50 text-slate-600 dark:border-slate-700 dark:bg-slate-800/70 dark:text-slate-300'
                                }`}>
                                    {card.isSectionStreaming ? (
                                        <div className="flex items-center gap-1.5">
                                            <Loader2 className="w-3 h-3 animate-spin shrink-0" />
                                            <span>{card.preview || '正在生成报告...'}</span>
                                        </div>
                                    ) : (
                                        <span>{card.preview || '报告已生成'}</span>
                                    )}
                                </div>
                            )}
                        </button>
                    )
                })}
            </div>
        </section>
    )
}

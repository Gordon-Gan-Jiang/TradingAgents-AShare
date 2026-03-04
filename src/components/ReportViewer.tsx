import { FileText, Download, ChevronDown, ChevronRight } from 'lucide-react'
import { useState } from 'react'
import { useAnalysisStore } from '@/stores/analysisStore'

const REPORT_SECTIONS = [
    { key: 'market_report', title: '市场分析报告', team: 'Analyst Team' },
    { key: 'sentiment_report', title: '舆情分析报告', team: 'Analyst Team' },
    { key: 'news_report', title: '新闻分析报告', team: 'Analyst Team' },
    { key: 'fundamentals_report', title: '基本面分析报告', team: 'Analyst Team' },
    { key: 'investment_plan', title: '研究团队决策', team: 'Research Team' },
    { key: 'trader_investment_plan', title: '交易团队计划', team: 'Trading Team' },
    { key: 'final_trade_decision', title: '最终交易决策', team: 'Portfolio Management' },
]

export default function ReportViewer() {
    const { report, isAnalyzing } = useAnalysisStore()
    const [expandedSections, setExpandedSections] = useState<string[]>([])

    const hasReport = report && Object.values(report).some(v => v && typeof v === 'string' && v.length > 0)

    const toggleSection = (key: string) => {
        setExpandedSections(prev =>
            prev.includes(key)
                ? prev.filter(k => k !== key)
                : [...prev, key]
        )
    }

    const handleExport = () => {
        if (!report) return

        const reportText = REPORT_SECTIONS
            .filter(section => report[section.key as keyof typeof report])
            .map(section => `## ${section.title}\n\n${report[section.key as keyof typeof report]}`)
            .join('\n\n---\n\n')

        const blob = new Blob([reportText], { type: 'text/markdown' })
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `analysis-report-${report.symbol || 'unknown'}.md`
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        URL.revokeObjectURL(url)
    }

    if (!hasReport && !isAnalyzing) {
        return (
            <div className="card flex items-center justify-center py-12 overflow-hidden">
                <div className="text-center">
                    <FileText className="w-12 h-12 text-trading-text-muted mx-auto mb-4" />
                    <p className="text-trading-text-secondary">暂无分析报告</p>
                    <p className="text-sm text-trading-text-muted mt-1">
                        开始分析后将在此显示报告
                    </p>
                </div>
            </div>
        )
    }

    return (
        <div className="card overflow-hidden">
            <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2">
                    <FileText className="w-5 h-5 text-trading-accent-blue" />
                    <h2 className="text-lg font-semibold text-trading-text-primary">分析报告</h2>
                    {isAnalyzing && (
                        <span className="badge-orange animate-pulse">生成中</span>
                    )}
                </div>
                {hasReport && (
                    <button
                        onClick={handleExport}
                        className="btn-secondary flex items-center gap-2 text-sm py-1.5 px-3"
                    >
                        <Download className="w-4 h-4" />
                        导出
                    </button>
                )}
            </div>

            <div className="space-y-2">
                {REPORT_SECTIONS.map((section) => {
                    const content = report?.[section.key as keyof typeof report]
                    if (!content || typeof content !== 'string' || content.length === 0) {
                        return null
                    }

                    const isExpanded = expandedSections.includes(section.key)

                    return (
                        <div
                            key={section.key}
                            className="border border-trading-border rounded-lg overflow-hidden"
                        >
                            <button
                                onClick={() => toggleSection(section.key)}
                                className="w-full flex items-center justify-between p-3 bg-trading-bg-tertiary/30 hover:bg-trading-bg-tertiary/50 transition-colors"
                            >
                                <div className="flex items-center gap-2">
                                    {isExpanded ? (
                                        <ChevronDown className="w-4 h-4 text-trading-text-muted" />
                                    ) : (
                                        <ChevronRight className="w-4 h-4 text-trading-text-muted" />
                                    )}
                                    <span className="font-medium text-trading-text-primary">
                                        {section.title}
                                    </span>
                                    <span className="text-xs text-trading-text-muted">
                                        {section.team}
                                    </span>
                                </div>
                                <span className="text-xs text-trading-accent-green">✓</span>
                            </button>

                            {isExpanded && (
                                <div className="p-4 prose prose-invert prose-sm max-w-none">
                                    <div className="whitespace-pre-wrap text-trading-text-secondary leading-relaxed">
                                        {content}
                                    </div>
                                </div>
                            )}
                        </div>
                    )
                })}

                {isAnalyzing && !hasReport && (
                    <div className="flex items-center justify-center py-12">
                        <div className="text-center">
                            <div className="w-8 h-8 border-2 border-trading-accent-blue border-t-transparent rounded-full animate-spin mx-auto mb-4" />
                            <p className="text-trading-text-secondary">正在生成报告...</p>
                        </div>
                    </div>
                )}
            </div>
        </div>
    )
}

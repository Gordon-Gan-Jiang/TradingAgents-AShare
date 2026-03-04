import { FileText, Download, Trash2, Search, ChevronLeft, Loader2 } from 'lucide-react'
import { useState, useEffect, useCallback } from 'react'
import { api } from '@/services/api'
import type { Report, ReportDetail } from '@/types'
import DecisionCard from '@/components/DecisionCard'
import AgentCollaboration from '@/components/AgentCollaboration'
import DataLaboratory from '@/components/DataLaboratory'

// Helper function to parse decision text
const parseDecision = (decisionText?: string): { action: string; label: string } => {
    if (!decisionText) return { action: 'hold', label: '观望' }

    const text = decisionText.toUpperCase()
    if (text.includes('BUY') || text.includes('增持') || text.includes('买入')) {
        return { action: 'add', label: '增持' }
    } else if (text.includes('SELL') || text.includes('减持') || text.includes('卖出')) {
        return { action: 'reduce', label: '减持' }
    }
    return { action: 'hold', label: '持有' }
}

// Helper function to get agent opinions from report
const getAgentOpinions = (report: ReportDetail) => {
    const agents = []

    if (report.market_report) {
        agents.push({
            id: 'market',
            name: '技术分析师',
            opinion: report.market_report.slice(0, 100) + (report.market_report.length > 100 ? '...' : ''),
            status: 'completed' as const,
            team: 'Analyst Team',
            avatar: '📊'
        })
    }

    if (report.sentiment_report) {
        agents.push({
            id: 'sentiment',
            name: '舆情分析师',
            opinion: report.sentiment_report.slice(0, 100) + (report.sentiment_report.length > 100 ? '...' : ''),
            status: 'completed' as const,
            team: 'Analyst Team',
            avatar: '📰'
        })
    }

    if (report.fundamentals_report) {
        agents.push({
            id: 'fundamentals',
            name: '估值分析师',
            opinion: report.fundamentals_report.slice(0, 100) + (report.fundamentals_report.length > 100 ? '...' : ''),
            status: 'completed' as const,
            team: 'Analyst Team',
            avatar: '💰'
        })
    }

    if (report.news_report) {
        agents.push({
            id: 'news',
            name: '新闻分析师',
            opinion: report.news_report.slice(0, 100) + (report.news_report.length > 100 ? '...' : ''),
            status: 'completed' as const,
            team: 'Analyst Team',
            avatar: '📢'
        })
    }

    if (report.final_trade_decision && report.final_trade_decision.toLowerCase().includes('risk')) {
        agents.push({
            id: 'risk',
            name: '风险分析师',
            opinion: '已识别潜在风险因素',
            status: 'completed' as const,
            team: 'Risk Management',
            avatar: '⚠️',
            isWarning: true
        })
    }

    return agents
}

export default function Reports() {
    const [searchQuery, setSearchQuery] = useState('')
    const [reports, setReports] = useState<Report[]>([])
    const [selectedReport, setSelectedReport] = useState<ReportDetail | null>(null)
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [deleting, setDeleting] = useState<string | null>(null)

    // Fetch reports
    const fetchReports = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const response = await api.getReports(undefined, 0, 100)
            setReports(response.reports)
        } catch (err) {
            setError(err instanceof Error ? err.message : '获取报告失败')
        } finally {
            setLoading(false)
        }
    }, [])

    useEffect(() => {
        fetchReports()
    }, [fetchReports])

    // Handle report deletion
    const handleDelete = async (e: React.MouseEvent, reportId: string) => {
        e.stopPropagation()
        if (!confirm('确定要删除这份报告吗？')) return

        setDeleting(reportId)
        try {
            await api.deleteReport(reportId)
            setReports(prev => prev.filter(r => r.id !== reportId))
        } catch (err) {
            alert(err instanceof Error ? err.message : '删除失败')
        } finally {
            setDeleting(null)
        }
    }

    // Handle report selection - fetch full details
    const handleSelectReport = async (report: Report) => {
        try {
            const detail = await api.getReport(report.id)
            setSelectedReport(detail)
        } catch (err) {
            alert(err instanceof Error ? err.message : '获取报告详情失败')
        }
    }

    const filteredReports = reports.filter(report =>
        report.symbol.toLowerCase().includes(searchQuery.toLowerCase())
    )

    const getDecisionColor = (decision?: string) => {
        const action = parseDecision(decision).action
        switch (action) {
            case 'add':
                return 'text-green-600 dark:text-green-400'
            case 'reduce':
                return 'text-red-600 dark:text-red-400'
            default:
                return 'text-slate-600 dark:text-slate-400'
        }
    }

    // 报告详情视图
    if (selectedReport) {
        const { action } = parseDecision(selectedReport.decision)
        const agents = getAgentOpinions(selectedReport)

        // 计算价格变化百分比（简化处理）
        const currentPrice = selectedReport.target_price ? selectedReport.target_price / 1.1 : 100
        const targetChange = selectedReport.target_price ?
            ((selectedReport.target_price - currentPrice) / currentPrice * 100) : 0
        const stopLossChange = selectedReport.stop_loss_price ?
            ((selectedReport.stop_loss_price - currentPrice) / currentPrice * 100) : 0

        return (
            <div className="space-y-6">
                {/* 返回按钮 */}
                <div className="flex items-center gap-4">
                    <button
                        onClick={() => setSelectedReport(null)}
                        className="flex items-center gap-2 px-4 py-2 rounded-lg bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors"
                    >
                        <ChevronLeft className="w-4 h-4" />
                        返回列表
                    </button>
                    <h1 className="text-xl font-bold text-slate-900 dark:text-slate-100">
                        {selectedReport.symbol} 分析报告
                    </h1>
                </div>

                {/* 报告元信息 */}
                <div className="flex items-center gap-4 text-sm text-slate-500">
                    <span>分析日期: {selectedReport.trade_date}</span>
                    <span>生成时间: {selectedReport.created_at ? new Date(selectedReport.created_at).toLocaleString('zh-CN') : '-'}</span>
                </div>

                {/* 三层分析展示 */}
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
                    {/* 第一层：决策驾驶舱 */}
                    <div className="lg:col-span-1">
                        <DecisionCard
                            symbol={selectedReport.symbol}
                            decision={action as 'add' | 'reduce' | 'hold'}
                            confidence={selectedReport.confidence || 0}
                            targetPrice={selectedReport.target_price || 0}
                            targetChange={targetChange}
                            stopLoss={selectedReport.stop_loss_price || 0}
                            stopLossChange={stopLossChange}
                            reasoning={selectedReport.final_trade_decision?.slice(0, 100) || '暂无详细分析'}
                        />
                    </div>

                    {/* 第二层和第三层 */}
                    <div className="lg:col-span-2 space-y-6">
                        {/* 第二层：Agent协作视图 */}
                        <AgentCollaboration agents={agents} />

                        {/* 第三层：数据实验室 */}
                        <DataLaboratory symbol={selectedReport.symbol} />
                    </div>
                </div>
            </div>
        )
    }

    // 报告列表视图
    return (
        <div className="space-y-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">历史报告</h1>
                    <p className="text-slate-500 dark:text-slate-400 mt-1">
                        查看和管理已生成的分析报告
                    </p>
                </div>
            </div>

            {/* Search Bar */}
            <div className="card">
                <div className="relative max-w-md">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                    <input
                        type="text"
                        value={searchQuery}
                        onChange={(e) => setSearchQuery(e.target.value)}
                        placeholder="搜索股票代码..."
                        className="input w-full pl-10"
                    />
                </div>
            </div>

            {/* Loading State */}
            {loading && (
                <div className="card py-12">
                    <div className="flex flex-col items-center gap-4">
                        <Loader2 className="w-8 h-8 text-blue-500 animate-spin" />
                        <p className="text-slate-500">加载报告中...</p>
                    </div>
                </div>
            )}

            {/* Error State */}
            {error && !loading && (
                <div className="card py-12">
                    <div className="text-center">
                        <p className="text-red-500 mb-4">{error}</p>
                        <button
                            onClick={fetchReports}
                            className="px-4 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 transition-colors"
                        >
                            重试
                        </button>
                    </div>
                </div>
            )}

            {/* Reports Table */}
            {!loading && !error && (
                <div className="card overflow-hidden">
                    <div className="overflow-x-auto">
                        <table className="w-full">
                            <thead>
                                <tr className="border-b border-slate-200 dark:border-slate-700">
                                    <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                                        股票代码
                                    </th>
                                    <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                                        分析日期
                                    </th>
                                    <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                                        决策建议
                                    </th>
                                    <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                                        置信度
                                    </th>
                                    <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                                        目标价/止损价
                                    </th>
                                    <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                                        生成时间
                                    </th>
                                    <th className="text-right py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                                        操作
                                    </th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                                {filteredReports.map((report) => {
                                    const { label } = parseDecision(report.decision)
                                    return (
                                        <tr
                                            key={report.id}
                                            className="hover:bg-slate-50 dark:hover:bg-slate-800/50 transition-colors cursor-pointer"
                                            onClick={() => handleSelectReport(report)}
                                        >
                                            <td className="py-3 px-4">
                                                <div className="flex items-center gap-3">
                                                    <div className="w-8 h-8 rounded-lg bg-blue-100 dark:bg-blue-500/10 flex items-center justify-center">
                                                        <FileText className="w-4 h-4 text-blue-600 dark:text-blue-400" />
                                                    </div>
                                                    <p className="font-medium text-slate-900 dark:text-slate-100">{report.symbol}</p>
                                                </div>
                                            </td>
                                            <td className="py-3 px-4 text-slate-600 dark:text-slate-400">
                                                {report.trade_date}
                                            </td>
                                            <td className="py-3 px-4">
                                                <span className={`font-medium ${getDecisionColor(report.decision)}`}>
                                                    {label}
                                                </span>
                                            </td>
                                            <td className="py-3 px-4">
                                                {report.confidence !== undefined && (
                                                    <div className="flex items-center gap-2">
                                                        <div className="w-16 h-1.5 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden">
                                                            <div
                                                                className="h-full bg-blue-500 rounded-full"
                                                                style={{ width: `${report.confidence}%` }}
                                                            />
                                                        </div>
                                                        <span className="text-sm text-slate-600 dark:text-slate-400">
                                                            {report.confidence}%
                                                        </span>
                                                    </div>
                                                )}
                                            </td>
                                            <td className="py-3 px-4 text-sm text-slate-600 dark:text-slate-400">
                                                {report.target_price ? `¥${report.target_price}` : '-'} / {report.stop_loss_price ? `¥${report.stop_loss_price}` : '-'}
                                            </td>
                                            <td className="py-3 px-4 text-sm text-slate-500 dark:text-slate-400">
                                                {report.created_at ? new Date(report.created_at).toLocaleString('zh-CN') : '-'}
                                            </td>
                                            <td className="py-3 px-4">
                                                <div className="flex items-center justify-end gap-2">
                                                    <button
                                                        className="p-2 text-slate-400 hover:text-blue-600 dark:hover:text-blue-400 transition-colors"
                                                        onClick={(e) => {
                                                            e.stopPropagation()
                                                            handleSelectReport(report)
                                                        }}
                                                    >
                                                        <FileText className="w-4 h-4" />
                                                    </button>
                                                    <button
                                                        className="p-2 text-slate-400 hover:text-blue-600 dark:hover:text-blue-400 transition-colors"
                                                        onClick={(e) => e.stopPropagation()}
                                                    >
                                                        <Download className="w-4 h-4" />
                                                    </button>
                                                    <button
                                                        className="p-2 text-slate-400 hover:text-red-600 dark:hover:text-red-400 transition-colors disabled:opacity-50"
                                                        onClick={(e) => handleDelete(e, report.id)}
                                                        disabled={deleting === report.id}
                                                    >
                                                        {deleting === report.id ? (
                                                            <Loader2 className="w-4 h-4 animate-spin" />
                                                        ) : (
                                                            <Trash2 className="w-4 h-4" />
                                                        )}
                                                    </button>
                                                </div>
                                            </td>
                                        </tr>
                                    )
                                })}
                            </tbody>
                        </table>
                    </div>

                    {filteredReports.length === 0 && (
                        <div className="text-center py-12">
                            <FileText className="w-12 h-12 text-slate-300 dark:text-slate-600 mx-auto mb-4" />
                            <p className="text-slate-500 dark:text-slate-400">暂无报告</p>
                            <p className="text-sm text-slate-400 dark:text-slate-500 mt-1">
                                在分析页面生成新的报告
                            </p>
                        </div>
                    )}
                </div>
            )}
        </div>
    )
}

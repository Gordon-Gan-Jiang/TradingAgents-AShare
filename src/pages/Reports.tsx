import { FileText, Download, Trash2, Search, ChevronLeft, Loader2 } from 'lucide-react'
import { useState, useEffect, useCallback } from 'react'
import { api } from '@/services/api'
import type { Report, ReportDetail } from '@/types'
import DecisionCard from '@/components/DecisionCard'
import ReportViewer from '@/components/ReportViewer'
import KlinePanel from '@/components/KlinePanel'

const parseDecision = (decisionText?: string): { action: 'add' | 'reduce' | 'hold'; label: string } => {
    if (!decisionText) return { action: 'hold', label: '观望' }
    const text = decisionText.toUpperCase()
    if (text.includes('BUY') || text.includes('增持') || text.includes('买入')) return { action: 'add', label: '增持' }
    if (text.includes('SELL') || text.includes('减持') || text.includes('卖出')) return { action: 'reduce', label: '减持' }
    return { action: 'hold', label: '持有' }
}

const getDecisionColor = (decision?: string) => {
    const { action } = parseDecision(decision)
    if (action === 'add') return 'text-green-600 dark:text-green-400'
    if (action === 'reduce') return 'text-red-600 dark:text-red-400'
    return 'text-slate-600 dark:text-slate-400'
}

function exportReport(report: ReportDetail) {
    const sections = [
        { key: 'market_report', title: '市场分析报告' },
        { key: 'sentiment_report', title: '舆情分析报告' },
        { key: 'news_report', title: '新闻分析报告' },
        { key: 'fundamentals_report', title: '基本面分析报告' },
        { key: 'investment_plan', title: '研究团队决策' },
        { key: 'trader_investment_plan', title: '交易团队计划' },
        { key: 'final_trade_decision', title: '最终交易决策' },
    ]
    const text = sections
        .filter(s => report[s.key as keyof ReportDetail])
        .map(s => `## ${s.title}\n\n${report[s.key as keyof ReportDetail]}`)
        .join('\n\n---\n\n')
    const blob = new Blob([text], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `analysis-${report.symbol}-${report.trade_date}.md`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
}

export default function Reports() {
    const [searchQuery, setSearchQuery] = useState('')
    const [reports, setReports] = useState<Report[]>([])
    const [total, setTotal] = useState(0)
    const [selectedReport, setSelectedReport] = useState<ReportDetail | null>(null)
    const [loading, setLoading] = useState(false)
    const [detailLoading, setDetailLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [deleting, setDeleting] = useState<string | null>(null)

    const fetchReports = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const response = await api.getReports(undefined, 0, 100)
            setReports(response.reports)
            setTotal(response.total)
        } catch (err) {
            setError(err instanceof Error ? err.message : '获取报告失败')
        } finally {
            setLoading(false)
        }
    }, [])

    useEffect(() => { fetchReports() }, [fetchReports])

    const handleDelete = async (e: React.MouseEvent, reportId: string) => {
        e.stopPropagation()
        if (!confirm('确定要删除这份报告吗？')) return
        setDeleting(reportId)
        try {
            await api.deleteReport(reportId)
            setReports(prev => prev.filter(r => r.id !== reportId))
            setTotal(prev => prev - 1)
        } catch (err) {
            alert(err instanceof Error ? err.message : '删除失败')
        } finally {
            setDeleting(null)
        }
    }

    const handleSelectReport = async (report: Report) => {
        setDetailLoading(true)
        try {
            const detail = await api.getReport(report.id)
            setSelectedReport(detail)
        } catch (err) {
            alert(err instanceof Error ? err.message : '获取报告详情失败')
        } finally {
            setDetailLoading(false)
        }
    }

    const filteredReports = reports.filter(r =>
        r.symbol.toLowerCase().includes(searchQuery.toLowerCase())
    )

    // ─── 详情视图 ────────────────────────────────────────────────────────────
    if (detailLoading) {
        return (
            <div className="flex items-center justify-center py-24">
                <Loader2 className="w-8 h-8 animate-spin text-blue-500" />
            </div>
        )
    }

    if (selectedReport) {
        const { action } = parseDecision(selectedReport.decision)

        return (
            <div className="space-y-6">
                {/* 返回按钮 + 标题 */}
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
                    <button
                        onClick={() => exportReport(selectedReport)}
                        className="ml-auto flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors"
                    >
                        <Download className="w-4 h-4" />
                        导出 Markdown
                    </button>
                </div>

                {/* 元信息 */}
                <div className="flex items-center gap-4 text-sm text-slate-500">
                    <span>分析日期：{selectedReport.trade_date}</span>
                    <span>生成时间：{selectedReport.created_at ? new Date(selectedReport.created_at).toLocaleString('zh-CN') : '-'}</span>
                </div>

                {/* 主体：左列决策卡 + K线，右列报告全文 */}
                <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 items-start">
                    <div className="space-y-4">
                        <DecisionCard
                            symbol={selectedReport.symbol}
                            decision={action}
                            confidence={selectedReport.confidence ?? undefined}
                            targetPrice={selectedReport.target_price ?? undefined}
                            stopLoss={selectedReport.stop_loss_price ?? undefined}
                            reasoning={selectedReport.final_trade_decision?.slice(0, 300) ?? undefined}
                        />
                        <KlinePanel symbol={selectedReport.symbol} />
                    </div>

                    <div className="lg:col-span-2">
                        <div className="card">
                            <ReportViewer reportData={selectedReport} />
                        </div>
                    </div>
                </div>
            </div>
        )
    }

    // ─── 列表视图 ────────────────────────────────────────────────────────────
    return (
        <div className="space-y-6">
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">历史报告</h1>
                    <p className="text-slate-500 dark:text-slate-400 mt-1">
                        共 {total} 份分析报告
                    </p>
                </div>
            </div>

            {/* 搜索 */}
            <div className="card">
                <div className="relative max-w-md">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                    <input
                        type="text"
                        value={searchQuery}
                        onChange={e => setSearchQuery(e.target.value)}
                        placeholder="搜索股票代码..."
                        className="input w-full pl-10"
                    />
                </div>
            </div>

            {/* 加载中 */}
            {loading && (
                <div className="card py-12">
                    <div className="flex flex-col items-center gap-4">
                        <Loader2 className="w-8 h-8 text-blue-500 animate-spin" />
                        <p className="text-slate-500">加载报告中...</p>
                    </div>
                </div>
            )}

            {/* 错误 */}
            {error && !loading && (
                <div className="card py-12 text-center">
                    <p className="text-red-500 mb-4">{error}</p>
                    <button
                        onClick={fetchReports}
                        className="px-4 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600 transition-colors"
                    >
                        重试
                    </button>
                </div>
            )}

            {/* 报告表格 */}
            {!loading && !error && (
                <div className="card overflow-hidden">
                    <div className="overflow-x-auto">
                        <table className="w-full">
                            <thead>
                                <tr className="border-b border-slate-200 dark:border-slate-700">
                                    {['股票代码', '分析日期', '决策建议', '置信度', '目标价/止损价', '生成时间', '操作'].map(h => (
                                        <th key={h} className={`py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400 ${h === '操作' ? 'text-right' : 'text-left'}`}>
                                            {h}
                                        </th>
                                    ))}
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
                                            <td className="py-3 px-4 text-slate-600 dark:text-slate-400">{report.trade_date}</td>
                                            <td className="py-3 px-4">
                                                <span className={`font-medium ${getDecisionColor(report.decision)}`}>
                                                    {label}
                                                </span>
                                            </td>
                                            <td className="py-3 px-4">
                                                {report.confidence != null ? (
                                                    <div className="flex items-center gap-2">
                                                        <div className="w-16 h-1.5 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden">
                                                            <div
                                                                className="h-full bg-blue-500 rounded-full"
                                                                style={{ width: `${report.confidence}%` }}
                                                            />
                                                        </div>
                                                        <span className="text-sm text-slate-600 dark:text-slate-400">{report.confidence}%</span>
                                                    </div>
                                                ) : (
                                                    <span className="text-slate-400">-</span>
                                                )}
                                            </td>
                                            <td className="py-3 px-4 text-sm text-slate-600 dark:text-slate-400">
                                                {report.target_price != null ? `¥${report.target_price}` : '-'} / {report.stop_loss_price != null ? `¥${report.stop_loss_price}` : '-'}
                                            </td>
                                            <td className="py-3 px-4 text-sm text-slate-500 dark:text-slate-400">
                                                {report.created_at ? new Date(report.created_at).toLocaleString('zh-CN') : '-'}
                                            </td>
                                            <td className="py-3 px-4">
                                                <div className="flex items-center justify-end gap-2">
                                                    <button
                                                        className="p-2 text-slate-400 hover:text-blue-600 dark:hover:text-blue-400 transition-colors"
                                                        onClick={e => { e.stopPropagation(); handleSelectReport(report) }}
                                                        title="查看详情"
                                                    >
                                                        <FileText className="w-4 h-4" />
                                                    </button>
                                                    <button
                                                        className="p-2 text-slate-400 hover:text-red-600 dark:hover:text-red-400 transition-colors disabled:opacity-50"
                                                        onClick={e => handleDelete(e, report.id)}
                                                        disabled={deleting === report.id}
                                                        title="删除"
                                                    >
                                                        {deleting === report.id
                                                            ? <Loader2 className="w-4 h-4 animate-spin" />
                                                            : <Trash2 className="w-4 h-4" />
                                                        }
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
                            <p className="text-slate-500 dark:text-slate-400">
                                {searchQuery ? '没有匹配的报告' : '暂无报告'}
                            </p>
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

import { FileText, Download, Trash2, Search } from 'lucide-react'
import { useState } from 'react'

// Mock data for demonstration
const MOCK_REPORTS = [
    {
        id: '1',
        symbol: '600519.SH',
        name: '贵州茅台',
        date: '2024-01-15',
        decision: '增持',
        confidence: 85,
        createdAt: '2024-01-15T14:30:00Z',
    },
    {
        id: '2',
        symbol: 'AAPL',
        name: 'Apple Inc.',
        date: '2024-01-14',
        decision: '持有',
        confidence: 72,
        createdAt: '2024-01-14T10:15:00Z',
    },
    {
        id: '3',
        symbol: '000858.SZ',
        name: '五粮液',
        date: '2024-01-13',
        decision: '减持',
        confidence: 65,
        createdAt: '2024-01-13T16:45:00Z',
    },
]

export default function Reports() {
    const [searchQuery, setSearchQuery] = useState('')
    const [reports] = useState(MOCK_REPORTS)

    const filteredReports = reports.filter(report =>
        report.symbol.toLowerCase().includes(searchQuery.toLowerCase()) ||
        report.name.toLowerCase().includes(searchQuery.toLowerCase())
    )

    const getDecisionColor = (decision: string) => {
        switch (decision) {
            case '增持':
            case '买入':
                return 'text-green-600 dark:text-green-400'
            case '减持':
            case '卖出':
                return 'text-red-600 dark:text-red-400'
            default:
                return 'text-slate-600 dark:text-slate-400'
        }
    }

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
                        placeholder="搜索股票代码或名称..."
                        className="input w-full pl-10"
                    />
                </div>
            </div>

            {/* Reports Table */}
            <div className="card overflow-hidden">
                <div className="overflow-x-auto">
                    <table className="w-full">
                        <thead>
                            <tr className="border-b border-slate-200 dark:border-slate-700">
                                <th className="text-left py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                                    股票
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
                                    生成时间
                                </th>
                                <th className="text-right py-3 px-4 text-sm font-medium text-slate-500 dark:text-slate-400">
                                    操作
                                </th>
                            </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-200 dark:divide-slate-700">
                            {filteredReports.map((report) => (
                                <tr key={report.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/50 transition-colors">
                                    <td className="py-3 px-4">
                                        <div className="flex items-center gap-3">
                                            <div className="w-8 h-8 rounded-lg bg-blue-100 dark:bg-blue-500/10 flex items-center justify-center">
                                                <FileText className="w-4 h-4 text-blue-600 dark:text-blue-400" />
                                            </div>
                                            <div>
                                                <p className="font-medium text-slate-900 dark:text-slate-100">{report.symbol}</p>
                                                <p className="text-sm text-slate-500 dark:text-slate-400">{report.name}</p>
                                            </div>
                                        </div>
                                    </td>
                                    <td className="py-3 px-4 text-slate-600 dark:text-slate-400">
                                        {report.date}
                                    </td>
                                    <td className="py-3 px-4">
                                        <span className={`font-medium ${getDecisionColor(report.decision)}`}>
                                            {report.decision}
                                        </span>
                                    </td>
                                    <td className="py-3 px-4">
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
                                    </td>
                                    <td className="py-3 px-4 text-sm text-slate-500 dark:text-slate-400">
                                        {new Date(report.createdAt).toLocaleString('zh-CN')}
                                    </td>
                                    <td className="py-3 px-4">
                                        <div className="flex items-center justify-end gap-2">
                                            <button className="p-2 text-slate-400 hover:text-blue-600 dark:hover:text-blue-400 transition-colors">
                                                <Download className="w-4 h-4" />
                                            </button>
                                            <button className="p-2 text-slate-400 hover:text-red-600 dark:hover:text-red-400 transition-colors">
                                                <Trash2 className="w-4 h-4" />
                                            </button>
                                        </div>
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>

                {filteredReports.length === 0 && (
                    <div className="text-center py-12">
                        <FileText className="w-12 h-12 text-slate-300 dark:text-slate-600 mx-auto mb-4" />
                        <p className="text-slate-500 dark:text-slate-400">未找到报告</p>
                        <p className="text-sm text-slate-400 dark:text-slate-500 mt-1">
                            尝试调整搜索条件
                        </p>
                    </div>
                )}
            </div>
        </div>
    )
}

import { useState } from 'react'
import { BarChart3, TrendingUp, Users, Zap, Download, Plus, Layers } from 'lucide-react'
import KlinePanel from './KlinePanel'

interface DataLaboratoryProps {
    symbol: string
}

type TabType = 'technical' | 'fundamental' | 'sentiment' | 'fund-flow'

const tabs = [
    { id: 'technical' as TabType, label: '技术分析', icon: BarChart3 },
    { id: 'fundamental' as TabType, label: '基本面', icon: TrendingUp },
    { id: 'sentiment' as TabType, label: '舆情', icon: Users },
    { id: 'fund-flow' as TabType, label: '资金面', icon: Zap },
]

export default function DataLaboratory({ symbol }: DataLaboratoryProps) {
    const [activeTab, setActiveTab] = useState<TabType>('technical')
    const [selectedIndicators, setSelectedIndicators] = useState<string[]>(['volume', 'macd'])

    const toggleIndicator = (indicator: string) => {
        setSelectedIndicators((prev) =>
            prev.includes(indicator) ? prev.filter((i) => i !== indicator) : [...prev, indicator]
        )
    }

    return (
        <div className="card">
            {/* 头部 */}
            <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2">
                    <Layers className="w-5 h-5 text-cyan-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">数据实验室</h2>
                </div>
                <div className="flex items-center gap-2">
                    <button className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors">
                        <Plus className="w-3.5 h-3.5" />
                        添加对比
                    </button>
                    <button className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-lg bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors">
                        <Download className="w-3.5 h-3.5" />
                        导出
                    </button>
                </div>
            </div>

            {/* 标签切换 */}
            <div className="flex gap-1 border-b border-slate-200 dark:border-slate-700 mb-4">
                {tabs.map((tab) => (
                    <button
                        key={tab.id}
                        onClick={() => setActiveTab(tab.id)}
                        className={`flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${activeTab === tab.id
                                ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                                : 'border-transparent text-slate-500 hover:text-slate-700 dark:hover:text-slate-300'
                            }`}
                    >
                        <tab.icon className="w-4 h-4" />
                        {tab.label}
                    </button>
                ))}
            </div>

            {/* 内容区域 */}
            <div className="min-h-[300px]">
                {activeTab === 'technical' && (
                    <TechnicalPanel
                        symbol={symbol}
                        selectedIndicators={selectedIndicators}
                        onToggleIndicator={toggleIndicator}
                    />
                )}
                {activeTab === 'fundamental' && <FundamentalPanel />}
                {activeTab === 'sentiment' && <SentimentPanel />}
                {activeTab === 'fund-flow' && <FundFlowPanel />}
            </div>
        </div>
    )
}

// 技术分析面板
function TechnicalPanel({
    symbol,
    selectedIndicators,
    onToggleIndicator,
}: {
    symbol: string
    selectedIndicators: string[]
    onToggleIndicator: (indicator: string) => void
}) {
    const indicators = [
        { id: 'volume', label: '成交量', color: 'bg-purple-100 dark:bg-purple-500/20 text-purple-600 dark:text-purple-400' },
        { id: 'macd', label: 'MACD', color: 'bg-blue-100 dark:bg-blue-500/20 text-blue-600 dark:text-blue-400' },
        { id: 'rsi', label: 'RSI', color: 'bg-green-100 dark:bg-green-500/20 text-green-600 dark:text-green-400' },
        { id: 'kdj', label: 'KDJ', color: 'bg-orange-100 dark:bg-orange-500/20 text-orange-600 dark:text-orange-400' },
        { id: 'boll', label: '布林带', color: 'bg-cyan-100 dark:bg-cyan-500/20 text-cyan-600 dark:text-cyan-400' },
    ]

    return (
        <div className="space-y-4">
            {/* 指标选择器 */}
            <div className="flex flex-wrap gap-2">
                {indicators.map((indicator) => (
                    <button
                        key={indicator.id}
                        onClick={() => onToggleIndicator(indicator.id)}
                        className={`px-3 py-1.5 text-sm rounded-full border transition-all ${selectedIndicators.includes(indicator.id)
                                ? `${indicator.color} border-current`
                                : 'bg-slate-100 dark:bg-slate-800 border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-400'
                            }`}
                    >
                        {selectedIndicators.includes(indicator.id) && '✓ '}
                        {indicator.label}
                    </button>
                ))}
            </div>

            {/* K线图区域 */}
            <div className="h-[280px] rounded-xl border border-slate-200 dark:border-slate-700 overflow-hidden">
                <KlinePanel symbol={symbol} />
            </div>

            {/* 指标说明 */}
            <div className="p-3 rounded-lg bg-slate-50 dark:bg-slate-800/50 text-sm">
                <div className="flex items-start gap-2">
                    <span className="text-blue-500">💡</span>
                    <div className="text-slate-600 dark:text-slate-400">
                        <span className="font-medium">这意味着什么？</span>
                        <p className="mt-1">
                            MACD金叉表示短期趋势转强，历史上该信号在此股票上成功率约65%，但需要结合成交量确认。
                            当前成交量较5日均量放大23%，信号可靠性较高。
                        </p>
                    </div>
                </div>
            </div>
        </div>
    )
}

// 基本面面板
function FundamentalPanel() {
    const metrics = [
        { label: '市盈率(PE)', value: '28.5x', desc: '历史30%分位', status: 'good' },
        { label: '市净率(PB)', value: '8.2x', desc: '历史45%分位', status: 'normal' },
        { label: 'ROE', value: '25.3%', desc: '行业领先', status: 'good' },
        { label: '营收增速', value: '15.8%', desc: 'YoY', status: 'good' },
        { label: '净利润增速', value: '18.2%', desc: 'YoY', status: 'good' },
        { label: '负债率', value: '32.5%', desc: '健康水平', status: 'good' },
    ]

    return (
        <div className="grid grid-cols-2 gap-3">
            {metrics.map((metric) => (
                <div
                    key={metric.label}
                    className="p-4 rounded-xl bg-slate-50 dark:bg-slate-800/50 border border-slate-100 dark:border-slate-700"
                >
                    <p className="text-sm text-slate-500">{metric.label}</p>
                    <div className="flex items-end justify-between mt-1">
                        <p
                            className={`text-xl font-bold ${metric.status === 'good'
                                    ? 'text-green-600 dark:text-green-400'
                                    : metric.status === 'bad'
                                        ? 'text-red-600 dark:text-red-400'
                                        : 'text-slate-700 dark:text-slate-300'
                                }`}
                        >
                            {metric.value}
                        </p>
                        <p className="text-xs text-slate-400">{metric.desc}</p>
                    </div>
                </div>
            ))}
        </div>
    )
}

// 舆情面板
function SentimentPanel() {
    const keywords = [
        { word: '业绩超预期', sentiment: 'positive' },
        { word: '机构增持', sentiment: 'positive' },
        { word: '产品提价', sentiment: 'positive' },
        { word: '渠道扩张', sentiment: 'positive' },
        { word: '竞争加剧', sentiment: 'negative' },
        { word: '成本上升', sentiment: 'negative' },
    ]

    return (
        <div className="space-y-4">
            <div className="flex items-center justify-between p-4 rounded-xl bg-green-50 dark:bg-green-500/10 border border-green-100 dark:border-green-500/20">
                <div>
                    <p className="text-sm text-slate-600 dark:text-slate-400">市场情绪指数</p>
                    <p className="text-2xl font-bold text-green-600 dark:text-green-400">72/100</p>
                </div>
                <div className="text-right">
                    <p className="text-sm text-slate-500">较昨日</p>
                    <p className="text-sm font-medium text-green-600 dark:text-green-400">+5.2%</p>
                </div>
            </div>

            <div>
                <p className="text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">舆情关键词</p>
                <div className="flex flex-wrap gap-2">
                    {keywords.map((item) => (
                        <span
                            key={item.word}
                            className={`px-3 py-1.5 text-sm rounded-full ${item.sentiment === 'positive'
                                    ? 'bg-green-50 dark:bg-green-500/10 text-green-600 dark:text-green-400 border border-green-100 dark:border-green-500/20'
                                    : 'bg-red-50 dark:bg-red-500/10 text-red-600 dark:text-red-400 border border-red-100 dark:border-red-500/20'
                                }`}
                        >
                            {item.sentiment === 'positive' ? '+' : '-'} {item.word}
                        </span>
                    ))}
                </div>
            </div>

            <div className="space-y-2">
                <p className="text-sm font-medium text-slate-700 dark:text-slate-300">最新舆情</p>
                {[
                    { title: '多家机构上调目标价至¥185', source: '财经网', time: '2小时前' },
                    { title: 'Q4业绩有望超预期', source: '证券时报', time: '4小时前' },
                ].map((news, i) => (
                    <div
                        key={i}
                        className="p-3 rounded-lg bg-slate-50 dark:bg-slate-800/50 flex items-center justify-between"
                    >
                        <span className="text-sm text-slate-700 dark:text-slate-300">{news.title}</span>
                        <div className="text-xs text-slate-400">
                            {news.source} · {news.time}
                        </div>
                    </div>
                ))}
            </div>
        </div>
    )
}

// 资金面板
function FundFlowPanel() {
    return (
        <div className="space-y-4">
            <div className="grid grid-cols-3 gap-3">
                <div className="p-4 rounded-xl bg-green-50 dark:bg-green-500/10 border border-green-100 dark:border-green-500/20">
                    <p className="text-xs text-slate-500">主力净流入</p>
                    <p className="text-lg font-bold text-green-600 dark:text-green-400">+2.35亿</p>
                </div>
                <div className="p-4 rounded-xl bg-red-50 dark:bg-red-500/10 border border-red-100 dark:border-red-500/20">
                    <p className="text-xs text-slate-500">散户流出</p>
                    <p className="text-lg font-bold text-red-600 dark:text-red-400">-1.12亿</p>
                </div>
                <div className="p-4 rounded-xl bg-blue-50 dark:bg-blue-500/10 border border-blue-100 dark:border-blue-500/20">
                    <p className="text-xs text-slate-500">北向资金</p>
                    <p className="text-lg font-bold text-blue-600 dark:text-blue-400">+5.8亿</p>
                </div>
            </div>

            <div className="p-4 rounded-xl bg-slate-50 dark:bg-slate-800/50">
                <p className="text-sm font-medium text-slate-700 dark:text-slate-300 mb-3">资金流向趋势（5日）</p>
                <div className="space-y-2">
                    {[
                        { day: '今日', value: 45, type: 'in' },
                        { day: '1日', value: 62, type: 'in' },
                        { day: '2日', value: 28, type: 'in' },
                        { day: '3日', value: -15, type: 'out' },
                        { day: '4日', value: 38, type: 'in' },
                    ].map((item) => (
                        <div key={item.day} className="flex items-center gap-3">
                            <span className="text-sm text-slate-500 w-10">{item.day}</span>
                            <div className="flex-1 h-6 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden relative">
                                <div
                                    className={`absolute top-0 h-full rounded-full ${item.type === 'in' ? 'bg-green-500 left-1/2' : 'bg-red-500 right-1/2'
                                        }`}
                                    style={{ width: `${Math.abs(item.value)}%` }}
                                />
                            </div>
                            <span
                                className={`text-sm font-medium w-16 text-right ${item.type === 'in' ? 'text-green-600 dark:text-green-400' : 'text-red-600 dark:text-red-400'
                                    }`}
                            >
                                {item.type === 'in' ? '+' : '-'}
                                {item.value}%
                            </span>
                        </div>
                    ))}
                </div>
            </div>
        </div>
    )
}

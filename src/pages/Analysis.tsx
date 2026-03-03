import { useEffect, useState } from 'react'
import { Play, Pause, RotateCcw, Settings2 } from 'lucide-react'
import { useAnalysisStore } from '@/stores/analysisStore'
import { api } from '@/services/api'
import { useSSE } from '@/hooks/useSSE'
import AgentPipeline from '@/components/AgentPipeline'
import LogStream from '@/components/LogStream'
import ReportViewer from '@/components/ReportViewer'
import { useSearchParams } from 'react-router-dom'

const ANALYST_OPTIONS = [
    { id: 'market', label: '市场分析', description: '技术面和市场趋势分析' },
    { id: 'social', label: '舆情分析', description: '社交媒体情绪监测' },
    { id: 'news', label: '新闻分析', description: '财经新闻事件分析' },
    { id: 'fundamentals', label: '基本面', description: '财务报表和估值分析' },
]

export default function Analysis() {
    const [searchParams] = useSearchParams()
    const [symbol, setSymbol] = useState('')
    const [tradeDate, setTradeDate] = useState(new Date().toISOString().split('T')[0])
    const [selectedAnalysts, setSelectedAnalysts] = useState<string[]>(['market', 'social', 'news', 'fundamentals'])

    const {
        currentJobId,
        isAnalyzing,
        isConnected,
        setCurrentJobId,
        setIsAnalyzing,
        reset
    } = useAnalysisStore()

    // Connect to SSE when job is created
    useSSE(currentJobId)

    // Prefill from global header search
    useEffect(() => {
        const querySymbol = (searchParams.get('symbol') || '').trim()
        if (!querySymbol) return
        setSymbol((prev) => prev || querySymbol.toUpperCase())
    }, [searchParams])

    const handleStartAnalysis = async () => {
        if (!symbol) return

        reset()
        setIsAnalyzing(true)

        try {
            const response = await api.startAnalysis({
                symbol: symbol.toUpperCase(),
                trade_date: tradeDate,
                selected_analysts: selectedAnalysts,
            })

            setCurrentJobId(response.job_id)
        } catch (error) {
            console.error('Failed to start analysis:', error)
            setIsAnalyzing(false)
        }
    }

    const handleStopAnalysis = () => {
        setIsAnalyzing(false)
    }

    const handleReset = () => {
        reset()
        setSymbol('')
    }

    const toggleAnalyst = (id: string) => {
        setSelectedAnalysts(prev =>
            prev.includes(id)
                ? prev.filter(a => a !== id)
                : [...prev, id]
        )
    }

    return (
        <div className="h-full min-h-0 flex flex-col gap-6">
            {/* Header */}
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-trading-text-primary">智能分析</h1>
                    <p className="text-trading-text-secondary mt-1">
                        配置分析参数并启动多 Agent 协作分析
                    </p>
                </div>
                <div className="flex items-center gap-2">
                    {isAnalyzing ? (
                        <button
                            onClick={handleStopAnalysis}
                            className="btn-secondary flex items-center gap-2"
                        >
                            <Pause className="w-4 h-4" />
                            暂停
                        </button>
                    ) : (
                        <button
                            onClick={handleStartAnalysis}
                            disabled={!symbol || selectedAnalysts.length === 0}
                            className="btn-primary flex items-center gap-2"
                        >
                            <Play className="w-4 h-4" />
                            开始分析
                        </button>
                    )}
                    <button
                        onClick={handleReset}
                        className="btn-secondary flex items-center gap-2"
                    >
                        <RotateCcw className="w-4 h-4" />
                        重置
                    </button>
                </div>
            </div>

            {/* Configuration Panel */}
            <div className="card shrink-0">
                <div className="flex items-center gap-2 mb-4">
                    <Settings2 className="w-5 h-5 text-trading-accent-blue" />
                    <h2 className="text-lg font-semibold text-trading-text-primary">分析配置</h2>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                    {/* Symbol Input */}
                    <div>
                        <label className="block text-sm font-medium text-trading-text-secondary mb-2">
                            股票代码
                        </label>
                        <input
                            type="text"
                            value={symbol}
                            onChange={(e) => setSymbol(e.target.value)}
                            placeholder="如: 600519.SH, AAPL"
                            className="input w-full"
                            disabled={isAnalyzing}
                        />
                    </div>

                    {/* Date Input */}
                    <div>
                        <label className="block text-sm font-medium text-trading-text-secondary mb-2">
                            分析日期
                        </label>
                        <input
                            type="date"
                            value={tradeDate}
                            onChange={(e) => setTradeDate(e.target.value)}
                            className="input w-full"
                            disabled={isAnalyzing}
                        />
                    </div>
                </div>

                {/* Analyst Selection */}
                <div className="mt-4">
                    <label className="block text-sm font-medium text-trading-text-secondary mb-2">
                        选择分析师
                    </label>
                    <div className="flex flex-wrap gap-2">
                        {ANALYST_OPTIONS.map((option) => (
                            <button
                                key={option.id}
                                onClick={() => toggleAnalyst(option.id)}
                                disabled={isAnalyzing}
                                className={`px-4 py-2 rounded-lg border transition-all duration-200 ${selectedAnalysts.includes(option.id)
                                        ? 'bg-trading-accent-blue/10 border-trading-accent-blue text-trading-accent-blue'
                                        : 'bg-trading-bg-tertiary border-trading-border text-trading-text-secondary hover:border-trading-text-muted'
                                    }`}
                            >
                                <span className="font-medium">{option.label}</span>
                                <span className="block text-xs opacity-70">{option.description}</span>
                            </button>
                        ))}
                    </div>
                </div>
            </div>

            {/* Main Content Area */}
            <div className="flex-1 grid grid-cols-1 lg:grid-cols-3 gap-6 min-h-0 overflow-hidden">
                {/* Agent Pipeline */}
                <div className="lg:col-span-2 grid grid-rows-[minmax(0,1fr)_minmax(0,1fr)] gap-4 min-h-0 overflow-hidden">
                    <div className="min-h-0">
                        <AgentPipeline />
                    </div>
                    <div className="min-h-0">
                        <ReportViewer />
                    </div>
                </div>

                {/* Log Stream */}
                <div className="min-h-0">
                    <LogStream />
                </div>
            </div>
        </div>
    )
}

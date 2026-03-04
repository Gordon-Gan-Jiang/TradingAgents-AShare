
import { useEffect, useState } from 'react'
import { Settings2 } from 'lucide-react'
import { useSearchParams } from 'react-router-dom'
import AgentPipeline from '@/components/AgentPipeline'
import ReportViewer from '@/components/ReportViewer'
import ChatCopilotPanel from '@/components/ChatCopilotPanel'
import KlinePanel from '@/components/KlinePanel'

const ANALYST_OPTIONS = [
    { id: 'market', label: '市场分析', description: '技术面和市场趋势分析' },
    { id: 'social', label: '舆情分析', description: '社交媒体情绪监测' },
    { id: 'news', label: '新闻分析', description: '财经新闻事件分析' },
    { id: 'fundamentals', label: '基本面', description: '财务报表和估值分析' },
]

// 使用字符串字面量，避免被windicss扫描
const CTX_SECTION = "card"

export default function Analysis() {
    const [searchParams] = useSearchParams()
    const [selectedAnalysts, setSelectedAnalysts] = useState<string[]>(['market', 'social', 'news', 'fundamentals'])
    const [activeSymbol, setActiveSymbol] = useState('000001.SH')

    useEffect(() => {
        const querySymbol = (searchParams.get('symbol') || '').trim()
        if (!querySymbol) return
        setActiveSymbol(querySymbol.toUpperCase())
    }, [searchParams])

    const toggleAnalyst = (id: string) => {
        setSelectedAnalysts((prev) => (
            prev.includes(id)
                ? prev.filter((a) => a !== id)
                : [...prev, id]
        ))
    }

    return (
        <div className="flex flex-col gap-4">
            <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_430px] gap-4 items-start">
                <div className="space-y-4">
                    <KlinePanel
                        symbol={activeSymbol}
                        onSymbolChange={setActiveSymbol}
                    />

                    <section className="card">
                        <div className="flex items-center gap-2 mb-3">
                            <Settings2 className="w-5 h-5 text-blue-500" />
                            <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">分析师配置</h2>
                        </div>
                        <p className="text-sm text-slate-600 dark:text-slate-400 mb-3">
                            通过右侧对话启动分析，可在此选择启用的分析师。
                        </p>
                        <div className="flex flex-wrap gap-2">
                            {ANALYST_OPTIONS.map((option) => (
                                <button
                                    key={option.id}
                                    onClick={() => toggleAnalyst(option.id)}
                                    className={`px-4 py-2 rounded-lg border transition-all duration-200 ${selectedAnalysts.includes(option.id)
                                        ? 'bg-blue-50 dark:bg-blue-500/10 border-blue-500 text-blue-600 dark:text-blue-400'
                                        : 'bg-slate-100 dark:bg-slate-800 border-slate-200 dark:border-slate-700 text-slate-600 dark:text-slate-400 hover:border-slate-400 dark:hover:border-slate-500'
                                        }`}
                                >
                                    <span className="font-medium">{option.label}</span>
                                    <span className="block text-xs opacity-70">{option.description}</span>
                                </button>
                            ))}
                        </div>
                    </section>

                    <AgentPipeline />
                    <ReportViewer />
                </div>

                <div className="xl:sticky xl:top-0 self-start h-[calc(100vh-7rem)] min-h-[620px]">
                    <ChatCopilotPanel
                        selectedAnalysts={selectedAnalysts}
                        onSymbolDetected={setActiveSymbol}
                    />
                </div>
            </div>
        </div>
    )
}

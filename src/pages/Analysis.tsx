import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import AgentCollaboration from '@/components/AgentCollaboration'
import ReportViewer from '@/components/ReportViewer'
import ChatCopilotPanel from '@/components/ChatCopilotPanel'
import KlinePanel from '@/components/KlinePanel'
import DecisionCard from '@/components/DecisionCard'
import RiskRadar from '@/components/RiskRadar'
import KeyMetrics from '@/components/KeyMetrics'
import { useAnalysisStore } from '@/stores/analysisStore'

function extractConfidence(text?: string): number | undefined {
    if (!text) return undefined
    const m = text.match(/置信度[:：]\s*(\d+)%/i) ?? text.match(/confidence[:：]\s*(\d+)%/i)
    if (m) {
        const v = parseInt(m[1])
        return v >= 0 && v <= 100 ? v : undefined
    }
    return undefined
}

function extractPrice(text: string | undefined, type: 'target' | 'stop'): number | undefined {
    if (!text) return undefined
    const patterns = type === 'target'
        ? [/目标价[:：]\s*[¥$]?\s*([\d.]+)/, /目标价格[:：]\s*[¥$]?\s*([\d.]+)/, /target[:：]\s*[¥$]?\s*([\d.]+)/i]
        : [/止损价[:：]\s*[¥$]?\s*([\d.]+)/, /止损价格[:：]\s*[¥$]?\s*([\d.]+)/, /stop[-\s_]?loss[:：]\s*[¥$]?\s*([\d.]+)/i]
    for (const p of patterns) {
        const m = text.match(p)
        if (m) return parseFloat(m[1])
    }
    return undefined
}

export default function Analysis() {
    const [searchParams] = useSearchParams()
    const [activeSymbol, setActiveSymbol] = useState('000001.SH')
    const [showReport, setShowReport] = useState(false)
    const [activeSection, setActiveSection] = useState<string | undefined>()
    const { report, jobConfidence, jobTargetPrice, jobStopLoss } = useAnalysisStore()

    const handleShowReport = (section?: string) => {
        setShowReport(true)
        setActiveSection(section)
    }

    const querySymbol = (searchParams.get('symbol') || '').trim().toUpperCase()
    const initialChatInput = querySymbol ? `分析 ${querySymbol} 今日走势` : undefined

    useEffect(() => {
        if (querySymbol) setActiveSymbol(querySymbol)
    }, [querySymbol])

    // 分析完成后自动展开报告面板
    useEffect(() => {
        if (report) setShowReport(true)
    }, [report])

    // Esc 关闭报告面板
    useEffect(() => {
        const onKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'Escape') setShowReport(false)
        }
        document.addEventListener('keydown', onKeyDown)
        return () => document.removeEventListener('keydown', onKeyDown)
    }, [])

    const finalDecision = report?.final_trade_decision
    // Prefer LLM-extracted structured values, fall back to regex parsing
    const confidence = jobConfidence ?? extractConfidence(finalDecision)
    const targetPrice = jobTargetPrice ?? extractPrice(finalDecision, 'target')
    const stopLoss = jobStopLoss ?? extractPrice(finalDecision, 'stop')

    return (
        <div className="flex gap-4 h-[calc(100vh-5rem)]">
            {/* 左侧：智能分析对话 + 决策卡 */}
            <div className="w-[400px] shrink-0 h-full flex flex-col gap-4">
                {report && (
                    <div className="shrink-0">
                        <DecisionCard
                            symbol={activeSymbol}
                            report={report}
                            confidence={confidence}
                            targetPrice={targetPrice}
                            stopLoss={stopLoss}
                            reasoning={finalDecision?.slice(0, 300)}
                        />
                    </div>
                )}
                <div className="flex-1 min-h-0">
                    <ChatCopilotPanel
                        onSymbolDetected={setActiveSymbol}
                        onShowReport={handleShowReport}
                        initialInput={initialChatInput}
                    />
                </div>
            </div>

            {/* 中间：Agent 协作讨论 */}
            <div className="w-[480px] shrink-0 h-full">
                <AgentCollaboration />
            </div>

            {/* 右侧：K线 + 风险雷达 + 指标 + 报告 */}
            <div className="flex-1 min-w-0 h-full flex flex-col gap-4">
                <div className="h-[340px] shrink-0">
                    <KlinePanel
                        symbol={activeSymbol}
                        onSymbolChange={setActiveSymbol}
                    />
                </div>

                <div className="grid grid-cols-2 gap-4 shrink-0">
                    <RiskRadar />
                    <KeyMetrics />
                </div>

                {showReport ? (
                    <div className="flex-1 min-h-0 relative card overflow-y-auto">
                        <button
                            onClick={() => setShowReport(false)}
                            className="absolute top-2 right-2 z-10 p-1.5 rounded-lg bg-slate-100 hover:bg-slate-200 dark:bg-slate-700 dark:hover:bg-slate-600 text-slate-500 dark:text-slate-400 transition-colors"
                        >
                            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                            </svg>
                        </button>
                        <ReportViewer activeSection={activeSection} />
                    </div>
                ) : (
                    <div className="flex-1 min-h-0 flex items-center justify-center text-slate-400 text-sm card">
                        <span>点击左侧"查看报告"按钮查看完整分析</span>
                    </div>
                )}
            </div>
        </div>
    )
}

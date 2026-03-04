import { FormEvent, useMemo, useState } from 'react'
import { Bot, Loader2, Send, Sparkles } from 'lucide-react'
import { api } from '@/services/api'
import { useAnalysisStore } from '@/stores/analysisStore'
import type {
    AgentMessageEvent,
    AgentReportEvent,
    AgentSnapshotEvent,
    AgentStatusEvent,
    AgentToolCallEvent,
    AnalysisReport,
    LogEntry,
} from '@/types'

interface ChatCopilotPanelProps {
    selectedAnalysts: string[]
    onSymbolDetected: (symbol: string) => void
}

interface ChatBubble {
    id: string
    role: 'user' | 'assistant'
    content: string
    timestamp: string
}

interface StreamEvent {
    event: string
    data: Record<string, unknown>
}

const PRESET_PROMPTS = [
    '分析一下贵州茅台(600519.SH)今天走势',
    '请分析稀土ETF嘉实(516150)在2026-03-03的情况',
    '分析宁德时代300750.SZ，给出交易建议',
]

export default function ChatCopilotPanel({ selectedAnalysts, onSymbolDetected }: ChatCopilotPanelProps) {
    const [input, setInput] = useState('')
    const [streaming, setStreaming] = useState(false)
    const [chat, setChat] = useState<ChatBubble[]>([
        {
            id: 'init',
            role: 'assistant',
            content: '我是你的 A 股多智能体投研助手。直接告诉我你想分析的标的和日期。',
            timestamp: new Date().toISOString(),
        },
    ])

    const {
        logs,
        setCurrentJobId,
        setIsAnalyzing,
        setIsConnected,
        updateAgentStatus,
        updateAgentSnapshot,
        addAgentMessage,
        addAgentToolCall,
        addAgentReport,
        addLog,
        setReport,
        reset,
    } = useAnalysisStore()

    const timelineLogs = useMemo(() => [...logs].reverse(), [logs])

    const pushAssistant = (content: string) => {
        setChat((prev) => [
            ...prev,
            {
                id: `${Date.now()}-${Math.random()}`,
                role: 'assistant',
                content,
                timestamp: new Date().toISOString(),
            },
        ])
    }

    const parseAndDispatch = (event: StreamEvent) => {
        const { event: eventName, data } = event
        switch (eventName) {
            case 'job.ready':
                setIsConnected(true)
                break
            case 'job.created': {
                const jobId = String(data.job_id || '')
                const symbol = String(data.symbol || '')
                const tradeDate = String(data.trade_date || '')
                if (jobId) setCurrentJobId(jobId)
                if (symbol) onSymbolDetected(symbol)
                pushAssistant(`已启动任务：${symbol} @ ${tradeDate}`)
                break
            }
            case 'job.running':
                setIsAnalyzing(true)
                break
            case 'job.completed':
                setIsAnalyzing(false)
                setReport((data.result || null) as AnalysisReport | null)
                pushAssistant(`分析完成，最终建议：${String(data.decision || 'HOLD')}`)
                break
            case 'job.failed':
                setIsAnalyzing(false)
                pushAssistant(`任务失败：${String(data.error || 'unknown error')}`)
                break
            case 'agent.status':
                updateAgentStatus(data as unknown as AgentStatusEvent)
                break
            case 'agent.snapshot':
                updateAgentSnapshot(data as unknown as AgentSnapshotEvent)
                break
            case 'agent.message':
                addAgentMessage(data as unknown as AgentMessageEvent)
                break
            case 'agent.tool_call':
                addAgentToolCall(data as unknown as AgentToolCallEvent)
                break
            case 'agent.report':
                addAgentReport(data as unknown as AgentReportEvent)
                break
            default:
                break
        }
    }

    const streamChat = async (prompt: string) => {
        const response = await api.chatCompletion(
            [{ role: 'user', content: prompt }],
            true,
            selectedAnalysts,
        )

        if (!response.body) {
            throw new Error('SSE stream unavailable')
        }

        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        let currentEvent = 'message'

        while (true) {
            const { value, done } = await reader.read()
            if (done) break
            buffer += decoder.decode(value, { stream: true })

            const blocks = buffer.split('\n\n')
            buffer = blocks.pop() || ''

            for (const block of blocks) {
                const lines = block.split('\n')
                let dataLine = ''
                for (const raw of lines) {
                    const line = raw.trim()
                    if (!line) continue
                    if (line.startsWith('event:')) {
                        currentEvent = line.slice(6).trim()
                    } else if (line.startsWith('data:')) {
                        dataLine = line.slice(5).trim()
                    }
                }

                if (!dataLine) continue
                if (dataLine === '[DONE]' || currentEvent === 'done') {
                    setIsConnected(false)
                    setIsAnalyzing(false)
                    return
                }

                try {
                    const data = JSON.parse(dataLine) as Record<string, unknown>
                    parseAndDispatch({ event: currentEvent, data })
                } catch {
                    addLog({
                        id: `${Date.now()}-${Math.random()}`,
                        timestamp: new Date().toISOString(),
                        type: 'error',
                        content: `SSE解析失败: ${dataLine.slice(0, 120)}`,
                    } as LogEntry)
                }
            }
        }

        setIsConnected(false)
        setIsAnalyzing(false)
    }

    const handleSubmit = async (e: FormEvent) => {
        e.preventDefault()
        const prompt = input.trim()
        if (!prompt || streaming) return

        setInput('')
        setChat((prev) => [
            ...prev,
            {
                id: `${Date.now()}-${Math.random()}`,
                role: 'user',
                content: prompt,
                timestamp: new Date().toISOString(),
            },
        ])

        reset()
        setStreaming(true)
        setIsAnalyzing(true)
        setIsConnected(false)
        addLog({
            id: `${Date.now()}-${Math.random()}`,
            timestamp: new Date().toISOString(),
            type: 'system',
            content: '正在连接实时事件流...',
        })

        try {
            await streamChat(prompt)
        } catch (error) {
            pushAssistant(`请求失败：${error instanceof Error ? error.message : 'unknown error'}`)
            addLog({
                id: `${Date.now()}-${Math.random()}`,
                timestamp: new Date().toISOString(),
                type: 'error',
                content: error instanceof Error ? error.message : '请求失败',
            })
            setIsAnalyzing(false)
            setIsConnected(false)
        } finally {
            setStreaming(false)
        }
    }

    return (
        <aside className="card h-full min-h-0 flex flex-col overflow-hidden">
            <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                    <Bot className="w-5 h-5 text-cyan-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">智能对话</h2>
                </div>
                {streaming && (
                    <span className="badge-blue inline-flex items-center gap-1">
                        <Loader2 className="w-3 h-3 animate-spin" />
                        分析中
                    </span>
                )}
            </div>

            <div className="text-xs text-slate-500 dark:text-slate-400 mb-3 flex items-center gap-1">
                <Sparkles className="w-3 h-3" />
                示例：分析贵州茅台 600519.SH 今天走势
            </div>

            <div className="flex flex-wrap gap-2 mb-3">
                {PRESET_PROMPTS.map((prompt) => (
                    <button
                        key={prompt}
                        onClick={() => setInput(prompt)}
                        className="text-xs px-2.5 py-1 rounded-md border border-slate-200 dark:border-slate-600 text-slate-600 dark:text-slate-400 hover:border-blue-400 dark:hover:border-blue-500 hover:text-slate-900 dark:hover:text-slate-200"
                    >
                        {prompt}
                    </button>
                ))}
            </div>

            <div className="flex-1 min-h-0 overflow-y-auto space-y-3 pr-1">
                {chat.map((msg) => (
                    <div key={msg.id} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                        <div
                            className={`max-w-[92%] rounded-xl px-3 py-2 text-sm leading-relaxed ${msg.role === 'user'
                                    ? 'bg-blue-100 dark:bg-blue-500/20 border border-blue-300 dark:border-blue-500/30 text-slate-900 dark:text-slate-100'
                                    : 'bg-slate-100 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 text-slate-700 dark:text-slate-300'
                                }`}
                        >
                            {msg.content}
                        </div>
                    </div>
                ))}

                <div className="pt-2 border-t border-slate-200 dark:border-slate-700">
                    <p className="text-xs text-slate-500 dark:text-slate-400 mb-2">思考流程</p>
                    <div className="space-y-1.5">
                        {timelineLogs.slice(-120).map((log) => (
                            <div key={log.id} className="text-xs text-slate-600 dark:text-slate-400 bg-slate-50 dark:bg-slate-800/50 border border-slate-200 dark:border-slate-700 rounded-md px-2 py-1">
                                <span className="text-slate-400 dark:text-slate-500 mr-2">{new Date(log.timestamp).toLocaleTimeString()}</span>
                                {log.agent && <span className="text-cyan-500 mr-2">{log.agent}</span>}
                                <span>{log.content}</span>
                            </div>
                        ))}
                    </div>
                </div>
            </div>

            <form onSubmit={handleSubmit} className="mt-3 shrink-0">
                <div className="flex items-center gap-2">
                    <input
                        value={input}
                        onChange={(e) => setInput(e.target.value)}
                        placeholder="直接描述你的分析需求..."
                        className="input flex-1"
                    />
                    <button
                        type="submit"
                        disabled={!input.trim() || streaming}
                        className="btn-primary px-3 py-2 inline-flex items-center gap-1"
                    >
                        <Send className="w-4 h-4" />
                        发送
                    </button>
                </div>
            </form>
        </aside>
    )
}

import { Terminal, Clock } from 'lucide-react'
import { useAnalysisStore } from '@/stores/analysisStore'
import { format } from 'date-fns'
import { zhCN } from 'date-fns/locale'

export default function LogStream() {
    const { logs, isConnected } = useAnalysisStore()

    const getLogIcon = (type: string) => {
        switch (type) {
            case 'error':
                return '🔴'
            case 'system':
                return '🔵'
            case 'tool':
                return '🛠️'
            case 'agent':
                return '🤖'
            default:
                return '⚪'
        }
    }

    const getLogColor = (type: string) => {
        switch (type) {
            case 'error':
                return 'text-trading-accent-red'
            case 'system':
                return 'text-trading-accent-blue'
            case 'tool':
                return 'text-trading-accent-orange'
            case 'agent':
                return 'text-trading-accent-green'
            default:
                return 'text-trading-text-secondary'
        }
    }

    return (
        <div className="card h-full flex flex-col min-h-0 overflow-hidden">
            <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2">
                    <Terminal className="w-5 h-5 text-trading-accent-blue" />
                    <h2 className="text-lg font-semibold text-trading-text-primary">实时日志</h2>
                </div>
                <div className="flex items-center gap-2">
                    <div className={`w-2 h-2 rounded-full ${isConnected ? 'bg-trading-accent-green animate-pulse' : 'bg-trading-text-muted'}`} />
                    <span className="text-xs text-trading-text-muted">
                        {isConnected ? '实时' : '离线'}
                    </span>
                </div>
            </div>

            <div className="flex-1 overflow-y-auto space-y-1 font-mono text-sm min-h-0">
                {logs.length === 0 ? (
                    <div className="flex flex-col items-center justify-center h-full text-trading-text-muted">
                        <Clock className="w-8 h-8 mb-2 opacity-50" />
                        <p>等待分析开始...</p>
                    </div>
                ) : (
                    logs.map((log) => (
                        <div
                            key={log.id}
                            className={`flex items-start gap-2 p-2 rounded hover:bg-trading-bg-tertiary/50 transition-colors ${getLogColor(log.type)}`}
                        >
                            <span className="flex-shrink-0">{getLogIcon(log.type)}</span>
                            <div className="flex-1 min-w-0">
                                <p className="break-words">{log.content}</p>
                                <p className="text-xs opacity-50 mt-0.5">
                                    {format(new Date(log.timestamp), 'HH:mm:ss', { locale: zhCN })}
                                    {log.agent && ` · ${log.agent}`}
                                </p>
                            </div>
                        </div>
                    ))
                )}
            </div>
        </div>
    )
}

import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import AgentPipeline from '@/components/AgentPipeline'
import ReportViewer from '@/components/ReportViewer'
import ChatCopilotPanel from '@/components/ChatCopilotPanel'
import KlinePanel from '@/components/KlinePanel'

export default function Analysis() {
    const [searchParams] = useSearchParams()
    const [activeSymbol, setActiveSymbol] = useState('000001.SH')

    useEffect(() => {
        const querySymbol = (searchParams.get('symbol') || '').trim()
        if (!querySymbol) return
        setActiveSymbol(querySymbol.toUpperCase())
    }, [searchParams])

    return (
        <div className="flex flex-col gap-4">
            <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_430px] gap-4 items-start">
                <div className="space-y-4">
                    <KlinePanel
                        symbol={activeSymbol}
                        onSymbolChange={setActiveSymbol}
                    />

                    <AgentPipeline />
                    <ReportViewer />
                </div>

                <div className="xl:sticky xl:top-0 self-start h-[calc(100vh-7rem)] min-h-[620px]">
                    <ChatCopilotPanel
                        onSymbolDetected={setActiveSymbol}
                    />
                </div>
            </div>
        </div>
    )
}

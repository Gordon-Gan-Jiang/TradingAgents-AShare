import { create } from 'zustand'
import type {
    Agent,
    JobStatus,
    AnalysisReport,
    LogEntry,
    AgentStatusEvent,
    AgentMessageEvent,
    AgentToolCallEvent,
    AgentReportEvent,
    AgentSnapshotEvent
} from '@/types'

interface AnalysisState {
    // Current Job
    currentJobId: string | null
    jobStatus: JobStatus | null

    // Agents
    agents: Agent[]

    // Report
    report: AnalysisReport | null

    // Logs
    logs: LogEntry[]

    // Loading States
    isAnalyzing: boolean
    isConnected: boolean

    // Actions
    setCurrentJobId: (jobId: string | null) => void
    setJobStatus: (status: JobStatus | null) => void
    updateAgentStatus: (event: AgentStatusEvent) => void
    updateAgentSnapshot: (event: AgentSnapshotEvent) => void
    addAgentMessage: (event: AgentMessageEvent) => void
    addAgentToolCall: (event: AgentToolCallEvent) => void
    addAgentReport: (event: AgentReportEvent) => void
    addLog: (log: LogEntry) => void
    setReport: (report: AnalysisReport | null) => void
    setIsAnalyzing: (isAnalyzing: boolean) => void
    setIsConnected: (isConnected: boolean) => void
    reset: () => void
}

const initialAgents: Agent[] = [
    // Analyst Team
    { id: 'market', name: 'Market Analyst', team: 'Analyst Team', status: 'pending' },
    { id: 'social', name: 'Social Analyst', team: 'Analyst Team', status: 'pending' },
    { id: 'news', name: 'News Analyst', team: 'Analyst Team', status: 'pending' },
    { id: 'fundamentals', name: 'Fundamentals Analyst', team: 'Analyst Team', status: 'pending' },

    // Research Team
    { id: 'bull', name: 'Bull Researcher', team: 'Research Team', status: 'pending' },
    { id: 'bear', name: 'Bear Researcher', team: 'Research Team', status: 'pending' },
    { id: 'research_manager', name: 'Research Manager', team: 'Research Team', status: 'pending' },

    // Trading Team
    { id: 'trader', name: 'Trader', team: 'Trading Team', status: 'pending' },

    // Risk Management
    { id: 'aggressive', name: 'Aggressive Analyst', team: 'Risk Management', status: 'pending' },
    { id: 'conservative', name: 'Conservative Analyst', team: 'Risk Management', status: 'pending' },
    { id: 'neutral', name: 'Neutral Analyst', team: 'Risk Management', status: 'pending' },

    // Portfolio Management
    { id: 'portfolio_manager', name: 'Portfolio Manager', team: 'Portfolio Management', status: 'pending' },
]

const truncateLog = (content: string, max = 420) => {
    if (!content) return ''
    if (content.length <= max) return content
    return `${content.slice(0, max)}...`
}

export const useAnalysisStore = create<AnalysisState>((set) => ({
    currentJobId: null,
    jobStatus: null,
    agents: initialAgents,
    report: null,
    logs: [],
    isAnalyzing: false,
    isConnected: false,

    setCurrentJobId: (jobId) => set({ currentJobId: jobId }),

    setJobStatus: (status) => set({ jobStatus: status }),

    updateAgentStatus: (event) => set((state) => ({
        agents: state.agents.map(agent =>
            agent.name === event.agent
                ? { ...agent, status: event.status }
                : agent
        )
    })),

    updateAgentSnapshot: (event) => set((state) => {
        const agentMap = new Map(event.agents.map(a => [a.agent, a.status]))
        return {
            agents: state.agents.map(agent => ({
                ...agent,
                status: agentMap.get(agent.name) || agent.status
            }))
        }
    }),

    addAgentMessage: (event) => set((state) => {
        const summarized = event.message_type === 'tool'
            ? truncateLog(event.content, 240)
            : truncateLog(event.content, 420)
        const log: LogEntry = {
            id: `${Date.now()}-${Math.random()}`,
            timestamp: new Date().toISOString(),
            type: 'agent',
            content: summarized,
            agent: event.agent || undefined
        }
        return { logs: [log, ...state.logs].slice(0, 100) }
    }),

    addAgentToolCall: (event) => set((state) => {
        const log: LogEntry = {
            id: `${Date.now()}-${Math.random()}`,
            timestamp: new Date().toISOString(),
            type: 'tool',
            content: truncateLog(`${event.tool_call.name}: ${JSON.stringify(event.tool_call.args)}`, 260),
            agent: event.agent || undefined
        }
        return { logs: [log, ...state.logs].slice(0, 100) }
    }),

    addAgentReport: (event) => set((state) => ({
        report: {
            ...state.report,
            [event.section]: event.content
        } as AnalysisReport
    })),

    addLog: (log) => set((state) => ({
        logs: [log, ...state.logs].slice(0, 100)
    })),

    setReport: (report) => set({ report }),

    setIsAnalyzing: (isAnalyzing) => set({ isAnalyzing }),

    setIsConnected: (isConnected) => set({ isConnected }),

    reset: () => set({
        currentJobId: null,
        jobStatus: null,
        agents: initialAgents.map(a => ({ ...a, status: 'pending' })),
        report: null,
        logs: [],
        isAnalyzing: false,
        isConnected: false
    })
}))

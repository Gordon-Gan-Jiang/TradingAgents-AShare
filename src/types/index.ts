// Agent Types
export type AgentStatus = 'pending' | 'in_progress' | 'completed' | 'error' | 'skipped'

export interface Agent {
    id: string
    name: string
    team: string
    status: AgentStatus
    description?: string
}

export interface AgentTeam {
    name: string
    agents: Agent[]
}

// Analysis Types
export interface AnalysisRequest {
    symbol: string
    trade_date: string
    selected_analysts: string[]
    config_overrides?: Record<string, unknown>
    dry_run?: boolean
}

export interface AnalysisResponse {
    job_id: string
    status: 'pending' | 'running' | 'completed' | 'failed'
    created_at: string
}

export interface JobStatus {
    job_id: string
    status: 'pending' | 'running' | 'completed' | 'failed'
    created_at: string
    started_at?: string
    finished_at?: string
    symbol: string
    trade_date: string
    error?: string
}

// SSE Event Types
export type SSEEventType =
    | 'job.created'
    | 'job.running'
    | 'job.completed'
    | 'job.failed'
    | 'agent.status'
    | 'agent.message'
    | 'agent.tool_call'
    | 'agent.report'
    | 'agent.report.chunk'
    | 'agent.snapshot'
    | 'agent.milestone'
    | 'agent.writing'
    | 'agent.activity'
    | 'agent.activity_complete'

export interface SSEEvent {
    event: SSEEventType
    data: Record<string, unknown>
    timestamp: string
}

export interface AgentStatusEvent {
    agent: string
    status: AgentStatus
    previous_status?: AgentStatus
}

export interface AgentMessageEvent {
    agent: string | null
    message_type: string | null
    content: string
}

export interface AgentToolCallEvent {
    agent: string | null
    tool_call: {
        name: string
        args: Record<string, unknown>
    }
}

export interface AgentReportEvent {
    section: string
    content: string
}

export interface ReportChunkEvent {
    section: string
    chunk: string
    index: number
    is_complete: boolean
}

export interface AgentMilestoneEvent {
    stage: string
    title: string
    summary: string
    timestamp: string
}

export interface AgentToolCallDisplayEvent {
    agent: string
    tool: string
    description: string
}

export interface AgentWritingEvent {
    agent: string
    report: string
    report_name: string
    status: 'writing' | 'completed'
}

// 新增：Agent活动事件（聚合工具调用）
export interface AgentActivityEvent {
    agent: string
    type: 'data_fetch' | 'data_analysis' | 'writing' | 'thinking'
    details: string
    tools?: string[]
    is_update?: boolean
}

export interface AgentActivityCompleteEvent {
    agent: string
    type: string
}

export interface AgentSnapshotEvent {
    agents: Array<{
        team: string
        agent: string
        status: AgentStatus
    }>
}

// Streaming Report State
export interface StreamingSectionState {
    buffer: string      // 待渲染的完整内容
    displayed: string   // 已显示的内容
    isTyping: boolean   // 是否正在打字
    isComplete: boolean // 是否完成
}

export interface MilestoneMessage {
    id: string
    stage: string
    title: string
    summary: string
    timestamp: string
}

// Report Types
export interface AnalysisReport {
    symbol: string
    trade_date: string
    market_report?: string
    sentiment_report?: string
    news_report?: string
    fundamentals_report?: string
    investment_plan?: string
    trader_investment_plan?: string
    final_trade_decision?: string
}

// UI Types
export interface LogEntry {
    id: string
    timestamp: string
    type: 'system' | 'agent' | 'tool' | 'data' | 'error'
    content: string
    agent?: string
}

export interface StockInfo {
    symbol: string
    name: string
    price: number
    change: number
    changePercent: number
}

export interface KlineCandle {
    date: string
    open: number
    high: number
    low: number
    close: number
    volume?: number | null
}

export interface KlineResponse {
    symbol: string
    start_date: string
    end_date: string
    candles: KlineCandle[]
}

// Report Types (from database)
export interface Report {
    id: string
    user_id?: string
    symbol: string
    trade_date: string
    decision?: string
    confidence?: number
    target_price?: number
    stop_loss_price?: number
    created_at?: string
    updated_at?: string
}

export interface ReportDetail extends Report {
    market_report?: string
    sentiment_report?: string
    news_report?: string
    fundamentals_report?: string
    investment_plan?: string
    trader_investment_plan?: string
    final_trade_decision?: string
    result_data?: AnalysisReport
}

export interface ReportListResponse {
    total: number
    reports: Report[]
}

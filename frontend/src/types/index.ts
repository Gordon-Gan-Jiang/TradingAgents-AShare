// Agent Types
export type AgentStatus = 'pending' | 'in_progress' | 'completed' | 'error' | 'skipped'

export interface Agent {
    id: string
    name: string
    team: string
    status: AgentStatus
    description?: string
    startedAt?: number
    finishedAt?: number
}

export interface AgentTeam {
    name: string
    agents: Agent[]
}

// Analysis Types
export interface InstrumentContext {
    symbol: string
    security_name: string
    market_country: string
    exchange: string
    currency: string
    asset_type: string
}

export interface MarketContext {
    trade_date: string
    timezone: string
    market_country: string
    exchange: string
    market_session: string
    market_is_open: boolean
    analysis_mode: string
    data_as_of: string
    session_note: string
}

export interface UserContext {
    objective?: string
    risk_profile?: string
    investment_horizon?: string
    cash_available?: number
    current_position?: number
    current_position_pct?: number
    average_cost?: number
    max_loss_pct?: number
    constraints?: string[]
    user_notes?: string
}

export interface WorkflowContext {
    context_version: string
    request_source: string
    selected_analysts: string[]
}

export interface GameTheorySignals {
    board?: string
    players?: string[]
    player_states?: Record<string, string>
    likely_actions?: Record<string, string[]>
    dominant_strategy?: string
    fragile_equilibrium?: string
    counter_consensus_signal?: string
    confidence?: number
}

export interface RiskFeedbackState {
    retry_count: number
    max_retries: number
    revision_required: boolean
    latest_risk_verdict: string
    hard_constraints: string[]
    soft_constraints: string[]
    execution_preconditions: string[]
    de_risk_triggers: string[]
    revision_reason: string
}

export interface ConsensusAgentBreakdown {
    agent: string
    label: string
    horizon: string
    verdict: string
    confidence: number
    weight: number
    contribution: number
    key_finding?: string
    stance?: 'supporting' | 'opposing' | 'neutral'
}

export interface ConsensusSummary {
    consensus_direction: string
    consensus_score: number
    consensus_strength: number
    disagreement_score: number
    stability_score: number
    execution_mode: 'direct' | 'conditional' | 'observe'
    execution_mode_label: string
    dominant_horizon: string
    horizon_conflict: boolean
    support_ratio: number
    oppose_ratio: number
    neutral_ratio: number
    risk_gate?: string
    execution_preconditions?: string[]
    de_risk_triggers?: string[]
    flip_conditions?: string[]
    final_direction?: string | null
    final_confidence?: number | null
    supporting_agents?: string[]
    opposing_agents?: string[]
    agent_breakdown?: ConsensusAgentBreakdown[]
    /**
     * 解析失败、因而**未计入**加权投票的分析师数量。
     * 必须展示：否则「7 位分析师一致看多」与「只有 2 份报告解析成功」在界面上完全一样。
     */
    unparsed_analyst_n?: number
    summary?: string
}

export interface AnalysisRequest {
    symbol: string
    /** 不传则由后端默认取当前 A 股交易日 */
    trade_date?: string
    selected_analysts: string[]
    objective?: string
    risk_profile?: string
    investment_horizon?: string
    cash_available?: number
    current_position?: number
    current_position_pct?: number
    average_cost?: number
    max_loss_pct?: number
    constraints?: string[]
    user_notes?: string
    config_overrides?: Record<string, unknown>
    model_profile_id?: string
    dry_run?: boolean
    prompt_template_id?: string
    prompt_vars?: Record<string, unknown>
}

export interface ModelProfile {
    id: string
    name: string
    description?: string | null
    llm_provider: string
    backend_url?: string | null
    quick_think_llm?: string | null
    deep_think_llm?: string | null
    is_default: boolean
    is_active: boolean
    tags: string[]
    has_api_key: boolean
    last_probe_status?: string | null
    last_probe_error?: string | null
    last_probe_at?: string | null
    created_at?: string | null
    updated_at?: string | null
}

export interface ModelProfileCreateRequest {
    name: string
    description?: string
    llm_provider: string
    backend_url?: string
    quick_think_llm?: string
    deep_think_llm?: string
    api_key?: string
    is_default?: boolean
    is_active?: boolean
    tags?: string[]
}

export interface ModelProfileUpdateRequest {
    name?: string
    description?: string
    llm_provider?: string
    backend_url?: string
    quick_think_llm?: string
    deep_think_llm?: string
    api_key?: string
    clear_api_key?: boolean
    is_default?: boolean
    is_active?: boolean
    tags?: string[]
}

export interface ModelProfileWarmupResponse {
    profile_id: string
    prompt: string
    results: RuntimeWarmupResult[]
    profile: ModelProfile
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
    waiting_ahead_count?: number | null
    scheduled_running_count?: number | null
    scheduled_concurrency_limit?: number | null
    progress?: number | null
    phase?: string | null
    progress_detail?: string | null
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
    | 'agent.token'
    | 'agent.debate'
    | 'agent.debate.token'

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

export interface AgentTokenEvent {
    agent: string
    report: string
    token: string
    horizon?: string
}

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
    buffer: string
    displayed: string
    isTyping: boolean
    isComplete: boolean
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
    decision?: string
    direction?: string
    instrument_context?: InstrumentContext
    market_context?: MarketContext
    user_context?: UserContext
    workflow_context?: WorkflowContext
    market_report?: string
    sentiment_report?: string
    news_report?: string
    fundamentals_report?: string
    macro_report?: string
    smart_money_report?: string
    volume_price_report?: string
    game_theory_report?: string
    game_theory_signals?: GameTheorySignals
    investment_plan?: string
    trader_investment_plan?: string
    risk_feedback_state?: RiskFeedbackState
    final_trade_decision?: string
    consensus_summary?: ConsensusSummary
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
    amount?: number | null
    change?: number | null
    change_percent?: number | null
    turnover_rate?: number | null
}

export interface KlineResponse {
    symbol: string
    start_date: string
    end_date: string
    candles: KlineCandle[]
}

// Structured extraction types
export interface RiskItem {
    name: string
    level: 'high' | 'medium' | 'low'
    description?: string
}

export interface KeyMetric {
    name: string
    value: string
    status: 'good' | 'neutral' | 'bad'
}

export interface FreshnessSummary {
    overall_status: 'fresh' | 'warning' | 'stale' | 'error'
    overall_label?: string
    expected_anchor?: string
    confidence_cap?: number
    fetch_errors?: Array<{ source_key: string; error_code?: string; error_message?: string }>
    blocking_sources?: Array<{
        source_key: string
        status?: string
        anchor_expected?: string
        anchor_actual?: string
        error_message?: string
        lag_note?: string
    }>
    section_impacts?: Record<string, string>
    report_age_note?: string | null
    report_age_days?: number
    warning_sources?: Array<{
        source_key: string
        lag_note?: string
        status?: string
    }>
    datasets?: Array<{
        source_key: string
        status?: string
        lag_note?: string
        error_message?: string
        error_code?: string
        anchor_actual?: string
    }>
}

// Report Types (from database)
export interface Report {
    id: string
    user_id?: string
    symbol: string
    name?: string
    trade_date: string
    status: 'pending' | 'running' | 'completed' | 'failed'
    error?: string
    decision?: string
    direction?: string
    confidence?: number
    target_price?: number
    stop_loss_price?: number
    risk_items?: RiskItem[]
    key_metrics?: KeyMetric[]
    created_at?: string
    updated_at?: string
    waiting_ahead_count?: number | null
    scheduled_running_count?: number | null
    scheduled_concurrency_limit?: number | null
    model_profile_id?: string | null
    model_profile_name?: string | null
    llm_provider?: string | null
    quick_think_llm?: string | null
    deep_think_llm?: string | null
    backend_url?: string | null
    freshness_status?: 'fresh' | 'warning' | 'stale' | 'error' | null
    freshness_summary?: FreshnessSummary | null
}

export interface ReportDetail extends Report {
    market_report?: string
    sentiment_report?: string
    news_report?: string
    fundamentals_report?: string
    macro_report?: string
    smart_money_report?: string
    volume_price_report?: string
    game_theory_report?: string
    investment_plan?: string
    trader_investment_plan?: string
    final_trade_decision?: string
    result_data?: AnalysisReport
}

export interface ReportListResponse {
    total: number
    reports: Report[]
}

export interface AnnouncementItem {
    title: string
    detail: string
}

export interface Announcement {
    id: string
    tag?: string
    title: string
    summary?: string
    published_at: string
    items: AnnouncementItem[]
    cta_label?: string
    cta_path?: string
}

export interface LatestAnnouncementResponse {
    announcement: Announcement | null
}

// Watchlist & Scheduled Analysis
export interface WatchlistItem {
    id: string
    symbol: string
    name: string
    sort_order: number
    created_at: string
    has_scheduled: boolean
}

export interface WatchlistBatchResult {
    input: string
    symbol?: string
    name?: string
    status: 'added' | 'duplicate' | 'invalid' | 'failed'
    message: string
    item?: WatchlistItem
}

export interface WatchlistBatchResponse {
    message: string
    summary: {
        total: number
        added: number
        duplicate: number
        failed: number
    }
    results: WatchlistBatchResult[]
}

export interface ScheduledAnalysis {
    id: string
    symbol: string
    name: string
    horizon: string
    trigger_time: string
    prompt_template_id?: string | null
    prompt_vars?: Record<string, unknown>
    is_active: boolean
    last_run_date: string | null
    last_run_status: string | null
    last_report_id: string | null
    consecutive_failures: number
    created_at: string
    has_imported_context?: boolean
    imported_current_position?: number | null
    imported_average_cost?: number | null
    imported_trade_points_count?: number
}

export interface ScheduledBatchUpdateResponse {
    items: ScheduledAnalysis[]
}

export interface ScheduledBatchDeleteResponse {
    deleted_ids: string[]
    missing_ids: string[]
}

export interface ScheduledBatchTriggerJob {
    item_id: string
    job_id: string
    symbol: string
    name: string
    status: 'pending' | 'running' | 'completed' | 'failed'
    created_at: string
}

export interface ScheduledBatchTriggerResponse {
    summary: {
        total: number
    }
    jobs: ScheduledBatchTriggerJob[]
}

export interface StockSearchResult {
    symbol: string
    name: string
}

export interface PromptTemplate {
    id: string
    user_id?: string | null
    scope: string
    name: string
    description?: string | null
    template_text: string
    intent_json: Record<string, unknown>
    is_builtin: boolean
    is_active: boolean
    created_at?: string | null
    updated_at?: string | null
}

export interface ImportedPortfolioPosition {
    symbol: string
    name: string
    current_position?: number | null
    available_position?: number | null
    average_cost?: number | null
    market_value?: number | null
    current_position_pct?: number | null
    trade_points_count: number
    latest_trade_at?: string | null
    latest_trade_action?: string | null
    last_imported_at?: string | null
    recent_trade_points?: Array<Record<string, unknown>>
}

export interface ImportedScheduledSyncSummary {
    created: string[]
    existing: string[]
    skipped_limit: string[]
}

export interface PortfolioImportState {
    auto_apply_scheduled: boolean
    last_synced_at?: string | null
    last_error?: string | null
    summary: {
        positions: number
    }
    scheduled_sync?: ImportedScheduledSyncSummary
    positions: ImportedPortfolioPosition[]
}

export interface PortfolioPositionInput {
    symbol: string
    name?: string
    current_position?: number | null
    available_position?: number | null
    average_cost?: number | null
    market_value?: number | null
    current_position_pct?: number | null
}

export interface PortfolioOverviewResponse {
    watchlist: WatchlistItem[]
    scheduled: ScheduledAnalysis[]
    latest_reports: Report[]
    portfolio_import: PortfolioImportState | null
}

export interface PaperPosition {
    symbol: string
    name?: string | null
    quantity: number
    avg_cost: number
    last_price?: number | null
}

export interface PaperPortfolioSnapshot {
    portfolio_id: string
    cash_balance: number
    initial_cash: number
    market_value: number
    equity: number
    total_return_pct?: number | null
    positions: PaperPosition[]
}

export interface PaperPortfolioBootstrapRequest {
    initial_cash?: number
    source?: string
    reset_existing?: boolean
}

export interface PaperTradeRequest {
    symbol: string
    name?: string
    side: 'BUY' | 'SELL'
    quantity: number
    price?: number
    reason?: string
    fee_rate?: number
    trade_date?: string
}

export interface DailyOperationRequest {
    trade_date?: string
    include_recommendations?: boolean
    recommendation_top_k?: number
    auto_execute?: boolean
}

export interface DailyOperationAction {
    symbol: string
    name?: string | null
    action: 'BUY' | 'SELL' | 'HOLD'
    quantity: number
    price?: number | null
    reason: string
}

export interface DailyOperationResult {
    plan: {
        trade_date: string
        generated_at: string
        cash_balance: number
        actions: DailyOperationAction[]
    }
    executed: Array<Record<string, unknown>>
    execution_errors: Array<Record<string, unknown>>
    review: PaperDailyReviewSummary
}

export interface PaperDailyReviewSummary {
    trade_date: string
    equity: number
    market_value: number
    cash_balance: number
    total_return_pct: number
    day_realized_pnl: number
    day_fees: number
    trade_count: number
    positions: Array<Record<string, unknown>>
}

export interface TrackingBoardAnalysis {
    report_id: string
    trade_date: string
    is_previous_trade_day: boolean
    decision?: string | null
    direction?: string | null
    high_price?: number | null
    low_price?: number | null
    trader_advice_summary?: string | null
    trader_investment_plan?: string | null
    final_trade_decision?: string | null
}

export interface TrackingBoardItem {
    symbol: string
    name: string
    current_position?: number | null
    available_position?: number | null
    average_cost?: number | null
    market_value?: number | null
    current_position_pct?: number | null
    live_market_value?: number | null
    floating_pnl?: number | null
    floating_pnl_pct?: number | null
    live_price?: number | null
    day_open?: number | null
    price_change?: number | null
    price_change_pct?: number | null
    day_high?: number | null
    day_low?: number | null
    previous_close?: number | null
    volume?: number | null
    amount?: number | null
    quote_time?: string | null
    quote_source?: string | null
    last_imported_at?: string | null
    analysis?: TrackingBoardAnalysis | null
}

export interface TrackingBoardResponse {
    previous_trade_date: string
    refresh_interval_seconds: number
    items: TrackingBoardItem[]
}

export interface RecommendationItem {
    symbol: string
    code: string
    name: string
    score: number
    reasons: string[]
    live_price?: number | null
    price_change_pct?: number | null
    day_high?: number | null
    day_open?: number | null
    amount?: number | null
    volume?: number | null
    volume_ratio?: number | null
    turnover_rate?: number | null
    sector?: string | null
    quote_time?: string | null
    quote_source?: string | null
    strategy_hits?: string[]
    risk_flags?: string[]
    score_breakdown?: Record<string, number> | null
}

export interface RecommendationAnalyzeJob {
    symbol: string
    job_id: string
    status: 'pending' | 'running' | 'completed' | 'failed'
    created_at: string
}

export interface RecommendationRequest {
    top_k?: number
    candidate_limit?: number
    source_mode?: 'user_pool' | 'market_scan'
    scan_limit?: number
    include_tracking?: boolean
    include_watchlist?: boolean
    seed_symbols?: string[]
    min_change_pct?: number
    market?: 'cn' | 'us'
    min_price?: number
    max_price?: number
    min_amount?: number
    score_profile?: string
    momentum_weight?: number
    activity_weight?: number
    near_high_weight?: number
    auto_start_analysis?: boolean
    auto_top_n?: number
    horizons?: string[]
    selected_analysts?: string[]
}

export interface RecommendationProfile {
    id: string
    name: string
    description: string
}

export interface RecommendationResponse {
    pool_size: number
    scored_size: number
    items: RecommendationItem[]
    scoring_model?: {
        market: 'cn' | 'us'
        profile: string
        engine?: string
        factors?: string[]
        weights: {
            momentum: number
            activity: number
            near_high: number
            sector?: number
            volume_ratio?: number
            [key: string]: number | undefined
        }
    }
    analysis_jobs: RecommendationAnalyzeJob[]
}

export interface DailyProductRunRequest {
    mode?: 'recommended' | 'watchlist' | 'tracking' | 'all'
    top_k?: number
    candidate_limit?: number
    recommendation_source?: 'user_pool' | 'market_scan'
    scan_limit?: number
    include_tracking?: boolean
    include_watchlist?: boolean
    seed_symbols?: string[]
    min_change_pct?: number
    market?: 'cn' | 'us'
    min_price?: number
    max_price?: number
    min_amount?: number
    score_profile?: string
    momentum_weight?: number
    activity_weight?: number
    near_high_weight?: number
    use_backtest_feedback?: boolean
    strategy_skills?: string[]
    strategy_mode?: 'manual' | 'auto'
    horizons?: string[]
    selected_analysts?: string[]
    ensure_scheduled?: boolean
    schedule_horizon?: 'short' | 'medium'
    schedule_trigger_time?: string
}

export interface DailyProductRunJob {
    symbol: string
    name: string
    job_id: string
    status: 'pending' | 'running' | 'completed' | 'failed'
    created_at: string
}

export interface DailyProductRunResponse {
    run_id: string
    status: 'pending' | 'running' | 'completed' | 'failed'
    mode: string
    summary: Record<string, unknown>
    recommendation?: RecommendationResponse | null
    jobs: DailyProductRunJob[]
}

export interface RecommendationHistoryItem {
    id: string
    run_id?: string | null
    symbol: string
    name: string
    rank?: number | null
    score?: number | null
    reasons: string[]
    strategy_hits: string[]
    risk_flags: string[]
    score_breakdown: Record<string, number>
    quote: Record<string, unknown>
    selected_for_analysis: boolean
    feedback_status: string
    feedback_horizon_days: number
    entry_price?: number | null
    exit_price?: number | null
    realized_return_pct?: number | null
    created_at?: string | null
    feedback_evaluated_at?: string | null
    source_mode?: string | null
    market?: string | null
    score_profile?: string | null
}

/** T+1 扫描推荐：按信号日聚合的曲线点 */
export interface InsightsT1RecommendationPoint {
    date: string
    sample_count: number
    avg_return_pct: number
    win_rate_pct?: number | null
}

/** 持仓深度报告：按报告信号日聚合的方向正确率 */
export interface InsightsT1ReportAccuracyPoint {
    date: string
    sample_count: number
    accuracy_pct: number | null
    /**
     * 去重后的唯一价格窗口数 —— 命中率真正的分母。
     * `sample_count` 是研报行数；同一标的同一信号日的多份研报共享同一次前瞻收益，
     * 只算一次。两者差距越大，说明这一天越是被少数标的上的重复研报主导。
     */
    effective_n?: number | null
    unique_symbols?: number | null
    top_symbol_share?: number | null
    /** Wilson 95% 区间。单日 effective_n 常常只有个位数，区间宽是常态。 */
    ci_low_pct?: number | null
    ci_high_pct?: number | null
    /** 该日被放弃的方向判断数（中性/无方向）。弃权不进命中率分母，但必须可见。 */
    abstain_count?: number | null
    /** 同一窗口被给出相反方向的次数：这些窗口不是「一次预测」，已排除出分母。 */
    conflict_count?: number | null
}

/**
 * 区间整体（跨日去重后）的方向命中率与显著性判定。
 *
 * `not_significant=true` 表示 Wilson 区间跨过 50%，即**现有样本无法把该命中率与
 * 掷硬币区分开**；`underpowered=true` 表示有效样本量远低于 `required_n`。
 * 这两种情况下都不应把 `accuracy_pct` 当作「系统有预测能力」的证据。
 */
export interface InsightsT1ReportAccuracySummary {
    scope: 'portfolio' | 'all'
    effective_n: number
    hits?: number
    accuracy_pct?: number | null
    ci_low_pct?: number | null
    ci_high_pct?: number | null
    n_days?: number
    unique_symbols?: number | null
    top_symbol_share?: number | null
    conflict_n?: number
    row_count?: number
    abstain_count?: number
    coverage_pct?: number | null
    required_n?: number
    underpowered?: boolean
    beats_coin_flip?: boolean
    not_significant?: boolean
    verdict?: 'beats_coin_flip' | 'not_significant' | 'worse_than_coin_flip' | 'no_sample'
    /**
     * 按日等权（每个交易日只投一票）+ 按日聚类标准误的第二种估计量。
     *
     * 池化的 `accuracy_pct` 把同一交易日内的判断当独立观测，但它们是横截面相关的
     * （大盘同涨同跌）。真实库上两者差 4.7pp（池化 52.34% vs 按日等权 47.66%），
     * 说明池化那个数被少数高产的交易日抬起来了。这个区间更保守也更可信。
     */
    clustered?: {
        accuracy_pct?: number | null
        se_pct?: number | null
        t?: number | null
        ci_low_pct?: number | null
        ci_high_pct?: number | null
        bootstrap_ci_low_pct?: number | null
        bootstrap_ci_high_pct?: number | null
        n_days?: number
        note?: string
    }
    /**
     * P5 去 beta：绝对命中率里绝大部分是市场 beta（池子基准上涨率约 51.8%），
     * 只要大盘在涨，闭眼买入就有约 52% 命中率。这一块是**相对市场**的口径，
     * 只有看多且跑赢同日同侪、或看空且跑输同侪才算「对」。这才是能力的体现。
     */
    excess?: {
        effective_n: number
        accuracy_pct?: number | null
        ci_low_pct?: number | null
        ci_high_pct?: number | null
        days_measured?: number
        days_without_peers?: number
        benchmark?: string
        note?: string
        verdict?: 'beats_coin_flip' | 'not_significant' | 'worse_than_coin_flip'
        not_significant?: boolean
        clustered?: {
            accuracy_pct?: number | null
            se_pct?: number | null
            t?: number | null
            ci_low_pct?: number | null
            ci_high_pct?: number | null
            n_days?: number
        }
    }
    /**
     * 多空价差：看多标的的平均超额 − 看空标的的平均超额。
     * 单位是**百分点/日**（与交易成本可比）。`covers_cost=false` 表示这点价差
     * 不足以覆盖一个来回约 0.25% 的成本 —— 即不可交易。
     */
    excess_spread?: {
        unit?: string
        bullish_n?: number
        bearish_n?: number
        bullish_mean_excess_pct?: number | null
        bearish_mean_excess_pct?: number | null
        long_short_spread_pct?: number | null
        cost_round_trip_pct?: number
        covers_cost?: boolean
    }
    /** A5/D5：本摘要衡量的是哪个周期。恒为 `'t1'`（次日方向）。 */
    horizon?: 't1'
    /**
     * F1 期限错配：被评报告所附交易计划**自己**的持有期。
     *
     * 本命中率只评次日方向；计划里的目标价/止损/时间止损属于更长期限，其达成情况
     * 不在本指标内。`multi_day_plan_windows` 越大，说明越多窗口承载的是中期方案。
     * `windows_with_unrecorded_plan_horizon` 是「附有计划但该窗口在新增本列之前
     * 就已打分」的数量——刻意不回填，以免把猜出来的持有期当成记录值。
     */
    plan_horizon?: {
        measured_horizon?: string
        unit?: string
        windows_with_plan?: number
        windows_without_plan?: number
        windows_with_unrecorded_plan_horizon?: number
        median_plan_days?: number | null
        multi_day_plan_windows?: number
        /** 被评窗口在 `report_t1_outcomes.horizon` 上的实际取值分布（含 `null`）。 */
        scored_horizon_values?: Record<string, number>
        /** 除 t1/null 以外的取值；非空即表示口径声明已被破坏。 */
        foreign_horizon_values?: string[]
        /** 被评报告自身的周期分布（`reports.horizon`），`dual` = 当时给了两套结论。 */
        graded_report_horizon_values?: Record<string, number>
        note?: string
    }
    note?: string
}

/** 多模型同日共识：按信号日聚合的看涨/看跌正确率 */
export interface InsightsT1MultiModelConsensusPoint {
    date: string
    t1_trade_date?: string | null
    unanimous_bullish_count: number
    unanimous_bullish_accuracy_pct?: number | null
    unanimous_bearish_count: number
    unanimous_bearish_accuracy_pct?: number | null
}

export interface InsightsT1MultiModelConsensusModel {
    model_key: string
    model_profile_id?: string | null
    model_profile_name?: string | null
    model_display_name?: string | null
    llm_provider?: string | null
    quick_think_llm?: string | null
    deep_think_llm?: string | null
    report_id?: string | null
    direction_bucket?: string | null
    report_created_at?: string | null
}

export interface InsightsT1MultiModelConsensusDetail {
    symbol: string
    name?: string | null
    signal_trade_date: string
    t1_trade_date?: string | null
    consensus_direction: 'bullish' | 'bearish' | string
    model_count: number
    models: InsightsT1MultiModelConsensusModel[]
    return_t1_pct?: number | null
    label_correct?: boolean | null
    p0?: number | null
    p1?: number | null
}

/** T+1 推荐质量明细：单个信号日的每只股票 */
export interface InsightsT1RecommendationDetail {
    id: string
    symbol: string
    name?: string | null
    score?: number | null
    rank?: number | null
    t1_signal_date: string
    t1_trade_date?: string | null
    t1_return_pct?: number | null
    entry_price?: number | null
    source_mode?: string | null
    /** 扫描生成时间（CST，精确到分钟） */
    scan_created_at?: string | null
}

/** T+1 报告方向正确率明细：单个信号日的每只股票 */
export interface InsightsT1ReportDetail {
    symbol: string
    name?: string | null
    signal_trade_date: string
    t1_trade_date?: string | null
    /** 报告原始 trade_date 字段（报告标注的交易日） */
    report_trade_date?: string | null
    /** 报告生成时间（CST，精确到分钟） */
    report_created_at?: string | null
    p0?: number | null
    p1?: number | null
    return_t1_pct?: number | null
    direction_bucket?: string | null
    label_correct?: boolean | null
    direction?: string | null
    decision?: string | null
    report_id?: string | null
    /** pending | insufficient_data | evaluated */
    evaluation_status?: string | null
    evaluation_reason?: string | null
    model_profile_id?: string | null
    model_profile_name?: string | null
    llm_provider?: string | null
    quick_think_llm?: string | null
    deep_think_llm?: string | null
}

export interface ModelArenaLeaderboardItem {
    rank: number
    model_key: string
    model_profile_id?: string | null
    model_profile_name?: string | null
    model_display_name: string
    llm_provider?: string | null
    quick_think_llm?: string | null
    deep_think_llm?: string | null
    backend_url?: string | null
    sample_count: number
    symbol_count: number
    date_count: number
    accuracy_pct: number
    win_rate_pct?: number | null
    avg_return_t1_pct?: number | null
    return_volatility: number
    composite_score: number
}

export interface ModelArenaLeaderboardResponse {
    window_start: string
    window_end: string
    scope: 'portfolio' | 'all'
    total_samples: number
    generated_at: string
    leaderboard: ModelArenaLeaderboardItem[]
}

export interface ModelArenaSymbolCompareRow {
    signal_trade_date: string
    t1_trade_date?: string | null
    report_id?: string | null
    symbol: string
    return_t1_pct?: number | null
    label_correct: boolean
    direction_bucket?: string | null
    decision?: string | null
    direction?: string | null
    model_key: string
    model_profile_id?: string | null
    model_profile_name?: string | null
    model_display_name: string
    llm_provider?: string | null
    quick_think_llm?: string | null
    deep_think_llm?: string | null
}

export interface ModelArenaSymbolCompareResponse {
    symbol: string
    window_start: string
    window_end: string
    scope: 'portfolio' | 'all'
    rows: ModelArenaSymbolCompareRow[]
    by_date: Record<string, ModelArenaSymbolCompareRow[]>
    model_summaries: Array<{
        model_key: string
        model_profile_id?: string | null
        model_profile_name?: string | null
        model_display_name: string
        llm_provider?: string | null
        quick_think_llm?: string | null
        deep_think_llm?: string | null
        sample_count: number
        accuracy_pct?: number | null
        avg_return_t1_pct?: number | null
    }>
}

export interface ModelArenaPromoteRequest {
    profile_id: string
    lookback_days?: number
    scope?: 'portfolio' | 'all'
    min_samples?: number
    min_accuracy_improvement_pct?: number
    max_return_drop_pct?: number
    force?: boolean
}

export interface ModelArenaRunRequest {
    symbol: string
    trade_date?: string
    model_profile_ids?: string[]
    selected_analysts?: string[]
    horizons?: string[]
    query?: string
    dry_run?: boolean
    prompt_template_id?: string
    prompt_vars?: Record<string, unknown>
    config_overrides?: Record<string, unknown>
}

export interface ModelArenaRunJob {
    model_profile_id?: string | null
    model_profile_name?: string | null
    job_id: string
    symbol: string
    trade_date: string
    status: 'pending' | 'running' | 'completed' | 'failed'
    created_at: string
}

export interface ModelArenaRunResponse {
    experiment_id: string
    input_snapshot_hash: string
    symbol: string
    trade_date: string
    jobs: ModelArenaRunJob[]
}

export interface ModelArenaDriftAlert {
    model_key: string
    model_profile_id?: string | null
    model_profile_name?: string | null
    model_display_name?: string | null
    baseline_accuracy_pct: number
    recent_accuracy_pct: number
    drop_pct: number
    recent_sample_count: number
    baseline_sample_count: number
    recent_window_days: number
    baseline_window_days: number
}

export interface ModelArenaDriftAlertResponse {
    scope: 'portfolio' | 'all'
    window_start: string
    window_end: string
    alerts: ModelArenaDriftAlert[]
}

export interface ModelArenaModelDetailRow {
    signal_trade_date: string
    t1_trade_date?: string | null
    symbol: string
    name?: string | null
    report_id?: string | null
    report_created_at?: string | null
    decision?: string | null
    direction?: string | null
    direction_bucket?: string | null
    label_correct: boolean
    return_t1_pct?: number | null
    p0?: number | null
    p1?: number | null
    calendar_gap_days?: number | null
    date_drift_flag?: boolean
    model_key: string
    model_profile_id?: string | null
    model_profile_name?: string | null
    model_display_name: string
    llm_provider?: string | null
    quick_think_llm?: string | null
    deep_think_llm?: string | null
}

export interface ModelArenaModelDetailResponse {
    window_start: string
    window_end: string
    scope: 'portfolio' | 'all'
    model_profile_id?: string | null
    model_key?: string | null
    total_samples: number
    accuracy_pct?: number | null
    drift_warning_count?: number
    rows: ModelArenaModelDetailRow[]
    drift_rows?: ModelArenaModelDetailRow[]
}

export interface ModelArenaModelTrendPoint {
    date: string
    accuracy_pct?: number | null
    sample_count: number
}

export interface ModelArenaModelTrendSeries {
    model_key: string
    model_profile_id?: string | null
    model_profile_name?: string | null
    model_display_name: string
    quick_think_llm?: string | null
    deep_think_llm?: string | null
    total_samples: number
    points: ModelArenaModelTrendPoint[]
}

export interface ModelArenaModelTrendResponse {
    window_start: string
    window_end: string
    scope: 'portfolio' | 'all'
    days: string[]
    series: ModelArenaModelTrendSeries[]
}

export interface RecommendationStrategyStat {
    strategy_key: string
    factor_bucket: string
    sample_count: number
    win_rate?: number | null
    avg_return_pct?: number | null
    avg_score?: number | null
    weight_delta?: number | null
    last_entry_at?: string | null
    updated_at?: string | null
}

export interface RecommendationEvalGroupSummary {
    sample_count: number
    hit_at_k_pct?: number | null
    avg_return_pct?: number | null
    avg_excess_return_pct?: number | null
    max_drawdown_p95_pct?: number | null
}

export interface RecommendationEvalRun {
    run_id: string
    status: 'running' | 'completed' | 'failed'
    market: 'cn' | 'us'
    source_mode: 'user_pool' | 'market_scan'
    baseline_profile: string
    variant_profile: string
    lookback_days: number
    top_k: number
    benchmark_symbol: string
    summary: {
        groups: {
            baseline: RecommendationEvalGroupSummary
            variant: RecommendationEvalGroupSummary
        }
    }
    gate?: {
        allow_switch_default: boolean
        reasons: string[]
        thresholds?: Record<string, number>
        diff?: Record<string, number | null>
        samples?: Record<string, number>
    }
    error?: string | null
    created_at?: string | null
    evaluated_at?: string | null
}

// Runtime config
export interface RuntimeConfig {
    llm_provider: string
    deep_think_llm: string
    quick_think_llm: string
    backend_url: string
    max_debate_rounds: number
    max_risk_discuss_rounds: number
    decision_critic_enabled?: boolean
    decision_critic_revision_threshold?: number
    has_api_key?: boolean
    has_wecom_webhook?: boolean
    wecom_webhook_display?: string | null
    has_wps_webhook?: boolean
    wps_webhook_display?: string | null
    server_fallback_enabled?: boolean
    email_report_enabled?: boolean
    wecom_report_enabled?: boolean
    wps_report_enabled?: boolean
    methodology_ashare_fundamentals?: boolean
    methodology_ashare_news_events?: boolean
    methodology_ashare_sector_macro?: boolean
    methodology_stock_analysis_team?: boolean
    methodology_extra_path?: string | null
    methodology_extra_path_news?: string | null
    methodology_extra_path_macro?: string | null
    finskills_root?: string | null
    stock_analysis_team_root?: string | null
}

export interface RuntimeConfigUpdateResponse {
    message: string
    applied: RuntimeConfigUpdate
    has_api_key: boolean
    current: RuntimeConfig
    warmup?: RuntimeConfigWarmup
}

export interface RuntimeConfigUpdate {
    llm_provider?: string
    deep_think_llm?: string
    quick_think_llm?: string
    backend_url?: string
    max_debate_rounds?: number
    max_risk_discuss_rounds?: number
    decision_critic_enabled?: boolean
    decision_critic_revision_threshold?: number
    api_key?: string
    wecom_webhook_url?: string
    wps_webhook_url?: string
    clear_api_key?: boolean
    clear_wecom_webhook?: boolean
    clear_wps_webhook?: boolean
    email_report_enabled?: boolean
    wecom_report_enabled?: boolean
    wps_report_enabled?: boolean
    methodology_ashare_fundamentals?: boolean
    methodology_ashare_news_events?: boolean
    methodology_ashare_sector_macro?: boolean
    methodology_stock_analysis_team?: boolean
    methodology_extra_path?: string
    methodology_extra_path_news?: string
    methodology_extra_path_macro?: string
    finskills_root?: string
    stock_analysis_team_root?: string
    warmup?: boolean
    force_warmup?: boolean
}

export interface RuntimeWarmupRequest extends RuntimeConfigUpdate {
    prompt?: string
}

export interface RuntimeConfigWarmup {
    requested: boolean
    triggered: boolean
    status: 'scheduled' | 'skipped' | 'disabled'
    message: string
    models?: string[]
}

export interface RuntimeWarmupResult {
    model: string
    targets: string[]
    content?: string | null
    error?: string | null
}

export interface RuntimeWarmupResponse {
    prompt: string
    results: RuntimeWarmupResult[]
}

export interface WecomWarmupRequest {
    wecom_webhook_url?: string
    content?: string
}

export interface WecomWarmupResponse {
    sent: boolean
    message: string
    webhook_display?: string | null
}

export interface WpsWarmupRequest {
    wps_webhook_url?: string
    content?: string
}

export interface WpsWarmupResponse {
    sent: boolean
    message: string
    webhook_display?: string | null
}

export interface AuthUser {
    id: string
    email: string
    created_at?: string
    last_login_at?: string
}

export interface AuthVerifyResponse {
    access_token: string
    token_type: string
    user: AuthUser
}

export interface UserToken {
    id: string
    name: string
    token?: string
    token_hint?: string
    last_used_at?: string
    created_at: string
}

export interface UserTokenCreateRequest {
    name: string
}

// Feedback types
export interface FeedbackItem {
    id: string
    user_email: string
    subject: string
    content: string
    admin_reply?: string | null
    replied_at?: string | null
    is_read: boolean
    created_at?: string
    updated_at?: string
}

export interface FeedbackListResponse {
    total: number
    feedbacks: FeedbackItem[]
}

export interface FeedbackUnreadResponse {
    unread_count: number
}

// Debate message (for battle view)
export interface DebateMessage {
    debate: 'research' | 'risk'
    agent: string
    round: number        // -1 = verdict
    content: string
    isVerdict?: boolean
    horizon?: string
}

// ── 市场主线（M5） ───────────────────────────────────────────────
export interface MainlineBoard {
    board: string
    chg_1d?: number | null
}

export interface Mainline {
    name: string
    type?: 'concept' | 'industry'
    phase?: string
    confidence: number
    status_vs_yesterday?: string
    logic?: string
    drivers?: string[]
    representative_boards?: MainlineBoard[]
    leading_stocks?: string[]
    verify_conditions?: string[]
    risks?: string[]
    evidence?: string
}

export interface MainlineCandidate {
    id?: string
    symbol: string
    name: string
    mainline?: string
    tier?: string
    score?: number
    reasons?: string[]
    entry_hint?: string
    risk?: string
}

export interface MainlineReport {
    id: string
    user_id?: string
    trade_date: string
    perspective: string
    status: 'pending' | 'running' | 'completed' | 'failed'
    error?: string | null
    summary?: string | null
    mainlines?: Mainline[]
    candidates?: MainlineCandidate[]
    analyst_report?: string | null
    selector_report?: string | null
    gated_out?: string[]
    market_snapshot?: {
        sources?: Record<string, string | null>
        emotion?: { temperature?: number | null; regime?: string; gate?: string; gate_reason?: string } | null
        breadth?: { up?: number; down?: number; flat?: number; total?: number; approximate?: boolean } | null
        benchmark?: { close?: number | null; chg_1d?: number | null; chg_5d?: number | null } | null
        industry_spot_top10?: Array<Record<string, unknown>>
        concept_spot_top10?: Array<Record<string, unknown>>
        zt_heat_top10?: Array<Record<string, unknown>>
        rule_candidates?: MainlineRuleCandidate[]
        progress?: { phase?: string; percent?: number; detail?: string; updated_at?: string } | null
        progress_logs?: Array<{ at?: string; phase?: string; message: string }>
    } | null
    warnings?: string[]
    job_id?: string | null
    created_at?: string
    finished_at?: string | null
}

export interface MainlineAnalyzeRequest {
    trade_date?: string
    perspective?: 'short' | 'medium'
    user_focus?: string | null
    yesterday_mainlines?: Mainline[] | null
}

export interface MainlineAnalyzeResponse {
    job_id: string
    status: string
    created_at: string
}

export interface MainlineRunListResponse {
    runs: MainlineReport[]
}

export interface MainlineBoardSpotResponse {
    trade_date: string
    type: string
    source?: string | null
    boards: Array<Record<string, unknown>>
    warnings: string[]
}

export interface MainlineT1Outcome {
    id?: string
    report_id: string
    mainline?: string
    trade_date?: string
    check_date?: string
    board?: string
    board_fwd_ret?: number | null
    benchmark_fwd_ret?: number | null
    excess_ret?: number | null
    outcome?: string
    notes?: string
    created_at?: string
}

export interface MainlineT1Overview {
    total: number
    by_outcome: Record<string, number>
    avg_excess_ret?: number | null
    hit_rate?: number | null
    days?: number
}

export interface MainlineRuleCandidate {
    name: string
    sector_type?: string
    chg_1d?: number | null
    rs20?: number | null
    ma_bullish?: boolean
    rsi14?: number | null
    passes_gate?: boolean
    pulse?: boolean
    phase_hint?: string
    heat_score?: number
    strength_score?: number
    composite_score?: number
    inflow_persistent_10?: boolean
    flow_data_missing?: boolean
    leader?: string
}

export interface MainlineBacktestRow {
    v1?: { top?: { mean?: number; hit_rate?: number }; edge_top_minus_bottom?: number; n_days?: number }
    v2?: { top?: { mean?: number; hit_rate?: number }; edge_top_minus_bottom?: number; n_days?: number }
}

export interface MainlineBacktestResult {
    found: boolean
    message?: string
    file?: string
    top_n?: number
    comparison?: Record<string, MainlineBacktestRow>
    config?: Record<string, unknown>
}

// 补充：市场宽度近似标记（新浪不可用时由 THS 行业聚合近似）
// breadth 类型内已有 approximate?: boolean 注入说明见下

// ── 主线周期分析（C2-C5） ──────────────────────────────────────
export interface MainlineCycle {
    id?: string
    mainline_key: string
    representative_boards?: string[]
    first_date?: string
    last_date?: string
    status?: string
    cycle_position?: string
    cycle_reason?: string
    progress?: number | null
    peak_strength?: number | null
    peak_gap?: number | null
    alerts?: string[]
    action?: string
    position_pct?: number | null
    daily_track?: Array<Record<string, unknown>>
    updated_at?: string
}

export interface MainlineDecision {
    id?: string
    report_id?: string
    mainline_key: string
    trade_date?: string
    stage?: string
    action?: string
    position_pct?: number | null
    reason?: string
    verify_conditions?: string[]
    outcome?: string
    outcome_note?: string
    forward_excess_ret?: number | null
}

export interface MainlineTradeCandidate {
    id?: string
    report_id?: string
    mainline_key?: string
    symbol?: string
    name?: string
    tier?: string
    score?: number | null
    buyable?: boolean
    timing?: string
    position_tier?: string
    deep_dive_job_id?: string | null
    deep_dive_status?: string
    deep_dive_report_id?: string | null
    deep_dive_error?: string | null
}

export interface MainlineCapability {
    evaluated: number
    hit_rate?: number | null
    verified: number
    falsified: number
    by_stage?: Record<string, { verified: number; falsified: number; total: number; hit_rate?: number | null }>
    days?: number
}

export interface MainlineRotationDay {
    date: string
    mainlines: Array<{ key: string; stage?: string; action?: string }>
}

export interface MainlineDailyLog {
    trade_date: string
    report_id?: string | null
    report_status?: string
    mainlines: Array<{ key: string; stage?: string; action?: string; position_pct?: number | null; reason?: string }>
    retreat_alerts: Array<{ key: string; alerts: string[]; stage?: string }>
    buyable: Array<{ symbol: string; name: string; tier?: string; timing?: string; deep_dive_status?: string; deep_dive_report_id?: string | null }>
    deep_dive_pending: number
}

// ── 交易计划与退出纪律（P0-2 / P0-3）────────────────────────────────────────
// 过去"何时卖、卖多少"只以散文形式存在于研报正文，产品既不结构化也不监控，
// 唯一的卖出逻辑是虚拟盘里写死的 -6%/+10%。这些类型让计划成为可展示、
// 可监控、可复盘的显式对象。

export interface TradePlan {
    id?: string
    report_id?: string
    symbol: string
    name?: string | null
    signal_trade_date?: string | null
    horizon?: string | null
    horizon_days?: number | null
    direction?: string | null
    entry_low?: number | null
    entry_high?: number | null
    position_cap_pct?: number | null
    first_tranche_pct?: number | null
    hard_stop_price?: number | null
    /** 分批止盈阶梯：[价格, 减仓百分比] */
    take_profit_ladder?: Array<[number, number]>
    trailing_stop_pct?: number | null
    time_stop_days?: number | null
    invalidation_conditions?: string[]
    de_risk_triggers?: string[]
    execution_preconditions?: string[]
    hard_constraints?: string[]
    risk_gate?: string | null
    confidence?: number | null
    status?: string | null
    source?: string | null
    parse_warnings?: string[]
    created_at?: string | null
    monitorable?: boolean
}

export interface ExitAdviceDecision {
    symbol: string
    name?: string
    /** 持有 / 减仓 / 清仓 / 观察 */
    action: string
    /** high / medium / low */
    priority: string
    reasons: string[]
    triggers: string[]
    suggested_shares: number
    suggested_pct: number
    price?: number | null
    previous_close?: number | null
    pnl_pct?: number | null
    position: number
    available_position: number
    average_cost?: number | null
    constraints: string[]
    plan_id?: string | null
    plan_source?: string | null
    plan_available: boolean
    /** 免责声明：仅为纪律提示，不构成投资建议 */
    label: string
}

export interface ExitAdviceResponse {
    summary: {
        total: number
        清仓?: number
        减仓?: number
        持有?: number
        观察?: number
        high_priority?: number
        with_plan?: number
        without_plan?: number
        label?: string
    }
    decisions: ExitAdviceDecision[]
}

import type { AnalysisRequest, AnalysisResponse, Announcement, AuthUser, AuthVerifyResponse, JobStatus, AnalysisReport, KlineResponse, LatestAnnouncementResponse, PortfolioImportState, PortfolioOverviewResponse, PortfolioPositionInput, RecommendationRequest, RecommendationResponse, DailyProductRunRequest, DailyProductRunResponse, RecommendationHistoryItem, RecommendationStrategyStat, RecommendationEvalRun, InsightsT1RecommendationPoint, InsightsT1ReportAccuracyPoint, InsightsT1ReportAccuracySummary, InsightsT1MultiModelConsensusPoint, InsightsT1MultiModelConsensusDetail, InsightsT1RecommendationDetail, InsightsT1ReportDetail, ModelArenaLeaderboardResponse, ModelArenaSymbolCompareResponse, ModelArenaPromoteRequest, ModelArenaRunRequest, ModelArenaRunResponse, ModelArenaDriftAlertResponse, ModelArenaModelDetailResponse, ModelArenaModelTrendResponse, Report, ReportDetail, ReportListResponse, RuntimeConfig, RuntimeConfigUpdate, RuntimeConfigUpdateResponse, RuntimeWarmupRequest, RuntimeWarmupResponse, WatchlistItem, WatchlistBatchResponse, ScheduledAnalysis, ScheduledBatchTriggerResponse, StockSearchResult, TrackingBoardResponse, UserToken, UserTokenCreateRequest, WecomWarmupRequest, WecomWarmupResponse, WpsWarmupRequest, WpsWarmupResponse, FeedbackItem, FeedbackListResponse, FeedbackUnreadResponse, PaperPortfolioSnapshot, PaperPortfolioBootstrapRequest, PaperTradeRequest as PaperTradePayload, DailyOperationRequest as DailyOperationPayload, DailyOperationResult, PaperDailyReviewSummary, PromptTemplate, ModelProfile, ModelProfileCreateRequest, ModelProfileUpdateRequest, ModelProfileWarmupResponse, TradePlan, ExitAdviceResponse } from '@/types'

export function getBaseUrl(): string {
    const envUrl = (import.meta.env.VITE_API_URL as string) || ''
    if (envUrl) return envUrl.replace(/\/$/, '')
    if (typeof window !== 'undefined' && window.location?.origin) {
        return window.location.origin.replace(/\/$/, '')
    }
    return 'http://127.0.0.1:8000'
}


function getAuthToken(): string | null {
    try {
        return localStorage.getItem('ta-access-token')
    } catch {
        return null
    }
}

class ApiService {
    private async request<T>(endpoint: string, options?: RequestInit, timeoutMs = 30000): Promise<T> {
        const url = `${getBaseUrl()}${endpoint}`
        const token = getAuthToken()
        const controller = new AbortController()
        const timer = window.setTimeout(() => controller.abort(), timeoutMs)
        let response: Response
        try {
            response = await fetch(url, {
                ...options,
                signal: controller.signal,
                headers: {
                    'Content-Type': 'application/json',
                    ...(token ? { Authorization: `Bearer ${token}` } : {}),
                    ...options?.headers,
                },
            })
        } catch (err) {
            // 把 "Failed to fetch" 等网络错误转成可读中文（后端未启动/负载高/超时）
            const reason = err instanceof Error && err.name === 'AbortError'
                ? `请求超时（${Math.round(timeoutMs / 1000)}s）`
                : '无法连接后端服务器'
            throw new Error(`${reason}：${endpoint}。请确认后端已启动（./scripts/dev.sh start 或单独启动 uvicorn）后重试`)
        } finally {
            window.clearTimeout(timer)
        }

        if (!response.ok) {
            const contentType = response.headers.get('content-type') || ''
            if (contentType.includes('application/json')) {
                const data = await response.json().catch(() => null)
                const detail = data?.detail || data?.message
                throw new Error(detail || `HTTP error! status: ${response.status}`)
            }
            const error = await response.text()
            throw new Error(error || `HTTP error! status: ${response.status}`)
        }

        if (response.status === 204 || response.status === 205) {
            return undefined as T
        }

        const contentType = response.headers.get('content-type') || ''
        if (!contentType.includes('application/json')) {
            const text = await response.text()
            return (text ? (text as T) : undefined) as T
        }

        const raw = await response.text()
        if (!raw) {
            return undefined as T
        }

        return JSON.parse(raw) as T
    }

    async startAnalysis(request: AnalysisRequest): Promise<AnalysisResponse> {
        return this.request<AnalysisResponse>('/v1/analyze', {
            method: 'POST',
            body: JSON.stringify(request),
        })
    }

    async listPromptTemplates(scope = 'deep_analysis'): Promise<{ templates: PromptTemplate[]; defaults: Record<string, string> }> {
        return this.request(`/v1/prompt-templates?scope=${encodeURIComponent(scope)}`)
    }

    async createPromptTemplate(payload: {
        scope?: string
        name: string
        description?: string
        template_text: string
        intent_json?: Record<string, unknown>
        is_active?: boolean
    }): Promise<PromptTemplate> {
        return this.request('/v1/prompt-templates', {
            method: 'POST',
            body: JSON.stringify(payload),
        })
    }

    async updatePromptTemplate(
        templateId: string,
        payload: {
            name?: string
            description?: string
            template_text?: string
            intent_json?: Record<string, unknown>
            is_active?: boolean
        },
    ): Promise<PromptTemplate> {
        return this.request(`/v1/prompt-templates/${encodeURIComponent(templateId)}`, {
            method: 'PATCH',
            body: JSON.stringify(payload),
        })
    }

    async getJobStatus(jobId: string): Promise<JobStatus> {
        return this.request<JobStatus>(`/v1/jobs/${jobId}`)
    }

    async getJobResult(jobId: string): Promise<{ job_id: string; status: string; decision: string; result: AnalysisReport }> {
        return this.request(`/v1/jobs/${jobId}/result`)
    }

    async streamJobEvents(jobId: string, signal?: AbortSignal): Promise<Response> {
        const token = getAuthToken()
        const response = await fetch(`${getBaseUrl()}/v1/jobs/${jobId}/events`, {
            headers: {
                Accept: 'text/event-stream',
                ...(token ? { Authorization: `Bearer ${token}` } : {}),
            },
            signal,
        })
        if (!response.ok) {
            throw new Error(`任务事件流连接失败（HTTP ${response.status}）`)
        }
        return response
    }

    async getKline(symbol: string, startDate?: string, endDate?: string): Promise<KlineResponse> {
        const params = new URLSearchParams({ symbol })
        if (startDate) params.append('start_date', startDate)
        if (endDate) params.append('end_date', endDate)
        return this.request<KlineResponse>(`/v1/market/kline?${params}`)
    }

    async chatCompletion(
        messages: Array<{ role: string; content: string }>,
        stream = true,
        selectedAnalysts?: string[],
        modelProfileId?: string,
    ) {
        const response = await fetch(`${getBaseUrl()}/v1/chat/completions`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                ...(getAuthToken() ? { Authorization: `Bearer ${getAuthToken()}` } : {}),
            },
            body: JSON.stringify({
                messages,
                stream,
                selected_analysts: selectedAnalysts,
                model_profile_id: modelProfileId,
            }),
        })

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`)
        }

        return response
    }

    // Report API Methods
    async getReports(
        symbol?: string,
        skip = 0,
        limit = 100,
        search?: string,
        startDate?: string,
        endDate?: string,
        modelProfileId?: string,
        freshnessIssue?: boolean,
    ): Promise<ReportListResponse> {
        const params = new URLSearchParams()
        if (symbol) params.append('symbol', symbol)
        const q = (search ?? '').trim()
        if (q) params.append('search', q)
        if (startDate) params.append('start_date', startDate)
        if (endDate) params.append('end_date', endDate)
        if (modelProfileId) params.append('model_profile_id', modelProfileId)
        if (freshnessIssue) params.append('freshness_issue', 'true')
        params.append('skip', skip.toString())
        params.append('limit', limit.toString())
        return this.request<ReportListResponse>(`/v1/reports?${params}`)
    }

    async getLatestReportsBySymbols(symbols: string[]): Promise<{ reports: Report[] }> {
        return this.request<{ reports: Report[] }>('/v1/reports/latest-by-symbols', {
            method: 'POST',
            body: JSON.stringify({ symbols }),
        })
    }

    async getReport(reportId: string): Promise<ReportDetail> {
        return this.request<ReportDetail>(`/v1/reports/${reportId}`)
    }

    async getLatestAnnouncement(): Promise<Announcement | null> {
        const data = await this.request<LatestAnnouncementResponse>('/v1/announcements/latest')
        return data.announcement
    }

    async deleteReport(reportId: string): Promise<{ message: string }> {
        return this.request<{ message: string }>(`/v1/reports/${reportId}`, {
            method: 'DELETE',
        })
    }


    async createReport(report: {
        symbol: string
        trade_date: string
        decision?: string
        result_data?: AnalysisReport
    }): Promise<Report> {
        return this.request<Report>('/v1/reports', {
            method: 'POST',
            body: JSON.stringify(report),
        })
    }

    // Watchlist
    async getWatchlist(): Promise<{ items: WatchlistItem[] }> {
        return this.request<{ items: WatchlistItem[] }>('/v1/watchlist')
    }
    async addToWatchlist(input: string): Promise<WatchlistBatchResponse> {
        return this.request<WatchlistBatchResponse>('/v1/watchlist', {
            method: 'POST',
            body: JSON.stringify({ text: input }),
        })
    }
    async removeFromWatchlist(id: string): Promise<void> {
        await this.request('/v1/watchlist/' + id, { method: 'DELETE' })
    }
    async deleteWatchlistBatch(item_ids: string[]): Promise<{
        deleted_ids: string[]
        deleted_symbols: string[]
        missing_ids: string[]
    }> {
        return this.request('/v1/watchlist/batch/delete', {
            method: 'POST',
            body: JSON.stringify({ item_ids }),
        })
    }

    // Scheduled Analysis
    async getScheduled(): Promise<{ items: ScheduledAnalysis[] }> {
        return this.request<{ items: ScheduledAnalysis[] }>('/v1/scheduled')
    }
    async getPortfolioOverview(): Promise<PortfolioOverviewResponse> {
        return this.request<PortfolioOverviewResponse>('/v1/portfolio/overview')
    }
    async createScheduled(
        symbol: string,
        horizon?: string,
        trigger_time?: string,
        prompt_template_id?: string,
        prompt_vars?: Record<string, unknown>,
    ): Promise<ScheduledAnalysis> {
        return this.request<ScheduledAnalysis>('/v1/scheduled', {
            method: 'POST',
            body: JSON.stringify({ symbol, horizon, trigger_time, prompt_template_id, prompt_vars }),
        })
    }
    async updateScheduled(
        id: string,
        data: {
            is_active?: boolean
            horizon?: string
            trigger_time?: string
            prompt_template_id?: string
            prompt_vars?: Record<string, unknown>
        },
    ): Promise<ScheduledAnalysis> {
        return this.request<ScheduledAnalysis>('/v1/scheduled/' + id, {
            method: 'PATCH',
            body: JSON.stringify(data),
        })
    }
    async updateScheduledBatch(
        item_ids: string[],
        data: {
            is_active?: boolean
            horizon?: string
            trigger_time?: string
            prompt_template_id?: string
            prompt_vars?: Record<string, unknown>
        }
    ): Promise<{ items: ScheduledAnalysis[] }> {
        return this.request<{ items: ScheduledAnalysis[] }>('/v1/scheduled/batch', {
            method: 'PATCH',
            body: JSON.stringify({ item_ids, ...data }),
        })
    }
    async deleteScheduled(id: string): Promise<void> {
        await this.request('/v1/scheduled/' + id, { method: 'DELETE' })
    }
    async deleteScheduledBatch(item_ids: string[]): Promise<{ deleted_ids: string[]; missing_ids: string[] }> {
        return this.request<{ deleted_ids: string[]; missing_ids: string[] }>('/v1/scheduled/batch/delete', {
            method: 'POST',
            body: JSON.stringify({ item_ids }),
        })
    }
    async ensureScheduledBatch(
        symbols: string[],
        opts?: {
            horizon?: string
            trigger_time?: string
            prompt_template_id?: string
            prompt_vars?: Record<string, unknown>
        },
    ): Promise<{ created: string[]; existing: string[]; skipped_limit: string[] }> {
        return this.request('/v1/scheduled/batch/ensure', {
            method: 'POST',
            body: JSON.stringify({
                symbols,
                horizon: opts?.horizon ?? 'short',
                trigger_time: opts?.trigger_time ?? '20:00',
                prompt_template_id: opts?.prompt_template_id,
                prompt_vars: opts?.prompt_vars,
            }),
        })
    }
    async triggerScheduledTest(id: string): Promise<AnalysisResponse> {
        return this.request<AnalysisResponse>(`/v1/scheduled/${id}/trigger`, {
            method: 'POST',
        })
    }
    async triggerScheduledBatch(item_ids: string[]): Promise<ScheduledBatchTriggerResponse> {
        return this.request<ScheduledBatchTriggerResponse>('/v1/scheduled/batch/trigger', {
            method: 'POST',
            body: JSON.stringify({ item_ids }),
        })
    }

    async getPortfolioImportState(): Promise<PortfolioImportState> {
        return this.request<PortfolioImportState>('/v1/portfolio/imports')
    }

    /** 某标的当前生效的交易计划（何时卖、卖多少的书面依据）。 */
    async getTradePlans(symbol?: string): Promise<{ total: number; plans: TradePlan[] }> {
        const qs = symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''
        return this.request<{ total: number; plans: TradePlan[] }>(`/v1/trade-plans${qs}`)
    }

    async getTradePlan(symbol: string): Promise<{ plan: TradePlan }> {
        return this.request<{ plan: TradePlan }>(`/v1/trade-plans/${encodeURIComponent(symbol)}`)
    }

    /** 按每份研报自己的交易计划给出持有/减仓/清仓的纪律化提示。 */
    async getExitAdvice(symbol?: string): Promise<ExitAdviceResponse> {
        const qs = symbol ? `?symbol=${encodeURIComponent(symbol)}` : ''
        return this.request<ExitAdviceResponse>(`/v1/exit-advice${qs}`, undefined, 60000)
    }

    async syncPortfolioImport(data: {
        positions: PortfolioPositionInput[]
        source?: string
        auto_apply_scheduled: boolean
    }): Promise<PortfolioImportState> {
        return this.request<PortfolioImportState>('/v1/portfolio/imports', {
            method: 'POST',
            body: JSON.stringify(data),
        })
    }

    /** 合并导入：追加单只或多只标的，不覆盖未出现在列表中的已有持仓（按 source，默认 manual） */
    async mergePortfolioImport(data: {
        positions: PortfolioPositionInput[]
        source?: string
        auto_apply_scheduled: boolean
    }): Promise<PortfolioImportState> {
        return this.request<PortfolioImportState>('/v1/portfolio/imports/merge', {
            method: 'POST',
            body: JSON.stringify(data),
        })
    }

    async clearPortfolioImport(): Promise<void> {
        await this.request('/v1/portfolio/imports', { method: 'DELETE' })
    }

    async deletePortfolioImportPosition(symbol: string): Promise<{
        symbol: string
        deleted_positions: number
        scheduled_removed: boolean
    }> {
        const q = encodeURIComponent(symbol)
        return this.request(`/v1/portfolio/imports/position?symbol=${q}`, { method: 'DELETE' })
    }

    async parsePositionImage(file: File): Promise<{ positions: PortfolioPositionInput[] }> {
        const formData = new FormData()
        formData.append('file', file)
        const url = `${getBaseUrl()}/v1/portfolio/parse-image`
        const token = getAuthToken()
        const response = await fetch(url, {
            method: 'POST',
            headers: {
                ...(token ? { Authorization: `Bearer ${token}` } : {}),
            },
            body: formData,
        })
        if (!response.ok) {
            const error = await response.json().catch(() => ({ detail: response.statusText }))
            throw new Error(error.detail || '图片解析失败')
        }
        return response.json()
    }

    async getDashboardTrackingBoard(): Promise<TrackingBoardResponse> {
        return this.request<TrackingBoardResponse>('/v1/dashboard/tracking-board')
    }

    async getPaperPortfolio(): Promise<PaperPortfolioSnapshot> {
        return this.request<PaperPortfolioSnapshot>('/v1/paper-portfolio')
    }

    async bootstrapPaperPortfolio(payload: PaperPortfolioBootstrapRequest): Promise<PaperPortfolioSnapshot> {
        return this.request<PaperPortfolioSnapshot>('/v1/paper-portfolio/bootstrap', {
            method: 'POST',
            body: JSON.stringify(payload),
        })
    }

    async createPaperTrade(payload: PaperTradePayload): Promise<Record<string, unknown>> {
        return this.request<Record<string, unknown>>('/v1/paper-trades', {
            method: 'POST',
            body: JSON.stringify(payload),
        })
    }

    async runDailyOperation(payload: DailyOperationPayload): Promise<DailyOperationResult> {
        return this.request<DailyOperationResult>('/v1/paper-portfolio/daily-ops', {
            method: 'POST',
            body: JSON.stringify(payload),
        })
    }

    async listDailyReviews(limit = 20): Promise<{ items: Array<{ trade_date: string; summary: PaperDailyReviewSummary }> }> {
        return this.request(`/v1/paper-portfolio/daily-reviews?limit=${limit}`)
    }

    async getDailyReview(tradeDate: string): Promise<{ trade_date: string; summary: PaperDailyReviewSummary }> {
        return this.request(`/v1/paper-portfolio/daily-reviews/${encodeURIComponent(tradeDate)}`)
    }

    async getRecommendations(payload: RecommendationRequest): Promise<RecommendationResponse> {
        return this.request<RecommendationResponse>('/v1/recommendations', {
            method: 'POST',
            body: JSON.stringify(payload),
        })
    }

    async runDailyProduct(payload: DailyProductRunRequest): Promise<DailyProductRunResponse> {
        return this.request<DailyProductRunResponse>('/v1/daily-product/run', {
            method: 'POST',
            body: JSON.stringify(payload),
        })
    }

    async listDailyProductRuns(limit = 20): Promise<{ runs: DailyProductRunResponse[]; total: number }> {
        return this.request<{ runs: DailyProductRunResponse[]; total: number }>(`/v1/daily-product/runs?limit=${limit}`)
    }

    async getDailyProductRun(runId: string): Promise<DailyProductRunResponse> {
        return this.request<DailyProductRunResponse>(`/v1/daily-product/runs/${encodeURIComponent(runId)}`)
    }

    async getDailyProductRunScanResults(runId: string, limit = 50): Promise<{ items: RecommendationHistoryItem[]; total: number }> {
        return this.request<{ items: RecommendationHistoryItem[]; total: number }>(
            `/v1/daily-product/runs/${encodeURIComponent(runId)}/scan-results?limit=${limit}`,
        )
    }

    async listDailyProductStrategySkills(): Promise<{ skills: Array<{ id: string; name: string; reference: string }> }> {
        return this.request<{ skills: Array<{ id: string; name: string; reference: string }> }>('/v1/daily-product/strategy-skills')
    }

    async listRecommendationHistory(limit = 50): Promise<{ items: RecommendationHistoryItem[]; total: number }> {
        return this.request<{ items: RecommendationHistoryItem[]; total: number }>(`/v1/recommendations/history?limit=${limit}`)
    }

    async getRecommendationStrategyStats(limit = 50): Promise<{
        stats: RecommendationStrategyStat[]
        learned_weights: { momentum: number; activity: number; near_high: number }
        learning_info: Record<string, unknown>
        total: number
    }> {
        return this.request(`/v1/recommendations/strategy-stats?limit=${limit}`)
    }

    async refreshRecommendationStrategyStats(): Promise<Record<string, unknown>> {
        return this.request('/v1/recommendations/strategy-stats/refresh', {
            method: 'POST',
        })
    }

    async refreshRecommendationEvalRun(opts: {
        baselineProfile?: string
        variantProfile?: string
        lookbackDays?: number
        topK?: number
        benchmarkSymbol?: string
        sourceMode?: 'user_pool' | 'market_scan'
        market?: 'cn' | 'us'
    } = {}): Promise<{ run_id: string; status: string; summary: Record<string, unknown>; gate: Record<string, unknown> }> {
        const {
            baselineProfile = 'ashare_balanced',
            variantProfile = 'ashare_aggressive',
            lookbackDays = 60,
            topK = 5,
            benchmarkSymbol = '000300.SH',
            sourceMode = 'market_scan',
            market = 'cn',
        } = opts
        const q = new URLSearchParams({
            baseline_profile: baselineProfile,
            variant_profile: variantProfile,
            lookback_days: String(lookbackDays),
            top_k: String(topK),
            benchmark_symbol: benchmarkSymbol,
            source_mode: sourceMode,
            market,
        })
        return this.request(`/v1/recommendations/eval-runs/refresh?${q.toString()}`, { method: 'POST' })
    }

    async getLatestRecommendationEvalRun(): Promise<{ run: RecommendationEvalRun | null }> {
        return this.request('/v1/recommendations/eval-runs/latest')
    }

    async manualPushRecommendations(phase: 'open' | 'close' = 'close'): Promise<{
        status: string
        items_count: number
        sent: boolean
        sent_wecom?: boolean
        sent_wps?: boolean
        today: string
        phase: string
    }> {
        return this.request(`/v1/recommendations/push/manual?phase=${phase}`, {
            method: 'POST',
        })
    }

    async refreshInsightsT1(opts: {
        windowStart?: string
        windowEnd?: string
    } = {}): Promise<Record<string, unknown>> {
        const params = new URLSearchParams()
        if (opts.windowStart) params.set('window_start', opts.windowStart)
        if (opts.windowEnd) params.set('window_end', opts.windowEnd)
        const suffix = params.toString() ? `?${params.toString()}` : ''
        return this.request(`/v1/insights/t1/refresh${suffix}`, { method: 'POST' })
    }

    async getInsightsT1RecommendationsTrend(
        opts: { days?: number; startDate?: string; endDate?: string } = {},
    ): Promise<{
        series: InsightsT1RecommendationPoint[]
        metric: string
        description: string
    }> {
        const { days = 90, startDate, endDate } = opts
        const params = startDate && endDate
            ? `start_date=${startDate}&end_date=${endDate}`
            : `days=${days}`
        return this.request(`/v1/insights/t1/recommendations-trend?${params}`)
    }

    async getInsightsT1ReportsAccuracyTrend(
        opts: { days?: number; startDate?: string; endDate?: string } = {},
    ): Promise<{
        series: InsightsT1ReportAccuracyPoint[]
        series_all: InsightsT1ReportAccuracyPoint[]
        summary?: InsightsT1ReportAccuracySummary
        summary_all?: InsightsT1ReportAccuracySummary
        metric: string
        description: string
        description_all: string
        denominator_note?: string
    }> {
        const { days = 90, startDate, endDate } = opts
        const params = startDate && endDate
            ? `start_date=${startDate}&end_date=${endDate}`
            : `days=${days}`
        return this.request(`/v1/insights/t1/reports-accuracy-trend?${params}`)
    }

    async getInsightsT1RecommendationsDetail(date: string): Promise<{
        date: string
        items: InsightsT1RecommendationDetail[]
    }> {
        return this.request(`/v1/insights/t1/recommendations-detail?date=${date}`)
    }

    async getInsightsT1ReportsDetail(
        date: string,
        opts: { scope?: 'portfolio' | 'all' } = {},
    ): Promise<{
        date: string
        items: InsightsT1ReportDetail[]
        scope: string
    }> {
        const scope = opts.scope ?? 'portfolio'
        return this.request(`/v1/insights/t1/reports-detail?date=${encodeURIComponent(date)}&scope=${scope}`)
    }

    async getInsightsT1MultiModelConsensusTrend(
        opts: { days?: number; startDate?: string; endDate?: string; minModels?: number } = {},
    ): Promise<{
        series: InsightsT1MultiModelConsensusPoint[]
        series_all: InsightsT1MultiModelConsensusPoint[]
        metric: string
        min_models: number
        description: string
        description_all: string
    }> {
        const { days = 90, startDate, endDate, minModels = 2 } = opts
        const params = new URLSearchParams()
        if (startDate && endDate) {
            params.set('start_date', startDate)
            params.set('end_date', endDate)
        } else {
            params.set('days', String(days))
        }
        params.set('min_models', String(minModels))
        return this.request(`/v1/insights/t1/multi-model-consensus-trend?${params.toString()}`)
    }

    async getInsightsT1MultiModelConsensusDetail(
        date: string,
        opts: { scope?: 'portfolio' | 'all'; direction?: 'bullish' | 'bearish'; minModels?: number } = {},
    ): Promise<{
        date: string
        scope: string
        direction?: string | null
        items: InsightsT1MultiModelConsensusDetail[]
    }> {
        const params = new URLSearchParams()
        params.set('date', date)
        params.set('scope', opts.scope ?? 'all')
        if (opts.direction) params.set('direction', opts.direction)
        params.set('min_models', String(opts.minModels ?? 2))
        return this.request(`/v1/insights/t1/multi-model-consensus-detail?${params.toString()}`)
    }

    async getModelArenaLeaderboard(
        opts: {
            days?: number
            startDate?: string
            endDate?: string
            scope?: 'portfolio' | 'all'
            minSamples?: number
        } = {},
    ): Promise<ModelArenaLeaderboardResponse> {
        const params = new URLSearchParams()
        if (opts.startDate && opts.endDate) {
            params.set('start_date', opts.startDate)
            params.set('end_date', opts.endDate)
        } else {
            params.set('days', String(opts.days ?? 90))
        }
        params.set('scope', opts.scope ?? 'portfolio')
        params.set('min_samples', String(opts.minSamples ?? 1))
        return this.request(`/v1/model-arena/leaderboard?${params.toString()}`)
    }

    async getModelArenaSymbolCompare(
        symbol: string,
        opts: {
            days?: number
            startDate?: string
            endDate?: string
            scope?: 'portfolio' | 'all'
        } = {},
    ): Promise<ModelArenaSymbolCompareResponse> {
        const params = new URLSearchParams()
        params.set('symbol', symbol)
        if (opts.startDate && opts.endDate) {
            params.set('start_date', opts.startDate)
            params.set('end_date', opts.endDate)
        } else {
            params.set('days', String(opts.days ?? 60))
        }
        params.set('scope', opts.scope ?? 'all')
        return this.request(`/v1/model-arena/symbol-compare?${params.toString()}`)
    }

    async createModelArenaRun(payload: ModelArenaRunRequest): Promise<ModelArenaRunResponse> {
        return this.request('/v1/model-arena/runs', {
            method: 'POST',
            body: JSON.stringify(payload),
        })
    }

    async getModelArenaDriftAlerts(
        opts: {
            scope?: 'portfolio' | 'all'
            lookbackDays?: number
            recentDays?: number
            baselineDays?: number
            minRecentSamples?: number
            alertDropPct?: number
        } = {},
    ): Promise<ModelArenaDriftAlertResponse> {
        const params = new URLSearchParams()
        params.set('scope', opts.scope ?? 'portfolio')
        params.set('lookback_days', String(opts.lookbackDays ?? 120))
        params.set('recent_days', String(opts.recentDays ?? 7))
        params.set('baseline_days', String(opts.baselineDays ?? 30))
        params.set('min_recent_samples', String(opts.minRecentSamples ?? 8))
        params.set('alert_drop_pct', String(opts.alertDropPct ?? 8))
        return this.request(`/v1/model-arena/drift-alerts?${params.toString()}`)
    }

    async getModelArenaModelDetail(
        opts: {
            modelProfileId?: string
            modelKey?: string
            days?: number
            startDate?: string
            endDate?: string
            scope?: 'portfolio' | 'all'
            limit?: number
        },
    ): Promise<ModelArenaModelDetailResponse> {
        const params = new URLSearchParams()
        if (opts.modelProfileId) params.set('model_profile_id', opts.modelProfileId)
        if (opts.modelKey) params.set('model_key', opts.modelKey)
        if (opts.startDate && opts.endDate) {
            params.set('start_date', opts.startDate)
            params.set('end_date', opts.endDate)
        } else {
            params.set('days', String(opts.days ?? 90))
        }
        params.set('scope', opts.scope ?? 'portfolio')
        params.set('limit', String(opts.limit ?? 300))
        return this.request(`/v1/model-arena/model-detail?${params.toString()}`)
    }

    async getModelArenaModelTrend(
        opts: {
            days?: number
            startDate?: string
            endDate?: string
            scope?: 'portfolio' | 'all'
            topN?: number
            minSamples?: number
            modelKeys?: string[]
        } = {},
    ): Promise<ModelArenaModelTrendResponse> {
        const params = new URLSearchParams()
        if (opts.startDate && opts.endDate) {
            params.set('start_date', opts.startDate)
            params.set('end_date', opts.endDate)
        } else {
            params.set('days', String(opts.days ?? 90))
        }
        params.set('scope', opts.scope ?? 'portfolio')
        params.set('top_n', String(opts.topN ?? 5))
        params.set('min_samples', String(opts.minSamples ?? 10))
        if (opts.modelKeys?.length) {
            params.set('model_keys', opts.modelKeys.join(','))
        }
        return this.request(`/v1/model-arena/model-trend?${params.toString()}`)
    }

    async promoteModelArenaDefault(payload: ModelArenaPromoteRequest): Promise<{
        promoted: boolean
        forced?: boolean
        profile: ModelProfile
        gate: Record<string, unknown>
    }> {
        return this.request('/v1/model-arena/promote-default', {
            method: 'POST',
            body: JSON.stringify(payload),
        })
    }

    async rollbackModelArenaDefault(profileId: string, reason?: string): Promise<{
        rolled_back: boolean
        profile: ModelProfile
        reason?: string | null
    }> {
        return this.request('/v1/model-arena/rollback-default', {
            method: 'POST',
            body: JSON.stringify({ profile_id: profileId, reason }),
        })
    }

    async listRecommendationProfiles(): Promise<{ profiles: import('@/types').RecommendationProfile[] }> {
        return this.request('/v1/recommendations/profiles')
    }

    // Stock Search
    async searchStocks(q: string): Promise<{ results: StockSearchResult[] }> {
        return this.request<{ results: StockSearchResult[] }>(`/v1/market/stock-search?q=${encodeURIComponent(q)}`)
    }

    async getConfig(): Promise<RuntimeConfig> {
        return this.request<RuntimeConfig>('/v1/config')
    }

    async listModelProfiles(includeInactive = false): Promise<{ profiles: ModelProfile[] }> {
        return this.request<{ profiles: ModelProfile[] }>(
            `/v1/model-profiles?include_inactive=${includeInactive ? 'true' : 'false'}`,
        )
    }

    async createModelProfile(payload: ModelProfileCreateRequest): Promise<ModelProfile> {
        return this.request<ModelProfile>('/v1/model-profiles', {
            method: 'POST',
            body: JSON.stringify(payload),
        })
    }

    async updateModelProfile(profileId: string, payload: ModelProfileUpdateRequest): Promise<ModelProfile> {
        return this.request<ModelProfile>(`/v1/model-profiles/${encodeURIComponent(profileId)}`, {
            method: 'PATCH',
            body: JSON.stringify(payload),
        })
    }

    async deleteModelProfile(profileId: string): Promise<{ message: string; id: string }> {
        return this.request<{ message: string; id: string }>(`/v1/model-profiles/${encodeURIComponent(profileId)}`, {
            method: 'DELETE',
        })
    }

    async warmupModelProfile(profileId: string, prompt = '你好'): Promise<ModelProfileWarmupResponse> {
        return this.request<ModelProfileWarmupResponse>(`/v1/model-profiles/${encodeURIComponent(profileId)}/warmup`, {
            method: 'POST',
            body: JSON.stringify({ prompt }),
        })
    }

    async updateConfig(updates: RuntimeConfigUpdate): Promise<RuntimeConfigUpdateResponse> {
        return this.request<RuntimeConfigUpdateResponse>('/v1/config', {
            method: 'PATCH',
            body: JSON.stringify(updates),
        })
    }

    async warmupConfig(request: RuntimeWarmupRequest): Promise<RuntimeWarmupResponse> {
        return this.request<RuntimeWarmupResponse>('/v1/config/warmup', {
            method: 'POST',
            body: JSON.stringify(request),
        })
    }

    async warmupWecom(request: WecomWarmupRequest): Promise<WecomWarmupResponse> {
        return this.request<WecomWarmupResponse>('/v1/config/wecom/warmup', {
            method: 'POST',
            body: JSON.stringify(request),
        })
    }

    async warmupWps(request: WpsWarmupRequest): Promise<WpsWarmupResponse> {
        return this.request<WpsWarmupResponse>('/v1/config/wps/warmup', {
            method: 'POST',
            body: JSON.stringify(request),
        })
    }

    async requestLoginCode(email: string): Promise<{ message: string; dev_code?: string }> {
        return this.request('/v1/auth/request-code', {
            method: 'POST',
            body: JSON.stringify({ email }),
        })
    }

    async verifyLoginCode(email: string, code: string): Promise<AuthVerifyResponse> {
        return this.request('/v1/auth/verify-code', {
            method: 'POST',
            body: JSON.stringify({ email, code }),
        })
    }

    async getMe(): Promise<AuthUser> {
        return this.request('/v1/auth/me')
    }

    // Token Management
    async getTokens(): Promise<UserToken[]> {
        return this.request<UserToken[]>('/v1/tokens')
    }

    async createToken(request: UserTokenCreateRequest): Promise<UserToken> {
        return this.request<UserToken>('/v1/tokens', {
            method: 'POST',
            body: JSON.stringify(request),
        })
    }

    async deleteToken(tokenId: string): Promise<{ message: string }> {
        return this.request<{ message: string }>(`/v1/tokens/${tokenId}`, {
            method: 'DELETE',
        })
    }

    // Feedback
    async createFeedback(subject: string, content: string): Promise<FeedbackItem> {
        return this.request<FeedbackItem>('/v1/feedbacks', {
            method: 'POST',
            body: JSON.stringify({ subject, content }),
        })
    }

    async listFeedbacks(page = 1, pageSize = 20): Promise<FeedbackListResponse> {
        return this.request<FeedbackListResponse>(`/v1/feedbacks?page=${page}&page_size=${pageSize}`)
    }

    async getFeedback(id: string): Promise<FeedbackItem> {
        return this.request<FeedbackItem>(`/v1/feedbacks/${id}`)
    }

    async getFeedbackUnreadCount(): Promise<FeedbackUnreadResponse> {
        return this.request<FeedbackUnreadResponse>('/v1/feedbacks/unread-count')
    }

    async markFeedbackRead(id: string): Promise<void> {
        return this.request<void>(`/v1/feedbacks/${id}/read`, { method: 'POST' })
    }

    /** 导出 stock-analysis-team 增强版 HTML 报告（返回 html 文本） */
    async exportStockTeamEnhancedReportHtml(params: {
        reportId: string
        market?: 'cn' | 'us'
        period?: '1y' | '6mo' | '3mo' | '1mo'
        include_charts?: boolean
    }): Promise<string> {
        const sp = new URLSearchParams()
        if (params.market) sp.set('market', params.market)
        if (params.period) sp.set('period', params.period)
        if (typeof params.include_charts === 'boolean') {
            sp.set('include_charts', String(params.include_charts))
        }
        return this.request<string>(`/v1/reports/${encodeURIComponent(params.reportId)}/export/stock-team-enhanced-html?${sp}`)
    }

    // Market Mainline (M5)
    async analyzeMainline(request: import('@/types').MainlineAnalyzeRequest): Promise<import('@/types').MainlineAnalyzeResponse> {
        return this.request<import('@/types').MainlineAnalyzeResponse>('/v1/mainline/analyze', {
            method: 'POST',
            body: JSON.stringify(request),
        })
    }

    async listMainlineRuns(limit = 20): Promise<import('@/types').MainlineRunListResponse> {
        return this.request<import('@/types').MainlineRunListResponse>(`/v1/mainline/runs?limit=${limit}`)
    }

    async getMainlineRun(runId: string): Promise<import('@/types').MainlineReport> {
        return this.request<import('@/types').MainlineReport>(`/v1/mainline/runs/${encodeURIComponent(runId)}`)
    }

    async getMainlineLatest(perspective = 'short'): Promise<import('@/types').MainlineReport> {
        return this.request<import('@/types').MainlineReport>(`/v1/mainline/latest?perspective=${encodeURIComponent(perspective)}`)
    }

    async getMainlineBoardSpot(type: 'industry' | 'concept'): Promise<import('@/types').MainlineBoardSpotResponse> {
        return this.request<import('@/types').MainlineBoardSpotResponse>(`/v1/mainline/boards/spot?type=${type}`)
    }

    async mainlineCandidateToWatchlist(candidateId: string): Promise<Record<string, unknown>> {
        return this.request<Record<string, unknown>>(`/v1/mainline/candidates/${encodeURIComponent(candidateId)}/watchlist`, {
            method: 'POST',
        })
    }

    async mainlineCandidateAnalyze(candidateId: string): Promise<Record<string, unknown>> {
        return this.request<Record<string, unknown>>(`/v1/mainline/candidates/${encodeURIComponent(candidateId)}/analyze`, {
            method: 'POST',
        })
    }

    async mainlineT1Refresh(): Promise<{ evaluated_reports: number; skipped: Array<Record<string, unknown>> }> {
        return this.request<{ evaluated_reports: number; skipped: Array<Record<string, unknown>> }>('/v1/mainline/t1/refresh', {
            method: 'POST',
        })
    }

    async getMainlineT1Overview(days = 30): Promise<import('@/types').MainlineT1Overview> {
        return this.request<import('@/types').MainlineT1Overview>(`/v1/mainline/t1/overview?days=${days}`)
    }

    async getMainlineT1Outcomes(reportId?: string, limit = 50): Promise<{ items: import('@/types').MainlineT1Outcome[]; total: number }> {
        const q = reportId ? `?report_id=${encodeURIComponent(reportId)}&limit=${limit}` : `?limit=${limit}`
        return this.request<{ items: import('@/types').MainlineT1Outcome[]; total: number }>(`/v1/mainline/t1/outcomes${q}`)
    }

    async getMainlineBacktestLatest(): Promise<import('@/types').MainlineBacktestResult> {
        return this.request<import('@/types').MainlineBacktestResult>('/v1/mainline/backtest/latest')
    }

    // 主线周期分析（C2-C5）
    async getMainlineCycles(status?: string, limit = 50): Promise<{ items: import('@/types').MainlineCycle[]; total: number }> {
        const q = new URLSearchParams()
        if (status) q.set('status', status)
        q.set('limit', String(limit))
        return this.request(`/v1/mainline/cycles?${q.toString()}`)
    }

    async getMainlineCycleTrack(mainlineKey: string): Promise<import('@/types').MainlineCycle> {
        return this.request<import('@/types').MainlineCycle>(`/v1/mainline/cycles/${encodeURIComponent(mainlineKey)}`)
    }

    async getMainlineRotation(days = 30): Promise<{ days: number; timeline: import('@/types').MainlineRotationDay[] }> {
        return this.request(`/v1/mainline/rotation?days=${days}`)
    }

    async getMainlineDecisions(days = 30, limit = 100): Promise<{ items: import('@/types').MainlineDecision[]; total: number }> {
        return this.request(`/v1/mainline/decisions?days=${days}&limit=${limit}`)
    }

    async getMainlineCapability(days = 90): Promise<import('@/types').MainlineCapability> {
        return this.request(`/v1/mainline/capability?days=${days}`)
    }

    async runMainlineAutodive(tradeDate?: string): Promise<Record<string, unknown>> {
        const q = tradeDate ? `?trade_date=${encodeURIComponent(tradeDate)}` : ''
        return this.request(`/v1/mainline/autodive/run${q}`, { method: 'POST' })
    }

    async getMainlineAutodiveRuns(tradeDate?: string, buyableOnly = false): Promise<{ items: import('@/types').MainlineTradeCandidate[]; total: number }> {
        const q = new URLSearchParams()
        if (tradeDate) q.set('trade_date', tradeDate)
        if (buyableOnly) q.set('buyable_only', 'true')
        return this.request(`/v1/mainline/autodive/runs?${q.toString()}`)
    }

    async getMainlineDailyLog(tradeDate?: string): Promise<import('@/types').MainlineDailyLog> {
        const q = tradeDate ? `?trade_date=${encodeURIComponent(tradeDate)}` : ''
        return this.request(`/v1/mainline/logs/daily${q}`)
    }
}

export const api = new ApiService()

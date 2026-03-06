import type { AnalysisRequest, AnalysisResponse, JobStatus, AnalysisReport, KlineResponse, Report, ReportDetail, ReportListResponse, RuntimeConfig, HotStock } from '@/types'

export function getBaseUrl(): string {
    try {
        const stored = localStorage.getItem('tradingagents-settings')
        if (stored) {
            const settings = JSON.parse(stored) as { apiUrl?: string }
            if (settings.apiUrl) return settings.apiUrl.replace(/\/$/, '')
        }
    } catch {}
    return (import.meta.env.VITE_API_URL as string) || 'http://localhost:8000'
}

// Kept for backward compatibility
export const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

class ApiService {
    private async request<T>(endpoint: string, options?: RequestInit): Promise<T> {
        const url = `${getBaseUrl()}${endpoint}`
        const response = await fetch(url, {
            ...options,
            headers: {
                'Content-Type': 'application/json',
                ...options?.headers,
            },
        })

        if (!response.ok) {
            const error = await response.text()
            throw new Error(error || `HTTP error! status: ${response.status}`)
        }

        return response.json()
    }

    async startAnalysis(request: AnalysisRequest): Promise<AnalysisResponse> {
        return this.request<AnalysisResponse>('/v1/analyze', {
            method: 'POST',
            body: JSON.stringify(request),
        })
    }

    async getJobStatus(jobId: string): Promise<JobStatus> {
        return this.request<JobStatus>(`/v1/jobs/${jobId}`)
    }

    async getJobResult(jobId: string): Promise<{ job_id: string; status: string; decision: string; result: AnalysisReport }> {
        return this.request(`/v1/jobs/${jobId}/result`)
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
    ) {
        const response = await fetch(`${getBaseUrl()}/v1/chat/completions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                messages,
                stream,
                selected_analysts: selectedAnalysts,
            }),
        })

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`)
        }

        return response
    }

    // Report API Methods
    async getReports(symbol?: string, skip = 0, limit = 100): Promise<ReportListResponse> {
        const params = new URLSearchParams()
        if (symbol) params.append('symbol', symbol)
        params.append('skip', skip.toString())
        params.append('limit', limit.toString())
        return this.request<ReportListResponse>(`/v1/reports?${params}`)
    }

    async getReport(reportId: string): Promise<ReportDetail> {
        return this.request<ReportDetail>(`/v1/reports/${reportId}`)
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

    async getHotStocks(limit = 30): Promise<{ stocks: HotStock[]; total: number }> {
        return this.request<{ stocks: HotStock[]; total: number }>(`/v1/market/hot-stocks?limit=${limit}`)
    }

    async getConfig(): Promise<RuntimeConfig> {
        return this.request<RuntimeConfig>('/v1/config')
    }

    async updateConfig(updates: Partial<RuntimeConfig>): Promise<{ message: string; applied: Partial<RuntimeConfig>; current: RuntimeConfig }> {
        return this.request('/v1/config', {
            method: 'PATCH',
            body: JSON.stringify(updates),
        })
    }
}

export const api = new ApiService()

import type { FreshnessSummary } from '@/types'

const BADGE_STYLES: Record<string, string> = {
    error: 'bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-400',
    stale: 'bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-400',
    warning: 'bg-amber-100 text-amber-700 dark:bg-amber-500/20 dark:text-amber-400',
}

const BADGE_LABELS: Record<string, string> = {
    error: '数据异常',
    stale: '数据过时',
    warning: '部分滞后',
}

export function FreshnessBadge({ status }: { status?: string | null }) {
    if (!status || status === 'fresh') return null
    const label = BADGE_LABELS[status] || status
    const cls = BADGE_STYLES[status] || BADGE_STYLES.warning
    return (
        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${cls}`}>
            {label}
        </span>
    )
}

function sectionTooltip(
    summary: FreshnessSummary | null | undefined,
    _sectionKey: string,
    sectionStatus?: string,
): string | undefined {
    if (!summary || !sectionStatus || sectionStatus === 'fresh' || sectionStatus === 'not_applicable' || sectionStatus === 'empty_ok') {
        return undefined
    }
    const datasets = summary.datasets || []
    for (const item of datasets) {
        if (item.status === sectionStatus && (item.lag_note || item.error_message)) {
            return item.lag_note || item.error_message
        }
    }
    for (const item of summary.blocking_sources || []) {
        if (item.status === sectionStatus && (item.lag_note || item.error_message)) {
            return item.lag_note || item.error_message
        }
    }
    for (const item of summary.fetch_errors || []) {
        if (sectionStatus === 'error' && item.error_message) {
            return `${item.source_key}: ${item.error_message}`
        }
    }
    return sectionStatus
}

export function FreshnessBanner({ summary }: { summary?: FreshnessSummary | null }) {
    if (!summary || summary.overall_status === 'fresh') return null
    const isError = summary.overall_status === 'error'

    return (
        <div
            className={`rounded-xl border px-4 py-3 text-sm ${
                isError
                    ? 'border-red-200 bg-red-50 text-red-800 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-200'
                    : summary.overall_status === 'stale'
                      ? 'border-red-200 bg-red-50 text-red-800 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-200'
                      : 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-200'
            }`}
        >
            <p className="font-semibold mb-1">{summary.overall_label || summary.overall_status}</p>
            {isError && (summary.fetch_errors?.length ?? 0) > 0 && (
                <ul className="list-disc pl-5 space-y-1">
                    {summary.fetch_errors!.map((e, i) => (
                        <li key={i}>
                            {e.source_key}: {e.error_message || e.error_code}
                        </li>
                    ))}
                </ul>
            )}
            {!isError && (summary.blocking_sources?.length ?? 0) > 0 && (
                <ul className="list-disc pl-5 space-y-1">
                    {summary.blocking_sources!
                        .filter(b => b.status === 'stale')
                        .map((b, i) => (
                            <li key={i}>
                                {b.source_key}: 截止 {b.anchor_actual}，预期 {b.anchor_expected}
                            </li>
                        ))}
                </ul>
            )}
            {!isError && summary.overall_status === 'warning' && (summary.warning_sources?.length ?? 0) > 0 && (
                <ul className="list-disc pl-5 space-y-1">
                    {summary.warning_sources!.map((w, i) => (
                        <li key={i}>
                            {w.source_key}: {w.lag_note || '数据可能仍在更新'}
                        </li>
                    ))}
                </ul>
            )}
            {summary.expected_anchor && (
                <p className="mt-2 text-xs opacity-80">预期数据锚点：{summary.expected_anchor}</p>
            )}
        </div>
    )
}

export function ReportAgeNote({ note }: { note?: string | null }) {
    if (!note) return null
    return (
        <p className="text-xs text-slate-500 dark:text-slate-400 mt-2">{note}</p>
    )
}

export function sectionFreshnessIcon(status?: string) {
    if (!status || status === 'fresh' || status === 'not_applicable' || status === 'empty_ok') return null
    if (status === 'error') return '✕'
    if (status === 'stale') return '⚠'
    return '◔'
}

export { sectionTooltip as sectionFreshnessTooltip }

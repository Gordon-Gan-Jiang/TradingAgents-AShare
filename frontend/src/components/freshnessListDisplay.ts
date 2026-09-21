/** Tooltip for the reports list 「数据状态」 column header. */
export const FRESHNESS_COLUMN_TOOLTIP =
    '数据异常：拉取失败；数据过时：关键数据滞后；部分滞后：非关键源滞后；—：正常或未评估'

/** Table headers for the reports list (excluding checkbox column). */
export const REPORT_LIST_TABLE_HEADERS = [
    '股票',
    '分析日期',
    '模型',
    '数据状态',
    '决策建议',
    '置信度',
    '目标价/止损价',
    '生成时间',
    '操作',
] as const

export type FreshnessStatus = 'error' | 'stale' | 'warning' | 'fresh' | string | null | undefined

/** Whether the list cell should render a colored badge (vs neutral em dash). */
export function shouldShowFreshnessBadge(status: FreshnessStatus): boolean {
    return Boolean(status && status !== 'fresh')
}

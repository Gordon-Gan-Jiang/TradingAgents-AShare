import { describe, expect, it } from 'vitest'

import {
    buildCapabilityRows,
    buildSourceTimelines,
    buildTrendBars,
    countProbeResults,
    EMPTY_PLACEHOLDER,
    filterProbeResultRows,
    formatLatency,
    formatProbeTimestamp,
    formatRowCount,
    formatTradingDay,
    isNonTradingDay,
    listProbeFieldValues,
    normalizeProbeStatus,
    probeErrorSummary,
    probeErrorText,
    probeStatusMeta,
    probeStatusRank,
    probeStatusSeverity,
    probeTierMeta,
    PROBE_SUMMARY_CARDS,
    PROBE_TIER_LEGEND,
    resolveProbeCounts,
    sortProbeResultRows,
    summariseBySource,
    trendSegmentHeights,
    triggerLabel,
    truncateText,
    worstProbeStatusMeta,
    type ProbeHistoryPointLike,
    type ProbeResultLike,
    type ProbeRunLike,
} from '@/pages/dataSourcesDisplay'

function result(overrides: Partial<ProbeResultLike> = {}): ProbeResultLike {
    return {
        source: 'cn_baostock',
        tool: 'get_daily_bars_df',
        label: '历史日线（结构化）',
        category: 'cn_research_data',
        status: 'ok',
        latency_ms: 210,
        row_count: 17,
        version: '00.9.40',
        error_kind: null,
        error_type: null,
        error_message: null,
        checked_at: '2026-09-24T09:00:03+08:00',
        ...overrides,
    }
}

const SAMPLE_RUN: ProbeRunLike = {
    id: 'run-1',
    started_at: '2026-09-24T09:00:01+08:00',
    finished_at: '2026-09-24T09:00:12+08:00',
    status: 'ok',
    trigger: 'scheduled',
    trading_day: true,
    total_count: 24,
    ok_count: 18,
    degraded_count: 2,
    failed_count: 3,
    timeout_count: 0,
    unsupported_count: 1,
    duration_ms: 11200,
}

describe('probeStatusMeta', () => {
    it('maps the five contract statuses to the required Chinese labels', () => {
        expect(probeStatusMeta('ok')).toMatchObject({ label: '可用', known: true })
        expect(probeStatusMeta('degraded')).toMatchObject({ label: '降级', known: true })
        expect(probeStatusMeta('failed')).toMatchObject({ label: '不可用', known: true })
        expect(probeStatusMeta('timeout')).toMatchObject({ label: '超时', known: true })
        expect(probeStatusMeta('unsupported')).toMatchObject({ label: '不支持', known: true })
    })

    it('uses green / amber / red / violet / gray for those five statuses', () => {
        expect(probeStatusMeta('ok').badgeClass).toContain('emerald')
        expect(probeStatusMeta('degraded').badgeClass).toContain('amber')
        expect(probeStatusMeta('failed').badgeClass).toContain('rose')
        expect(probeStatusMeta('unsupported').badgeClass).toContain('slate')
        // 超时必须一眼区别于「挂掉」（玫红）与「降级」（琥珀），所以单独用紫色
        expect(probeStatusMeta('timeout').badgeClass).toContain('violet')
        expect(probeStatusMeta('timeout').dotClass).toContain('violet')
        expect(probeStatusMeta('timeout').valueClass).toContain('violet')
    })

    it('treats 不支持 as informational rather than a failure', () => {
        expect(probeStatusMeta('unsupported').dotClass).toBe(probeStatusMeta('unknown-value').dotClass)
        expect(probeStatusRank('unsupported')).toBeLessThan(probeStatusRank('ok'))
    })

    it('keeps 超时 strictly between 不可用 and 降级 in both rank and severity', () => {
        // 排序权重（表格）与严重度（按源归并）必须一致，否则同一个源会在两处顺序不同
        expect(probeStatusRank('timeout')).toBeGreaterThan(probeStatusRank('failed'))
        expect(probeStatusRank('timeout')).toBeLessThan(probeStatusRank('degraded'))
        expect(probeStatusSeverity('timeout')).toBeGreaterThan(probeStatusSeverity('failed'))
        expect(probeStatusSeverity('timeout')).toBeLessThan(probeStatusSeverity('degraded'))
        // 归并成「最差状态」时，超时源仍比降级源更值得先看
        expect(worstProbeStatusMeta(['ok', 'degraded']).label).toBe('降级')
        expect(worstProbeStatusMeta(['timeout', 'degraded']).label).toBe('超时')
        expect(worstProbeStatusMeta(['timeout', 'failed']).label).toBe('不可用')
    })

    it('labels pending / skipped in Chinese while keeping them gray', () => {
        expect(probeStatusMeta('pending')).toMatchObject({ label: '等待中' })
        expect(probeStatusMeta('skipped')).toMatchObject({ label: '已跳过' })
        expect(probeStatusMeta('pending').badgeClass).toContain('slate')
    })

    it('falls back to gray plus the raw value for an unknown status', () => {
        const meta = probeStatusMeta('flaky')
        expect(meta.known).toBe(false)
        expect(meta.label).toBe('flaky')
        expect(meta.badgeClass).toContain('slate')
    })

    it('never throws for missing / empty / non-string statuses', () => {
        for (const value of [null, undefined, '', '   ']) {
            const meta = probeStatusMeta(value)
            expect(meta.known).toBe(false)
            expect(meta.label).toBe('未知')
            expect(meta.badgeClass).toContain('slate')
        }
        // 归一化：大小写与空白不应影响识别
        expect(probeStatusMeta('  OK  ').label).toBe('可用')
        expect(normalizeProbeStatus(' Failed ')).toBe('failed')
    })

    it('ranks problems before healthy results, with unknown values last', () => {
        const ranks = ['failed', 'timeout', 'degraded', 'unsupported', 'ok', 'mystery'].map(probeStatusRank)
        expect(ranks).toEqual([...ranks].sort((a, b) => a - b))
        expect(probeStatusRank('failed')).toBeLessThan(probeStatusRank('timeout'))
        expect(probeStatusRank('timeout')).toBeLessThan(probeStatusRank('degraded'))
        expect(probeStatusRank('degraded')).toBeLessThan(probeStatusRank('unsupported'))
        expect(probeStatusRank('unsupported')).toBeLessThan(probeStatusRank('ok'))
        expect(probeStatusRank('mystery')).toBeGreaterThan(probeStatusRank('ok'))
    })
})

describe('sortProbeResultRows', () => {
    it('orders failed → degraded → unsupported → ok, then by source, then tool', () => {
        const rows = [
            result({ source: 'cn_baostock', tool: 'b_tool', status: 'ok' }),
            result({ source: 'cn_akshare', tool: 'a_tool', status: 'failed' }),
            result({ source: 'cn_baostock', tool: 'a_tool', status: 'failed' }),
            result({ source: 'cn_tushare', tool: 'c_tool', status: 'degraded' }),
            result({ source: 'cn_baostock', tool: 'c_tool', status: 'unsupported' }),
        ]
        const sorted = sortProbeResultRows(rows)
        expect(sorted.map(row => `${row.source}/${row.tool}`)).toEqual([
            'cn_akshare/a_tool',
            'cn_baostock/a_tool',
            'cn_tushare/c_tool',
            'cn_baostock/c_tool',
            'cn_baostock/b_tool',
        ])
    })

    it('does not mutate the input and tolerates empty input', () => {
        const rows = [result({ status: 'ok' }), result({ status: 'failed', source: 'z' })]
        const snapshot = rows.map(row => row.status)
        sortProbeResultRows(rows)
        expect(rows.map(row => row.status)).toEqual(snapshot)
        expect(sortProbeResultRows(null)).toEqual([])
        expect(sortProbeResultRows(undefined)).toEqual([])
    })

    it('keeps 超时 rows after 不可用 but before 降级', () => {
        const sorted = sortProbeResultRows([
            result({ source: 'z_slow', status: 'degraded' }),
            result({ source: 'z_slow', status: 'timeout' }),
            result({ source: 'z_slow', status: 'failed' }),
        ])
        expect(sorted.map(row => row.status)).toEqual(['failed', 'timeout', 'degraded'])
    })
})

describe('countProbeResults / resolveProbeCounts', () => {
    it('counts each status and buckets unknown values into other', () => {
        const counts = countProbeResults([
            result({ status: 'ok' }),
            result({ status: 'ok' }),
            result({ status: 'degraded' }),
            result({ status: 'failed' }),
            result({ status: 'timeout' }),
            result({ status: 'unsupported' }),
            result({ status: 'pending' }),
        ])
        expect(counts).toEqual({
            total: 7,
            ok: 2,
            degraded: 1,
            failed: 1,
            timeout: 1,
            unsupported: 1,
            other: 1,
        })
    })

    it('counts 超时 on its own and never as other', () => {
        const counts = countProbeResults([result({ status: 'timeout' }), result({ status: 'TIMEOUT' })])
        expect(counts).toEqual({
            total: 2,
            ok: 0,
            degraded: 0,
            failed: 0,
            timeout: 2,
            unsupported: 0,
            other: 0,
        })
    })

    it('prefers the run summary counts', () => {
        expect(resolveProbeCounts(SAMPLE_RUN, [result()])).toEqual({
            total: 24,
            ok: 18,
            degraded: 2,
            failed: 3,
            timeout: 0,
            unsupported: 1,
            other: 0,
        })
    })

    it('reads timeout_count from the run summary without folding it into failed', () => {
        const counts = resolveProbeCounts({
            total_count: 12,
            ok_count: 8,
            degraded_count: 1,
            timeout_count: 2,
            failed_count: 1,
        })
        expect(counts).toEqual({
            total: 12,
            ok: 8,
            degraded: 1,
            failed: 1,
            timeout: 2,
            unsupported: 0,
            // timeout 参与 total 的配平：8 + 1 + 1 + 2 = 12，因此没有「其他」
            other: 0,
        })
    })

    it('still balances the total when only timeout_count is present', () => {
        // total_count 缺失时以已知分项之和为准；哪怕只回了 timeout_count 也不能退回明细统计
        expect(resolveProbeCounts({ timeout_count: 3 }, [result()])).toMatchObject({
            total: 3,
            timeout: 3,
            failed: 0,
            other: 0,
        })
    })

    it('derives 其他 from total minus the named buckets', () => {
        const counts = resolveProbeCounts({ total_count: 10, ok_count: 4, failed_count: 1 })
        expect(counts).toMatchObject({ total: 10, ok: 4, failed: 1, other: 5 })
        // 老后端不返回 timeout_count 时按 0 处理，行为与改造前一致
        expect(counts.timeout).toBe(0)
    })

    it('falls back to the detail rows when the run summary carries no counts', () => {
        const counts = resolveProbeCounts({ id: 'run-x', started_at: '2026-09-24T09:00:01+08:00' }, [
            result({ status: 'degraded' }),
        ])
        expect(counts).toMatchObject({ total: 1, degraded: 1 })
    })

    it('handles latest_run === null without throwing', () => {
        const fromResults = resolveProbeCounts(null, [result({ status: 'failed' }), result()])
        expect(fromResults).toEqual({
            total: 2,
            ok: 1,
            degraded: 0,
            failed: 1,
            timeout: 0,
            unsupported: 0,
            other: 0,
        })
        expect(() => resolveProbeCounts(null)).not.toThrow()
        expect(resolveProbeCounts(null, null)).toEqual({
            total: 0,
            ok: 0,
            degraded: 0,
            failed: 0,
            timeout: 0,
            unsupported: 0,
            other: 0,
        })
        expect(() => resolveProbeCounts(undefined, undefined)).not.toThrow()
    })

    it('clamps 其他 at zero when the summary is inconsistent', () => {
        const counts = resolveProbeCounts({ total_count: 2, ok_count: 5, failed_count: 1 })
        expect(counts.other).toBe(0)
        expect(counts.total).toBe(2)
    })
})

describe('PROBE_SUMMARY_CARDS', () => {
    it('lists the required summary cards in order', () => {
        expect(PROBE_SUMMARY_CARDS.map(card => card.label)).toEqual([
            '本次探测总数',
            '可用',
            '降级',
            '不可用',
            '超时',
            '不支持',
        ])
        expect(PROBE_SUMMARY_CARDS.map(card => card.key)).toEqual([
            'total',
            'ok',
            'degraded',
            'failed',
            'timeout',
            'unsupported',
        ])
    })

    it('gives 超时 its own violet value colour', () => {
        const card = PROBE_SUMMARY_CARDS.find(item => item.key === 'timeout')
        expect(card?.valueClass).toContain('violet')
    })
})

describe('formatLatency', () => {
    it('renders milliseconds and seconds', () => {
        expect(formatLatency(210)).toBe('210 ms')
        expect(formatLatency(0)).toBe('0 ms')
        expect(formatLatency(999)).toBe('999 ms')
        expect(formatLatency(1500)).toBe('1.5 s')
        expect(formatLatency(11200)).toBe('11.2 s')
    })

    it('renders a placeholder for missing or invalid values', () => {
        expect(formatLatency(null)).toBe(EMPTY_PLACEHOLDER)
        expect(formatLatency(undefined)).toBe(EMPTY_PLACEHOLDER)
        expect(formatLatency(Number.NaN)).toBe(EMPTY_PLACEHOLDER)
        expect(formatLatency(-1)).toBe(EMPTY_PLACEHOLDER)
    })
})

describe('formatRowCount', () => {
    it('renders row counts, keeping 0 as a meaningful value', () => {
        expect(formatRowCount(17)).toBe('17 行')
        expect(formatRowCount(0)).toBe('0 行')
    })

    it('marks 文本行数 with ≈ so it is not read as a record count', () => {
        // lines 是预格式化文本的非空行数（体量代理），不是记录数
        expect(formatRowCount(12, 'lines')).toContain('≈')
        expect(formatRowCount(12, 'lines')).toContain('行')
        expect(formatRowCount(12, 'lines')).toBe('≈12 行')
        // 大小写/空白归一化后同样生效
        expect(formatRowCount(12, ' LINES ')).toBe('≈12 行')
        // 真实记录数不加「≈」
        expect(formatRowCount(12, 'rows')).not.toContain('≈')
        expect(formatRowCount(12, undefined)).not.toContain('≈')
        expect(formatRowCount(12, null)).not.toContain('≈')
        // 未识别的单位保持旧行为，绝不猜
        expect(formatRowCount(12, 'cells')).not.toContain('≈')
        expect(formatRowCount(12, 'cells')).toBe('12 行')
    })

    it('renders a placeholder for missing or invalid values', () => {
        expect(formatRowCount(null)).toBe(EMPTY_PLACEHOLDER)
        expect(formatRowCount(undefined)).toBe(EMPTY_PLACEHOLDER)
        expect(formatRowCount(Number.NaN)).toBe(EMPTY_PLACEHOLDER)
        expect(formatRowCount(-3)).toBe(EMPTY_PLACEHOLDER)
        // 单位不影响「无值」的判定
        expect(formatRowCount(null, 'lines')).toBe(EMPTY_PLACEHOLDER)
    })
})

describe('formatProbeTimestamp', () => {
    it('formats east-8 timestamps without depending on the host timezone', () => {
        expect(formatProbeTimestamp('2026-09-24T09:00:03+08:00')).toBe('09-24 09:00')
        expect(formatProbeTimestamp('2026-09-24T09:00:03+08:00', { seconds: true })).toBe('09-24 09:00:03')
        expect(formatProbeTimestamp('2026-09-24T09:00:03+08:00', { year: true })).toBe('2026-09-24 09:00')
        // 同一时刻的 UTC 写法必须得到相同的东八区墙上时间
        expect(formatProbeTimestamp('2026-09-24T01:00:03Z')).toBe('09-24 09:00')
        expect(formatProbeTimestamp('2026-09-24 09:00:03')).toBe('09-24 09:00')
        expect(formatProbeTimestamp('2026-09-24')).toBe('09-24 00:00')
    })

    it('renders a placeholder for empty values and keeps unparseable text', () => {
        expect(formatProbeTimestamp(null)).toBe(EMPTY_PLACEHOLDER)
        expect(formatProbeTimestamp(undefined)).toBe(EMPTY_PLACEHOLDER)
        expect(formatProbeTimestamp('   ')).toBe(EMPTY_PLACEHOLDER)
        expect(formatProbeTimestamp('刚刚')).toBe('刚刚')
    })
})

describe('formatTradingDay / triggerLabel', () => {
    it('labels trading days and flags non-trading days distinctly', () => {
        expect(formatTradingDay(true)).toBe('交易日')
        expect(formatTradingDay(false)).toBe('非交易日')
        expect(formatTradingDay(null)).toBe(EMPTY_PLACEHOLDER)
        expect(isNonTradingDay(false)).toBe(true)
        expect(isNonTradingDay(true)).toBe(false)
        expect(isNonTradingDay(null)).toBe(false)
    })

    it('maps triggers to Chinese and keeps unknown values as-is', () => {
        expect(triggerLabel('scheduled')).toBe('定时')
        expect(triggerLabel('manual')).toBe('手动')
        expect(triggerLabel('MANUAL')).toBe('手动')
        expect(triggerLabel('cli')).toBe('cli')
        expect(triggerLabel(null)).toBe(EMPTY_PLACEHOLDER)
        expect(triggerLabel('')).toBe(EMPTY_PLACEHOLDER)
    })
})

describe('probeErrorText / truncateText / probeErrorSummary', () => {
    it('joins kind, type and message', () => {
        expect(
            probeErrorText(result({ error_kind: 'network', error_type: 'ConnectionError', error_message: '连不上' })),
        ).toBe('network / ConnectionError: 连不上')
        expect(probeErrorText(result({ error_kind: 'network', error_message: '连不上' }))).toBe('network: 连不上')
        expect(probeErrorText(result({ error_message: '连不上' }))).toBe('连不上')
        expect(probeErrorText(result())).toBe('')
        expect(probeErrorText(null)).toBe('')
    })

    it('truncates long text with an ellipsis and never exceeds max', () => {
        expect(truncateText('abcdef', 4)).toBe('abc…')
        expect(truncateText('abcdef', 4)).toHaveLength(4)
        expect(truncateText('abc', 10)).toBe('abc')
        expect(truncateText(null, 4)).toBe('')
        expect(truncateText('abcdef', 0)).toBe('abcdef')
    })

    it('produces a placeholder summary when there is no error', () => {
        expect(probeErrorSummary(result())).toBe(EMPTY_PLACEHOLDER)
        const long = probeErrorSummary(result({ error_message: 'x'.repeat(80) }), 20)
        expect(long).toHaveLength(20)
        expect(long.endsWith('…')).toBe(true)
    })
})

describe('filterProbeResultRows / listProbeFieldValues', () => {
    const rows = [
        result({ source: 'cn_baostock', category: 'cn_research_data', status: 'ok' }),
        result({ source: 'cn_akshare', category: 'cn_research_data', status: 'failed', error_message: 'timeout' }),
        result({ source: 'cn_akshare', category: 'cn_realtime', status: 'ok', tool: 'get_spot_df', label: '实时快照' }),
    ]

    it('filters by source and category, treating all/empty as no filter', () => {
        expect(filterProbeResultRows(rows, { source: 'cn_akshare' })).toHaveLength(2)
        expect(filterProbeResultRows(rows, { source: 'all' })).toHaveLength(3)
        expect(filterProbeResultRows(rows, { source: '' })).toHaveLength(3)
        expect(filterProbeResultRows(rows, { category: 'cn_realtime' })).toHaveLength(1)
        expect(filterProbeResultRows(rows, { source: 'cn_akshare', category: 'cn_realtime' })).toHaveLength(1)
    })

    it('matches the free-text query against source / dimension / error text', () => {
        expect(filterProbeResultRows(rows, { query: 'akshare' })).toHaveLength(2)
        expect(filterProbeResultRows(rows, { query: '实时' })).toHaveLength(1)
        expect(filterProbeResultRows(rows, { query: 'timeout' })).toHaveLength(1)
        expect(filterProbeResultRows(rows, { query: '不可用' })).toHaveLength(1)
        expect(filterProbeResultRows(rows, { query: '不存在的东西' })).toHaveLength(0)
    })

    it('tolerates empty input', () => {
        expect(filterProbeResultRows(null)).toEqual([])
        expect(filterProbeResultRows(undefined, { query: 'x' })).toEqual([])
    })

    it('lists unique sorted field values for the filter dropdowns', () => {
        expect(listProbeFieldValues(rows, 'source')).toEqual(['cn_akshare', 'cn_baostock'])
        expect(listProbeFieldValues(rows, 'category')).toEqual(['cn_realtime', 'cn_research_data'])
        expect(listProbeFieldValues(null, 'source')).toEqual([])
    })
})

describe('summariseBySource', () => {
    it('groups by source and reports the worst status per source', () => {
        const summaries = summariseBySource([
            result({ source: 'cn_baostock', status: 'ok' }),
            result({ source: 'cn_baostock', status: 'unsupported' }),
            result({ source: 'cn_akshare', status: 'ok' }),
            result({ source: 'cn_akshare', status: 'failed' }),
        ])
        expect(summaries.map(item => item.source)).toEqual(['cn_akshare', 'cn_baostock'])
        expect(summaries[0]).toMatchObject({ total: 2, ok: 1, failed: 1 })
        expect(summaries[0].worstMeta.label).toBe('不可用')
        // 「不支持」不是故障：一个可用 + 一个不支持的源仍然应该是绿的
        expect(summaries[1]).toMatchObject({ total: 2, ok: 1, unsupported: 1 })
        expect(summaries[1].worstMeta.label).toBe('可用')
        expect(summariseBySource(null)).toEqual([])
    })

    it('reports 超时 sources as 超时 (not 不可用) and counts them separately', () => {
        const summaries = summariseBySource([
            result({ source: 'cn_slow', status: 'ok' }),
            result({ source: 'cn_slow', status: 'timeout' }),
        ])
        expect(summaries[0]).toMatchObject({ total: 2, ok: 1, timeout: 1, failed: 0 })
        expect(summaries[0].worstMeta.label).toBe('超时')
        expect(summaries[0].worstMeta.dotClass).toContain('violet')
    })
})

describe('probeTierMeta / buildCapabilityRows', () => {
    it('labels tiers as 主源 / 兜底 / 最后手段', () => {
        expect(probeTierMeta(1).label).toBe('主源')
        expect(probeTierMeta(2).label).toBe('兜底')
        expect(probeTierMeta(3).label).toBe('最后手段')
        expect(probeTierMeta(4).label).toBe('T4')
        expect(probeTierMeta(null).label).toBe('未分级')
        expect(PROBE_TIER_LEGEND.map(item => item.label)).toEqual(['主源', '兜底', '最后手段'])
    })

    it('joins the expected matrix with the actual probe results', () => {
        const rows = buildCapabilityRows(
            [
                {
                    tool: 'get_daily_bars_df',
                    label: '历史日线（结构化）',
                    category: 'cn_research_data',
                    note: '本地 K 线库的生产源',
                    sources: [
                        { source: 'cn_akshare', tier: 2, batchable: false, needs_credential: false, note: '' },
                        { source: 'cn_baostock', tier: 1, batchable: true, needs_credential: false, note: '官方协议' },
                    ],
                },
            ],
            [result({ source: 'cn_baostock', tool: 'get_daily_bars_df', status: 'ok', latency_ms: 210 })],
        )
        expect(rows).toHaveLength(1)
        expect(rows[0].label).toBe('历史日线（结构化）')
        // tier 升序：主源在前
        expect(rows[0].sources.map(source => source.source)).toEqual(['cn_baostock', 'cn_akshare'])
        expect(rows[0].sources[0]).toMatchObject({
            probed: true,
            latencyLabel: '210 ms',
            status: 'ok',
            batchable: true,
        })
        expect(rows[0].sources[0].statusMeta.label).toBe('可用')
        // 矩阵里有、但还没探测过的组合 → 未探测（灰），而不是「不支持」
        expect(rows[0].sources[1]).toMatchObject({ probed: false, status: null })
        expect(rows[0].sources[1].statusMeta.label).toBe('未探测')
    })

    it('tolerates a missing matrix or missing fields', () => {
        expect(buildCapabilityRows(null, null)).toEqual([])
        const rows = buildCapabilityRows([{ tool: 'x' }], null)
        expect(rows).toHaveLength(1)
        expect(rows[0].label).toBe('x')
        expect(rows[0].sources).toEqual([])
    })
})

describe('buildTrendBars / trendSegmentHeights', () => {
    const runs: ProbeRunLike[] = [
        { ...SAMPLE_RUN, id: 'newest', started_at: '2026-09-25T09:00:00+08:00', total_count: 10, ok_count: 10, degraded_count: 0, failed_count: 0, unsupported_count: 0 },
        { ...SAMPLE_RUN, id: 'older', started_at: '2026-09-24T09:00:00+08:00', total_count: 20, ok_count: 15, degraded_count: 2, failed_count: 2, unsupported_count: 1 },
    ]

    it('renders oldest first with the tallest bar at 100%', () => {
        const bars = buildTrendBars(runs)
        expect(bars.map(bar => bar.id)).toEqual(['older', 'newest'])
        expect(bars[0].heightPct).toBe(100)
        expect(bars[1].heightPct).toBe(50)
        expect(bars[0].triggerLabel).toBe('定时')
        expect(bars[0].tradingDayLabel).toBe('交易日')
        expect(bars[0].durationLabel).toBe('11.2 s')
        expect(bars[0].timeLabel).toBe('09-24 09:00')
    })

    it('honours the limit and tolerates empty input', () => {
        expect(buildTrendBars(runs, 1).map(bar => bar.id)).toEqual(['newest'])
        expect(buildTrendBars(null)).toEqual([])
        expect(buildTrendBars(undefined, 0)).toEqual([])
    })

    it('splits each bar into proportional segments summing to 100', () => {
        const segments = trendSegmentHeights(resolveProbeCounts(SAMPLE_RUN))
        expect(segments.map(segment => segment.key)).toEqual([
            'ok',
            'degraded',
            'unsupported',
            'timeout',
            'failed',
        ])
        expect(segments.map(segment => segment.count)).toEqual([18, 2, 1, 0, 3])
        expect(segments.reduce((acc, segment) => acc + segment.pct, 0)).toBeCloseTo(100, 6)
        expect(segments[0].pct).toBeCloseTo(75, 6)
    })

    it('gives 超时 a violet segment so slow runs are visible in the trend', () => {
        const timeoutSegment = trendSegmentHeights({
            total: 10,
            ok: 6,
            degraded: 1,
            failed: 1,
            timeout: 2,
            unsupported: 0,
            other: 0,
        }).find(segment => segment.key === 'timeout')
        expect(timeoutSegment?.count).toBe(2)
        expect(timeoutSegment?.pct).toBeCloseTo(20, 6)
        expect(timeoutSegment?.barClass).toContain('violet')
        expect(timeoutSegment?.label).toBe('超时')
    })

    it('returns zero-height segments when there is no data', () => {
        const segments = trendSegmentHeights(null)
        expect(segments.every(segment => segment.pct === 0 && segment.count === 0)).toBe(true)
    })
})

describe('buildSourceTimelines', () => {
    const points: ProbeHistoryPointLike[] = [
        { checked_at: '2026-09-25T09:00:00+08:00', source: 'cn_baostock', tool: 't', status: 'ok' },
        { checked_at: '2026-09-24T09:00:00+08:00', source: 'cn_baostock', tool: 't', status: 'failed', latency_ms: 30000 },
        { checked_at: '2026-09-24T09:00:00+08:00', source: 'cn_akshare', tool: 't', status: 'degraded', row_count: 0 },
    ]

    it('groups points per source and orders cells oldest first', () => {
        const timelines = buildSourceTimelines(points)
        // 有 failed 的源排在有 degraded 的源前面（按严重度）
        expect(timelines.map(item => item.source)).toEqual(['cn_baostock', 'cn_akshare'])
        const baostock = timelines.find(item => item.source === 'cn_baostock')
        expect(baostock?.cells.map(cell => cell.statusMeta.label)).toEqual(['不可用', '可用'])
        expect(baostock?.cells[0].latencyLabel).toBe('30.0 s')
        expect(baostock?.lastCell?.statusMeta.label).toBe('可用')
        expect(baostock).toMatchObject({ total: 2, ok: 1, failed: 1 })
        expect(baostock?.worstMeta.label).toBe('不可用')
        const akshare = timelines.find(item => item.source === 'cn_akshare')
        expect(akshare?.cells[0].rowCountLabel).toBe('0 行')
        expect(akshare?.worstMeta.label).toBe('降级')
    })

    it('keeps only the most recent points per source and tolerates empty input', () => {
        const timelines = buildSourceTimelines(points, { limitPerSource: 1 })
        const baostock = timelines.find(item => item.source === 'cn_baostock')
        expect(baostock?.cells).toHaveLength(1)
        expect(baostock?.lastCell?.statusMeta.label).toBe('可用')
        expect(buildSourceTimelines(null)).toEqual([])
        expect(buildSourceTimelines([{ source: 'x' }])).toHaveLength(1)
    })

    it('passes row_unit through so 文本行数 shows ≈ while 记录数 does not', () => {
        const timelines = buildSourceTimelines([
            {
                checked_at: '2026-09-25T09:00:00+08:00',
                source: 'cn_text',
                tool: 't',
                status: 'ok',
                row_count: 12,
                row_unit: 'lines',
            },
            {
                checked_at: '2026-09-24T09:00:00+08:00',
                source: 'cn_table',
                tool: 't',
                status: 'timeout',
                row_count: 12,
                row_unit: 'rows',
            },
        ])
        expect(timelines.find(item => item.source === 'cn_text')?.cells[0].rowCountLabel).toBe('≈12 行')
        expect(timelines.find(item => item.source === 'cn_table')?.cells[0].rowCountLabel).toBe('12 行')
        // 超时源的最差状态是「超时」而不是「不可用」，也不计入 failed
        const slow = timelines.find(item => item.source === 'cn_table')
        expect(slow?.worstMeta.label).toBe('超时')
        expect(slow).toMatchObject({ total: 1, timeout: 1, failed: 0 })
    })
})

import { describe, expect, it } from 'vitest'

import { REAL_ANALYST_BLOCK_SAMPLES } from '@/utils/__fixtures__/realAnalystBlocks'
import { extractVerdict } from '@/utils/reportText'

/**
 * 真实语料一致性测试。
 *
 * 素材直接取自生产库 tradingagents.db 中已完成报告的 7 个分析师正文字段
 * （尾部 2600 字，含 ANALYST_JSON 机读块），期望方向由**后端解析器**
 * extract_analyst_structured_json 得出。
 *
 * 意义：合成样本只能证明「我按自己以为的形状写对了」；这份测试证明前端
 * 提取器与后端在**模型真正产出的文本**上给出同一个方向 —— 也就是修好回归
 * 的真正判据。
 */

// 与 reportText.ts 的 DIRECTION_ALIAS 保持一致的前端展示映射
const DISPLAY: Record<string, string> = {
    BULLISH: '看多',
    LEAN_BULLISH: '偏多',
    BEARISH: '看空',
    LEAN_BEARISH: '偏空',
    NEUTRAL: '中性',
    CAUTIOUS: '谨慎',
}

function toDisplay(direction: string): string {
    return DISPLAY[direction.toUpperCase()] ?? direction
}

describe('extractVerdict — 真实语料一致性', () => {
    it('样本集非空（防止素材丢失导致测试空跑）', () => {
        expect(REAL_ANALYST_BLOCK_SAMPLES.length).toBeGreaterThanOrEqual(10)
    })

    for (const sample of REAL_ANALYST_BLOCK_SAMPLES) {
        it(`${sample.report_id} / ${sample.field} 方向应为 ${sample.direction}`, () => {
            const verdict = extractVerdict(sample.text_tail)
            expect(verdict, `未能从真实文本解析出方向：${sample.report_id}/${sample.field}`).not.toBeNull()
            expect(verdict?.direction).toBe(toDisplay(sample.direction))
        })
    }

    it('真实语料中解析出的置信度均为合法 0-100 整数', () => {
        for (const sample of REAL_ANALYST_BLOCK_SAMPLES) {
            const verdict = extractVerdict(sample.text_tail)
            expect(Number.isInteger(verdict?.confidence)).toBe(true)
            expect(verdict?.confidence).toBeGreaterThanOrEqual(0)
            expect(verdict?.confidence).toBeLessThanOrEqual(100)
        }
    })

    it('真实 ANALYST_JSON 无 reason 时不得编造理由', () => {
        for (const sample of REAL_ANALYST_BLOCK_SAMPLES) {
            const verdict = extractVerdict(sample.text_tail)
            // ANALYST_JSON 没有 reason 字段；若解析出理由，必须能在原文中找到，不能是合成文案
            if (verdict?.reason) {
                expect(sample.text_tail).toContain(verdict.reason.slice(0, 12))
            }
        }
    })
})

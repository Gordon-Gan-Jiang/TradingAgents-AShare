import { describe, expect, it } from 'vitest'

import { extractVerdict, sanitizeReportMarkdown } from '@/utils/reportText'

/**
 * 回归测试：分析师机读块形态识别
 *
 * 背景：会话中把分析师提示词从 VERDICT 换成 ANALYST_JSON 后，前端 extractVerdict
 * 仍只认 `<!-- VERDICT: ... -->`，导致 7 张分析师卡片的方向徽章全部消失。
 * 这里用**生产库里真实出现过的**块形态作为输入（逐字拷贝，未加工）。
 */

// 生产库真实形态①：wrapped ANALYST_JSON（当前主流）
const WRAPPED_ANALYST_JSON =
    '<!-- ANALYST_JSON: {"agent_role":"market","direction":"偏多","confidence":68,' +
    '"evidence_anchors":[{"field":"MA5","value":"12.30","source":"get_stock_data"}],"data_gaps":[]} -->'

// 生产库真实形态②：模型漏掉 <!-- --> 注释包裹（裸写块）
const BARE_ANALYST_JSON =
    'ANALYST_JSON: {"agent_role":"fundamentals","direction":"看多","confidence":72,' +
    '"evidence_anchors":[],"data_gaps":["现金流量表字段为空"]}'

// 生产库真实形态③：双花括号 VERDICT（trader 提示词未 .format() 所致，189 份报告）
const DOUBLED_BRACE_VERDICT =
    '<!-- VERDICT: {{"direction": "偏空", "reason": "量价显示上涨动力减弱"}} -->'

// 生产库真实形态④：两个相邻 JSON 对象（第二个才是证据块）
const ADJACENT_OBJECTS =
    '<!-- ANALYST_JSON: {"agent_role":"fundamentals","direction":"看多","confidence":70}' +
    '{"evidence_anchors":[{"field":"ROE","value":"15%"}],"data_gaps":[]} -->'

// 生产库真实形态⑤：被 max_tokens 截断的块（必须不解析，绝不编造）
const TRUNCATED =
    '<!-- ANALYST_JSON: {"agent_role":"macro","direction":"中性","confidence":65,' +
    '"evidence_anchors":[{"field":"LPR数据","value":"LPR1Y为3.0%"}]'

// 生产库真实形态⑥：带 reason 的现行 VERDICT（最终裁决用，必须继续可用）
const PLAIN_VERDICT =
    '<!-- VERDICT: {"direction": "看空", "reason": "资金持续流出", "confidence": 75} -->'

describe('extractVerdict — ANALYST_JSON 支持（回归修复）', () => {
    it('能解析 wrapped ANALYST_JSON 的方向与置信度', () => {
        const v = extractVerdict(WRAPPED_ANALYST_JSON)
        expect(v).not.toBeNull()
        expect(v?.direction).toBe('偏多')
        expect(v?.confidence).toBe(68)
    })

    it('能解析裸写的 ANALYST_JSON（无 <!-- --> 包裹）', () => {
        const v = extractVerdict(BARE_ANALYST_JSON)
        expect(v).not.toBeNull()
        expect(v?.direction).toBe('看多')
        expect(v?.confidence).toBe(72)
    })

    it('ANALYST_JSON 没有 reason 字段时也要返回方向（不得因此返回 null）', () => {
        const v = extractVerdict(WRAPPED_ANALYST_JSON)
        expect(v?.direction).toBe('偏多')
        // 没有 reason 就必须留空，绝不编造文案
        expect(v?.reason ?? '').toBe('')
    })

    it('ANALYST_JSON 未给 confidence 时按方向给出回退置信度', () => {
        const v = extractVerdict(
            '<!-- ANALYST_JSON: {"agent_role":"news","direction":"看多","evidence_anchors":[],"data_gaps":[]} -->',
        )
        expect(v?.direction).toBe('看多')
        expect(v?.confidence).toBe(68) // 与后端 direction prior 一致（看多/看空=68）
    })

    it('ANALYST_JSON 的「中性」是合法方向，不得当作缺失', () => {
        const v = extractVerdict(
            '<!-- ANALYST_JSON: {"agent_role":"macro","direction":"中性","confidence":55} -->',
        )
        expect(v?.direction).toBe('中性')
        expect(v?.confidence).toBe(55)
    })

    it('英文方向别名仍能映射为中文展示标签', () => {
        const v = extractVerdict(
            '<!-- ANALYST_JSON: {"agent_role":"market","direction":"BULLISH","confidence":80} -->',
        )
        expect(v?.direction).toBe('看多')
    })
})

describe('extractVerdict — 容错形态', () => {
    it('能解析双花括号的 VERDICT（189 份历史报告）', () => {
        const v = extractVerdict(DOUBLED_BRACE_VERDICT)
        expect(v).not.toBeNull()
        expect(v?.direction).toBe('偏空')
        expect(v?.reason).toBe('量价显示上涨动力减弱')
    })

    it('两个相邻 JSON 对象时取第一个带 direction 的', () => {
        const v = extractVerdict(ADJACENT_OBJECTS)
        expect(v?.direction).toBe('看多')
        expect(v?.confidence).toBe(70)
    })

    it('被截断的块不得解析出方向（绝不编造）', () => {
        expect(extractVerdict(TRUNCATED)).toBeNull()
    })

    it('没有 direction 字段的 JSON 不得被当作方向', () => {
        expect(extractVerdict('<!-- ANALYST_JSON: {"agent_role":"news","confidence":70} -->')).toBeNull()
    })

    it('空文本与无块文本返回 null', () => {
        expect(extractVerdict('')).toBeNull()
        expect(extractVerdict(null)).toBeNull()
        expect(extractVerdict(undefined)).toBeNull()
        expect(extractVerdict('这是一段没有任何机读块的正文。')).toBeNull()
    })

    it('正文里的花括号不会误触发（字符串内的括号需被跳过）', () => {
        const text =
            '正文提到 { 一个未闭合的花括号，然后才是块：' +
            '<!-- ANALYST_JSON: {"agent_role":"market","direction":"偏空","confidence":60} -->'
        expect(extractVerdict(text)?.direction).toBe('偏空')
    })
})

describe('extractVerdict — 现行 VERDICT 行为不得回退', () => {
    it('标准 VERDICT 仍然可解析（reason/confidence 齐全）', () => {
        const v = extractVerdict(PLAIN_VERDICT)
        expect(v?.direction).toBe('看空')
        expect(v?.reason).toBe('资金持续流出')
        expect(v?.confidence).toBe(75)
    })

    it('同时存在 VERDICT 与 ANALYST_JSON 时，VERDICT 优先', () => {
        const both = `${PLAIN_VERDICT}\n\n${WRAPPED_ANALYST_JSON}`
        expect(extractVerdict(both)?.direction).toBe('看空')
    })

    it('1-100 的置信度按原值使用，0-1 的小数视为比例', () => {
        expect(extractVerdict('<!-- VERDICT: {"direction":"看多","reason":"x","confidence":72} -->')?.confidence).toBe(72)
        expect(extractVerdict('<!-- VERDICT: {"direction":"看多","reason":"x","confidence":0.72} -->')?.confidence).toBe(72)
    })
})

describe('sanitizeReportMarkdown — 机读块不得漏进正文（回归修复）', () => {
    it('应移除 wrapped ANALYST_JSON 块', () => {
        const cleaned = sanitizeReportMarkdown(`这是分析师正文。\n\n${WRAPPED_ANALYST_JSON}`)
        expect(cleaned).toContain('这是分析师正文。')
        expect(cleaned).not.toContain('ANALYST_JSON')
        expect(cleaned).not.toContain('evidence_anchors')
    })

    it('应移除裸写的 ANALYST_JSON 块', () => {
        const cleaned = sanitizeReportMarkdown(`这是分析师正文。\n\n${BARE_ANALYST_JSON}`)
        expect(cleaned).toContain('这是分析师正文。')
        expect(cleaned).not.toContain('ANALYST_JSON')
        expect(cleaned).not.toContain('data_gaps')
    })

    it('应移除 VERDICT 块（原行为不得回退）', () => {
        const cleaned = sanitizeReportMarkdown(`正文。\n\n${PLAIN_VERDICT}`)
        expect(cleaned).toContain('正文。')
        expect(cleaned).not.toContain('VERDICT')
    })

    it('应移除 RISK_JUDGE 等重复机读块，但必须保留 TRADE_PLAN', () => {
        const text =
            '正文。\n\n<!-- RISK_JUDGE: {"verdict": "pass", "revision_reason": "ok"} -->\n\n' +
            '<!-- TRADE_PLAN\ndirection: BUY\nhorizon_days: 20\nhard_stop_price: 11.50\n-->'
        const cleaned = sanitizeReportMarkdown(text)
        expect(cleaned).toContain('正文。')
        expect(cleaned).not.toContain('RISK_JUDGE')
        // TRADE_PLAN 必须保留：其结构化字段目前解析失败（57/57 块不可达），
        // 原始块是用户唯一能看到止盈阶梯/止损价的途径，剥离等于销毁信息。
        expect(cleaned).toContain('TRADE_PLAN')
        expect(cleaned).toContain('hard_stop_price: 11.50')
    })

    it('移除块之后不得把正文里的普通 JSON 一并删掉（避免过度剥离）', () => {
        const text = '正文中提到 {"foo": "bar"} 这样的普通 JSON 示例。\n\n' + WRAPPED_ANALYST_JSON
        const cleaned = sanitizeReportMarkdown(text)
        expect(cleaned).toContain('{"foo": "bar"}')
        expect(cleaned).not.toContain('ANALYST_JSON')
    })
})

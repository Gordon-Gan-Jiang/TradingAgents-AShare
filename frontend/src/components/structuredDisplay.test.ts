import { describe, expect, it } from 'vitest'
import {
    levelTone,
    statusTone,
    risksEmptyText,
    metricsEmptyText,
} from './structuredDisplay'

/**
 * 后端现在会把无法识别的等级落成显式的 `unknown`（审计 §4.6），而不是冒充
 * `medium`。前端必须**接得住**这个新取值：
 *
 * 旧代码写的是 `LEVEL_CONFIG[risk.level]` —— 直接下标取值。一旦 level 是
 * `unknown`（或历史脏数据 `"高"`），`config` 就是 `undefined`，
 * 紧跟的 `config.color` 会抛 `TypeError`，React 整棵树崩掉。
 *
 * 所以这里钉两条：
 *   1) 任何未知字符串都必须有**兜底显示**，绝不 undefined；
 *   2) 兜底必须**看得出来是"没识别出来"**，不能长得跟真实取值一样。
 */
describe('levelTone', () => {
    it('maps the three canonical levels', () => {
        expect(levelTone('high').label).toBe('高风险')
        expect(levelTone('medium').label).toBe('中风险')
        expect(levelTone('low').label).toBe('低风险')
    })

    it('never returns undefined for the new unknown value', () => {
        const tone = levelTone('unknown')
        expect(tone).toBeTruthy()
        expect(tone.color).toBeTruthy()
        expect(tone.bg).toBeTruthy()
    })

    it('shows unknown as "未识别" rather than silently as medium', () => {
        const tone = levelTone('unknown')
        expect(tone.label).not.toBe('中风险')
        expect(tone.label).toContain('未')
    })

    it('is safe for legacy dirty values that predate the fix', () => {
        for (const legacy of ['高', '严重', '', null, undefined, 'HIGH']) {
            const tone = levelTone(legacy as never)
            expect(tone).toBeTruthy()
            expect(tone.color).toBeTruthy()
            expect(tone.label).toBeTruthy()
        }
    })

    it('treats legacy chinese values as their real level, not unknown', () => {
        expect(levelTone('高').label).toBe('高风险')
        expect(levelTone('低').label).toBe('低风险')
    })
})

describe('statusTone', () => {
    it('maps the three canonical statuses', () => {
        expect(statusTone('good').key).toBe('good')
        expect(statusTone('neutral').key).toBe('neutral')
        expect(statusTone('bad').key).toBe('bad')
    })

    it('never returns undefined for unknown or legacy values', () => {
        for (const raw of ['unknown', '优', '差', '', null, undefined]) {
            const tone = statusTone(raw as never)
            expect(tone).toBeTruthy()
            expect(tone.color).toBeTruthy()
        }
    })

    it('does not render an unknown status as neutral', () => {
        expect(statusTone('unknown').key).not.toBe('neutral')
    })

    it('maps legacy chinese values to their real status', () => {
        expect(statusTone('优').key).toBe('good')
        expect(statusTone('差').key).toBe('bad')
    })
})

/**
 * §4.7：`null`（抽取结果里没有这个字段）与 `[]`（模型明确给出空列表 = 确认没有）
 * 必须给用户**不同的话**。旧代码只有一个分支 `risks.length === 0`，
 * 两者都说"分析完成后展示风险评估"，读者无法分辨"没抽到"与"确认无风险"。
 */
describe('risksEmptyText', () => {
    it('distinguishes not-extracted from confirmed-none', () => {
        const unknown = risksEmptyText(null)
        const confirmed = risksEmptyText([])
        expect(unknown).not.toBe(confirmed)
    })

    it('says it is a gap when nothing was extracted', () => {
        expect(risksEmptyText(null)).toContain('未')
        expect(risksEmptyText(undefined)).toContain('未')
    })

    it('says confirmed-none only for an explicit empty list', () => {
        expect(risksEmptyText([])).toContain('未发现')
    })
})

describe('metricsEmptyText', () => {
    it('distinguishes not-extracted from confirmed-none', () => {
        expect(metricsEmptyText(null)).not.toBe(metricsEmptyText([]))
    })
})

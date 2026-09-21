import { describe, expect, it, vi, afterEach } from 'vitest'

import {
    MODEL_MANAGEMENT_PATH,
    MODEL_SELECTION_STORAGE_KEY,
    describeSelection,
    isModelConfigError,
    modelProfileOptions,
    needsModelSetup,
    pickInitialProfileId,
    readStoredProfileId,
    storeProfileId,
} from '@/components/modelSelection'
import type { ModelProfile } from '@/types'

function profile(overrides: Partial<ModelProfile> & { id: string }): ModelProfile {
    return {
        name: '档案',
        llm_provider: 'openai',
        is_default: false,
        is_active: true,
        tags: [],
        has_api_key: true,
        ...overrides,
    } as ModelProfile
}

afterEach(() => {
    vi.unstubAllGlobals()
})

describe('modelProfileOptions', () => {
    it('prepends a "follow system default" option', () => {
        const options = modelProfileOptions([profile({ id: 'p1', name: '主力档' })])
        expect(options[0]).toEqual({ id: '', label: '默认模型（系统设置）' })
        expect(options[1].id).toBe('p1')
    })

    it('marks the default profile and hides inactive ones', () => {
        const options = modelProfileOptions([
            profile({ id: 'p1', name: '主力档', is_default: true }),
            profile({ id: 'p2', name: '停用档', is_active: false }),
        ])
        expect(options.map((o) => o.id)).toEqual(['', 'p1'])
        expect(options[1].label).toContain('（默认）')
    })

    it('tolerates null input', () => {
        expect(modelProfileOptions(null)).toHaveLength(1)
    })
})

describe('pickInitialProfileId', () => {
    const rows = [
        profile({ id: 'p1', name: '默认档', is_default: true }),
        profile({ id: 'p2', name: '备用档' }),
    ]

    it('reuses the stored choice when it still exists', () => {
        expect(pickInitialProfileId(rows, 'p2')).toBe('p2')
    })

    it('falls back to the default profile when the stored choice is gone', () => {
        // 用户删掉/停用了档案后，一直发已失效的 id 只会换来一个莫名其妙的错误
        expect(pickInitialProfileId(rows, 'deleted')).toBe('p1')
        expect(pickInitialProfileId(rows, 'p2-inactive')).toBe('p1')
    })

    it('ignores a stored id that points at an inactive profile', () => {
        const withInactive = [...rows, profile({ id: 'p3', is_active: false })]
        expect(pickInitialProfileId(withInactive, 'p3')).toBe('p1')
    })

    it('returns empty when nothing is configured', () => {
        expect(pickInitialProfileId([], 'p1')).toBe('')
        expect(pickInitialProfileId(null)).toBe('')
    })
})

describe('describeSelection', () => {
    it('names the effective profile, provider and model', () => {
        const text = describeSelection(
            [profile({ id: 'p1', name: '主力档', llm_provider: 'openai', deep_think_llm: 'gpt-4o' })],
            'p1',
        )
        expect(text).toContain('主力档')
        expect(text).toContain('openai')
        expect(text).toContain('gpt-4o')
    })

    it('warns when the selected profile has no api key', () => {
        const text = describeSelection(
            [profile({ id: 'p1', name: '空Key档', has_api_key: false })],
            'p1',
        )
        expect(text).toContain('未配置 API Key')
    })

    it('explains the fallback for the empty selection', () => {
        const text = describeSelection([profile({ id: 'p1', name: '默认档', is_default: true })], '')
        expect(text).toContain('默认档')
    })

    it('reports a vanished selection instead of pretending', () => {
        expect(describeSelection([], 'gone')).toContain('不可用')
    })
})

describe('isModelConfigError', () => {
    it('recognizes the messages users can actually fix', () => {
        expect(isModelConfigError('模型鉴权失败：API Key 无效或已过期，请在「系统 → 模型管理」中更新后重试。')).toBe(true)
        expect(isModelConfigError('尚未配置可用的模型：请填写 API Key')).toBe(true)
        expect(isModelConfigError('模型服务限流或额度不足')).toBe(true)
    })

    it('leaves unrelated failures alone', () => {
        expect(isModelConfigError('未能从消息中识别出单只股票标的。')).toBe(false)
        expect(isModelConfigError('')).toBe(false)
        expect(isModelConfigError(null)).toBe(false)
    })
})

describe('needsModelSetup', () => {
    it('is true only when nothing is usable', () => {
        expect(needsModelSetup([])).toBe(true)
        expect(needsModelSetup([profile({ id: 'p1', is_active: false })])).toBe(true)
        expect(needsModelSetup([profile({ id: 'p1' })])).toBe(false)
    })
})

describe('storage helpers', () => {
    it('round-trips the selected profile id', () => {
        const store = new Map<string, string>()
        vi.stubGlobal('localStorage', {
            getItem: (k: string) => store.get(k) ?? null,
            setItem: (k: string, v: string) => void store.set(k, v),
            removeItem: (k: string) => void store.delete(k),
        })
        storeProfileId('p2')
        expect(readStoredProfileId()).toBe('p2')
        expect(store.get(MODEL_SELECTION_STORAGE_KEY)).toBe('p2')
        storeProfileId('')
        expect(readStoredProfileId()).toBe('')
    })

    it('survives a throwing localStorage (private mode)', () => {
        vi.stubGlobal('localStorage', {
            getItem: () => {
                throw new Error('denied')
            },
            setItem: () => {
                throw new Error('denied')
            },
            removeItem: () => {
                throw new Error('denied')
            },
        })
        expect(() => storeProfileId('p1')).not.toThrow()
        expect(readStoredProfileId()).toBe('')
    })

    it('exposes the real model management route', () => {
        expect(MODEL_MANAGEMENT_PATH).toBe('/model-profiles')
    })
})

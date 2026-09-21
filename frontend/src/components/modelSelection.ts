/**
 * 研判对话的模型选择：纯函数 + 存储读写。
 *
 * ## 为什么单独成模块
 *
 * 面板组件很难测（流式 SSE + 副作用），但"该选哪个模型""这轮到底用的哪个"
 * 这类判断必须可测——它们是用户唯一能确认「我的选择生效了」的依据。
 * 本仓库既有前端测试都是纯函数测试（chatStream / verdictPresentation /
 * progressFeedback），这里沿用同一约定。
 *
 * ## 背景
 *
 * `POST /v1/chat` 早已接受 `model_profile_id`，但研判对话面板**从来没传过**，
 * 于是用户在「模型管理」里配好的档案对这一页完全无效，只能吃系统默认——
 * 截图里那句「模型鉴权失败」出现在这里，正是因为用户无法在这一页换一个能用的
 * 模型。本模块支撑的正是这个入口。
 */

import type { ModelProfile } from '@/types'

/** 记住用户的选择：换页面/刷新后不该被重置回默认。 */
export const MODEL_SELECTION_STORAGE_KEY = 'ap.judgement.modelProfileId'

/** 模型管理页的真实路由与导航名（文案必须与界面一致）。 */
export const MODEL_MANAGEMENT_PATH = '/model-profiles'
export const MODEL_MANAGEMENT_LABEL = '系统 → 模型管理'

export interface ModelOption {
    id: string
    label: string
}

function activeProfiles(profiles: ModelProfile[] | null | undefined): ModelProfile[] {
    return (profiles || []).filter((row) => row && row.is_active !== false)
}

/** 下拉选项：空值代表"跟随系统默认"。 */
export function modelProfileOptions(profiles: ModelProfile[] | null | undefined): ModelOption[] {
    return [
        { id: '', label: '默认模型（系统设置）' },
        ...activeProfiles(profiles).map((row) => ({
            id: row.id,
            label: `${row.name}${row.is_default ? '（默认）' : ''}`,
        })),
    ]
}

/**
 * 初值。记住的选择**必须仍然存在且启用**才沿用——否则用户删掉/停用了档案，
 * 这一页会一直发一个已失效的 id，然后收到一个看起来莫名其妙的错误。
 */
export function pickInitialProfileId(
    profiles: ModelProfile[] | null | undefined,
    storedId?: string | null,
): string {
    const rows = activeProfiles(profiles)
    if (storedId && rows.some((row) => row.id === storedId)) return storedId
    return rows.find((row) => row.is_default)?.id ?? ''
}

/** 当前生效模型的说明：用户要能确认"这轮到底用哪个"。 */
export function describeSelection(
    profiles: ModelProfile[] | null | undefined,
    selectedId: string,
): string {
    const rows = activeProfiles(profiles)
    if (!selectedId) {
        const fallback = rows.find((row) => row.is_default)
        return fallback
            ? `跟随默认档案：${fallback.name}`
            : '跟随系统设置（未配置模型档案）'
    }
    const row = rows.find((item) => item.id === selectedId)
    if (!row) return '所选档案已不可用，将回落到默认模型'
    const model = row.deep_think_llm || row.quick_think_llm || '未指定模型'
    const key = row.has_api_key ? '' : '，未配置 API Key'
    return `${row.name}：${row.llm_provider} / ${model}${key}`
}

/**
 * 是否属于"用户自己能修"的模型配置类失败。
 *
 * 这类失败必须给直达「系统 → 模型管理」的入口。只把它显示成一段红字，
 * 用户不知道该去哪修——截图里的情形正是如此。
 */
export function isModelConfigError(message?: string | null): boolean {
    const text = (message || '').trim()
    if (!text) return false
    return /API Key|鉴权|尚未配置|未配置模型|模型配置|额度|限流/.test(text)
}

/** 一个启用中的档案都没有：此时任何模型选择都无意义，应引导去配置。 */
export function needsModelSetup(profiles: ModelProfile[] | null | undefined): boolean {
    return activeProfiles(profiles).length === 0
}

export function readStoredProfileId(): string {
    try {
        return globalThis.localStorage?.getItem(MODEL_SELECTION_STORAGE_KEY) || ''
    } catch {
        return ''
    }
}

export function storeProfileId(profileId: string): void {
    try {
        if (profileId) globalThis.localStorage?.setItem(MODEL_SELECTION_STORAGE_KEY, profileId)
        else globalThis.localStorage?.removeItem(MODEL_SELECTION_STORAGE_KEY)
    } catch {
        // 隐私模式下 localStorage 会抛错：记不住选择是小事，不该让页面崩掉
    }
}

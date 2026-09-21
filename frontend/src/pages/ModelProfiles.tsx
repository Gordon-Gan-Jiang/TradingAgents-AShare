import { Link2, Loader2, Plus, Save, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'

import { api } from '@/services/api'
import type {
    ModelProfile,
    ModelProfileCreateRequest,
    ModelProfileUpdateRequest,
    RuntimeWarmupResult,
} from '@/types'

type EditState = Record<string, Partial<ModelProfileUpdateRequest>>

type ProviderPreset = {
    id: string
    label: string
    provider: string
    baseUrl: string
    protocol: string
    editableBaseUrl?: boolean
    baseUrlHint?: string
}

const PROVIDER_PRESETS: ProviderPreset[] = [
    { id: 'openai', label: 'OpenAI', provider: 'openai', baseUrl: 'https://api.openai.com/v1', protocol: 'OpenAI' },
    { id: 'anthropic', label: 'Anthropic', provider: 'anthropic', baseUrl: '', protocol: 'Anthropic' },
    { id: 'google', label: 'Google Gemini', provider: 'google', baseUrl: '', protocol: 'Google' },
    { id: 'dashscope', label: '阿里云百炼（DashScope）', provider: 'openai', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', protocol: 'OpenAI 兼容' },
    { id: 'deepseek', label: 'DeepSeek', provider: 'openai', baseUrl: 'https://api.deepseek.com/v1', protocol: 'OpenAI 兼容' },
    { id: 'moonshot', label: 'Moonshot AI（Kimi）', provider: 'openai', baseUrl: 'https://api.moonshot.cn/v1', protocol: 'OpenAI 兼容' },
    { id: 'zhipu', label: '智谱 AI', provider: 'openai', baseUrl: 'https://open.bigmodel.cn/api/paas/v4', protocol: 'OpenAI 兼容' },
    { id: 'siliconflow', label: '硅基流动', provider: 'openai', baseUrl: 'https://api.siliconflow.cn/v1', protocol: 'OpenAI 兼容' },
    {
        id: 'volcengine-ark',
        label: '火山引擎方舟（豆包等）',
        provider: 'openai',
        baseUrl: 'https://ark.cn-beijing.volces.com/api/v3',
        protocol: 'OpenAI 兼容',
        editableBaseUrl: true,
        baseUrlHint:
            '默认北京地域；若控制台为其他地域，请改为方舟文档中的 Base URL（须含 /api/v3）。模型名填接入点 ID。',
    },
    { id: 'custom-openai', label: '自定义 OpenAI 兼容', provider: 'openai', baseUrl: '', protocol: 'OpenAI 兼容', editableBaseUrl: true },
]

const MODEL_OPTIONS_BY_PRESET: Record<string, string[]> = {
    openai: ['gpt-4o', 'gpt-4.1', 'gpt-4.1-mini', 'o3', 'o4-mini'],
    anthropic: ['claude-3-5-sonnet-latest', 'claude-3-7-sonnet-latest'],
    google: ['gemini-2.0-flash', 'gemini-1.5-pro'],
    dashscope: ['qwen-max', 'qwen-plus', 'qwen-turbo'],
    deepseek: ['deepseek-chat', 'deepseek-reasoner', 'deepseek-v4-flash', 'deepseek-v4-pro'],
    moonshot: ['moonshot-v1-8k', 'moonshot-v1-32k'],
    zhipu: ['glm-4-plus', 'glm-4-air', 'glm-4-flash'],
    siliconflow: ['deepseek-ai/DeepSeek-V3', 'Qwen/Qwen2.5-72B-Instruct'],
    'volcengine-ark': ['doubao-seed-1-6-thinking', 'doubao-seed-1-6-flash'],
    'custom-openai': [''],
}

const DEEPSEEK_ALLOWED = new Set([
    'deepseek-chat',
    'deepseek-reasoner',
    'deepseek-v4-flash',
    'deepseek-v4-pro',
])

function parseTags(text: string): string[] {
    return text
        .split(/[,\n]/)
        .map((x) => x.trim())
        .filter(Boolean)
}

function formatWarmupSummary(results: RuntimeWarmupResult[]): { ok: boolean; text: string } {
    if (!results.length) return { ok: false, text: '未返回测试结果' }
    const hasError = results.some((item) => !!item.error)
    return {
        ok: !hasError,
        text: hasError ? '测试完成（存在失败项）' : '测试通过',
    }
}

export default function ModelProfiles() {
    const [profiles, setProfiles] = useState<ModelProfile[]>([])
    const [loading, setLoading] = useState(false)
    const [saving, setSaving] = useState(false)
    const [warmingProfileId, setWarmingProfileId] = useState<string | null>(null)
    const [draftWarming, setDraftWarming] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [successMsg, setSuccessMsg] = useState<string | null>(null)
    const [edits, setEdits] = useState<EditState>({})

    const [newForm, setNewForm] = useState<ModelProfileCreateRequest>({
        name: '',
        llm_provider: 'openai',
        quick_think_llm: '',
        deep_think_llm: '',
        api_key: '',
        description: '',
        is_default: false,
        is_active: true,
        tags: [],
    })
    const [providerPreset, setProviderPreset] = useState('openai')
    const [customBaseUrl, setCustomBaseUrl] = useState('')
    const [newTagsText, setNewTagsText] = useState('')
    const [draftWarmupResults, setDraftWarmupResults] = useState<RuntimeWarmupResult[]>([])
    const [draftWarmupOk, setDraftWarmupOk] = useState<boolean | null>(null)
    const [draftWarmupMsg, setDraftWarmupMsg] = useState<string | null>(null)
    const [showCreatePanel, setShowCreatePanel] = useState(false)

    const activeCount = useMemo(() => profiles.filter((p) => p.is_active).length, [profiles])
    const selectedPreset = useMemo(
        () => PROVIDER_PRESETS.find((item) => item.id === providerPreset) || PROVIDER_PRESETS[0],
        [providerPreset],
    )
    const modelOptions = MODEL_OPTIONS_BY_PRESET[providerPreset] || MODEL_OPTIONS_BY_PRESET.openai
    const effectiveProvider = selectedPreset.provider
    const effectiveBaseUrl = selectedPreset.editableBaseUrl ? customBaseUrl.trim() : selectedPreset.baseUrl
    const quickModel = (newForm.quick_think_llm || '').trim()
    const deepModel = (newForm.deep_think_llm || '').trim()

    const draftValidationError = useMemo(() => {
        if (!newForm.name?.trim()) return '请先填写模型配置名称。'
        if (!quickModel && !deepModel) return '请至少填写一个模型（常规模型或推理模型）。'
        if (providerPreset === 'deepseek') {
            const checkList = [quickModel, deepModel].filter(Boolean)
            const bad = checkList.find((m) => !DEEPSEEK_ALLOWED.has(m))
            if (bad) {
                return `DeepSeek 预设支持的模型：deepseek-chat（V3.2）、deepseek-reasoner（V3.2 推理）、deepseek-v4-flash、deepseek-v4-pro，当前为 ${bad}。`
            }
        }
        return null
    }, [newForm.name, quickModel, deepModel, providerPreset])

    const load = useCallback(async () => {
        setLoading(true)
        setError(null)
        try {
            const resp = await api.listModelProfiles(true)
            setProfiles(resp.profiles || [])
        } catch (e) {
            setError(e instanceof Error ? e.message : '加载模型配置失败')
        } finally {
            setLoading(false)
        }
    }, [])

    useEffect(() => {
        void load()
    }, [load])

    useEffect(() => {
        setDraftWarmupResults([])
        setDraftWarmupOk(null)
        setDraftWarmupMsg(null)
    }, [providerPreset, customBaseUrl, newForm.quick_think_llm, newForm.deep_think_llm, newForm.api_key])

    const setEdit = (id: string, patch: Partial<ModelProfileUpdateRequest>) => {
        setEdits((prev) => ({
            ...prev,
            [id]: { ...(prev[id] || {}), ...patch },
        }))
    }

    const handleDraftWarmup = async () => {
        if (draftValidationError) {
            setError(draftValidationError)
            return
        }
        setDraftWarming(true)
        setError(null)
        setSuccessMsg(null)
        setDraftWarmupMsg(null)
        try {
            const resp = await api.warmupConfig({
                llm_provider: effectiveProvider,
                backend_url: effectiveBaseUrl || undefined,
                quick_think_llm: quickModel || undefined,
                deep_think_llm: deepModel || undefined,
                api_key: (newForm.api_key || '').trim() || undefined,
                prompt: '你好',
            })
            const results = resp.results || []
            const summary = formatWarmupSummary(results)
            setDraftWarmupResults(results)
            setDraftWarmupOk(summary.ok)
            setDraftWarmupMsg(summary.text)
        } catch (e) {
            setDraftWarmupResults([])
            setDraftWarmupOk(false)
            setDraftWarmupMsg(e instanceof Error ? e.message : '草稿测试失败')
        } finally {
            setDraftWarming(false)
        }
    }

    const handleCreate = async () => {
        if (draftValidationError) {
            setError(draftValidationError)
            return
        }
        setSaving(true)
        setError(null)
        setSuccessMsg(null)
        try {
            await api.createModelProfile({
                ...newForm,
                name: newForm.name.trim(),
                llm_provider: effectiveProvider,
                quick_think_llm: quickModel || undefined,
                deep_think_llm: deepModel || undefined,
                backend_url: effectiveBaseUrl || undefined,
                api_key: (newForm.api_key || '').trim() || undefined,
                description: (newForm.description || '').trim() || undefined,
                tags: parseTags(newTagsText),
            })
            setNewForm({
                name: '',
                llm_provider: 'openai',
                quick_think_llm: '',
                deep_think_llm: '',
                api_key: '',
                description: '',
                is_default: false,
                is_active: true,
                tags: [],
            })
            setProviderPreset('openai')
            setCustomBaseUrl('')
            setNewTagsText('')
            setDraftWarmupResults([])
            setDraftWarmupOk(null)
            setDraftWarmupMsg(null)
            await load()
            setSuccessMsg('模型配置已创建')
        } catch (e) {
            setError(e instanceof Error ? e.message : '创建模型配置失败')
        } finally {
            setSaving(false)
        }
    }

    const handleSaveRow = async (row: ModelProfile) => {
        const patch = edits[row.id]
        if (!patch || Object.keys(patch).length === 0) return
        setSaving(true)
        setError(null)
        setSuccessMsg(null)
        try {
            await api.updateModelProfile(row.id, patch)
            setEdits((prev) => {
                const next = { ...prev }
                delete next[row.id]
                return next
            })
            await load()
            setSuccessMsg(`已保存「${row.name}」`)
        } catch (e) {
            setError(e instanceof Error ? e.message : '保存模型配置失败')
        } finally {
            setSaving(false)
        }
    }

    const handleDelete = async (row: ModelProfile) => {
        if (!window.confirm(`确认删除模型配置「${row.name}」？`)) return
        setSaving(true)
        setError(null)
        setSuccessMsg(null)
        try {
            await api.deleteModelProfile(row.id)
            await load()
            setSuccessMsg(`已删除「${row.name}」`)
        } catch (e) {
            setError(e instanceof Error ? e.message : '删除模型配置失败')
        } finally {
            setSaving(false)
        }
    }

    const handleWarmup = async (row: ModelProfile) => {
        setWarmingProfileId(row.id)
        setError(null)
        setSuccessMsg(null)
        try {
            const resp = await api.warmupModelProfile(row.id, '你好')
            const summary = formatWarmupSummary(resp.results || [])
            await load()
            setSuccessMsg(`「${row.name}」${summary.text}`)
        } catch (e) {
            setError(e instanceof Error ? e.message : '模型连接测试失败')
        } finally {
            setWarmingProfileId(null)
        }
    }

    return (
        <div className="space-y-4">
            <div className="flex items-center justify-between gap-3">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">模型管理</h1>
                    <p className="text-sm text-slate-500 dark:text-slate-400">
                        在新增阶段先做连通性测试，再保存为可复用的模型配置。
                    </p>
                </div>
                <div className="text-xs text-slate-500 dark:text-slate-400">
                    总计 {profiles.length} · 启用 {activeCount}
                </div>
            </div>
            <div className="flex justify-end">
                <button
                    type="button"
                    onClick={() => setShowCreatePanel(prev => !prev)}
                    className="btn-primary inline-flex items-center gap-2"
                >
                    <Plus className="h-4 w-4" />
                    {showCreatePanel ? '收起新增模型' : '添加模型'}
                </button>
            </div>

            {error && (
                <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-600">
                    {error}
                </div>
            )}
            {successMsg && (
                <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700">
                    {successMsg}
                </div>
            )}

            {showCreatePanel && (
                <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
                <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
                    <div className="mb-3 flex items-center gap-2 text-sm font-medium text-slate-700 dark:text-slate-200">
                        <Plus className="h-4 w-4 text-violet-500" />
                        新增模型配置
                    </div>
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                        <input
                            value={newForm.name}
                            onChange={(e) => setNewForm((p) => ({ ...p, name: e.target.value }))}
                            placeholder="名称，例如：DeepSeek-V4"
                            className="input"
                        />
                        <div>
                            <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">模型厂商</label>
                            <select
                                value={providerPreset}
                                onChange={(e) => {
                                    const nextPreset = e.target.value
                                    const preset = PROVIDER_PRESETS.find((p) => p.id === nextPreset)
                                    setProviderPreset(nextPreset)
                                    setNewForm((p) => ({
                                        ...p,
                                        llm_provider: preset?.provider || 'openai',
                                        quick_think_llm: '',
                                        deep_think_llm: '',
                                    }))
                                    if (nextPreset === 'volcengine-ark' && preset?.baseUrl) {
                                        const u = customBaseUrl.trim()
                                        if (!u || !/volces\.com/i.test(u)) setCustomBaseUrl(preset.baseUrl)
                                    }
                                }}
                                className="input"
                            >
                                {PROVIDER_PRESETS.map((option) => (
                                    <option key={option.id} value={option.id}>
                                        {option.label}
                                    </option>
                                ))}
                            </select>
                        </div>
                        <div>
                            <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">接入协议</label>
                            <div className="input flex items-center gap-2 bg-slate-50 text-slate-600 dark:bg-slate-900/70 dark:text-slate-300">
                                <Link2 className="h-4 w-4 text-slate-400" />
                                <span>{selectedPreset.protocol}</span>
                            </div>
                        </div>
                        {(selectedPreset.baseUrl || selectedPreset.editableBaseUrl) && (
                            <div className="md:col-span-2">
                                <label className="mb-1 block text-xs text-slate-500 dark:text-slate-400">Base URL</label>
                                <input
                                    value={selectedPreset.editableBaseUrl ? customBaseUrl : selectedPreset.baseUrl}
                                    onChange={(e) => setCustomBaseUrl(e.target.value)}
                                    className="input w-full"
                                    disabled={!selectedPreset.editableBaseUrl}
                                    placeholder="https://your-openai-compatible-endpoint/v1"
                                />
                                <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                                    {selectedPreset.baseUrlHint
                                        ? selectedPreset.baseUrlHint
                                        : selectedPreset.editableBaseUrl
                                            ? '自定义 OpenAI 兼容服务需要自行填写 Base URL。'
                                            : '该厂商默认通过预设地址接入，通常只需填写模型名和 API Key。'}
                                </p>
                            </div>
                        )}
                        <input
                            list="model-profile-quick-options"
                            value={newForm.quick_think_llm}
                            onChange={(e) => setNewForm((p) => ({ ...p, quick_think_llm: e.target.value }))}
                            placeholder="常规模型（可下拉选择或手输）"
                            className="input"
                        />
                        <input
                            list="model-profile-deep-options"
                            value={newForm.deep_think_llm}
                            onChange={(e) => setNewForm((p) => ({ ...p, deep_think_llm: e.target.value }))}
                            placeholder="推理模型（可下拉选择或手输）"
                            className="input"
                        />
                        <datalist id="model-profile-quick-options">
                            {modelOptions.map((m) => (
                                <option key={`quick-${m}`} value={m} />
                            ))}
                        </datalist>
                        <datalist id="model-profile-deep-options">
                            {modelOptions.map((m) => (
                                <option key={`deep-${m}`} value={m} />
                            ))}
                        </datalist>
                        <input
                            value={newForm.api_key}
                            onChange={(e) => setNewForm((p) => ({ ...p, api_key: e.target.value }))}
                            placeholder="API Key（可选，保存后加密）"
                            className="input md:col-span-2"
                        />
                        <input
                            value={newTagsText}
                            onChange={(e) => setNewTagsText(e.target.value)}
                            placeholder="标签，逗号分隔（可选）"
                            className="input"
                        />
                        <input
                            value={newForm.description || ''}
                            onChange={(e) => setNewForm((p) => ({ ...p, description: e.target.value }))}
                            placeholder="描述（可选）"
                            className="input"
                        />
                    </div>
                    <div className="mt-3 flex flex-wrap items-center gap-4 text-sm">
                        <label className="inline-flex items-center gap-2">
                            <input
                                type="checkbox"
                                checked={newForm.is_default || false}
                                onChange={(e) => setNewForm((p) => ({ ...p, is_default: e.target.checked }))}
                            />
                            设为默认
                        </label>
                        <label className="inline-flex items-center gap-2">
                            <input
                                type="checkbox"
                                checked={newForm.is_active ?? true}
                                onChange={(e) => setNewForm((p) => ({ ...p, is_active: e.target.checked }))}
                            />
                            启用
                        </label>
                        <button
                            type="button"
                            onClick={() => void handleDraftWarmup()}
                            disabled={draftWarming || !!draftValidationError}
                            className="btn-secondary inline-flex items-center gap-2"
                        >
                            {draftWarming ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                            测试草稿连接
                        </button>
                        <button
                            type="button"
                            onClick={() => void handleCreate()}
                            disabled={saving || !!draftValidationError}
                            className="btn-primary inline-flex items-center gap-2"
                        >
                            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
                            添加模型
                        </button>
                    </div>
                    {draftValidationError && (
                        <p className="mt-2 text-xs text-rose-600">{draftValidationError}</p>
                    )}
                </div>

                <div className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
                    <div className="text-sm font-medium text-slate-700 dark:text-slate-200">新增阶段测试</div>
                    <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                        建议先测试连接再保存。你刚遇到的 DeepSeek/豆包错配，会在这里被提前拦截或暴露。
                    </p>
                    {draftWarmupMsg && (
                        <div
                            className={`mt-3 rounded border px-2 py-1 text-xs ${
                                draftWarmupOk
                                    ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                                    : 'border-amber-200 bg-amber-50 text-amber-700'
                            }`}
                        >
                            {draftWarmupMsg}
                        </div>
                    )}
                    {draftWarmupResults.length > 0 && (
                        <div className="mt-3 space-y-2">
                            {draftWarmupResults.map((item, idx) => (
                                <div key={`${item.model}-${idx}`} className="rounded border border-slate-200 bg-slate-50 px-2 py-2 text-xs dark:border-slate-700 dark:bg-slate-800/50">
                                    <div className="font-medium text-slate-700 dark:text-slate-200">
                                        {item.targets.join(' / ')} · {item.model}
                                    </div>
                                    {item.error ? (
                                        <div className="mt-1 text-rose-600">{item.error}</div>
                                    ) : (
                                        <div className="mt-1 text-emerald-600">{item.content || '连接正常'}</div>
                                    )}
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            </div>
            )}

            <div className="rounded-xl border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
                <div className="border-b border-slate-200 px-4 py-3 text-sm font-medium text-slate-700 dark:border-slate-700 dark:text-slate-200">
                    模型配置列表
                </div>
                {loading ? (
                    <div className="px-4 py-8 text-center text-sm text-slate-500">加载中...</div>
                ) : profiles.length === 0 ? (
                    <div className="px-4 py-8 text-center text-sm text-slate-500">暂无模型配置，请先新增。</div>
                ) : (
                    <div className="divide-y divide-slate-100 dark:divide-slate-800">
                        {profiles.map((row) => {
                            const patch = edits[row.id] || {}
                            const dirty = Object.keys(patch).length > 0
                            return (
                                <div key={row.id} className="space-y-2 px-4 py-3 text-sm">
                                    <div className="flex flex-wrap items-center gap-2">
                                        <span className="font-medium text-slate-900 dark:text-slate-100">{row.name}</span>
                                        {row.is_default && (
                                            <span className="rounded bg-violet-100 px-2 py-0.5 text-xs text-violet-700 dark:bg-violet-900/30 dark:text-violet-300">
                                                默认
                                            </span>
                                        )}
                                        {!row.is_active && (
                                            <span className="rounded bg-slate-200 px-2 py-0.5 text-xs text-slate-600 dark:bg-slate-700 dark:text-slate-300">
                                                已停用
                                            </span>
                                        )}
                                        <span className="ml-auto text-xs text-slate-500">{row.llm_provider}</span>
                                    </div>
                                    {(row.last_probe_status || row.last_probe_error || row.last_probe_at) && (
                                        <div className="rounded border border-slate-200 bg-slate-50 px-2 py-1 text-xs text-slate-600 dark:border-slate-700 dark:bg-slate-800/50 dark:text-slate-300">
                                            最近测试：
                                            {row.last_probe_status === 'ok' ? ' 通过' : row.last_probe_status === 'failed' ? ' 失败' : ` ${row.last_probe_status || '-'}`}
                                            {row.last_probe_at ? ` · ${new Date(row.last_probe_at).toLocaleString('zh-CN')}` : ''}
                                            {row.last_probe_error ? ` · ${row.last_probe_error}` : ''}
                                        </div>
                                    )}
                                    <div className="grid grid-cols-1 gap-2 md:grid-cols-4">
                                        <input
                                            value={(patch.llm_provider as string | undefined) ?? row.llm_provider ?? ''}
                                            onChange={(e) => setEdit(row.id, { llm_provider: e.target.value })}
                                            placeholder="provider"
                                            className="input"
                                        />
                                        <input
                                            value={(patch.backend_url as string | undefined) ?? row.backend_url ?? ''}
                                            onChange={(e) => setEdit(row.id, { backend_url: e.target.value })}
                                            placeholder="Base URL"
                                            className="input"
                                        />
                                        <input
                                            value={(patch.quick_think_llm as string | undefined) ?? row.quick_think_llm ?? ''}
                                            onChange={(e) => setEdit(row.id, { quick_think_llm: e.target.value })}
                                            placeholder="常规模型"
                                            className="input"
                                        />
                                        <input
                                            value={(patch.deep_think_llm as string | undefined) ?? row.deep_think_llm ?? ''}
                                            onChange={(e) => setEdit(row.id, { deep_think_llm: e.target.value })}
                                            placeholder="推理模型"
                                            className="input"
                                        />
                                        <label className="inline-flex items-center gap-2 px-2 text-xs md:justify-end">
                                            <input
                                                type="checkbox"
                                                checked={patch.is_default ?? row.is_default}
                                                onChange={(e) => setEdit(row.id, { is_default: e.target.checked })}
                                            />
                                            默认
                                        </label>
                                        <label className="inline-flex items-center gap-2 px-2 text-xs md:justify-end">
                                            <input
                                                type="checkbox"
                                                checked={patch.is_active ?? row.is_active}
                                                onChange={(e) => setEdit(row.id, { is_active: e.target.checked })}
                                            />
                                            启用
                                        </label>
                                    </div>
                                    <div className="flex flex-wrap items-center gap-2">
                                        <button
                                            type="button"
                                            disabled={saving || warmingProfileId === row.id}
                                            onClick={() => void handleWarmup(row)}
                                            className="inline-flex items-center gap-1 rounded border border-blue-300 px-2 py-1 text-xs text-blue-600 hover:bg-blue-50 disabled:opacity-60 dark:border-blue-700 dark:text-blue-400 dark:hover:bg-blue-950/20"
                                        >
                                            {warmingProfileId === row.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
                                            测试连接
                                        </button>
                                        <button
                                            type="button"
                                            disabled={!dirty || saving}
                                            onClick={() => void handleSaveRow(row)}
                                            className="btn-secondary inline-flex items-center gap-1"
                                        >
                                            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                                            保存
                                        </button>
                                        <button
                                            type="button"
                                            disabled={saving}
                                            onClick={() => void handleDelete(row)}
                                            className="inline-flex items-center gap-1 rounded border border-rose-300 px-2 py-1 text-xs text-rose-600 hover:bg-rose-50 dark:border-rose-700 dark:text-rose-400 dark:hover:bg-rose-950/20"
                                        >
                                            <Trash2 className="h-3.5 w-3.5" />
                                            删除
                                        </button>
                                    </div>
                                </div>
                            )
                        })}
                    </div>
                )}
            </div>
        </div>
    )
}

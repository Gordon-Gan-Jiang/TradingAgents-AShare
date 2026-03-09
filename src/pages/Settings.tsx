import { useState, useEffect } from 'react'
import { Save, Key, Database, Loader2, MessageSquare, User, Trash2 } from 'lucide-react'
import { api } from '@/services/api'
import { useAuthStore } from '@/stores/authStore'

export default function Settings() {
    const { user } = useAuthStore()
    const [defaultAnalysts, setDefaultAnalysts] = useState(['market', 'social', 'news', 'fundamentals'])
    const [customPrompt, setCustomPrompt] = useState('')
    const [llmApiKey, setLlmApiKey] = useState('')
    const [hasStoredApiKey, setHasStoredApiKey] = useState(false)

    // LLM config (synced with backend)
    const [llmProvider, setLlmProvider] = useState('openai')
    const [deepThinkLlm, setDeepThinkLlm] = useState('')
    const [quickThinkLlm, setQuickThinkLlm] = useState('')
    const [maxDebateRounds, setMaxDebateRounds] = useState(1)
    const [maxRiskRounds, setMaxRiskRounds] = useState(1)

    const [configLoading, setConfigLoading] = useState(false)
    const [saving, setSaving] = useState(false)
    const [saved, setSaved] = useState(false)
    const [configError, setConfigError] = useState<string | null>(null)

    // Load local settings from localStorage
    useEffect(() => {
        try {
            const stored = localStorage.getItem('tradingagents-settings')
            if (stored) {
                const s = JSON.parse(stored) as Record<string, unknown> & {
                    defaultAnalysts?: string[]
                }
                if ('apiUrl' in s) {
                    delete s.apiUrl
                    localStorage.setItem('tradingagents-settings', JSON.stringify(s))
                }
                if (s.defaultAnalysts) setDefaultAnalysts(s.defaultAnalysts)
                if (typeof s.customPrompt === 'string') setCustomPrompt(s.customPrompt)
            }
        } catch {}
    }, [])

    // Fetch backend LLM config
    useEffect(() => {
        setConfigLoading(true)
        setConfigError(null)
        api.getConfig()
            .then(cfg => {
                setLlmProvider(cfg.llm_provider)
                setDeepThinkLlm(cfg.deep_think_llm)
                setQuickThinkLlm(cfg.quick_think_llm)
                setMaxDebateRounds(cfg.max_debate_rounds)
                setMaxRiskRounds(cfg.max_risk_discuss_rounds)
                setHasStoredApiKey(!!cfg.has_api_key)
            })
            .catch(err => {
                setConfigError(err instanceof Error ? err.message : '无法连接到后端')
            })
            .finally(() => setConfigLoading(false))
    }, [])

    const handleSave = async () => {
        setSaving(true)
        // Save local settings
        localStorage.setItem('tradingagents-settings', JSON.stringify({
            defaultAnalysts,
            customPrompt,
        }))
        localStorage.setItem('ta-custom-prompt', customPrompt)
        // Push LLM config to backend
        try {
            const response = await api.updateConfig({
                llm_provider: llmProvider,
                deep_think_llm: deepThinkLlm,
                quick_think_llm: quickThinkLlm,
                max_debate_rounds: maxDebateRounds,
                max_risk_discuss_rounds: maxRiskRounds,
                api_key: llmApiKey || undefined,
            })
            setHasStoredApiKey(!!response.has_api_key)
            setLlmApiKey('')
            setSaved(true)
            setTimeout(() => setSaved(false), 2000)
        } catch (err) {
            alert(err instanceof Error ? err.message : '保存配置失败')
        } finally {
            setSaving(false)
        }
    }

    const handleClearApiKey = async () => {
        if (!hasStoredApiKey) return
        setSaving(true)
        try {
            const response = await api.updateConfig({ clear_api_key: true })
            setHasStoredApiKey(!!response.has_api_key)
            setLlmApiKey('')
            setSaved(true)
            setTimeout(() => setSaved(false), 2000)
        } catch (err) {
            alert(err instanceof Error ? err.message : '清除密钥失败')
        } finally {
            setSaving(false)
        }
    }

    const toggleAnalyst = (analyst: string) => {
        setDefaultAnalysts(prev =>
            prev.includes(analyst) ? prev.filter(a => a !== analyst) : [...prev, analyst]
        )
    }

    return (
        <div className="space-y-6 max-w-3xl">
            <div>
                <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">系统设置</h1>
                <p className="text-slate-500 dark:text-slate-400 mt-1">配置当前账户的分析参数与私有模型</p>
            </div>

            <div className="card space-y-3">
                <div className="flex items-center gap-2">
                    <User className="w-5 h-5 text-cyan-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">账户空间</h2>
                </div>
                <div className="text-sm text-slate-600 dark:text-slate-300">
                    <div>当前登录：{user?.email || '-'}</div>
                    <div className="mt-1 text-slate-500 dark:text-slate-400">报告历史、分析任务和模型配置仅当前账户可见。</div>
                </div>
            </div>

            {/* LLM Config */}
            <div className="card space-y-4">
                <div className="flex items-center gap-2">
                    <Database className="w-5 h-5 text-purple-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">LLM 配置</h2>
                    {configLoading && <Loader2 className="w-4 h-4 animate-spin text-slate-400 ml-auto" />}
                </div>

                {configError && (
                    <p className="text-sm text-amber-500">⚠ {configError}（显示本地默认值）</p>
                )}

                <div className="grid grid-cols-2 gap-4">
                    <div>
                        <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                            LLM 提供商
                        </label>
                        <select
                            value={llmProvider}
                            onChange={e => setLlmProvider(e.target.value)}
                            className="input w-full"
                            disabled={configLoading}
                        >
                            <option value="openai">OpenAI</option>
                            <option value="anthropic">Anthropic</option>
                            <option value="google">Google</option>
                            <option value="dashscope">阿里云 DashScope</option>
                            <option value="deepseek">DeepSeek</option>
                        </select>
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                            深度思考模型
                        </label>
                        <input
                            type="text"
                            value={deepThinkLlm}
                            onChange={e => setDeepThinkLlm(e.target.value)}
                            className="input w-full"
                            placeholder="e.g. gpt-4o"
                            disabled={configLoading}
                        />
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                            快速推理模型
                        </label>
                        <input
                            type="text"
                            value={quickThinkLlm}
                            onChange={e => setQuickThinkLlm(e.target.value)}
                            className="input w-full"
                            placeholder="e.g. gpt-4o-mini"
                            disabled={configLoading}
                        />
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                            用户模型 Key
                        </label>
                        <div className="relative">
                            <Key className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                            <input
                                type="password"
                                value={llmApiKey}
                                onChange={e => setLlmApiKey(e.target.value)}
                                className="input w-full pl-10"
                                placeholder={hasStoredApiKey ? '已保存，留空则保持不变' : '输入你的模型 API Key'}
                                disabled={configLoading}
                            />
                        </div>
                        {hasStoredApiKey && (
                            <div className="mt-1 flex items-center justify-between gap-3">
                                <p className="text-xs text-emerald-500">当前账户已保存私有模型密钥</p>
                                <button
                                    type="button"
                                    onClick={handleClearApiKey}
                                    disabled={saving}
                                    className="inline-flex items-center gap-1 text-xs text-rose-500 hover:text-rose-600 disabled:opacity-50"
                                >
                                    <Trash2 className="w-3.5 h-3.5" />
                                    清除密钥
                                </button>
                            </div>
                        )}
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                            辩论轮数上限
                        </label>
                        <input
                            type="number"
                            min={1}
                            max={5}
                            value={maxDebateRounds}
                            onChange={e => setMaxDebateRounds(Number(e.target.value))}
                            className="input w-full"
                            disabled={configLoading}
                        />
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                            风险讨论轮数上限
                        </label>
                        <input
                            type="number"
                            min={1}
                            max={5}
                            value={maxRiskRounds}
                            onChange={e => setMaxRiskRounds(Number(e.target.value))}
                            className="input w-full"
                            disabled={configLoading}
                        />
                    </div>
                </div>
            </div>

            {/* Default Analysts */}
            <div className="card space-y-4">
                <div className="flex items-center gap-2">
                    <Database className="w-5 h-5 text-green-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">默认分析配置</h2>
                </div>

                <div>
                    <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                        默认启用的分析师
                    </label>
                    <div className="flex flex-wrap gap-2">
                        {[
                            { id: 'market', label: '市场分析' },
                            { id: 'social', label: '舆情分析' },
                            { id: 'news', label: '新闻分析' },
                            { id: 'fundamentals', label: '基本面' },
                        ].map(({ id, label }) => (
                            <label
                                key={id}
                                className={`px-4 py-2 rounded-lg border cursor-pointer transition-all ${
                                    defaultAnalysts.includes(id)
                                        ? 'bg-blue-50 dark:bg-blue-500/10 border-blue-500 text-blue-600 dark:text-blue-400'
                                        : 'bg-slate-100 dark:bg-slate-800 border-slate-200 dark:border-slate-600 text-slate-600 dark:text-slate-400'
                                }`}
                            >
                                <input
                                    type="checkbox"
                                    className="sr-only"
                                    checked={defaultAnalysts.includes(id)}
                                    onChange={() => toggleAnalyst(id)}
                                />
                                {label}
                            </label>
                        ))}
                    </div>
                </div>
            </div>

            {/* Custom Analysis Prompt */}
            <div className="card space-y-4">
                <div className="flex items-center gap-2">
                    <MessageSquare className="w-5 h-5 text-cyan-500" />
                    <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">自定义分析提示</h2>
                </div>
                <div>
                    <label className="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-2">
                        追加到每次分析请求
                    </label>
                    <textarea
                        value={customPrompt}
                        onChange={e => setCustomPrompt(e.target.value)}
                        className="input w-full min-h-[100px] resize-y"
                        placeholder="例如：请特别关注技术面支撑位，并结合量价关系分析。尽量给出明确的买卖点建议。"
                        maxLength={500}
                    />
                    <p className="text-xs text-slate-400 mt-1">{customPrompt.length}/500 · 留空则不追加额外提示</p>
                </div>
            </div>

            {/* Save */}
            <div className="flex items-center gap-4">
                <button
                    onClick={handleSave}
                    disabled={saving}
                    className="btn-primary flex items-center gap-2 disabled:opacity-60"
                >
                    {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                    保存设置
                </button>

                {saved && (
                    <span className="text-sm text-green-600 dark:text-green-400">✓ 设置已保存</span>
                )}
            </div>
        </div>
    )
}

import { useState } from 'react'
import { Save, Server, Key, Database } from 'lucide-react'

export default function Settings() {
    const [settings, setSettings] = useState({
        apiUrl: 'http://localhost:8000',
        apiKey: '',
        llmProvider: 'openai',
        defaultAnalysts: ['market', 'social', 'news', 'fundamentals'],
    })

    const [saved, setSaved] = useState(false)

    const handleSave = () => {
        // Save to localStorage or API
        localStorage.setItem('tradingagents-settings', JSON.stringify(settings))
        setSaved(true)
        setTimeout(() => setSaved(false), 2000)
    }

    return (
        <div className="space-y-6 max-w-3xl">
            {/* Header */}
            <div>
                <h1 className="text-2xl font-bold text-trading-text-primary">系统设置</h1>
                <p className="text-trading-text-secondary mt-1">
                    配置 API 连接和分析参数
                </p>
            </div>

            {/* API Settings */}
            <div className="card space-y-6">
                <div className="flex items-center gap-2 mb-4">
                    <Server className="w-5 h-5 text-trading-accent-blue" />
                    <h2 className="text-lg font-semibold text-trading-text-primary">API 配置</h2>
                </div>

                <div className="space-y-4">
                    <div>
                        <label className="block text-sm font-medium text-trading-text-secondary mb-2">
                            API 地址
                        </label>
                        <input
                            type="text"
                            value={settings.apiUrl}
                            onChange={(e) => setSettings({ ...settings, apiUrl: e.target.value })}
                            className="input w-full"
                            placeholder="http://localhost:8000"
                        />
                        <p className="text-xs text-trading-text-muted mt-1">
                            TradingAgents FastAPI 后端服务地址
                        </p>
                    </div>

                    <div>
                        <label className="block text-sm font-medium text-trading-text-secondary mb-2">
                            API Key (可选)
                        </label>
                        <div className="relative">
                            <Key className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-trading-text-muted" />
                            <input
                                type="password"
                                value={settings.apiKey}
                                onChange={(e) => setSettings({ ...settings, apiKey: e.target.value })}
                                className="input w-full pl-10"
                                placeholder="输入 API Key"
                            />
                        </div>
                    </div>
                </div>
            </div>

            {/* LLM Settings */}
            <div className="card space-y-6">
                <div className="flex items-center gap-2 mb-4">
                    <Database className="w-5 h-5 text-trading-accent-purple" />
                    <h2 className="text-lg font-semibold text-trading-text-primary">LLM 配置</h2>
                </div>

                <div className="space-y-4">
                    <div>
                        <label className="block text-sm font-medium text-trading-text-secondary mb-2">
                            默认 LLM 提供商
                        </label>
                        <select
                            value={settings.llmProvider}
                            onChange={(e) => setSettings({ ...settings, llmProvider: e.target.value })}
                            className="input w-full"
                        >
                            <option value="openai">OpenAI</option>
                            <option value="anthropic">Anthropic</option>
                            <option value="google">Google</option>
                        </select>
                    </div>
                </div>
            </div>

            {/* Default Analysts */}
            <div className="card space-y-6">
                <div className="flex items-center gap-2 mb-4">
                    <Database className="w-5 h-5 text-trading-accent-green" />
                    <h2 className="text-lg font-semibold text-trading-text-primary">默认分析配置</h2>
                </div>

                <div>
                    <label className="block text-sm font-medium text-trading-text-secondary mb-2">
                        默认启用的分析师
                    </label>
                    <div className="flex flex-wrap gap-2">
                        {['market', 'social', 'news', 'fundamentals'].map((analyst) => (
                            <label
                                key={analyst}
                                className={`px-4 py-2 rounded-lg border cursor-pointer transition-all ${settings.defaultAnalysts.includes(analyst)
                                        ? 'bg-trading-accent-blue/10 border-trading-accent-blue text-trading-accent-blue'
                                        : 'bg-trading-bg-tertiary border-trading-border text-trading-text-secondary'
                                    }`}
                            >
                                <input
                                    type="checkbox"
                                    className="sr-only"
                                    checked={settings.defaultAnalysts.includes(analyst)}
                                    onChange={(e) => {
                                        if (e.target.checked) {
                                            setSettings({
                                                ...settings,
                                                defaultAnalysts: [...settings.defaultAnalysts, analyst]
                                            })
                                        } else {
                                            setSettings({
                                                ...settings,
                                                defaultAnalysts: settings.defaultAnalysts.filter(a => a !== analyst)
                                            })
                                        }
                                    }}
                                />
                                {analyst === 'market' && '市场分析'}
                                {analyst === 'social' && '舆情分析'}
                                {analyst === 'news' && '新闻分析'}
                                {analyst === 'fundamentals' && '基本面'}
                            </label>
                        ))}
                    </div>
                </div>
            </div>

            {/* Save Button */}
            <div className="flex items-center gap-4">
                <button
                    onClick={handleSave}
                    className="btn-primary flex items-center gap-2"
                >
                    <Save className="w-4 h-4" />
                    保存设置
                </button>

                {saved && (
                    <span className="text-sm text-trading-accent-green">
                        ✓ 设置已保存
                    </span>
                )}
            </div>
        </div>
    )
}
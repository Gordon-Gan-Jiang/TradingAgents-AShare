import type { MouseEvent as ReactMouseEvent } from 'react'
import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import {
    AlertCircle,
    ArrowLeft,
    ArrowRight,
    CheckCircle2,
    Fingerprint,
    Loader2,
    LockKeyhole,
    Mail,
    Moon,
    Radar,
    RefreshCw,
    ShieldCheck,
    Sun,
    TrendingUp,
    Waypoints,
} from 'lucide-react'
import { api } from '@/services/api'
import { useAuthStore } from '@/stores/authStore'
import { useNavigate } from 'react-router-dom'
import '@/styles/login.css'

const SIGNALS = [
    { label: '研究框架', value: '14-Agent' },
    { label: '工作区', value: '私有' },
    { label: '报告流', value: '实时' },
]

const AGENT_GROUPS = [
    {
        title: '分析团队',
        count: '6',
        items: ['市场分析', '舆情分析', '新闻分析', '基本面分析', '宏观分析', '主力资金'],
        description: '围绕行情、情绪、新闻、财务、宏观与资金流建立初始判断。',
    },
    {
        title: '研究团队',
        count: '3',
        items: ['多头研究', '空头研究', '研究总监'],
        description: '组织多空辩论，收敛成投资计划与核心分歧。',
    },
    {
        title: '交易与风控',
        count: '4',
        items: ['交易员', '激进风控', '中性风控', '稳健风控'],
        description: '生成执行方案，并从不同风险偏好给出约束。',
    },
    {
        title: '组合决策',
        count: '1',
        items: ['组合经理'],
        description: '综合研究与风控结论，输出最终决策。',
    },
]

type ThemeMode = 'system' | 'light' | 'dark'

function isDarkFor(mode: ThemeMode) {
    if (mode === 'system') return window.matchMedia('(prefers-color-scheme: dark)').matches
    return mode === 'dark'
}

function readStoredTheme(): ThemeMode {
    const saved = localStorage.getItem('ta-theme') || 'system'
    return saved === 'light' || saved === 'dark' ? saved : 'system'
}

function BrandMark() {
    return (
        <svg width="42" height="42" viewBox="0 0 42 42" fill="none" aria-hidden="true" focusable="false">
            <defs>
                <linearGradient id="ap-mark-gradient" x1="3" y1="1" x2="39" y2="41" gradientUnits="userSpaceOnUse">
                    <stop stopColor="#22D3EE" />
                    <stop offset="0.52" stopColor="#3B82F6" />
                    <stop offset="1" stopColor="#8B5CF6" />
                </linearGradient>
            </defs>
            <rect width="42" height="42" rx="13" fill="url(#ap-mark-gradient)" />
            <rect x="0.75" y="0.75" width="40.5" height="40.5" rx="12.25" stroke="rgba(255,255,255,0.4)" strokeWidth="1.5" />
            <path
                d="M10.5 29 L18.5 19.5 L24 24.5 L31.5 13.5"
                stroke="white"
                strokeWidth="2.4"
                strokeLinecap="round"
                strokeLinejoin="round"
            />
            <circle cx="31.5" cy="13.5" r="2.6" fill="white" />
            <circle cx="10.5" cy="29" r="1.7" fill="rgba(255,255,255,0.72)" />
        </svg>
    )
}

export default function Login() {
    const navigate = useNavigate()
    const { setAuth } = useAuthStore()
    const [email, setEmail] = useState('')
    const [code, setCode] = useState('')
    const [step, setStep] = useState<'email' | 'code'>('email')
    const [loading, setLoading] = useState(false)
    const [resending, setResending] = useState(false)
    const [message, setMessage] = useState<string | null>(null)
    const [error, setError] = useState<string | null>(null)

    const codeInputRef = useRef<HTMLInputElement | null>(null)
    const spotlightFrame = useRef<number | null>(null)

    const themeMode = useMemo<ThemeMode>(readStoredTheme, [])
    const [currentTheme, setCurrentTheme] = useState<ThemeMode>(themeMode)
    const [isDark, setIsDark] = useState(() => isDarkFor(themeMode))

    const submitLabel = useMemo(() => step === 'email' ? '发送验证码' : '进入研究终端', [step])
    const pendingLabel = step === 'email' ? '正在发送' : '正在验证'

    // 进入验证码步骤后自动聚焦，减少一次点击
    useEffect(() => {
        if (step === 'code') codeInputRef.current?.focus()
    }, [step])

    useEffect(() => () => {
        if (spotlightFrame.current !== null) cancelAnimationFrame(spotlightFrame.current)
    }, [])

    const cycleTheme = () => {
        const next: ThemeMode = currentTheme === 'system' ? 'light' : currentTheme === 'light' ? 'dark' : 'system'
        const dark = isDarkFor(next)
        document.documentElement.classList.toggle('dark', dark)
        localStorage.setItem('ta-theme', next)
        setCurrentTheme(next)
        setIsDark(dark)
    }

    // 卡片跟随指针的柔光：仅在精细指针设备上启用，并合并到每一帧
    const handleSpotlight = (event: ReactMouseEvent<HTMLDivElement>) => {
        const card = event.currentTarget
        const x = event.clientX
        const y = event.clientY
        if (spotlightFrame.current !== null) return
        spotlightFrame.current = requestAnimationFrame(() => {
            spotlightFrame.current = null
            const rect = card.getBoundingClientRect()
            card.style.setProperty('--ap-mx', `${x - rect.left}px`)
            card.style.setProperty('--ap-my', `${y - rect.top}px`)
        })
    }

    const handleRequestCode = async (e: FormEvent) => {
        e.preventDefault()
        setLoading(true)
        setError(null)
        setMessage(null)
        try {
            const res = await api.requestLoginCode(email)
            setStep('code')
            setMessage(res.dev_code ? `开发环境验证码：${res.dev_code}` : '验证码已发送，请查收邮箱。')
        } catch (err) {
            setError(err instanceof Error ? err.message : '发送验证码失败')
        } finally {
            setLoading(false)
        }
    }

    const handleVerify = async (e: FormEvent) => {
        e.preventDefault()
        setLoading(true)
        setError(null)
        try {
            const res = await api.verifyLoginCode(email, code)
            setAuth(res.access_token, res.user)
            navigate('/analysis', { replace: true })
        } catch (err) {
            setError(err instanceof Error ? err.message : '登录失败')
        } finally {
            setLoading(false)
        }
    }

    const handleResend = async () => {
        setResending(true)
        setError(null)
        setMessage(null)
        try {
            const res = await api.requestLoginCode(email)
            setMessage(res.dev_code ? `开发环境验证码：${res.dev_code}` : '验证码已重新发送，请查收邮箱。')
        } catch (err) {
            setError(err instanceof Error ? err.message : '重新发送验证码失败')
        } finally {
            setResending(false)
        }
    }

    const backToEmail = () => {
        setCode('')
        setStep('email')
        setMessage(null)
        setError(null)
    }

    const emailInvalid = Boolean(error) && step === 'email'
    const codeInvalid = Boolean(error) && step === 'code'

    return (
        <div className="ap-login">
            <div className="ap-bg" aria-hidden="true">
                <div className="ap-bg-grid" />
                <div className="ap-blob ap-blob-1" />
                <div className="ap-blob ap-blob-2" />
                <div className="ap-blob ap-blob-3" />
                <div className="ap-bg-noise" />
            </div>

            <div className="ap-shell">
                <header className="ap-topbar">
                    <div className="ap-brand">
                        <span className="ap-mark">
                            <BrandMark />
                        </span>
                        <span className="ap-wordmark">
                            <strong>AlphaPilot</strong>
                            <span>A-Share Research OS</span>
                        </span>
                    </div>

                    <div className="ap-topbar-actions">
                        <span className="ap-pill ap-pill-topbar">
                            <Radar className="h-3.5 w-3.5" aria-hidden="true" />
                            A 股多智能体研究系统
                        </span>
                        <button
                            type="button"
                            className="ap-icon-btn"
                            onClick={cycleTheme}
                            aria-label={`切换主题，当前为${currentTheme === 'system' ? '跟随系统' : currentTheme === 'dark' ? '深色' : '浅色'}模式`}
                            title="切换主题（跟随系统 / 浅色 / 深色）"
                        >
                            {isDark
                                ? <Moon className="h-[18px] w-[18px]" aria-hidden="true" />
                                : <Sun className="h-[18px] w-[18px]" aria-hidden="true" />}
                        </button>
                    </div>
                </header>

                <div className="ap-cols">
                    <section className="ap-story" aria-labelledby="ap-headline">
                        <span className="ap-pill ap-rise" style={{ alignSelf: 'flex-start', animationDelay: '40ms' }}>
                            <span className="ap-dot" aria-hidden="true" />
                            14-Agent 协同研究框架
                        </span>

                        <h1 id="ap-headline" className="ap-display ap-rise" style={{ animationDelay: '100ms' }}>
                            为投研决策
                            <span className="ap-grad block">设计的智能工作台</span>
                        </h1>

                        <p className="ap-lede ap-rise" style={{ animationDelay: '160ms' }}>
                            从市场、舆情、新闻、基本面、宏观、主力资金到风控与组合决策，将 14 个 Agent 的协作过程沉淀为可追踪、可复盘、可持续更新的研究链路。
                        </p>

                        <dl className="ap-stats ap-rise" style={{ animationDelay: '220ms' }}>
                            {SIGNALS.map((item) => (
                                <div key={item.label} className="ap-stat">
                                    <dt>{item.label}</dt>
                                    <dd>{item.value}</dd>
                                </div>
                            ))}
                        </dl>

                        <div className="ap-panel ap-rise" style={{ animationDelay: '280ms' }}>
                            <div className="ap-panel-head">
                                <div>
                                    <div className="ap-eyebrow">14-Agent Architecture</div>
                                    <h2>协同分工概览</h2>
                                </div>
                                <span className="ap-panel-badge">
                                    <Waypoints className="h-[18px] w-[18px]" aria-hidden="true" />
                                </span>
                            </div>

                            <ol className="ap-stages">
                                {AGENT_GROUPS.map((group, index) => (
                                    <li key={group.title} className="ap-stage">
                                        <div className="ap-stage-top">
                                            <span className="ap-stage-idx">{String(index + 1).padStart(2, '0')}</span>
                                            <span className="ap-stage-count">{group.count} 名</span>
                                        </div>
                                        <h3>{group.title}</h3>
                                        <p>{group.description}</p>
                                        <ul className="ap-tags">
                                            {group.items.map((item) => (
                                                <li key={item}>{item}</li>
                                            ))}
                                        </ul>
                                    </li>
                                ))}
                            </ol>

                            <p className="ap-note">
                                <TrendingUp className="h-4 w-4" aria-hidden="true" />
                                <span>登录后可持续保存研究历史、模型配置与分析上下文，用于跟踪同一标的在不同日期下的判断演进。</span>
                            </p>
                        </div>
                    </section>

                    <section className="ap-auth">
                        <div
                            className="ap-card ap-rise"
                            style={{ animationDelay: '200ms' }}
                            onMouseMove={handleSpotlight}
                        >
                            <span className="ap-card-spot" aria-hidden="true" />

                            <div className="ap-card-head">
                                <div>
                                    <div className="ap-eyebrow">Identity Verification</div>
                                    <h2>进入个人研究空间</h2>
                                    <p>使用邮箱验证码登录，无需记忆密码。</p>
                                </div>
                                <span className="ap-shield">
                                    <ShieldCheck className="h-[22px] w-[22px]" aria-hidden="true" />
                                </span>
                            </div>

                            <ol className="ap-steps" aria-label="登录步骤">
                                <li className="ap-steps-rail" aria-hidden="true">
                                    <i style={{ transform: `scaleX(${step === 'code' ? 1 : 0})` }} />
                                </li>
                                <li
                                    className={`ap-step${step === 'code' ? ' is-done' : ''}`}
                                    aria-current={step === 'email' ? 'step' : undefined}
                                >
                                    <span className="ap-step-dot">1</span>
                                    <span className="ap-step-label">邮箱验证</span>
                                </li>
                                <li
                                    className="ap-step"
                                    aria-current={step === 'code' ? 'step' : undefined}
                                >
                                    <span className="ap-step-dot">2</span>
                                    <span className="ap-step-label">输入验证码</span>
                                </li>
                            </ol>

                            <form
                                className="ap-form"
                                onSubmit={step === 'email' ? handleRequestCode : handleVerify}
                                aria-busy={loading}
                            >
                                {step === 'code' && (
                                    <p className="ap-recipient">
                                        <span>验证码已发送至 <strong>{email}</strong></span>
                                        <button type="button" className="ap-linkbtn" onClick={backToEmail} disabled={loading}>
                                            修改
                                        </button>
                                    </p>
                                )}

                                <div className="ap-field">
                                    <label className="ap-label" htmlFor="ap-email">邮箱地址</label>
                                    <div className="ap-control">
                                        <Mail aria-hidden="true" />
                                        <input
                                            id="ap-email"
                                            name="email"
                                            type="email"
                                            className={`ap-input${emailInvalid ? ' is-invalid' : ''}`}
                                            value={email}
                                            onChange={(e) => setEmail(e.target.value)}
                                            placeholder="you@example.com"
                                            autoComplete="email"
                                            inputMode="email"
                                            disabled={loading || step === 'code'}
                                            aria-invalid={emailInvalid || undefined}
                                            required
                                        />
                                    </div>
                                    {step === 'email' && (
                                        <p className="ap-hint">验证码将发送至该邮箱，用于创建或进入你的私有研究空间。</p>
                                    )}
                                </div>

                                {step === 'code' && (
                                    <div className="ap-field">
                                        <label className="ap-label" htmlFor="ap-code">邮箱验证码</label>
                                        <div className="ap-control">
                                            <LockKeyhole aria-hidden="true" />
                                            <input
                                                id="ap-code"
                                                name="code"
                                                ref={codeInputRef}
                                                type="text"
                                                className={`ap-input ap-otp${codeInvalid ? ' is-invalid' : ''}`}
                                                value={code}
                                                onChange={(e) => setCode(e.target.value)}
                                                placeholder="输入 6 位验证码"
                                                autoComplete="one-time-code"
                                                inputMode="numeric"
                                                maxLength={6}
                                                aria-invalid={codeInvalid || undefined}
                                                aria-describedby="ap-code-hint"
                                                required
                                            />
                                        </div>
                                        <p className="ap-hint" id="ap-code-hint">验证码为 6 位数字，支持直接粘贴填写。</p>
                                    </div>
                                )}

                                {message && (
                                    <p className="ap-alert ap-alert-ok" role="status">
                                        <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                                        <span>{message}</span>
                                    </p>
                                )}

                                {error && (
                                    <p className="ap-alert ap-alert-err" role="alert">
                                        <AlertCircle className="h-4 w-4" aria-hidden="true" />
                                        <span>{error}</span>
                                    </p>
                                )}

                                <button type="submit" className="ap-cta" disabled={loading}>
                                    {loading
                                        ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                                        : <ArrowRight className="h-4 w-4" aria-hidden="true" />}
                                    {loading ? pendingLabel : submitLabel}
                                </button>

                                {step === 'code' && (
                                    <div className="grid gap-2.5">
                                        <button
                                            type="button"
                                            className="ap-ghost"
                                            onClick={handleResend}
                                            disabled={loading || resending}
                                        >
                                            {resending
                                                ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                                                : <RefreshCw className="h-4 w-4" aria-hidden="true" />}
                                            {resending ? '正在重新发送' : '重新发送验证码'}
                                        </button>
                                        <button type="button" className="ap-ghost" onClick={backToEmail} disabled={loading}>
                                            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
                                            更换邮箱地址
                                        </button>
                                    </div>
                                )}
                            </form>

                            <p className="ap-secure">
                                <Fingerprint className="h-4 w-4" aria-hidden="true" />
                                <span>当前账户将独占保存报告历史、模型密钥与分析上下文，适合持续跟踪个人研究对象。</span>
                            </p>
                        </div>
                    </section>
                </div>

                <footer className="ap-foot">
                    <p>
                        &copy; {new Date().getFullYear()} KylinMountain
                        <span className="ap-sep">&middot;</span>
                        仅限非商业用途（PolyForm NC 1.0）
                        <span className="ap-sep">&middot;</span>
                        <a href="https://github.com/KylinMountain/TradingAgents-AShare" target="_blank" rel="noopener noreferrer">
                            GitHub
                        </a>
                        <span className="ap-sep">&middot;</span>
                        <a href="https://app.510168.xyz" target="_blank" rel="noopener noreferrer">
                            官网
                        </a>
                    </p>
                </footer>
            </div>
        </div>
    )
}

/**
 * 研判对话面板：流式回答 + 工具轨迹 + 结构化判定卡片。
 *
 * ## 与既有 `ChatCopilotPanel` 的关系
 *
 * 既有面板走 `/v1/chat/completions`，语义是「提交分析任务」——响应只有任务回执。
 * 本面板走 `/v1/chat`，语义是「对话并得到有依据的回答」。两者并存：
 * 既有流程（重档、7 分析师管道）保持不动，避免为了新能力重构 1021 行组件
 * 而扩大回归面。
 *
 * ## 判定卡片的数据来源
 *
 * SSE 的 `VerdictEvent` 只携带精简判定（状态/置信度/失效条件文本）。完整卡片
 * 需要逐维度观测，因此面板收到判定事件后**直取** `/v1/signals/washout`——
 * 该端点不调用模型，成本可忽略。这也让「模型说错了」与「信号算错了」可以
 * 分开定位：卡片内容永远来自确定性层，不经过模型。
 */

import { Cpu, Loader2, Send, ShieldAlert, Sparkles, Wrench } from 'lucide-react'
import { FormEvent, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { useNavigate } from 'react-router-dom'
import remarkGfm from 'remark-gfm'

import {
    MODEL_MANAGEMENT_LABEL,
    MODEL_MANAGEMENT_PATH,
    describeSelection,
    isModelConfigError,
    modelProfileOptions,
    needsModelSetup,
    pickInitialProfileId,
    readStoredProfileId,
    storeProfileId,
} from '@/components/modelSelection'
import VerdictCard from '@/components/VerdictCard'
import { api } from '@/services/api'
import { applyChatEvent, consumeChatStream, createChatState, type ChatState } from '@/services/chatStream'
import type { ModelProfile, WashoutVerdict } from '@/types'

const PRESET_PROMPTS = [
    '600519 是不是在洗盘',
    '300750 现在多少钱',
    '帮我看看 000001.SZ 还能不能拿',
    '600519 为什么今天跌',
]

const STATUS_BANNER: Record<string, { text: string; cls: string }> = {
    degraded: {
        text: '本轮未能给出可溯源的回答，已降级为「说明缺什么」——未通过证据校验的内容不会被输出。',
        cls: 'bg-amber-50 text-amber-800 dark:bg-amber-500/10 dark:text-amber-200',
    },
    cancelled: { text: '本轮已取消。', cls: 'bg-slate-50 text-slate-600 dark:bg-slate-800 dark:text-slate-300' },
    failed: { text: '本轮执行失败。', cls: 'bg-rose-50 text-rose-700 dark:bg-rose-500/10 dark:text-rose-300' },
}

export default function ConversationalPanel() {
    const [input, setInput] = useState('')
    const [state, setState] = useState<ChatState>(createChatState())
    const [verdict, setVerdict] = useState<WashoutVerdict | null>(null)
    const [running, setRunning] = useState(false)
    const abortRef = useRef<AbortController | null>(null)
    // 会话续接：由 SessionEvent 下发，下一轮回传。
    // 修复审计 A1——此前组件从不传 conversationId，所谓对话实质是单轮。
    const [conversationId, setConversationId] = useState<string | undefined>(undefined)

    // ── 模型选择 ────────────────────────────────────────────────────────
    // `/v1/chat` 早就接受 model_profile_id，但这一页此前从不传，于是用户在
    // 「模型管理」里配好的档案对本页无效、只能吃系统默认；模型不可用时也无处
    // 换一个能用的。选择记在 localStorage，刷新与切换页面后保持。
    const navigate = useNavigate()
    const [modelProfiles, setModelProfiles] = useState<ModelProfile[]>([])
    const [modelProfileId, setModelProfileId] = useState<string>('')
    const [modelProfilesLoading, setModelProfilesLoading] = useState(false)

    useEffect(() => {
        let cancelled = false
        setModelProfilesLoading(true)
        api.listModelProfiles(false)
            .then((resp) => {
                if (cancelled) return
                const rows = resp.profiles || []
                setModelProfiles(rows)
                setModelProfileId(pickInitialProfileId(rows, readStoredProfileId()))
            })
            .catch(() => {
                if (!cancelled) setModelProfiles([])
            })
            .finally(() => {
                if (!cancelled) setModelProfilesLoading(false)
            })
        return () => {
            cancelled = true
        }
    }, [])

    const onSelectModel = (value: string) => {
        setModelProfileId(value)
        storeProfileId(value)
    }

    useEffect(() => () => abortRef.current?.abort(), [])

    const submit = async (text: string) => {
        const question = text.trim()
        if (!question || running) return

        setRunning(true)
        setVerdict(null)
        setInput('')
        let next = createChatState()
        setState(next)

        const controller = new AbortController()
        abortRef.current = controller

        try {
            const response = await api.streamChat(
                {
                    messages: [{ role: 'user', content: question }],
                    conversationId,
                    modelProfileId: modelProfileId || undefined,
                },
                controller.signal,
            )
            await consumeChatStream(
                response.body as ReadableStream<Uint8Array>,
                (event) => {
                    next = applyChatEvent(next, event)
                    setState(next)
                    if (next.conversationId && next.conversationId !== conversationId) {
                        setConversationId(next.conversationId)
                    }
                    if (event.event === 'VerdictEvent') {
                        const slim = (event.data as { verdict?: { symbol?: string; as_of?: string } }).verdict
                        if (slim?.symbol) {
                            // 直取完整判定（无模型调用）。失败不影响回答展示。
                            api.getWashoutVerdict(slim.symbol, { asOf: slim.as_of })
                                .then(setVerdict)
                                .catch(() => setVerdict(null))
                        }
                    }
                },
                { signal: controller.signal },
            )
        } catch (err) {
            setState((prev) => ({
                ...prev,
                error: err instanceof Error ? err.message : String(err),
                finished: true,
                failed: true,
            }))
        } finally {
            setRunning(false)
            abortRef.current = null
        }
    }

    const onSubmit = (e: FormEvent) => {
        e.preventDefault()
        void submit(input)
    }

    const banner = state.status ? STATUS_BANNER[state.status] : undefined

    return (
        <div className="flex h-full flex-col gap-3 p-4">
            <header className="flex items-center gap-2">
                <Sparkles className="h-4 w-4 text-blue-500" />
                <h1 className="text-sm font-semibold text-slate-800 dark:text-slate-100">研判对话</h1>
                <span className="text-xs text-slate-500 dark:text-slate-400">
                    回答只使用已取到的证据；没有依据时会明确说「未覆盖」，而不是编一个解释
                </span>
            </header>

            <div className="flex-1 space-y-3 overflow-y-auto">
                {state.answer && (
                    <div className="prose prose-sm max-w-none dark:prose-invert">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{state.answer}</ReactMarkdown>
                    </div>
                )}

                {state.tools.length > 0 && (
                    <div className="flex flex-wrap gap-2">
                        {state.tools.map((tool, index) => (
                            <span
                                key={`${tool.tool}-${index}`}
                                className={`flex items-center gap-1 rounded px-2 py-0.5 text-xs ${
                                    tool.ok
                                        ? 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300'
                                        : 'bg-rose-50 text-rose-600 dark:bg-rose-500/10 dark:text-rose-300'
                                }`}
                                title={tool.detail ?? undefined}
                            >
                                <Wrench className="h-3 w-3" />
                                {tool.tool}
                                {tool.ok && tool.evidenceCount > 0 && `（${tool.evidenceCount} 条证据）`}
                                {!tool.ok && '（失败）'}
                            </span>
                        ))}
                    </div>
                )}

                {state.notices.length > 0 && (
                    <ul className="space-y-1">
                        {state.notices.map((notice, index) => (
                            <li
                                key={index}
                                className="flex items-start gap-2 rounded bg-slate-50 p-2 text-xs text-slate-600 dark:bg-slate-800/50 dark:text-slate-300"
                            >
                                <ShieldAlert className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
                                <span>{notice}</span>
                            </li>
                        ))}
                    </ul>
                )}

                {banner && <div className={`rounded p-2 text-xs ${banner.cls}`}>{banner.text}</div>}
                {state.error && (
                    <div className="space-y-2 rounded bg-rose-50 p-2 text-xs text-rose-700 dark:bg-rose-500/10 dark:text-rose-300">
                        <div>{state.error}</div>
                        {isModelConfigError(state.error) && (
                            /* 模型类失败是用户自己能修的：只显示一段红字等于让人卡住 */
                            <button
                                type="button"
                                onClick={() => navigate(MODEL_MANAGEMENT_PATH)}
                                className="rounded border border-rose-300 px-2 py-1 font-medium hover:bg-rose-100 dark:border-rose-500/40 dark:hover:bg-rose-500/10"
                            >
                                去「{MODEL_MANAGEMENT_LABEL}」配置或更换模型
                            </button>
                        )}
                    </div>
                )}

                {state.protocolViolation && (
                    <div className="rounded bg-amber-50 p-2 text-xs text-amber-800 dark:bg-amber-500/10 dark:text-amber-200">
                        事件流出现协议异常（终止后仍有内容）。请刷新后重试。
                    </div>
                )}

                {verdict && <VerdictCard verdict={verdict} />}

                {!state.answer && !running && (
                    <div className="flex flex-wrap gap-2">
                        {PRESET_PROMPTS.map((prompt) => (
                            <button
                                key={prompt}
                                type="button"
                                onClick={() => void submit(prompt)}
                                className="rounded-full border border-slate-200 px-3 py-1 text-xs text-slate-600 hover:border-blue-300 hover:text-blue-600 dark:border-slate-700 dark:text-slate-300"
                            >
                                {prompt}
                            </button>
                        ))}
                    </div>
                )}
            </div>

            <div className="flex flex-wrap items-center gap-2 rounded-lg border border-slate-200 p-2 dark:border-slate-700">
                <Cpu className="h-3.5 w-3.5 text-slate-400" />
                <span className="text-[11px] text-slate-500 dark:text-slate-400">回答模型</span>
                <select
                    value={modelProfileId}
                    onChange={(e) => onSelectModel(e.target.value)}
                    disabled={running || modelProfilesLoading}
                    title={describeSelection(modelProfiles, modelProfileId)}
                    className="rounded border border-slate-200 bg-white px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
                >
                    {modelProfileOptions(modelProfiles).map((option) => (
                        <option key={option.id || 'default'} value={option.id}>
                            {option.label}
                        </option>
                    ))}
                </select>
                <span className="text-[11px] text-slate-400 dark:text-slate-500">
                    {describeSelection(modelProfiles, modelProfileId)}
                </span>
                {needsModelSetup(modelProfiles) && (
                    <button
                        type="button"
                        onClick={() => navigate(MODEL_MANAGEMENT_PATH)}
                        className="rounded border border-amber-300 px-2 py-0.5 text-[11px] text-amber-700 hover:bg-amber-50 dark:border-amber-500/40 dark:text-amber-300"
                    >
                        尚未配置模型档案，去「{MODEL_MANAGEMENT_LABEL}」新建
                    </button>
                )}
            </div>

            <form onSubmit={onSubmit} className="flex items-center gap-2">
                <input
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    placeholder="例如：600519 是不是在洗盘"
                    className="flex-1 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
                />
                <button
                    type="submit"
                    disabled={running || !input.trim()}
                    className="flex items-center gap-1 rounded-lg bg-blue-600 px-3 py-2 text-sm text-white disabled:opacity-50"
                >
                    {running ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                    发送
                </button>
            </form>
        </div>
    )
}

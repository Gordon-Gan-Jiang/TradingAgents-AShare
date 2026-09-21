/**
 * SSE 流解析与回合状态机（纯函数，可单测）。
 *
 * ## 为什么单独成模块
 *
 * 流式渲染的正确性几乎全在边界情况上：网络分片会把一帧切成两半、坏帧会
 * 混在正常帧之间、终止事件之后可能还有残留数据。这些如果写在组件里
 * （`ChatCopilotPanel.tsx` 已有 1021 行）就只能靠手测，而手测覆盖不到
 * 分片边界——那正是最容易静默丢字的地方。
 *
 * ## 一条语义要求
 *
 * **判定只能来自 `VerdictEvent`，不得从正文里解析。** 后端已经用
 * `VerdictGuard` 保证正文与探针判定一致；前端若再从散文里猜状态，等于
 * 在客户端重建一个更弱的护栏，且会在措辞变化时静默失效。
 */

import type { ChatEndEvent, ChatVerdictEvent } from '@/types'

export interface SseEvent {
    event: string
    data: Record<string, unknown>
}

export interface ChatToolActivity {
    tool: string
    ok: boolean
    detail: string | null
    elapsedMs: number
    evidenceCount: number
}

export interface ChatState {
    /** 本次会话 id。由 SessionEvent 下发——没有它前端无法续接下一轮。 */
    conversationId: string | null
    answer: string
    tools: ChatToolActivity[]
    verdict: ChatVerdictEvent['verdict'] | null
    notices: string[]
    error: string | null
    status: ChatEndEvent['status'] | null
    finished: boolean
    degraded: boolean
    cancelled: boolean
    failed: boolean
    /** 违反事件协议（例如终止后又收到 delta）。用于开发期暴露问题，而不是静默接受。 */
    protocolViolation: boolean
}

export function createChatState(): ChatState {
    return {
        conversationId: null,
        answer: '',
        tools: [],
        verdict: null,
        notices: [],
        error: null,
        status: null,
        finished: false,
        degraded: false,
        cancelled: false,
        failed: false,
        protocolViolation: false,
    }
}

function asRecord(value: unknown): Record<string, unknown> {
    return value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
}

/**
 * 解析一段 SSE 文本。
 *
 * 返回 `rest` 是**未完成的残帧**：必须与下一次 chunk 拼接后重试。
 * 丢掉残片会静默丢失回答内容——流式渲染里最难发现的一类 bug。
 */
export function parseSseChunk(
    buffer: string,
    chunk: string,
): { events: SseEvent[]; rest: string } {
    const combined = buffer + chunk
    const parts = combined.split('\n\n')
    const rest = parts.pop() ?? ''
    const events: SseEvent[] = []

    for (const part of parts) {
        let name = ''
        const dataLines: string[] = []
        for (const line of part.split('\n')) {
            if (line.startsWith(':')) continue // keep-alive 注释行
            if (line.startsWith('event: ')) name = line.slice(7).trim()
            else if (line.startsWith('data: ')) dataLines.push(line.slice(6))
            else if (line.startsWith('data:')) dataLines.push(line.slice(5))
        }
        if (!name || dataLines.length === 0) continue
        try {
            const parsed = JSON.parse(dataLines.join('\n'))
            events.push({ event: name, data: asRecord(parsed) })
        } catch {
            // 单帧坏数据不应让整轮回答渲染失败；跳过它继续消费后续帧
            continue
        }
    }

    return { events, rest }
}

export function applyChatEvent(state: ChatState, incoming: SseEvent): ChatState {
    const { event, data } = incoming
    const next: ChatState = { ...state, tools: [...state.tools], notices: [...state.notices] }

    if (state.finished && event === 'AnswerDelta') {
        // 终止之后不该再有内容：协议被破坏时暴露出来，而不是悄悄追加
        next.protocolViolation = true
        return next
    }

    switch (event) {
        case 'SessionEvent':
            // 回合一开始就下发，用户等待回答时即可建立会话状态
            next.conversationId = String(data.conversation_id ?? '') || null
            return next

        case 'AnswerDelta':
            next.answer = state.answer + String(data.text ?? '')
            return next

        case 'AnswerDone':
            // 以最终文本为准：它已经过校验，是权威版本
            next.answer = String(data.text ?? state.answer)
            return next

        case 'ToolStart':
            next.tools = [
                ...state.tools,
                {
                    tool: String(data.tool ?? ''),
                    ok: true,
                    detail: null,
                    elapsedMs: 0,
                    evidenceCount: 0,
                },
            ]
            return next

        case 'ToolDone': {
            const tool = String(data.tool ?? '')
            const activity: ChatToolActivity = {
                tool,
                ok: Boolean(data.ok),
                detail: (data.detail as string | null) ?? null,
                elapsedMs: Number(data.elapsed_ms ?? 0),
                evidenceCount: Number(data.evidence_count ?? 0),
            }
            // 用同名的最后一条替换，避免 ToolStart/ToolDone 各留一条造成重复展示
            const index = state.tools.map((t) => t.tool).lastIndexOf(tool)
            if (index >= 0) {
                next.tools = [...state.tools]
                next.tools[index] = activity
            } else {
                next.tools = [...state.tools, activity]
            }
            return next
        }

        case 'VerdictEvent':
            next.verdict = (data.verdict as ChatVerdictEvent['verdict']) ?? null
            return next

        case 'Notice':
            next.notices = [...state.notices, String(data.text ?? '')]
            return next

        case 'ErrorEvent':
            next.error = String(data.message ?? '')
            return next

        case 'EndEvent': {
            const status = (data.status as ChatEndEvent['status']) ?? 'failed'
            next.status = status
            next.finished = true
            next.degraded = status === 'degraded'
            next.cancelled = status === 'cancelled'
            next.failed = status === 'failed'
            return next
        }

        default:
            // 未知事件不影响已有状态：前向兼容比严格校验更重要
            return next
    }
}

export function sseEventsOf(events: SseEvent[], name: string): SseEvent[] {
    return events.filter((e) => e.event === name)
}

/**
 * 消费一个 `fetch` 响应体，逐事件回调。
 *
 * 不在这里做渲染，只做「读流 → 解析 → 回调」，因此可以被任何消费者复用
 * （对话面板、命令行、集成测试）。
 */
export async function consumeChatStream(
    body: ReadableStream<Uint8Array>,
    onEvent: (event: SseEvent) => void,
    options: { signal?: AbortSignal } = {},
): Promise<void> {
    const reader = body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    try {
        for (;;) {
            if (options.signal?.aborted) break
            const { done, value } = await reader.read()
            if (done) break
            const { events, rest } = parseSseChunk(buffer, decoder.decode(value, { stream: true }))
            buffer = rest
            for (const event of events) onEvent(event)
        }
        if (buffer.trim()) {
            // 流结束时仍有残帧：补一个换行再解析一次，尽量不丢内容
            const { events } = parseSseChunk(buffer + '\n\n', '')
            for (const event of events) onEvent(event)
        }
    } finally {
        reader.releaseLock()
    }
}

/**
 * 把非 2xx 响应翻译成一句用户能读的话。
 *
 * 后端错误体是 `{"detail": "..."}`；早期实现直接把原始 body 拼进提示，
 * 用户看到的是 `HTTP 400：{"detail":"未能从消息中识别..."}`——JSON 外壳
 * 混在中文提示里。这里解出 detail，非 JSON 体则原样透出。
 */
export function describeChatHttpError(status: number, body: string): string {
    const trimmed = (body || '').trim()
    let detail = ''
    if (trimmed) {
        try {
            const parsed = JSON.parse(trimmed) as { detail?: unknown; message?: unknown }
            const candidate = parsed?.detail ?? parsed?.message
            detail = typeof candidate === 'string' ? candidate : trimmed
        } catch {
            detail = trimmed
        }
    }
    return `对话连接失败（HTTP ${status}）${detail ? `：${detail}` : ''}`
}

import { describe, expect, it } from 'vitest'

import {
    applyChatEvent,
    createChatState,
    describeChatHttpError,
    parseSseChunk,
    sseEventsOf,
} from '@/services/chatStream'

const frame = (event: string, data: unknown) =>
    `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`

describe('parseSseChunk', () => {
    it('parses complete frames', () => {
        const { events, rest } = parseSseChunk('', frame('AnswerDelta', { text: '你好' }))
        expect(events).toHaveLength(1)
        expect(events[0].event).toBe('AnswerDelta')
        expect(events[0].data.text).toBe('你好')
        expect(rest).toBe('')
    })

    it('keeps a partial frame in the remainder instead of dropping it', () => {
        // 网络分片会把一帧切成两半；丢掉残片会静默丢失回答内容
        const first = parseSseChunk('', 'event: AnswerDelta\ndata: {"text":"你')
        expect(first.events).toHaveLength(0)
        expect(first.rest).toContain('AnswerDelta')

        const second = parseSseChunk(first.rest, '好"}\n\n')
        expect(second.events).toHaveLength(1)
        expect(second.events[0].data.text).toBe('你好')
        expect(second.rest).toBe('')
    })

    it('handles several frames arriving in one chunk', () => {
        const { events } = parseSseChunk(
            '',
            frame('AnswerDelta', { text: 'a' }) + frame('AnswerDelta', { text: 'b' }),
        )
        expect(events).toHaveLength(2)
    })

    it('skips malformed payloads without breaking the stream', () => {
        // 一帧坏数据不应让整轮回答渲染失败
        const { events } = parseSseChunk(
            '',
            'event: AnswerDelta\ndata: {not json}\n\n' + frame('AnswerDelta', { text: 'ok' }),
        )
        expect(events).toHaveLength(1)
        expect(events[0].data.text).toBe('ok')
    })

    it('ignores keep-alive comment lines', () => {
        const { events } = parseSseChunk('', ': keep-alive\n\n' + frame('Notice', { text: 'hi' }))
        expect(events).toHaveLength(1)
    })
})

describe('applyChatEvent', () => {
    it('accumulates answer deltas', () => {
        let state = createChatState()
        state = applyChatEvent(state, { event: 'AnswerDelta', data: { text: '缩量' } })
        state = applyChatEvent(state, { event: 'AnswerDelta', data: { text: '下杀' } })
        expect(state.answer).toBe('缩量下杀')
    })

    it('exposes the terminal status', () => {
        let state = createChatState()
        state = applyChatEvent(state, { event: 'EndEvent', data: { status: 'degraded', reason: 'x' } })
        expect(state.status).toBe('degraded')
        expect(state.finished).toBe(true)
    })

    it('distinguishes a degraded answer from a successful one', () => {
        // 「能回答但没有依据」必须与正常完成在界面上可区分
        const degraded = applyChatEvent(createChatState(), {
            event: 'EndEvent',
            data: { status: 'degraded', reason: 'unsourced_number' },
        })
        const completed = applyChatEvent(createChatState(), {
            event: 'EndEvent',
            data: { status: 'completed', reason: null },
        })
        expect(degraded.degraded).toBe(true)
        expect(completed.degraded).toBe(false)
    })

    it('marks cancelled and failed distinctly', () => {
        const cancelled = applyChatEvent(createChatState(), {
            event: 'EndEvent', data: { status: 'cancelled', reason: null },
        })
        const failed = applyChatEvent(createChatState(), {
            event: 'EndEvent', data: { status: 'failed', reason: 'boom' },
        })
        expect(cancelled.cancelled).toBe(true)
        expect(failed.failed).toBe(true)
    })

    it('records tool activity in order', () => {
        let state = createChatState()
        state = applyChatEvent(state, {
            event: 'ToolStart', data: { tool: 'signal_washout_probe', arguments: {} },
        })
        state = applyChatEvent(state, {
            event: 'ToolDone',
            data: { tool: 'signal_washout_probe', ok: true, detail: null, elapsed_ms: 120, evidence_count: 7 },
        })
        expect(state.tools).toHaveLength(1)
        expect(state.tools[0].tool).toBe('signal_washout_probe')
        expect(state.tools[0].ok).toBe(true)
        expect(state.tools[0].evidenceCount).toBe(7)
    })

    it('records a failing tool without dropping it', () => {
        const state = applyChatEvent(createChatState(), {
            event: 'ToolDone',
            data: { tool: 'no_such_tool', ok: false, detail: 'unknown tool', elapsed_ms: 1, evidence_count: 0 },
        })
        expect(state.tools).toHaveLength(1)
        expect(state.tools[0].ok).toBe(false)
    })

    it('captures the structured verdict separately from the prose', () => {
        // 判定必须来自结构化事件，不能从正文里解析——否则护栏在前端被绕过
        const state = applyChatEvent(createChatState(), {
            event: 'VerdictEvent',
            data: { verdict: { id: 'V1', state: 'washout', confidence: 61, daily_only: true } },
        })
        expect(state.verdict?.state).toBe('washout')
        expect(state.verdict?.daily_only).toBe(true)
    })

    it('keeps notices so budget truncation stays visible', () => {
        const state = applyChatEvent(createChatState(), {
            event: 'Notice', data: { text: '已达工具调用预算', kind: 'notice' },
        })
        expect(state.notices).toEqual(['已达工具调用预算'])
    })

    it('records errors', () => {
        const state = applyChatEvent(createChatState(), {
            event: 'ErrorEvent', data: { message: 'boom' },
        })
        expect(state.error).toBe('boom')
    })

    it('ignores unknown event types without breaking state', () => {
        const before = createChatState()
        const after = applyChatEvent(before, { event: 'SomethingNew', data: {} })
        expect(after.answer).toBe('')
        expect(after.status).toBeNull()
    })

    it('does not treat a late delta as part of the final answer silently', () => {
        // 终止之后不该再有 delta；若出现说明协议被破坏，状态必须反映出来
        let state = createChatState()
        state = applyChatEvent(state, { event: 'EndEvent', data: { status: 'completed', reason: null } })
        state = applyChatEvent(state, { event: 'AnswerDelta', data: { text: 'x' } })
        expect(state.protocolViolation).toBe(true)
    })
})

describe('sseEventsOf', () => {
    it('filters by event name', () => {
        const events = [
            { event: 'AnswerDelta', data: { text: 'a' } },
            { event: 'Notice', data: { text: 'b' } },
        ]
        expect(sseEventsOf(events, 'AnswerDelta')).toHaveLength(1)
    })
})

describe('session continuity', () => {
    it('captures the conversation id from SessionEvent', () => {
        // 修复审计 A1：没有这个 id，前端无法续接下一轮
        const state = applyChatEvent(createChatState(), {
            event: 'SessionEvent',
            data: { conversation_id: 'conv-1' },
        })
        expect(state.conversationId).toBe('conv-1')
    })

    it('starts with no conversation id', () => {
        expect(createChatState().conversationId).toBeNull()
    })

    it('keeps the id across subsequent events', () => {
        let state = applyChatEvent(createChatState(), {
            event: 'SessionEvent', data: { conversation_id: 'conv-2' },
        })
        state = applyChatEvent(state, { event: 'AnswerDelta', data: { text: 'x' } })
        state = applyChatEvent(state, { event: 'EndEvent', data: { status: 'completed', reason: null } })
        expect(state.conversationId).toBe('conv-2')
    })
})

describe('describeChatHttpError', () => {
    it('surfaces the backend detail instead of raw JSON', () => {
        const message = describeChatHttpError(
            400,
            JSON.stringify({ detail: '未能从消息中识别出股票标的。请输入代码（如 600519.SH）。' }),
        )
        expect(message).toContain('HTTP 400')
        expect(message).toContain('未能从消息中识别出股票标的')
        // 不该把 JSON 外壳原样甩给用户
        expect(message).not.toContain('{"detail"')
    })

    it('falls back to the raw body when it is not JSON', () => {
        expect(describeChatHttpError(500, 'Internal Server Error')).toContain('Internal Server Error')
    })

    it('omits the separator when there is no body', () => {
        expect(describeChatHttpError(500, '')).toBe('对话连接失败（HTTP 500）')
    })
})

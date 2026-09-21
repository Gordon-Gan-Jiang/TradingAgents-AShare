/**
 * 研判对话页。
 *
 * 新能力的**真实用户入口**：本仓库有过「实现了却没人用」的先例
 * （recommendation_eval_items 0 行、/v1/backtest 无前端调用者），
 * 因此每个能力都必须挂到导航上、可点、可用。
 */

import ConversationalPanel from '@/components/ConversationalPanel'
import VerdictHistoryPanel from '@/components/VerdictHistoryPanel'

export default function Judgement() {
    return (
        <div className="flex h-[calc(100vh-4rem)] flex-col gap-3 overflow-y-auto lg:flex-row">
            <div className="min-h-[28rem] flex-1 lg:min-h-0">
                <ConversationalPanel />
            </div>
            {/* 判定可回看后续走势：校准闭环的对外窗口。
                没有这个视图，判定就只是「当下说了一句话」，无法被检验。 */}
            <div className="lg:w-[26rem] lg:flex-shrink-0">
                <VerdictHistoryPanel limit={20} />
            </div>
        </div>
    )
}

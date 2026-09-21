"""信号与判定相关的端点。

## 为什么把这些端点做成「无模型调用」的直取接口

`GET /v1/signals/washout` 直接返回确定性探针的结构化判定，不经过任何 LLM。
这有三个用处：

1. **前端判定卡片**可以独立渲染，不依赖对话链路
2. **调试与回归**：判定质量问题可以直接定位到信号层，而不是在「模型说错了」
   与「信号算错了」之间猜
3. **成本**：大量重复查看不需要付模型费用

`GET /v1/signals/verdicts` 返回历史判定及其后续实际走势——这是「判定可回看」
的产品要求，也是校准闭环的对外窗口。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies import current_user
from api.services import signal_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/signals", tags=["signals"])

DISCLAIMER = (
    "本结论为基于可观测数据的概率化研究判断，不构成投资建议。"
    "判定附带失效条件，请在条件触发时重新评估。"
)


@router.get("/washout")
def get_washout_verdict(
    symbol: str = Query(..., description="A 股代码，如 600519.SH"),
    as_of: str | None = Query(None, description="评估基准日 YYYY-MM-DD，默认最近交易日"),
    window_days: int = Query(20, ge=6, le=120, description="回看交易日数"),
    persist: bool = Query(False, description="是否把判定落库（供 T+3/T+5 校准）"),
    user=Depends(current_user),
):
    """直取洗盘/派发判定。**不调用任何模型。**"""
    try:
        payload = signal_service.evaluate_symbol(
            symbol=symbol,
            as_of=as_of,
            window_days=window_days,
            user_id=getattr(user, "id", None),
            persist=persist,
        )
    except signal_service.SignalEvaluationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    payload["disclaimer"] = DISCLAIMER
    return payload


@router.get("/verdicts")
def list_verdicts(
    symbol: str | None = Query(None, description="按标的过滤"),
    limit: int = Query(50, ge=1, le=200),
    user=Depends(current_user),
):
    """历史判定 + 后续实际走势。判定不可变，只做追加。"""
    return signal_service.list_verdicts(user_id=user.id, symbol=symbol, limit=limit)


@router.post("/backfill")
def trigger_backfill(
    horizon_days: int | None = Query(
        None, description="只回填指定持有期（交易日）；留空回填 T+3 与 T+5"
    ),
    limit: int = Query(500, ge=1, le=2000, description="单次最多处理的判定数"),
    user=Depends(current_user),
):
    """手动触发 T+3/T+5 回填。

    刻意对当前用户作用域执行：校准统计本就按用户隔离，而且这样普通用户也能
    触发自己判定的回填，不必等定时任务。定时调度接线见 tasks.md 7.3。

    回填是**幂等且不可改写**的：已存在结果的 (判定, 持有期) 会被跳过。
    """
    from api.services import signal_outcome_service
    from tradingagents.dataflows.trade_calendar import cn_today_str

    horizons = (horizon_days,) if horizon_days else signal_outcome_service.DEFAULT_HORIZONS
    try:
        return signal_outcome_service.run_backfill(
            today=cn_today_str(), horizons=horizons, user_id=user.id, limit=limit
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("backfill failed: %s", exc)
        raise HTTPException(status_code=422, detail=f"回填失败：{exc}") from exc


@router.post("/replay")
def run_replay(
    symbols: str = Query(..., description="逗号分隔的 A 股代码，建议 ≥20 只"),
    dates: str = Query(..., description="逗号分隔的基准日 YYYY-MM-DD，建议 ≥25 个"),
    window_days: int = Query(20, ge=6, le=120),
    horizon_days: int = Query(5, ge=1, le=20),
    max_points: int = Query(600, ge=1, le=3000, description="单次最多回放的 (标的, 日期) 组合数"),
    user=Depends(current_user),
):
    """离线回放：对历史基准日重跑探针并回填兑现，产出标定报告。

    **只用于否决维度，不用于激活权重** —— 同一份代码既选样本又调参，
    回放上的最优可能只是过拟合那段行情。报告里的
    `authority.may_activate_weights` 恒为 false，激活权重必须由前瞻样本支持。

    该操作会实际拉取历史行情，耗时随组合数增长；用 `max_points` 控制规模。
    """
    from api.services import signal_replay_service

    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()]
    date_list = [d.strip() for d in dates.split(",") if d.strip()]
    if not symbol_list or not date_list:
        raise HTTPException(status_code=422, detail="symbols 与 dates 均不能为空")

    points = signal_replay_service.build_replay_points(symbols=symbol_list, dates=date_list)
    truncated = len(points) > max_points
    points = points[:max_points]

    try:
        report = signal_replay_service.replay(
            points=points,
            loader=signal_replay_service.default_loader(),
            ports_factory=signal_replay_service.default_ports_factory(),
            window_days=window_days,
            horizon_days=horizon_days,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("replay failed: %s", exc)
        raise HTTPException(status_code=422, detail=f"回放失败：{exc}") from exc

    payload = report.as_dict()
    payload["requested_points"] = len(symbol_list) * len(date_list)
    payload["truncated"] = truncated
    return payload


@router.get("/cost-by-tier")
def get_cost_by_tier(user=Depends(current_user)):
    """按档位归因成本：轮次、token、平均延迟。

    数据来自 `chat_messages` 的**真实记账**（tier / tokens / latency_ms），
    不是估算值。14.2 的验收口径依赖它。
    """
    from api.services import chat_service

    report = chat_service.cost_by_tier(user_id=user.id)
    return {
        "by_tier": report,
        "note": (
            "tokens 为 0 表示该 provider 未返回 usage（如实记 0，不估算）；"
            "重档（heavy）由既有分析管道服务，不经过对话记账。"
        ),
    }


@router.get("/calibration")
def get_calibration(
    symbol: str | None = Query(None),
    user=Depends(current_user),
):
    """校准状态：版本、各状态样本量、弃权率、按维度的判别力。

    刻意把「由构造保证」与「由监控校准」两类指标分列——混在一起会退化成
    「我们加了校验，应该没问题」（见 answer-grounding 的设计说明）。
    """
    return signal_service.calibration_report(user_id=user.id, symbol=symbol)

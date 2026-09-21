"""跟踪看板价格提醒：涨停、急拉，以及按交易计划的退出纪律提醒（企业微信 + WPS）。

两类提醒的方向完全不同，必须都在：
1. `run_tracking_price_alerts`：盘中涨停 / 急拉——只看涨的方向；
2. `run_exit_plan_alerts`：跌破硬止损 / 触及分批止盈 / 移动止盈回撤 / 时间止损到期 /
   逻辑失效复核——这是真正决定盈亏的方向。

历史缺陷：产品只在"涨停逼近"和"急拉"时推送，跌下去一声不响；且唯一的卖出逻辑是
paper_trading_service 里写死的 -6%/+10%，只作用于虚拟盘，与每份研报自己的计划无关。
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from api.database import ImportedPortfolioPositionDB, UserDB
from api.services import auth_service
from api.services.tracking_board_service import _fetch_live_quotes, _to_float
from api.services.wecom_notification_service import send_message
from api.services.wps_notification_service import send_markdown_message
from tradingagents.dataflows.trade_calendar import cn_today_str, is_cn_trading_day

logger = logging.getLogger(__name__)

_TZ = ZoneInfo("Asia/Shanghai")

_last_poll_price: dict[tuple[str, str], float] = {}
_limit_up_sent: dict[tuple[str, str], str] = {}
_last_surge_ts: dict[tuple[str, str], float] = {}


def price_alerts_enabled() -> bool:
    return os.getenv("TA_TRACKING_PRICE_ALERTS", "1").strip().lower() not in ("0", "false", "no")


def alert_poll_interval_sec() -> int:
    return max(30, int(os.getenv("TA_TRACKING_ALERT_INTERVAL_SEC", "75")))


def _surge_pct() -> float:
    return float(os.getenv("TA_TRACKING_SURGE_PCT", "2.5"))


def _surge_cooldown_sec() -> float:
    return float(os.getenv("TA_TRACKING_SURGE_COOLDOWN_SEC", "1800"))


def infer_daily_limit_pct(symbol: str, name: str = "") -> float:
    """与前端跟踪看板 `getDailyLimitPercent` 对齐的粗算涨跌幅上限（%）。"""
    sym = (symbol or "").upper()
    nm = (name or "").upper()
    if "ST" in nm:
        return 5.0
    if sym.endswith(".BJ"):
        return 30.0
    if sym.startswith(("300", "301", "688", "689")):
        return 20.0
    return 10.0


def _in_trading_session(now: datetime) -> bool:
    minutes = now.hour * 60 + now.minute
    morning = (9 * 60 + 30) <= minutes <= (11 * 60 + 30)
    afternoon = (13 * 60) <= minutes <= (15 * 60)
    return morning or afternoon


def _prune_limit_up_state(today: str) -> None:
    stale = [k for k, v in _limit_up_sent.items() if v != today]
    for k in stale:
        del _limit_up_sent[k]


def _prune_orphan_price_state(alive: set[tuple[str, str]]) -> None:
    for d in (_last_poll_price, _last_surge_ts):
        for k in list(d.keys()):
            if k not in alive:
                del d[k]


def _build_alert_markdown(title: str, *, label: str, detail_lines: list[str]) -> str:
    lines = [f"## {title}", "", f"- 标的：{label}"]
    lines.extend(f"- {line}" for line in detail_lines)
    return "\n".join(lines)[:5000]


def _send_alert_to_enabled_channels(
    *,
    user: UserDB,
    text_body: str,
    markdown_body: str,
    wecom_webhook: str | None,
    wps_webhook: str | None,
) -> bool:
    sent = False
    if getattr(user, "wecom_report_enabled", True) and wecom_webhook:
        try:
            sent = bool(send_message(text_body, wecom_webhook)) or sent
        except Exception as exc:
            logger.warning("[tracking-alert] wecom send failed: %s", exc)
    if getattr(user, "wps_report_enabled", True) and wps_webhook:
        try:
            sent = bool(send_markdown_message(markdown_body, wps_webhook)) or sent
        except Exception as exc:
            logger.warning("[tracking-alert] wps send failed: %s", exc)
    return sent


def run_tracking_price_alerts(db: Session) -> None:
    if not price_alerts_enabled():
        return

    now = datetime.now(tz=_TZ)
    today = cn_today_str()
    if not is_cn_trading_day(today):
        return
    if not _in_trading_session(now):
        return

    _prune_limit_up_state(today)

    rows = db.query(ImportedPortfolioPositionDB).all()
    if not rows:
        return

    user_symbols: dict[str, dict[str, str]] = defaultdict(dict)
    for row in rows:
        sym = row.symbol
        nm = (row.security_name or sym or "").strip()
        cur = user_symbols[row.user_id].get(sym, "")
        if len(nm) >= len(cur):
            user_symbols[row.user_id][sym] = nm

    all_symbols = {s for m in user_symbols.values() for s in m}
    alive_keys = {(uid, sym) for uid, m in user_symbols.items() for sym in m}
    _prune_orphan_price_state(alive_keys)

    quotes = _fetch_live_quotes(sorted(all_symbols))
    surge_threshold = _surge_pct()
    surge_cooldown = _surge_cooldown_sec()
    poll_sec = alert_poll_interval_sec()
    now_mono = time.monotonic()

    for uid, sym_to_name in user_symbols.items():
        user = db.query(UserDB).filter(UserDB.id == uid).first()
        if not user:
            continue
        cfg = auth_service.get_user_llm_config(db, uid)
        wecom_webhook = auth_service.decrypt_secret(getattr(cfg, "wecom_webhook_encrypted", None))
        wps_webhook = auth_service.decrypt_secret(getattr(cfg, "wps_webhook_encrypted", None))
        wecom_enabled = bool(getattr(user, "wecom_report_enabled", True) and wecom_webhook and str(wecom_webhook).strip())
        wps_enabled = bool(getattr(user, "wps_report_enabled", True) and wps_webhook and str(wps_webhook).strip())
        if not (wecom_enabled or wps_enabled):
            continue

        for symbol in sorted(sym_to_name):
            name = sym_to_name[symbol]
            quote = quotes.get(symbol, {})
            price = _to_float(quote.get("price"))
            change_pct = _to_float(quote.get("change_pct"))
            if price is None:
                continue

            key = (uid, symbol)
            limit_pct = infer_daily_limit_pct(symbol, name)
            label = f"{name}（{symbol}）" if name and name != symbol else symbol

            if change_pct is not None and change_pct >= limit_pct - 0.15:
                if _limit_up_sent.get(key) != today:
                    body = (
                        f"【涨停提醒】跟踪看板\n"
                        f"标的：{label}\n"
                        f"当前涨跌：{change_pct:+.2f}%（约触及涨停 {limit_pct:g}%）\n"
                        f"最新价：{price}\n"
                        f"交易日：{today}"
                    )[:1800]
                    md_body = _build_alert_markdown(
                        "涨停提醒（跟踪看板）",
                        label=label,
                        detail_lines=[
                            f"当前涨跌：{change_pct:+.2f}%（约触及涨停 {limit_pct:g}%）",
                            f"最新价：{price}",
                            f"交易日：{today}",
                        ],
                    )
                    if _send_alert_to_enabled_channels(
                        user=user,
                        text_body=body,
                        markdown_body=md_body,
                        wecom_webhook=wecom_webhook,
                        wps_webhook=wps_webhook,
                    ):
                        _limit_up_sent[key] = today
                        logger.info("[tracking-alert] limit-up user=%s symbol=%s", uid, symbol)

            prev = _last_poll_price.get(key)
            if prev is not None and prev > 0 and price > prev:
                jump = (price - prev) / prev * 100.0
                if jump >= surge_threshold:
                    if now_mono - _last_surge_ts.get(key, 0) >= surge_cooldown:
                        body = (
                            f"【急拉提醒】跟踪看板\n"
                            f"标的：{label}\n"
                            f"约 {poll_sec} 秒内涨幅约 {jump:+.2f}%（相对上轮询价）\n"
                            f"最新价：{price}（上轮询约 {prev}）\n"
                            f"交易日：{today}"
                        )[:1800]
                        md_body = _build_alert_markdown(
                            "急拉提醒（跟踪看板）",
                            label=label,
                            detail_lines=[
                                f"约 {poll_sec} 秒内涨幅约 {jump:+.2f}%（相对上轮询价）",
                                f"最新价：{price}（上轮询约 {prev}）",
                                f"交易日：{today}",
                            ],
                        )
                        if _send_alert_to_enabled_channels(
                            user=user,
                            text_body=body,
                            markdown_body=md_body,
                            wecom_webhook=wecom_webhook,
                            wps_webhook=wps_webhook,
                        ):
                            _last_surge_ts[key] = now_mono
                            logger.info("[tracking-alert] surge user=%s symbol=%s", uid, symbol)

            _last_poll_price[key] = float(price)


# --------------------------------------------------------------------------- #
# 退出纪律提醒（P0-3）：把交易计划里的止损/止盈变成真正的双向预警
# --------------------------------------------------------------------------- #

_peak_price: dict[tuple[str, str], float] = {}
_exit_alert_sent: dict[tuple[str, str, str], str] = {}

_ACTIONABLE_TRIGGERS = (
    "hard_stop",
    "trailing_stop",
    "take_profit",
    "time_stop",
    "invalidation_review",
)

_TRIGGER_TITLES = {
    "hard_stop": "跌破硬止损",
    "trailing_stop": "移动止盈回撤",
    "take_profit": "触及分批止盈",
    "time_stop": "时间止损到期",
    "invalidation_review": "逻辑失效条件复核",
}

_PRIORITY_ZH = {"high": "高", "medium": "中", "low": "低"}


def _prune_exit_alert_state(today: str, alive: set[tuple[str, str]]) -> None:
    for key in [k for k, v in _exit_alert_sent.items() if v != today]:
        del _exit_alert_sent[key]
    for key in [k for k in _peak_price if k not in alive]:
        del _peak_price[key]


def _observed_peak(uid: str, symbol: str, quote: dict, price: float | None) -> float | None:
    """观测到的阶段高点：取当日最高价与轮询记录高点的较大者。

    注意这是"自监控开始以来观测到的最高价"，不等于复权后的真实历史最高价，
    因此提醒文案里据实写成"观测高点"，不假装是精确历史峰值。
    """
    key = (uid, symbol)
    high = _to_float((quote or {}).get("high"))
    candidates = [v for v in (high, price, _peak_price.get(key)) if v]
    if not candidates:
        return None
    peak = max(candidates)
    _peak_price[key] = peak
    return peak


def run_exit_plan_alerts(db: Session) -> int:
    """按每份研报自己的交易计划做双向纪律提醒，返回推送条数。

    与 `run_tracking_price_alerts` 共用同一次轮询节拍与推送渠道；每个
    (用户, 标的, 触发原因) 每天最多提醒一次，避免 75 秒一次的重复轰炸。
    """
    if not price_alerts_enabled():
        return 0

    now = datetime.now(tz=_TZ)
    today = cn_today_str()
    if not is_cn_trading_day(today):
        return 0
    if not _in_trading_session(now):
        return 0

    # 延迟导入：exit_engine_service 依赖本模块的 infer_daily_limit_pct，
    # 模块级互相导入会成环。
    from api.services.exit_engine_service import evaluate_portfolio

    rows = db.query(ImportedPortfolioPositionDB).all()
    if not rows:
        return 0

    by_user: dict[str, dict[str, str]] = defaultdict(dict)
    for row in rows:
        if not row.user_id:
            continue
        nm = (row.security_name or row.symbol or "").strip()
        cur = by_user[row.user_id].get(row.symbol, "")
        if len(nm) >= len(cur):
            by_user[row.user_id][row.symbol] = nm

    all_symbols = sorted({s for m in by_user.values() for s in m})
    alive = {(uid, sym) for uid, m in by_user.items() for sym in m}
    _prune_exit_alert_state(today, alive)

    quotes = _fetch_live_quotes(all_symbols)

    sent_count = 0
    for uid, sym_to_name in by_user.items():
        user = db.query(UserDB).filter(UserDB.id == uid).first()
        if not user:
            continue
        cfg = auth_service.get_user_llm_config(db, uid)
        wecom_webhook = auth_service.decrypt_secret(getattr(cfg, "wecom_webhook_encrypted", None))
        wps_webhook = auth_service.decrypt_secret(getattr(cfg, "wps_webhook_encrypted", None))
        has_channel = bool(
            (getattr(user, "wecom_report_enabled", True) and str(wecom_webhook or "").strip())
            or (getattr(user, "wps_report_enabled", True) and str(wps_webhook or "").strip())
        )
        if not has_channel:
            continue

        def _peak_provider(sym: str, _uid: str = uid) -> float | None:
            q = quotes.get(sym) or {}
            return _observed_peak(_uid, sym, q, _to_float(q.get("price")))

        try:
            decisions = evaluate_portfolio(
                db,
                user_id=uid,
                quotes=quotes,
                today=today,
                peak_price_provider=_peak_provider,
            )
        except Exception as exc:
            logger.warning("[exit-alert] 退出评估失败 user=%s: %s", uid, exc)
            continue

        for decision in decisions:
            # 只推送"今天需要做点什么"的决策（清仓/减仓）。观察与逻辑失效复核
            # 留给前端面板：几乎每份计划都带 invalidation_conditions，若也推送，
            # 10 只持仓每天就是 10 条推送——用户会立刻学会忽略它。这和风控闸门
            # 99.94% 通过率是同一种病：警报疲劳会让真正的信号一起失效。
            if decision.action not in ("清仓", "减仓"):
                continue

            # 方向/锚点前置校验（退出引擎规则 0）：只有方向明确为多头、价格锚点
            # 自洽的计划，其止损/止盈位才能按多头语义解读。计划方向为 SELL/HOLD
            # 或锚点自相矛盾时，这里的"清仓/减仓"是错误价位算出来的——旧实现会把
            # 它直接推送给真实用户。此时只记录不推送，等计划方向被确认。
            if not decision.is_actionable_sell:
                logger.info(
                    "[exit-alert] 已抑制推送（方向校验未通过）user=%s symbol=%s action=%s gate=%s",
                    uid, decision.symbol, decision.action, decision.anchor_gate,
                )
                continue

            triggers = [t for t in decision.triggers if t in _ACTIONABLE_TRIGGERS]
            if not triggers:
                continue
            primary = triggers[0]

            key = (uid, decision.symbol, primary)
            if _exit_alert_sent.get(key) == today:
                continue

            name = sym_to_name.get(decision.symbol) or decision.name or ""
            label = (
                f"{name}（{decision.symbol}）"
                if name and name != decision.symbol
                else decision.symbol
            )
            title = _TRIGGER_TITLES.get(primary, "持仓纪律提醒")

            detail_lines = [f"建议动作：{decision.action}（优先级{_PRIORITY_ZH.get(decision.priority, decision.priority)}）"]
            if decision.price is not None:
                price_line = f"最新价：{decision.price}"
                if decision.average_cost:
                    price_line += f"（成本 {decision.average_cost}"
                    if decision.pnl_pct is not None:
                        price_line += f"，浮动 {decision.pnl_pct:+.2f}%"
                    price_line += "）"
                detail_lines.append(price_line)
            detail_lines.extend(decision.reasons)
            if decision.suggested_shares and decision.suggested_shares > 0:
                detail_lines.append(
                    f"建议减仓股数：{int(decision.suggested_shares)} 股"
                    f"（约 {decision.suggested_pct:g}% 仓位）"
                )
            detail_lines.extend(f"限制：{c}" for c in decision.constraints)
            if not decision.plan_available:
                detail_lines.append("提示：该持仓暂无可用交易计划，本提醒仅基于持仓本身。")
            detail_lines.append(decision.label)

            body = (f"【{title}】持仓纪律提醒\n标的：{label}\n" + "\n".join(detail_lines))[:1800]
            md_body = _build_alert_markdown(f"{title}（持仓纪律提醒）", label=label, detail_lines=detail_lines)

            if _send_alert_to_enabled_channels(
                user=user,
                text_body=body,
                markdown_body=md_body,
                wecom_webhook=wecom_webhook,
                wps_webhook=wps_webhook,
            ):
                _exit_alert_sent[key] = today
                sent_count += 1
                logger.info(
                    "[exit-alert] user=%s symbol=%s trigger=%s action=%s",
                    uid, decision.symbol, primary, decision.action,
                )
    return sent_count

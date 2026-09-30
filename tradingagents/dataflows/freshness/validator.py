from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from .anchor import is_in_grace_window, resolve_expected_anchor
from .contract import (
    all_contract_source_keys,
    get_contract_entry,
    report_pipeline_sources,
    resolve_source_key,
)
from .parser import (
    detect_fetch_error,
    disclosure_period_lag,
    expected_disclosure_period,
    is_lhb_empty_ok,
    parse_cutoff_from_text,
    parse_disclosure_period,
    parse_last_date_from_csv,
    parse_latest_event_timestamp,
    trading_days_between,
)
from .types import EvalContext, FreshnessMeta

#: 源在契约里存在、但不在本条管线的采集范围内时给出的说明。
#: 措辞要让人一眼看出"这是设计如此"，而不是"这里漏采了"——否则下一个人
#: 会把它当 bug "修"回 error，假故障就会回来。
NOTE_OUT_OF_PIPELINE_SCOPE = (
    "该源不在本采集管线范围内（由工具/对话路径自行评估），不参与本次新鲜度判定"
)


def _intraday_only_not_collected(entry: dict[str, Any], raw_result: Any, ctx: EvalContext) -> bool:
    if not entry.get("intraday_only") or raw_result is not None:
        return False
    from tradingagents.dataflows.trade_calendar import cn_today_str

    if ctx.trade_date != cn_today_str():
        return True
    return ctx.analysis_mode in ("historical", "closed", "t_plus_1", "forward_look")


def evaluate_source(
    source_key: str,
    raw_result: Any,
    ctx: EvalContext,
    *,
    trade_date: str | None = None,
) -> FreshnessMeta:
    entry = get_contract_entry(source_key)
    now_iso = ctx.now_iso
    if not entry:
        return FreshnessMeta(
            source_key=source_key,
            category="event_driven",
            criticality="informational",
            status="unknown",
            evaluated_at=now_iso,
            lag_note="未注册的数据源",
        )

    category = entry.get("category", "event_driven")
    criticality = entry.get("criticality", "informational")
    grace = int(entry.get("grace_minutes") or 0)

    is_err, err_code, err_msg = detect_fetch_error(raw_result)
    if _intraday_only_not_collected(entry, raw_result, ctx):
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="not_applicable",
            anchor_expected=ctx.expected_anchor,
            evaluated_at=now_iso,
            lag_note="非盘中实时采集窗口，跳过实时行情新鲜度",
        )

    if category == "conditional_daily" and is_lhb_empty_ok(raw_result, trade_date or ctx.trade_date):
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="empty_ok",
            anchor_expected=ctx.expected_anchor,
            evaluated_at=now_iso,
            empty_ok=True,
            lag_note="非异动日无龙虎榜数据（正常）",
        )

    # 契约里 `empty_ok: true` 的源，"数据源明确答『无数据』"是正常态而非故障。
    # 此前这个 flag **从未被读过**（真正生效的只有上面 `is_lhb_empty_ok` 的龙虎榜
    # 文案匹配），契约注释承诺的语义在实现里是空的——"契约与实现漂移"的典型样本，
    # 也是假故障反复出现的土壤。
    #
    # 判定刻意收紧两处，免得它退化成"把采集失败也当正常"：
    #   ① 必须有**回包**：`raw_result is None` 意味着采集方根本没拿到答案
    #      （任务没跑 / 被丢弃），那仍然是 error，不能被 empty_ok 抹平；
    #   ② 回包必须是**非空文案**：空串更像上游静默失败，不像"今天没有"。
    if (
        is_err
        and entry.get("empty_ok")
        and err_code == "empty_response"
        and raw_result is not None
        and str(raw_result).strip()
    ):
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="empty_ok",
            anchor_expected=ctx.expected_anchor,
            evaluated_at=now_iso,
            empty_ok=True,
            note="数据源明确回答『无数据』，契约声明 empty_ok（不算取数失败）",
        )

    if is_err:
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="error",
            anchor_expected=ctx.expected_anchor,
            evaluated_at=now_iso,
            issue_type="fetch_error",
            error_code=err_code,
            error_message=err_msg,
            lag_note=err_msg,
        )

    if category == "calendar_anchored":
        return _eval_calendar(source_key, raw_result, entry, ctx, grace)
    if category == "disclosure_period":
        return _eval_disclosure(source_key, raw_result, entry, ctx)
    if category == "event_driven":
        return _eval_event_driven(source_key, raw_result, entry, ctx)
    # 不认识的 category：**不得**判 fresh。
    #
    # 旧实现在这里 `status="fresh"`，于是任何没被上面三个分支接住的 category
    # 都自动获得"最新"的结论——**信息越少，结论越乐观**。
    # category 是"该用什么口径判定"的声明；声明不认识就意味着**无法判定**，
    # 正确取值是 unknown（并由聚合层施加置信度上限，见 CONFIDENCE_CAPS["unknown"]）。
    return FreshnessMeta(
        source_key=source_key,
        category=category,
        criticality=criticality,
        status="unknown",
        evaluated_at=now_iso,
        lag_note=f"未知的新鲜度类别 {category!r}，无法判定",
    )


def _eval_calendar(
    source_key: str,
    raw_result: Any,
    entry: dict[str, Any],
    ctx: EvalContext,
    grace: int,
) -> FreshnessMeta:
    category = entry.get("category", "calendar_anchored")
    criticality = entry.get("criticality", "important")
    expected = ctx.expected_anchor

    if source_key == "stock_data":
        actual = parse_last_date_from_csv(str(raw_result))
    elif isinstance(raw_result, str) and raw_result.lstrip().lower().startswith("date,"):
        actual = parse_last_date_from_csv(str(raw_result))
    else:
        actual = parse_cutoff_from_text(str(raw_result))

    if not actual:
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="error",
            anchor_expected=expected,
            evaluated_at=ctx.now_iso,
            issue_type="fetch_error",
            error_code="parse_error",
            error_message="无法解析数据截止日期",
            lag_note="无法解析数据截止日期",
        )

    try:
        lag_days = trading_days_between(actual, expected)
    except Exception as _lag_exc:
        # 日历不可用 ⇒ 滞后天数**未知**。绝不能回退成 0（0 = fresh），
        # 也不能让异常穿出去打断整条采集链：按 unknown 处理并施加置信度上限
        # （审计 §4.13/§4.16）。下面 `actual >= expected` 的分支仍可安全判定 fresh，
        # 因为那是纯粹基于锚点的比较，不依赖交易日历。
        lag_days = None
        _lag_note_exc = f"{type(_lag_exc).__name__}: {_lag_exc}"
    else:
        _lag_note_exc = None

    if actual >= expected:
        status = "fresh"
        lag_note = None
    elif is_in_grace_window(
        grace_minutes=grace,
        analysis_mode=ctx.analysis_mode,
        now=datetime.fromisoformat(ctx.now_iso.replace("Z", "+00:00")),
    ):
        status = "warning"
        lag_note = f"数据尚未更新至 {expected}（grace 缓冲期内）"
    else:
        status = "stale"
        lag_note = f"数据截止 {actual}，预期至少 {expected}"

    if _lag_note_exc is not None:
        # 让"滞后天数算不出来"在结果里可见，而不是静默地当作 0 天。
        status = "unknown" if status == "fresh" else status
        extra = f"（交易日历不可用，滞后天数未知：{_lag_note_exc}）"
        lag_note = f"{lag_note}{extra}" if lag_note else extra.lstrip("（").rstrip("）")

    return FreshnessMeta(
        source_key=source_key,
        category=category,
        criticality=criticality,
        status=status,
        anchor_expected=expected,
        anchor_actual=actual,
        evaluated_at=ctx.now_iso,
        issue_type="lag" if status in ("warning", "stale") else None,
        lag_note=lag_note,
        # `lag_days is None` = 日历不可用、滞后未知（上面已显式接住），
        # 不能参与比较，也不能被写成 0。
        lag_trading_days=lag_days if (lag_days is not None and lag_days > 0) else None,
    )


def _eval_disclosure(
    source_key: str,
    raw_result: Any,
    entry: dict[str, Any],
    ctx: EvalContext,
) -> FreshnessMeta:
    category = entry.get("category", "disclosure_period")
    criticality = entry.get("criticality", "important")
    period = parse_disclosure_period(raw_result)
    expected = expected_disclosure_period(ctx.trade_date)
    if not period:
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="warning",
            anchor_expected=expected,
            evaluated_at=ctx.now_iso,
            lag_note="未能解析财报披露期",
        )
    lag_q = disclosure_period_lag(period, expected)
    if lag_q <= 0:
        status = "fresh"
        lag_note = None
    elif lag_q == 1:
        status = "warning"
        lag_note = f"披露期 {period} 滞后于预期 {expected}（约 1 个报告期）"
    else:
        status = "stale"
        lag_note = f"披露期 {period} 滞后于预期 {expected}（约 {lag_q} 个报告期）"
    return FreshnessMeta(
        source_key=source_key,
        category=category,
        criticality=criticality,
        status=status,
        anchor_expected=expected,
        anchor_actual=period,
        evaluated_at=ctx.now_iso,
        issue_type="lag" if status in ("warning", "stale") else None,
        lag_note=lag_note,
    )


def _eval_event_driven(
    source_key: str,
    raw_result: Any,
    entry: dict[str, Any],
    ctx: EvalContext,
) -> FreshnessMeta:
    category = entry.get("category", "event_driven")
    criticality = entry.get("criticality", "informational")
    text = str(raw_result or "").strip()
    if not text:
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="not_applicable",
            evaluated_at=ctx.now_iso,
        )
    window_hours = int(entry.get("window_hours") or 0)
    if window_hours <= 0:
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="fresh",
            evaluated_at=ctx.now_iso,
        )
    latest = parse_latest_event_timestamp(raw_result)
    if latest is None:
        # 解析不出事件时间戳 = **无法判定**，不是"新鲜"。
        #
        # 旧实现返回 `status="fresh"` 外加一句 lag_note。问题是聚合层只看 status、
        # 不看 lag_note，于是"未能解析时间戳"被折叠成"最新"，
        # 上限照给 100。沉默地乐观正是本审计要消除的东西。
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="unknown",
            evaluated_at=ctx.now_iso,
            lag_note="未能解析事件时间戳，无法判定新鲜度",
        )
    from datetime import timezone

    now_dt = datetime.fromisoformat(ctx.now_iso.replace("Z", "+00:00"))
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    age_hours = (now_dt - latest.replace(tzinfo=timezone.utc)).total_seconds() / 3600.0
    if age_hours <= window_hours:
        return FreshnessMeta(
            source_key=source_key,
            category=category,
            criticality=criticality,
            status="fresh",
            anchor_actual=latest.strftime("%Y-%m-%d %H:%M:%S"),
            evaluated_at=ctx.now_iso,
        )
    return FreshnessMeta(
        source_key=source_key,
        category=category,
        criticality=criticality,
        status="stale",
        anchor_actual=latest.strftime("%Y-%m-%d %H:%M:%S"),
        evaluated_at=ctx.now_iso,
        issue_type="lag",
        lag_note=f"最新事件时间 {latest.strftime('%Y-%m-%d %H:%M')} 超出 {window_hours}h 窗口",
    )


def build_eval_context(
    trade_date: str,
    *,
    symbol: str = "600519.SH",
    analysis_mode: str | None = None,
    now: datetime | None = None,
) -> EvalContext:
    from .anchor import _infer_analysis_mode

    del symbol
    mode = analysis_mode or _infer_analysis_mode(trade_date, now=now)
    now_dt = now or datetime.now(timezone.utc)
    return EvalContext(
        trade_date=trade_date,
        analysis_mode=mode,
        expected_anchor=resolve_expected_anchor(trade_date, analysis_mode=mode, now=now),
        now_iso=now_dt.astimezone(timezone.utc).isoformat(),
    )


def evaluate_collector_results(
    results: dict[str, Any],
    trade_date: str,
    *,
    symbol: str = "600519.SH",
    analysis_mode: str | None = None,
    scope: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Evaluate all collector keys and return freshness_pool keyed by contract source_key.

    `scope` 是本管线**负责采集**的源集合（默认取自 `contract.yaml` 的
    `report_pipeline_sources`）。区别只在"某源没出现在 `results` 里"这一种情形：

    * **在范围内** → "该采没采到"，判 `error`（守卫，见
      `test_evaluate_collector_results_marks_missing_critical_as_error`）；
    * **不在范围内** → `not_applicable`：这条管线本来就不负责取它，报告不据它
      判新鲜度（由工具/对话路径用 `evaluate_tool_sources` 自行评估）。

    混用这两者是实测过的事故：`intraday_bars` / `chip_distribution` /
    `symbol_news` 为工具路径加进契约后，被报告管线当成"该采没采到"，每条报告都
    多出两条 error +「数据源返回空」→ overall 变「数据异常」→ 置信度被
    `CONFIDENCE_CAPS['error']` 硬压到 40，并被模型写进终审正文
    （"L0：intraday_bars / chip_distribution 未恢复 → 0% 真实新开仓"）。
    而当时这两个源实测完全可用——纯粹是假故障。
    """
    eval_ctx = build_eval_context(trade_date, symbol=symbol, analysis_mode=analysis_mode)
    in_scope = frozenset(scope) if scope is not None else report_pipeline_sources()
    pool: dict[str, dict[str, Any]] = {}

    stock_meta: FreshnessMeta | None = None
    for collector_key, raw in results.items():
        if collector_key.startswith("_") or collector_key in ("vpa_indicators", "macro_ashare_brief"):
            continue
        source_key = resolve_source_key(collector_key)
        entry = get_contract_entry(source_key)
        if entry and entry.get("inherit_from") == "stock_data":
            continue
        meta = evaluate_source(source_key, raw, eval_ctx, trade_date=trade_date)
        pool[source_key] = meta.to_dict()
        if source_key == "stock_data":
            stock_meta = meta

    if stock_meta:
        ind = FreshnessMeta(
            source_key="indicators",
            category="calendar_anchored",
            criticality="critical",
            status=stock_meta.status,
            anchor_expected=stock_meta.anchor_expected,
            anchor_actual=stock_meta.anchor_actual,
            evaluated_at=stock_meta.evaluated_at,
            issue_type=stock_meta.issue_type,
            lag_note=stock_meta.lag_note,
            lag_trading_days=stock_meta.lag_trading_days,
            error_code=stock_meta.error_code,
            error_message=stock_meta.error_message,
        )
        pool["indicators"] = ind.to_dict()

    for source_key in ("individual_fund_flow", "board_fund_flow", "zt_pool", "lhb_detail"):
        if source_key not in pool:
            meta = evaluate_source(source_key, None, eval_ctx, trade_date=trade_date)
            pool[source_key] = meta.to_dict()

    for source_key in all_contract_source_keys():
        if source_key in pool:
            continue
        entry = get_contract_entry(source_key) or {}
        if entry.get("inherit_from") == "stock_data":
            continue
        if source_key not in in_scope:
            # 不在本管线采集范围内 → "不适用"，**不是**"取数失败"。
            # 仍留在 pool 里（而不是省掉）是为了可追溯：报告落库后能看出
            # "这个源报告压根不出，所以不参与新鲜度判定"。
            pool[source_key] = FreshnessMeta(
                source_key=source_key,
                category=entry.get("category", "event_driven"),
                criticality=entry.get("criticality", "informational"),
                status="not_applicable",
                anchor_expected=eval_ctx.expected_anchor,
                evaluated_at=eval_ctx.now_iso,
                note=NOTE_OUT_OF_PIPELINE_SCOPE,
            ).to_dict()
            continue
        meta = evaluate_source(source_key, None, eval_ctx, trade_date=trade_date)
        pool[source_key] = meta.to_dict()

    return pool

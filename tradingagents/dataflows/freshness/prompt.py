from __future__ import annotations

from typing import Any


def format_freshness_context_for_sources(
    pool: dict[str, dict[str, Any]] | None,
    source_keys: list[str],
) -> str:
    if not pool:
        return ""
    lines: list[str] = []
    for key in source_keys:
        meta = pool.get(key)
        if not meta:
            continue
        status = meta.get("status")
        if status in ("fresh", "empty_ok", "not_applicable"):
            continue
        if status == "error":
            lines.append(
                f"- [{key}] 数据拉取异常（{meta.get('error_code', 'fetch_error')}）："
                f"{meta.get('error_message') or meta.get('lag_note') or '不可用'}。"
                f"请勿基于该维度臆造结论。"
            )
        elif status == "stale":
            lines.append(
                f"- [{key}] 数据滞后：截止 {meta.get('anchor_actual')}，"
                f"预期至少 {meta.get('anchor_expected')}。请勿仅凭该数据做高置信判断。"
            )
        elif status == "warning":
            lines.append(
                f"- [{key}] 数据可能仍在更新（grace 缓冲）：{meta.get('lag_note') or '尚未完全落定'}。"
            )
    if not lines:
        return ""
    return "【数据新鲜度提示】\n" + "\n".join(lines)


def format_freshness_context_summary(summary: dict[str, Any] | None) -> str:
    if not summary:
        return ""
    overall = summary.get("overall_status", "fresh")
    if overall == "fresh":
        expected = summary.get("expected_anchor")
        return f"【数据新鲜度】关键日历型数据截至 {expected}，状态正常。"
    cap = summary.get("confidence_cap")
    parts = [f"【数据新鲜度】报告整体状态：{summary.get('overall_label', overall)}（confidence 上限 {cap}）。"]
    for err in summary.get("fetch_errors") or []:
        parts.append(
            f"- 拉取异常 {err.get('source_key')}: {err.get('error_message') or err.get('error_code')}"
        )
    for blk in summary.get("blocking_sources") or []:
        if blk.get("status") == "stale":
            parts.append(
                f"- 滞后 {blk.get('source_key')}: 实际 {blk.get('anchor_actual')} / 预期 {blk.get('anchor_expected')}"
            )
    return "\n".join(parts)

#!/usr/bin/env bash
# 可执行的质量闸门：测试 + 黄金集指标 + 前端。
#
# ## 为什么需要它
#
# 本会话反复出现同一类问题：**改完行为之后，指标悄悄回退却没人发现**。
# 例如规划层上线时工具精确率从 86.2% 掉到 54.4%、探针延迟预算追不上真实工作量
# 导致证据归零——这些都是"测试全绿但能力变差"。
# 测试绿 != 没退步；所以把**度量**也变成闸门。
#
# 用法：
#   bash scripts/check.sh            # 后端 + 前端
#   bash scripts/check.sh --golden   # 追加黄金集指标门槛（规则路径，离线）
#
# 门槛口径：
#   * 后端失败**集合**必须 ⊆ 已知既有失败集（见 KNOWN_BASELINE_FAILURES）
#   * 前端 tsc 必须 0 错、vitest 必须全绿
#   * 黄金集（可选）：规划准确率不低于 GOLDEN_MIN_PLANNING，工具召回不低于 GOLDEN_MIN_RECALL
#
# ## 为什么比对**名字**而不是条数
#
# 条数会跳：`TestPortfolioOverviewEndpoint` 是顺序相关的偶发失败，同一条命令
# 在不同运行里给出 7 或 8。按条数设阈值有两个坏结果——阈值取 7 就误报回归，
# 取 8 就正好放过一条真实回归。按名字比对两头都不丢：偶发的那条在已知集里，
# 真新增的那条不在。
#
# 顺带修掉一个**闸门自身的 bug**：原第 36 行写的是
# `echo "... $BASELINE_FAILURES（有新增回归）"`，而 bash 会把中文全角括号的首字节
# 当成变量名的一部分 → `set -u` 下报"未绑定的变量"并**直接中止脚本**。
# 也就是说"检测到回归"这条分支永远只输出一个语法错误，不给结论。中文文案里的
# 变量一律写成 `${VAR}`。

set -uo pipefail
cd "$(dirname "$0")/.."

GOLDEN_MIN_PLANNING=${GOLDEN_MIN_PLANNING:-0.40}
# 离线门槛按**离线口径**设：规则路径实测召回 66.7%，规划层启用时 96.2%。
# 两个口径基线不同，阈值必须跟着口径走——否则要么常红（被无视），要么形同虚设。
# 这里的作用是"别让离线能力继续下滑"，不是给规划层打分。
GOLDEN_MIN_RECALL=${GOLDEN_MIN_RECALL:-0.60}

# 已知的既有失败（与本改造无关）。**只增不减**：修掉一条就从这里删一条，
# 新加一条必须能说出"为什么它与本次改造无关"。
KNOWN_BASELINE_FAILURES=(
  "tests/test_api_smoke.py::TestModelArenaApi::test_model_arena_leaderboard_compare_promote_and_rollback"
  "tests/test_api_smoke.py::TestModelProfileApi::test_model_profile_crud_and_analyze_dry_run"
  "tests/test_api_smoke.py::TestPortfolioOverviewEndpoint::test_overview_returns_watchlist_scheduled_portfolio_and_latest_reports"
  "tests/test_api_smoke.py::TestWatchlistAddEndpoint::test_batch_add_marks_duplicates"
  "tests/test_api_smoke.py::TestWatchlistAddEndpoint::test_batch_add_supports_codes_and_full_names"
  "tests/test_report_recovery.py::test_recover_stale_active_reports_marks_empty_running_report_failed"
  "tests/test_report_recovery.py::test_recover_stale_active_reports_marks_partial_running_report_failed"
  "tests/test_stock_map_fast_path.py::test_reverse_stock_map_cached_only_does_not_trigger_cold_load"
)

fail=0
log() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

log "后端全量测试（失败集合必须 ⊆ 已知既有失败）"
report="$(mktemp)"
# 完整输出落文件再解析：`tail -3` 会把失败明细切掉，也就没法按名字比对。
no_proxy='*' .venv/bin/python -m pytest tests -q -p no:cacheprovider >"$report" 2>&1 || true
tail -3 "$report"
printf '%s\n' "${KNOWN_BASELINE_FAILURES[@]}" | sort > "${report}.known"
grep -oE '^FAILED [^ ]+' "$report" | sed 's/^FAILED //' | sort > "${report}.new" || true
new_failures="$(comm -13 "${report}.known" "${report}.new")"
new_count="$(printf '%s' "$new_failures" | grep -c . || true)"
if [ "${new_count}" -gt 0 ]; then
  echo "✗ 出现 $(printf '%s' "${new_count}") 条**不在已知集**里的失败（新增回归）：" >&2
  printf '%s\n' "${new_failures}" >&2
  fail=1
else
  fixed_count="$(comm -23 "${report}.known" "${report}.new" | grep -c . || true)"
  echo "✓ 无新增失败（已知既有失败里已修掉 ${fixed_count} 条，可从 KNOWN_BASELINE_FAILURES 删掉）"
fi
rm -f "$report"

log "前端类型检查与单测"
if (cd frontend && npx tsc --noEmit >/dev/null 2>&1); then
  echo "✓ tsc 0 错"
else
  echo "✗ tsc 报错" >&2; fail=1
fi
if (cd frontend && npx vitest run >/tmp/ap-vitest.log 2>&1); then
  # 先落盘再匹配，**不要**写成 `npx vitest run | grep -q`：`grep -q` 命中即关闭管道，
  # vitest 收到 SIGPIPE 后退出码非 0，而 `set -o pipefail` 会把这个非 0 当成整条管道
  # 的结果——于是**全绿的 vitest 也被判成未通过**（实测：84 passed 却报错）。
  if grep -qE 'Tests +[0-9]+ passed' /tmp/ap-vitest.log; then
    echo "✓ vitest 通过（$(grep -oE 'Tests +[0-9]+ passed' /tmp/ap-vitest.log | head -1 | tr -s ' ')）"
  else
    echo "✗ vitest 输出里没有通过计数，无法确认全绿" >&2; tail -20 /tmp/ap-vitest.log >&2; fail=1
  fi
else
  echo "✗ vitest 未通过" >&2; tail -20 /tmp/ap-vitest.log >&2; fail=1
fi

if [ "${1:-}" = "--golden" ]; then
  log "黄金集指标门槛（规则路径，离线）"
  # 规划准确率口径依赖名称表预热，脚本内部已处理。
  # `--fail-under-recall` 此前**只是被声明、从未被读取**（`GOLDEN_MIN_RECALL` 定义了
  # 却没人用）——又一处"声明而不执行"。现在两个门槛都真的挡在这里。
  if no_proxy='*' .venv/bin/python scripts/golden_report.py --no-write \
       --fail-under "$GOLDEN_MIN_PLANNING" \
       --fail-under-recall "$GOLDEN_MIN_RECALL"; then
    echo "✓ 规划准确率 ≥ ${GOLDEN_MIN_PLANNING} 且工具召回 ≥ ${GOLDEN_MIN_RECALL}（离线口径）"
    echo "  提示：规划层启用后的口径请用 scripts/golden_report.py --with-planner 单独跑"
  else
    echo "✗ 黄金集指标低于门槛" >&2; fail=1
  fi
fi

log "结论"
if [ "$fail" -eq 0 ]; then echo "✓ 全部通过"; else echo "✗ 有未通过项（见上）" >&2; fi
exit "$fail"

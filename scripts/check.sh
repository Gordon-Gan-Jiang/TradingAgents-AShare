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
#   bash scripts/check.sh              # 后端测试 + 黄金集（离线口径）+ 前端
#   bash scripts/check.sh --planner    # 另加黄金集（规划层口径 = 生产）
#   bash scripts/check.sh --acceptance # 另加 35 例端到端工具回合验收
#
# ## 三个档位各自守什么（**别把它们搞混**）
#
#   档位                 守的对象            成本        默认
#   -------------------  ------------------  ----------  ------
#   后端+前端测试         代码不坏            低           是
#   黄金集（离线口径）     规则路径不退化       低（不调模型） 是
#   黄金集（规划层口径）    **生产**不退化      50 次 LLM   否
#   端到端回合验收        工具**真的**被调对    35 次 LLM   否
#
# 这个区分是必须的：离线口径与生产口径**基线差得远**（规划准确率 46% vs 88%，
# 工具召回 64% vs 96%）。一个只破坏规划层的改动，离线闸门照样全绿。
# 所以"CI 全绿"从来不等于"生产可用"——除非规划层档位也跑过。
#
# 门槛口径：
#   * 后端失败**集合**必须 ⊆ 已知既有失败集（见 KNOWN_BASELINE_FAILURES）
#   * 前端 tsc 必须 0 错、vitest 必须全绿
#   * 黄金集：规划准确率不低于 GOLDEN_MIN_PLANNING、工具召回不低于 GOLDEN_MIN_RECALL
#     （规划层档位用**另一组**阈值 GOLDEN_MIN_PLANNING_PLANNER / GOLDEN_MIN_RECALL_PLANNER）
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

# 规划层（生产）口径的门槛，**与上面那组分开**。
# 规划层实测：规划准确率 88.0%、工具召回 96.2%。取 0.80/0.85 留出合理余量——
# 规划器是 LLM，同一配置重复跑会有波动（实测精确率 88.1↔92.9），
# 阈值贴着实测值设会把正常波动误报成回归。
#
# 注意：这两组阈值**不可互换**。用离线阈值去卡规划层等于没卡；
# 用规划层阈值去卡离线则必然常红（离线只有 46%/64%）。
GOLDEN_MIN_PLANNING_PLANNER=${GOLDEN_MIN_PLANNING_PLANNER:-0.80}
GOLDEN_MIN_RECALL_PLANNER=${GOLDEN_MIN_RECALL_PLANNER:-0.85}

# 已知的既有失败（与本改造无关）。**只增不减**：修掉一条就从这里删一条，
# 新加一条必须能说出"为什么它与本次改造无关"。
#
# 2026-09-22：8 条全部修掉，现在是**空的**——套件首次全绿（2673 passed）。
# 修的是真根因，不是把断言改松：
#   * 两条 watchlist + portfolio overview：测试 patch 的是 `_load_cn_stock_map`，
#     而端点读 `_get_stock_name_map_cached_only()` / `_get_reverse_stock_map_cached_only()`
#     （读接口刻意不触发冷加载），patch 从未生效——单独跑必败、全量跑靠别人预热偶然通过。
#   * stock_map_fast_path：测试把 `_cn_stock_map` 置空，而函数读的是
#     `_cn_stock_reverse_map`，且没掐掉它安排的后台刷新（会污染后续用例）。
#   * report_recovery ×2：`recover_stale_active_reports` 的返回缺少 `completed` 键，
#     且 `total` 恒等于 `failed`（调用方日志因此会少报）。
#   * model profile：dry run 的 payload 漏了 `quick_think_llm` / `deep_think_llm`。
#   * model arena：**时间炸弹测试**——样本日期硬编码 2026-05-20，而晋升门禁用
#     "今天往前 N 天"的滚动窗口，日历推过 90 天样本就滑出窗口。
#
# ⚠️ 空数组在 bash 3.2 + `set -u` 下展开 `"${arr[@]}"` 会报 unbound variable，
# 所以下面取值处一律用 `${arr[@]+"${arr[@]}"}` 兜底。
KNOWN_BASELINE_FAILURES=(
)

fail=0
log() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

log "后端全量测试（失败集合必须 ⊆ 已知既有失败）"
report="$(mktemp)"
# 完整输出落文件再解析：`tail -3` 会把失败明细切掉，也就没法按名字比对。
no_proxy='*' .venv/bin/python -m pytest tests -q -p no:cacheprovider >"$report" 2>&1 || true
tail -3 "$report"
# 空数组要用 `${arr[@]+"${arr[@]}"}` 展开：macOS 自带 bash 3.2 在 `set -u` 下
# 对空数组直接展开 `"${arr[@]}"` 会报 "unbound variable"，把一次正常的全绿
# 变成脚本崩溃。已知集为空是**期望状态**（全部修完），必须走得通。
printf '%s\n' ${KNOWN_BASELINE_FAILURES[@]+"${KNOWN_BASELINE_FAILURES[@]}"} | sort > "${report}.known"
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

log "黄金集指标门槛（规则路径，离线）——守的是**离线能力不继续下滑**"
# ## 为什么它现在**默认就跑**
#
# 修复前它只在 `--golden` 档位跑，而默认档位只有 pytest+tsc+vitest——
# **一个质量指标都没有**。于是"测试全绿但规划准确率悄悄掉一半"完全可能发生，
# 而 CI 照样放行。
#
# 不设为可选的理由是成本：这条路**不调模型**（纯规则路径），跑一次几秒钟。
# 相比之下规划层档位要 50 次 LLM 调用（见下），那个才该显式要。
#
# 规划准确率口径依赖名称表预热，脚本内部已处理。
# `--fail-under-recall` 此前**只是被声明、从未被读取**（`GOLDEN_MIN_RECALL` 定义了
# 却没人用）——又一处"声明而不执行"。现在两个门槛都真的挡在这里。
if no_proxy='*' .venv/bin/python scripts/golden_report.py --no-write \
     --fail-under "$GOLDEN_MIN_PLANNING" \
     --fail-under-recall "$GOLDEN_MIN_RECALL"; then
  echo "✓ 规划准确率 ≥ ${GOLDEN_MIN_PLANNING} 且工具召回 ≥ ${GOLDEN_MIN_RECALL}（离线口径 = 规则路径）"
  echo "  ⚠ 这只守离线能力。**生产走规划层**，那个口径要另跑："
  echo "     TA_PLANNER_USER_ID=<uuid> bash scripts/check.sh --planner"
else
  echo "✗ 黄金集指标低于门槛" >&2; fail=1
fi

# ── 档位二：规划层口径（生产） ────────────────────────────────────────
#
# ## 为什么需要它
#
# 上面那个离线闸门守的是**规则路径**，而生产走的是**规划层**——两者基线差得远
# （规划准确率 88% vs 46%，工具召回 96% vs 64%）。于是出现了一个真实的盲区：
# 一个**只破坏规划层**的改动，离线闸门照样全绿，CI 放行，生产退化。
#
# ## 为什么要显式要用户
#
# 模型档案是**按用户**存的（`model_profiles.user_id`），不给用户就解析不到档案，
# 规划器会静默退回规则层——那就又变成"用离线数字冒充生产口径"。
# `golden_report.py --with-planner` 已经会在构造失败时拒绝出报告，
# 这里再挡一道：**没有 TA_PLANNER_USER_ID 就不跑**，而不是跑出一个假数字。
#
# 成本：50 次 LLM 调用。所以不设为默认。
if [ "${1:-}" = "--planner" ]; then
  log "黄金集指标门槛（规划层口径 = 生产）——守的是**真实用户体验**"
  if [ -z "${TA_PLANNER_USER_ID:-}" ]; then
    echo "✗ 规划层档位需要 TA_PLANNER_USER_ID（模型档案是按用户存的）。" >&2
    echo "  不给它就只能解析到默认档案，规划器会静默退回规则层，" >&2
    echo "  于是跑出的是**离线数字却标成生产口径**——比不跑更危险。" >&2
    echo "  用法：TA_PLANNER_USER_ID=<uuid> bash scripts/check.sh --planner" >&2
    fail=1
  else
    if no_proxy='*' .venv/bin/python scripts/golden_report.py --no-write --with-planner \
         --fail-under "$GOLDEN_MIN_PLANNING_PLANNER" \
         --fail-under-recall "$GOLDEN_MIN_RECALL_PLANNER"; then
      echo "✓ 规划准确率 ≥ ${GOLDEN_MIN_PLANNING_PLANNER} 且工具召回 ≥ ${GOLDEN_MIN_RECALL_PLANNER}（规划层口径）"
    else
      echo "✗ 规划层口径指标低于门槛（这是**生产**退化，不是离线能力问题）" >&2; fail=1
    fi
  fi
fi

# ── 档位三：端到端工具回合验收 ────────────────────────────────────────
#
# 35 例真实回合（`build_turn_request` → `run_turn_streaming`），走完整链路。
# 它此前**根本没接进闸门**：单测全绿，但"真实问一句话，工具到底调没调对"
# 没人守。成本 35 次模型调用，所以同规划层一样不设为默认。
if [ "${1:-}" = "--acceptance" ]; then
  log "端到端工具回合验收（35 例真实回合）——守的是**工具真的被调对了**"
  if [ -z "${TA_PLANNER_USER_ID:-}" ]; then
    echo "✗ 验收套件需要 TA_PLANNER_USER_ID（同上：档案按用户存）。" >&2
    echo "  用法：TA_PLANNER_USER_ID=<uuid> bash scripts/check.sh --acceptance" >&2
    fail=1
  elif no_proxy='*' .venv/bin/python scripts/verify_tool_turns.py \
         --user-id "$TA_PLANNER_USER_ID"; then
    echo "✓ 35 例端到端回合全部通过"
  else
    echo "✗ 端到端回合有失败（真实链路退化，单测看不出来）" >&2; fail=1
  fi
fi

log "结论"
if [ "$fail" -eq 0 ]; then echo "✓ 全部通过"; else echo "✗ 有未通过项（见上）" >&2; fi
exit "$fail"

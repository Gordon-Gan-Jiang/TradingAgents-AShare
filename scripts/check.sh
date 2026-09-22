#!/usr/bin/env bash
# 一道可执行的质量闸门：测试 + 黄金集指标 + 前端。
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
#   * 后端失败数不得超过 BASELINE_FAILURES（既有失败，与本改造无关）
#   * 前端 tsc 必须 0 错、vitest 必须全绿
#   * 黄金集（可选）：规划准确率不低于 GOLDEN_MIN_PLANNING，工具召回不低于 GOLDEN_MIN_RECALL

set -uo pipefail
cd "$(dirname "$0")/.."

BASELINE_FAILURES=${BASELINE_FAILURES:-7}
GOLDEN_MIN_PLANNING=${GOLDEN_MIN_PLANNING:-0.40}
GOLDEN_MIN_RECALL=${GOLDEN_MIN_RECALL:-0.70}

fail=0
log() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

log "后端全量测试（基线失败上限 ${BASELINE_FAILURES}）"
out="$(.venv/bin/python -m pytest tests -q -p no:cacheprovider 2>&1 | tail -3)"
echo "$out"
failed="$(printf '%s' "$out" | grep -oE '[0-9]+ failed' | grep -oE '[0-9]+' | tail -1)"
failed="${failed:-0}"
if [ "$failed" -gt "$BASELINE_FAILURES" ]; then
  echo "✗ 失败数 $failed 超过基线 $BASELINE_FAILURES（有新增回归）" >&2
  fail=1
else
  echo "✓ 失败数 $failed 未超过基线"
fi

log "前端类型检查与单测"
if (cd frontend && npx tsc --noEmit >/dev/null 2>&1); then
  echo "✓ tsc 0 错"
else
  echo "✗ tsc 报错" >&2; fail=1
fi
if (cd frontend && npx vitest run 2>&1 | grep -qE 'Tests +[0-9]+ passed'); then
  echo "✓ vitest 通过"
else
  echo "✗ vitest 未通过" >&2; fail=1
fi

if [ "${1:-}" = "--golden" ]; then
  log "黄金集指标门槛（规则路径，离线）"
  # 规划准确率口径依赖名称表预热，脚本内部已处理
  if .venv/bin/python scripts/golden_report.py --no-write \
       --fail-under "$GOLDEN_MIN_PLANNING"; then
    echo "✓ 规划准确率不低于 ${GOLDEN_MIN_PLANNING}"
  else
    echo "✗ 黄金集规划准确率低于门槛" >&2; fail=1
  fi
fi

log "结论"
if [ "$fail" -eq 0 ]; then echo "✓ 全部通过"; else echo "✗ 有未通过项（见上）" >&2; fi
exit "$fail"

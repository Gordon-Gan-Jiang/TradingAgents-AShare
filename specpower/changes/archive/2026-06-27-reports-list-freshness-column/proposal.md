## Why

历史报告列表已有「仅数据异常/过时」筛选和 `freshness_status` 后端字段，但数据状态徽章目前叠在「决策建议」列内，与任务状态（排队中/失败/决策方向）混在一起，用户在扫表时难以一眼区分「研判结论」与「底层数据是否可信」。需要在列表中增加独立的「数据状态」列，显式展示过时、异常、部分滞后等状态。

## What Changes

- 在 `Reports.tsx` 历史报告表格新增 **「数据状态」** 列（建议位于「模型」与「决策建议」之间）
- 列内展示与现有 `FreshnessBadge` 一致的文案：`数据异常` / `数据过时` / `部分滞后`；`fresh` 或 `null`（旧报告/未完成/失败）统一显示中性占位 **「—」**（refine 定稿：不显示「正常」绿色标签）
- 「决策建议」列仅保留任务状态与决策方向，移除嵌套的 `FreshnessBadge`
- 新增薄包装组件 `FreshnessStatusCell`（badge 或 `—`），复用 `FreshnessBadge` 样式
- 列头 tooltip 说明各状态含义（原生 `title`）
- 列表 API 已返回 `freshness_status`，**无需新增后端字段**；需确认 list 查询已包含该列（`REPORT_SUMMARY_COLUMNS`）
- Dashboard 报告卡片保持现有行内 badge 布局，**不在本 change 范围**

## Capabilities

### New Capabilities

<!-- 无全新能力域；为既有 freshness 披露的 UI 增强 -->

### Modified Capabilities

- `freshness-disclosure`: 扩展「Report UI staleness badge」要求——历史报告列表 MUST 使用独立列展示数据状态，而非与决策建议混排

## Impact

- **前端**：`frontend/src/pages/Reports.tsx` 表头/表体列定义；可能抽取 `FreshnessStatusCell` 或在 `FreshnessBadge.tsx` 增加列表单元格变体
- **类型**：`Report` 已有 `freshness_status`，无 breaking change
- **Spec baseline**：`specpower/specs/freshness-disclosure/spec.md` 需 delta MODIFIED
- **测试**：可选前端快照或组件单测；回归现有 freshness 筛选行为
- **兼容**：`freshness_status=null` 的旧报告、失败/排队任务不显示异常徽章，与 spec 一致

## Baseline

Found **3** existing specs. Affected: **`freshness-disclosure`** (UI list presentation).

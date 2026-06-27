## Context

`data-freshness-contract` 已归档：`ReportDB.freshness_status` 与 list API 字段就绪，`FreshnessBadge` 组件已有三种文案（数据异常 / 数据过时 / 部分滞后）。历史报告页 `Reports.tsx` 表格列顺序为：股票 → 分析日期 → 模型 → **决策建议**（内含 `renderStatusBadge` + 嵌套 `FreshnessBadge`）→ 置信度 → …；筛选器「仅数据异常/过时」已按 `freshness_status in (error, warning, stale)` 工作。Dashboard 最近报告为卡片行布局，badge 与决策横向排列，非表格。

用户诉求：扫表时一眼区分「研判结论」与「底层数据是否可信」，避免决策列视觉噪音。

## Goals / Non-Goals

**Goals:**
- 历史报告表格新增独立 **「数据状态」** 列，位于 **「模型」** 与 **「决策建议」** 之间
- 复用 `FreshnessBadge` 样式与文案；`fresh` / `null` / 未完成任务显示 **「—」**（em dash，与置信度列 `-` 区分：本列统一用 `—`）
- 「决策建议」列仅保留任务状态（排队/执行/失败）或决策方向，移除 `FreshnessBadge`
- 列头 tooltip 说明三种异常态含义
- Delta spec 更新 `freshness-disclosure` 的 list 展示要求
- 回归「仅数据异常/过时」筛选行为不变

**Non-Goals:**
- 不新增后端字段或 API 变更（`REPORT_SUMMARY_COLUMNS` 已含 `freshness_status`）
- 不改 ReportViewer 详情页 banner / 章节指示器
- 不改 Dashboard 卡片布局（本 change 仅 `Reports.tsx` 表格）
- 不在列表列内展示 `blocking_sources` 详情（详情页负责）
- 不做 backfill 或重算历史 `freshness_status`

## Design Decisions

### Decision 1: 列位置与表头文案

**Options considered:**
- A. 放在「决策建议」之后 — 与决策相邻，但无法解决混排问题
- B. 放在「模型」与「决策建议」之间 — 信息层次：标的 → 日期 → 模型 → **数据质量** → 结论
- C. 放在「分析日期」之后 — 过早，模型与结论之间更自然

**Chosen:** B

**Rationale:** 用户从左扫到右：先看标的与日期，再看用哪个模型，再判断数据是否可信，最后看决策。与 proposal 一致。

表头数组由：
`['股票', '分析日期', '模型', '决策建议', ...]`
改为：
`['股票', '分析日期', '模型', '数据状态', '决策建议', ...]`

### Decision 2: 单元格渲染组件

**Options considered:**
- A. 内联 `<FreshnessBadge />` + 手动 `—` — 简单但 duplicate null/fresh 逻辑
- B. 新增 `FreshnessStatusCell` 包装组件 — 集中「badge 或 em dash」规则，Reports 表体一行调用
- C. 扩展 `FreshnessBadge` 增加 `variant="cell"` 显示 placeholder — 组件职责混合

**Chosen:** B（薄包装，内部调用 `FreshnessBadge`）

**Rationale:** `FreshnessBadge` 保持「有问题才显示 badge」语义，供 Dashboard 等场景复用；列表列需要「永远有内容（badge 或 —）」由 `FreshnessStatusCell` 负责。

```tsx
// frontend/src/components/FreshnessStatusCell.tsx (new, ~15 lines)
export function FreshnessStatusCell({ status }: { status?: string | null }) {
  const showBadge = status && status !== 'fresh'
  if (showBadge) return <FreshnessBadge status={status} />
  return <span className="text-slate-400 dark:text-slate-500">—</span>
}
```

### Decision 3: fresh 与 null 的占位符

**Options considered:**
- A. 两者都空白（仅 badge 时显示）— 列宽不稳定，扫表难对齐
- B. `fresh` 显示「正常」绿色 — 与 spec「fresh 不显示 badge」冲突且增加噪音
- C. `fresh` 与 `null`/进行中/失败均显示 **「—」** — 列对齐稳定，异常态才着色

**Chosen:** C

**Rationale:** Baseline spec 要求 fresh 不显示 badge；用中性 dash 保持列结构而不引入第四类绿色标签。失败/排队任务通常 `freshness_status=null`，与旧报告一致。

### Decision 4: 列头 tooltip

**Options considered:**
- A. 无 tooltip — 依赖用户熟悉筛选器文案
- B. `<th title="...">` 原生 tooltip — 零依赖，足够
- C. 可点击帮助图标 — 过度设计

**Chosen:** B

**Rationale:** 文案一行即可：`数据异常：拉取失败；数据过时：关键数据滞后；部分滞后：非关键源滞后；—：正常或未评估`

实现：表头渲染时对 `h === '数据状态'` 增加 `title` 属性，或抽常量 `FRESHNESS_COLUMN_TOOLTIP`。

### Decision 5: Dashboard 范围

**Options considered:**
- A. 同步改为「状态 | 决策」两列式 — 卡片非表格，收益低
- B. 保持 Dashboard 行内 `FreshnessBadge` — 空间足够，不混在决策文字里
- C. Dashboard 也加文字标签「数据状态:」 — 视觉冗余

**Chosen:** B

**Rationale:** Dashboard 已是 badge 与 decision 横向 `gap-4` 分离，无混排问题；本 change 明确以 Reports 表格为主。

### Decision 6: 移动端与横向滚动

**Options considered:**
- A. 小屏隐藏「数据状态」列 — 与筛选器能力不一致
- B. 保持列，依赖现有 `overflow-x-auto` — 与新增列前行为一致
- C. 小屏改为决策列内 icon — 退回混排

**Chosen:** B

**Rationale:** 表格已横向滚动；数据状态与筛选功能相关，不宜在小屏隐藏。

## Risks / Trade-offs

| Risk | Mitigation |
|------|------------|
| 表列 +1 导致小屏横向滚动增加 | 列宽仅 badge 宽度；「—」占位极窄 |
| 用户误以为 `—` 等于「数据正常」 | 列头 tooltip 写明「—：正常或未评估」 |
| `FreshnessBadge` 与 cell 行为分叉 | Cell 仅包装，不复制样式常量 |
| 筛选器与列展示不一致 | 无 API 变更；仅 UI 搬迁，E2E/手动回归筛选 |

## Open Questions

（refine 两轮后无阻塞项；Dashboard 同步列为 explicit non-goal）

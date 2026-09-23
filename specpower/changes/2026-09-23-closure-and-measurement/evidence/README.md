# 本轮证据与文档快照（2026-09-23）

## 为什么这里有副本

`docs/` 整个被 `.gitignore:2` 忽略（注释写的是"local design/plan documents"，是有意为之），
所以 `docs/` 下 19 份文档**0 个进 git**——别人 clone 不到、PR 里看不到、换机器就没了。

用户 2026-09-23 的决定：**保持忽略，但把关键文档同步进 `specpower/`**（本目录）。
`specpower/` 是被 git 跟踪的（本项目既有的正式变更记录位置），
所以放在这里的东西才是"落盘"的。

## 这些文件是什么

| 文件 | 来源 | 用途 |
|---|---|---|
| `golden-baseline.md` | `docs/golden-baseline.md` | 黄金集 **规则路径（离线）** 口径：规划准确率 46.0%、工具召回 64.1% |
| `golden-with-planner.md` | `docs/golden-with-planner.md` | 黄金集 **规划层启用（生产口径）**：规划准确率 88.0%、工具召回 96.2% |
| `agent-status-2026-09-23.md` | `docs/agent-status.md` | 项目状态快照，含本轮 §7 实施结果与三处认知更正 |
| `agent-tool-inventory-2026-09-23.md` | `docs/agent-tool-inventory.md` | 42 个工具的清单与层级 |

**两份黄金集报告是有意分开的**：它们的**口径不同、基线差一倍**，
混在一起看就会得出错误结论（这正是本轮修的 M2："规划层被误测成规则路径"）。
每份报告首行都标了口径，不要合并。

## 相关文件

* 本轮问题清单/修复方案/任务拆分/执行结果：`../tasks.md`
* 板块历史替代源可行性调查：`../../2026-09-23-board-history-source/tasks.md`

## Context

见 `proposal.md` 的三条实测证据。关键约束：

| 约束 | 事实 | 后果 |
|---|---|---|
| 分层方向 | `api → agent → tools → signals`，由 AST 边界测试强制 | 工具层 MUST NOT import `api.services.market_scanner_service`；必须走**端口 + 应用层注入** |
| 同步工具超时 | `ToolRegistry.invoke` 按 `ToolSpec.latency_budget_ms` 强制超时 | 扫描实测 61.5s，`latency_budget_ms` 必须显著大于它，否则必然被判为超时失败 |
| 档位目标延迟 | `TierBudget.target_latency_ms` 此前**只赋值、从未被读取** | 扫描必然超出中档 30s；本变更顺带把该声明变成事实（超出即记录 + 提示） |
| 证据校验 | 校验器零 LLM，逐条检查 `claims[].refs` 存在、数字必须出现在**被引用证据**中 | 候选与数值只能来自证据，模型无法编造得分 |
| 判定事件 | `_verdict_from_tool_result` 认 `data["state"]` / `data["results"]` 两个键 | 筛选结果的 `data` MUST NOT 使用这两个键，否则会被误当成洗盘判定 |

## Goals / Non-Goals

- 目标：把既有扫描能力接入对话，并让「候选/依据/缺失/耗时」四件事可校验。
- 非目标：不新增选股算法、不改扫描器权重、不顺手跑逐只探针、不做异步任务化。

## Design Decisions

### D1 意图检测放在 `routing.py`，与档位分类同层

理由是既有「零 LLM 规则层」的定位：可在毫秒内穷举、可单测、不产生成本。
新增两个纯函数：

- `looks_like_market_screen(text) -> bool`：只认明确的筛选措辞（`选股/筛选/放量票/哪些票/给我找 N 只/涨幅榜/全市场`…）。
- `parse_screen_request(text) -> ScreenRequest`：解析 `top_k` 与评分档案。

**保守性**：措辞命中但消息里解析出标的时，**标优先**（沿用「显式陈述不得被模糊匹配覆盖」的既有教训）。数量解析失败取缺省，且夹在扫描器允许区间内 —— 让模型或正则去猜「多一些」是几十只，必然两难。

### D2 工具走端口注入，而不是直接 import 应用层

    tools/screen.py        定义 MarketScreener 端口（Protocol）与 ToolSpec
    api/services/market_screen_ports.py   真实实现，包装 scan_market_candidates
    chat_service.build_registry(market_screener=...)  注入

与 `signal_ports.py` 同一套做法。缺注入时不注册该工具（沿用 `build_registry` 既有语义：**不装配半成品**），于是没有扫描器的环境里该工具根本不在册，而不是「在册但一调就炸」。

### D3 证据形态：一只候选一条

`EvidenceItem(field="候选1", value="{symbol} {name}｜{score:.2f}分｜{reasons}", source_key="realtime_quotes", as_of=...)`。

这样「回答里的代码与得分必须来自证据」由既有校验器机械保证，不需要额外规则。另加两条范围证据（扫描范围与条件、数据时点与来源）。

### D4 缺失因子进 `Coverage.unavailable`，不进数值

实测 `volume_ratio`/`turnover_rate` 为 `null`。**不得**把它们写成 0（0 是「量比为零」这一具体断言，与「拿不到」是两件事）。做法：逐候选把缺失字段列进 `Coverage.unavailable`（如 `volume_ratio（量比）`），并要求回答逐项声明 —— 复用既有的「未覆盖维度必须声明」机制。

### D5 崩溃/空结果走降级而不是抛错

扫描失败时返回结构化失败（空证据 + `unavailable=("market_screen",)`），回答据此说明「未取得候选」。若直接抛错，循环会记一条工具失败并继续，模型将面对**空证据**去回答一个筛选问题 —— 那正是编造的温床。

### D6 让 `target_latency_ms` 名副其实

回合结束时若实际耗时 > 该档 `target_latency_ms`，记录 `latency_exceeded` 并发一条 `Notice`（含实际耗时）。这样「扫描 61.5s 而中档目标 30s」是用户可见的事实，而不是藏在实现里的意外。60s 量级的静默等待会被用户当成卡死。

### D7 第 4 类意图不新增档位

档位是「成本/延迟类别」，筛选是「有没有标的」。给筛选加档位会让 `TIER_BUDGETS` 与既有管道语义打架。筛选回合用 `medium`（允许 5 次工具调用），耗时由 D6 与 D5 的诚实告知覆盖，`market_screen` 自己的 `latency_budget_ms` 保证不被误判超时。

## Risks / Trade-offs

| 风险 | 处理 |
|---|---|
| 措辞误判：把单标的问句当成筛选 | 标优先 + 只认明确措辞；有测试固定「含代码则绝不触发扫描」 |
| 扫描很慢（全市场可能数分钟） | `latency_budget_ms` 留足；开始前提示；D6 如实记录；默认 `scan_limit` 取保守值并在回答里声明覆盖范围 |
| 「放量」在数据源缺失时名不副实 | D4：缺失进 `unavailable`，回答必须声明；有测试固定该行为 |
| 结果只有代码没有理由 | 证据带 `reasons`（扫描器原生中文理由）+ 得分，理由同样受校验器约束 |

## Migration Plan

1. 纯函数与工具（含测试）→ 可独立验证。
2. 端口实现与应用层注入。
3. 回合编排接入（计划工具 + 提示词一行 + 耗时提示）。
4. 文案分流。
5. 回滚方式：不注入 `market_screener` 即回到今天的行为（该工具不注册），其余改动（文案分流）独立可用。

## Open Questions

1. 是否提供异步化扫描（先回执、完成后推送）以规避 60s 同步等待？本轮不做，但 D6 的记录会为这个决策提供数据。
2. 是否给候选附「该票是否在洗盘」的追加入口（一键追问）？用户本轮明确选择不需要。

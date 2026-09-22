# 交接：对话式投研 Agent（新会话从这里开始）

> 写给**下一个会话/下一个 agent**。读完这一页即可接手，不必重读 40 轮对话。
> 最后更新：2026-09-22（会话末）。工作区干净，HEAD 即验证过的内容。

## 0. 三十秒上手

```bash
cd /Users/gankang/AlphaPilot-A-Share
bash scripts/check.sh                 # 质量闸门：后端全量 + 前端（约 7 分钟）
bash scripts/check.sh --golden        # 追加黄金集指标门槛（离线）
bash scripts/dev.sh restart           # 重启前后端（:8000 / :5173）
```

**当前基线：后端 `7 failed / 2588 passed`**——7 条全部为**既有失败**（`test_api_smoke`
4 条、`test_report_recovery` 2 条、`test_stock_map_fast_path` 1 条），不是本改造引入的。
判定方法（务必沿用）：`git archive HEAD` 到临时目录跑同一条命令，逐条比对失败集，
要求 **最终失败集 ⊆ 基线失败集**，且 passed 数只增不减。
（历史：会话早期报过 12/16，那是环境差异；`test_api_smoke` 里 watchlist/portfolio 两条
是**顺序相关**的偶发失败——一次全量里 8 条、另一次 6 条，干净副本里同样出现，与本改造无关。）

## 1. 已完成（有证据，不需要重做）

| 项 | 证据 |
|---|---|
| LLM 规划层（P1） | 黄金集 **覆盖率 60%→100%、规划准确率 46%→82%**；灰度开关见 §3 |
| 声明由代码注入（②） | 实测 `degraded`→`completed`，正文出现「数据未覆盖」与「数据口径：模型估计」 |
| 工具面原子化（④） | **19 → 50**：L1 18 / L2 28 / L3 4，全部含证据契约 + 弃权声明 + 测试 |
| 判定跑通 | 跨层列名契约修复后，洗盘判定首次在真实数据上出结论（`distribution`） |
| 度量与闸门 | `tests/golden/questions.jsonl`(50 条) + `tests/golden/harness.py` + `scripts/golden_report.py` + `scripts/check.sh` |
| ④ 精确率收口 | 规划层启用下：**规划准确率 88.0%、工具召回 96.2%、工具精确率 90.5%**（已高于规则路径 86.2%）、澄清 98.0%。结论：此前"规划器乱调工具"的诊断**是错的**，失真是三层度量/边界缺陷叠加（见 §7） |
| ① 逐工具真实对话验证 | `scripts/verify_tool_turns.py`（23 条真实问句走完整回合）：**22/23 通过**。真实执行抓到并修掉 6 个缺陷（见 §7） |
| 归因正确性 | 对话落库的 `model` 标签此前丢弃用户选中档案，实测落成 `doubao-...` 而实际用 `deepseek-v4-flash` → 已修 |

## 2. 剩下的三件事（①②④⑤ 已完成，见 §1 与 §7）

1. **让对话默认可用**（只有用户能做）：在「系统 → 模型管理」设一个**默认模型档案**。
   该账号 5 个档案、0 个默认 → 不显式传 `model_profile_id` 时回落到失效兜底 Key。
   （脚本里的验证都显式传了档案，所以不受影响；**页面上直接对话会受影响**。）
2. **② L3 端到端**：真实提交一个分钟级任务 → `poll_async_job` 查到终态，
   重点验 `db=None` 时服务自开会话那条路径。
   已探明的地形：`backtest_service.submit` 起的是**普通线程**，任何进程都能跑，
   最适合做"终态"证据；`submit_daily_review`/`submit_deep_analysis` 走
   `api.main._submit_background_task` → 无事件循环时依赖 `_MAIN_EVENT_LOOP`，
   因此必须在**跑过 lifespan 的进程**里验（`TestClient` 或真实服务）。
3. **验等一次人工决策：轻档模型预算**（见 §7 的"待决策"）。

### 已确认的剩余缺口（不必重新发现）

* **`report_consensus_read` 没有规则入口**：带代码的问句一定能解析出标的 →
  永远不会走规划层 → 该工具对这类问句不可选（`verify_tool_turns.py` 里唯一未通过项）。
  同类还有 `t1_quality_read`（已在 `_LIGHT_MARKERS` 之外）。
* **多标的只认显式代码**：`茅台和宁德时代哪个好` 被当成歧义而追问
  （`test_name_ambiguity_still_asks` 锁着现行行为，改动前先确认产品意图）。
* **规划步骤之间没有数据传递**：`p02` 里规划器想 `watchlist_read` → `signal_washout_probe_batch`
  串联，而 batch 的 `symbols` 只能来自上一步输出 → 该步被跳过。
* **`index_quote_read` 不稳定**：同一条问句两次运行，一次 8 条证据、一次 0 条。

## 3. 配置入口（都实测过）

```bash
# .env（api/main.py 用 load_dotenv 明确加载 —— 守护进程不继承 shell export！）
TA_LLM_PLANNER=1                                  # LLM 规划层灰度开关（默认关闭）
TA_PLANNER_MODEL_PROFILE_ID=<profile_id>          # 规划器专用档案（可与回答模型分开）
TA_PLANNER_MODEL=<model_name>                     # 或用模型名
TA_EM_PROXY=http://127.0.0.1:7890                 # 东财出口代理（可选）
TUSHARE_TOKEN=<token>                             # 补资金流/筹码（可选，未配置则如实弃权）
```

跑黄金集/全量时建议 `no_proxy='*'`：本机系统代理（Clash 类，`127.0.0.1:7897`）
会把本机请求也接管，实测导致全量卡顿与 MCP 用例假失败。

## 4. 六个必须知道的坑（本会话都踩过，代价不小）

1. **打桩要打在"被测模块自己的命名空间"**：被测模块若是模块级
   `from x import y`，替换 `x.y` 或 `sys.modules['x']` **无效** → 以为打了桩
   其实真的发了网络请求，测试结果随网络状态红绿翻转。本会话一个文件里踩了两次。
2. **falsy 零陷阱**：`x or default` 会把 `0` 换成缺省值。会话里出现 **4 次**
   （`code or -1` 把成功判失败、`limit or 10`、`hold_days or 5`…）。一律写
   `None if x is None else x`。
3. **"读不到"不许说成"没有"**：本会话出现 **3 次**（读错返回键 `verdicts`/`items`、
   兑现查询分页主体错、空返回被当成源可用）。任何"读取类"端口都要有一条测试专门
   验证"有数据但被上层说成没有"不会发生。
4. **跨层契约**：`provider` 给 `Date/Close`（提示词表格用），纯信号层要
   `date/close`（数值计算用）。`get_daily_bars_df` 必须经 `to_signal_bars` 转换。
   这个断口曾让**洗盘判定从未在真实数据上跑通**，而 2000+ 用例全绿（测试桩恰好是
   信号层要的形状）。
5. **延迟预算要追上真实工作量**：探针曾声明 15s，而它要拉日线+自算筹码+分时+
   板块（首次扫 49 个板块）+资金流多通道回退 → 被注册表整体判超时、返回 0 证据。
   内层工具预算不得大于外层端到端预算（60s）。
6. **东财不是全面封禁**：`np-anotice-stock`（公告）、`push2`（指数快照）、
   资金流直连**实测都通**；被封的是 push2 的**个股/板块资金流**那条。别据此
   下"必须买代理"的结论——先实测。

## 5. 分层与不变量（改代码前务必遵守）

* 分层方向 `api → agent → tools → signals/grounding`，由
  `tests/test_architecture_boundaries.py`（AST，203 条）强制。
  `signals/` 与 `grounding/` 是**纯函数层**：禁网络、禁 LLM、禁时钟。
* 工具面：`ToolSpec` 单一权威定义；**L3 必须 `asynchronous=True`**（构造期强制）；
  端口在工具层声明、应用层装配注入、**未注入即不注册**；
  `user_id` 只在闭包里绑定，**工具参数里不得出现**。
* 回答层：模型只产出 `claims+refs`，零 LLM 校验器逐条核对（数字须出自**被引用
  证据**、股票代码须为本轮标的或出现在被引证据中）；**声明与口径由代码渲染**，
  不让模型复述。
* 灰度原则：每处接入都"新增而不替换"，出问题能立刻回退。

## 6. 关键文件地图

| 关注点 | 文件 |
|---|---|
| 意图路由 / 计划 | `tradingagents/agent/routing.py`（`Plan`/`parse_plan`/`LlmPlanner`） |
| 回合循环 | `tradingagents/agent/loop.py`（工具计划、提示词、降级） |
| 多轮状态 | `tradingagents/agent/state.py`、`api/services/conversation_state_service.py` |
| 工具面 | `tradingagents/tools/`（分层见 `spec.py`；样板见 `signals.py`/`history.py`） |
| 任务提交（唯一实现） | `api/services/analysis_job_service.py`（路由与工具共用） |
| 证据校验 | `tradingagents/grounding/verifier.py` |
| 度量 | `tests/golden/`、`scripts/golden_report.py`、`scripts/check.sh` |
| 记账 | 本目录 `tasks.md`（含第 12–15 轮记录） |

## 7. 本轮（收口轮）的核心发现——**先读这一节**

### 7.1 ④ 的结论：诊断错了，不是模型乱调工具

原判断是"规划层精确率 54.4% vs 规则 86.2%，说明规划器乱调工具"。追下去发现是
**三层度量/边界缺陷叠加**，修完后精确率 **90.5%**（高于规则路径）：

1. **跑分与规划器看到的不是同一份工具面**：`build_planner()` 用生产注册表（50 个），
   黄金集 harness 用哑依赖装配（26 个）。规划器选中只在生产注册表里的工具时，
   `_plan_tools` 以"未在册"丢掉 → 观测集合 ≠ 真实选择。
2. **`required_capabilities` 从不参与评分**：`score()` 只读 required/optional。
   9 个新 L1 工具写在 capability 里 → "选对了"不计分，"多调了一个"全额扣分。
3. **规划层的决定在边界被推翻**：空步骤被当成"没计划"→ 落进轻档兜底调
   `get_board_spot`（50 条里 8 条）；`plan.need_clarify` 从未传进 `TurnRequest`
   → 概念问答被要股票代码。

一句话教训：**在说"模型不行"之前，先证明尺子和管道是对的。**

### 7.2 ① 的结论：22/23，且真实执行抓到 6 个单测看不见的缺陷

`scripts/verify_tool_turns.py`（23 条真实问句 → 完整回合）：

* `market_screen` 对**多数问句**是必然失败的调用：`parse_screen_request` 在无档案
  关键词时给 `profile=None`，而 schema 声明为 `string` → 注册表直接拒。
  **单测只查参数名合法，从不查取值合法**，所以一直没被发现。
* 「我的自选股有哪些」被判成全市场筛选——子串「自**选股**」命中了「选股」。
* 落库的 `model` 标签丢弃用户选中档案（归因串样本）。
* 降级文本只给字段名不给值（用户问"净流入多少"，回答里没有那个数）。
* 脚本自身两处静默降级：没装 `.env` → 规划器静默关闭（量的是规则路径）；
  没建表 → 跨轮状态静默失效。**验证脚本必须自己证明它量的是对的那条路径。**

### 7.3 待决策：轻档 `max_model_calls=1` 让"修复一次"结构上不可达

实测 23 条里 **7 条降级**，原因全是"首答未过校验 + 预算已用尽（1/1）"，
失败理由是 `unsourced_date` / `unsourced_number` / `flow_direction_conflict`——
**证据取到了，回答出不去**。而循环里"校验失败 → 修复一次"的路径需要第二次调用。

* 现状：`TIER_LIGHT.max_model_calls = 1`，由
  `test_light_tier_has_single_call_budget` 明确锁住（是**已声明的成本约束**）。
* 本轮**没有动它**（那是产品决策）。改为把降级文本渲染成"证据的值 + 引用编号"，
  零额外成本地把"无用拒绝"变成"可读事实"。
* 若要进一步降低降级率，选项是 `max_model_calls=2`（仅在首答失败时多花一次），
  代价与收益都能从 `verify_tool_turns.py --json` 的 `status` 分布直接量出来。

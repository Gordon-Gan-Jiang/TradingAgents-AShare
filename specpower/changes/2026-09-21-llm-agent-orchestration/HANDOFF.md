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

**当前基线：后端 `12 failed / 2529 passed`**——12 条全部为**既有失败**，
不是本改造引入的。判定方法（务必沿用）：`git archive HEAD` 到临时目录跑同一条
命令，用 `comm` 比对失败集，要求 **最终失败集 ⊆ 基线失败集**。
不要相信"7 条基线"这个旧数字：会话早期环境不同（系统代理/数据源可用性变了），
真实基线是 **16**，其中 4 条已被本会话的测试密闭性修复消掉。

## 1. 已完成（有证据，不需要重做）

| 项 | 证据 |
|---|---|
| LLM 规划层（P1） | 黄金集 **覆盖率 60%→100%、规划准确率 46%→82%**；灰度开关见 §3 |
| 声明由代码注入（②） | 实测 `degraded`→`completed`，正文出现「数据未覆盖」与「数据口径：模型估计」 |
| 工具面原子化（④） | **19 → 50**：L1 18 / L2 28 / L3 4，全部含证据契约 + 弃权声明 + 测试 |
| 判定跑通 | 跨层列名契约修复后，洗盘判定首次在真实数据上出结论（`distribution`） |
| 度量与闸门 | `tests/golden/questions.jsonl`(50 条) + `tests/golden/harness.py` + `scripts/golden_report.py` + `scripts/check.sh` |

## 2. 立刻可做的六件事（按优先级）

1. **让对话默认可用**（只有用户能做）：在「系统 → 模型管理」设一个**默认模型档案**。
   该账号 5 个档案、0 个默认 → 不显式传 `model_profile_id` 时回落到失效兜底 Key，
   任何对话都会 `failed (AuthenticationError)`。
2. **逐工具真实对话验证**：50 个工具里只有约 4 个走过真实对话（市场主线、持仓组合、
   判定历史、事实型取数）。其余只有单测 + 端口级验证。**"测试绿 ≠ 能力可用"。**
3. **L3 端到端**：真实提交一个分钟级任务 → `poll_async_job` 查到终态。
   重点验 `db=None` 时服务自开会话那条路径（目前只有 1 条单测覆盖）。
4. **修不密闭的测试**：`test_recommendation_service`(2) 与 `test_api_smoke` 里
   3 条 market_scan 相关用例依赖实时行情。本会话已修 5 条同类（东财相关），
   修法是**打桩被测模块自己命名空间里的名字**——见 §4 的坑 1。
5. **工具精确率收口**：规划层 54.4% vs 规则 86.2%。先看规划器**实际多调了什么**
   （`scripts/golden_report.py --with-planner` + 失败明细），再决定收紧提示词
   要求最小工具集，还是校准黄金集的 `optional_tools`。
6. **600519 判定悬案**：从 `distribution/85` 变成 `insufficient_evidence/0`。
   需判定是"筹码作为模型估计参与门槛被过度扣分"还是"更保守的正确行为"。

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

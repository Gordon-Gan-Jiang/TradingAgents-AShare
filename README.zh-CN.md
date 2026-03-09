<p align="center">
  <img src="assets/TauricResearch.png" style="width: 60%; height: auto;">
</p>

<div align="center" style="line-height: 1;">
  <a href="https://arxiv.org/abs/2412.20138" target="_blank"><img alt="arXiv" src="https://img.shields.io/badge/arXiv-2412.20138-B31B1B?logo=arxiv"/></a>
  <a href="https://discord.com/invite/hk9PGKShPK" target="_blank"><img alt="Discord" src="https://img.shields.io/badge/Discord-TradingResearch-7289da?logo=discord&logoColor=white&color=7289da"/></a>
  <a href="./assets/wechat.png" target="_blank"><img alt="WeChat" src="https://img.shields.io/badge/WeChat-TauricResearch-brightgreen?logo=wechat&logoColor=white"/></a>
  <a href="https://x.com/TauricResearch" target="_blank"><img alt="X Follow" src="https://img.shields.io/badge/X-TauricResearch-white?logo=x&logoColor=white"/></a>
  <br>
  <a href="https://github.com/TauricResearch/" target="_blank"><img alt="Community" src="https://img.shields.io/badge/Join_GitHub_Community-TauricResearch-14C290?logo=discourse"/></a>
</div>

<div align="center">
  <a href="./README.en.md">English</a> |
  <a href="./README.md">中文</a>
</div>

---

# TradingAgents-AShare：多智能体 LLM 交易研究框架

这是一个面向 A 股投研与策略验证的多智能体框架，用于把“市场/新闻/基本面/风控”分析流程化、可复现化，并输出可执行的交易决策。

## 致谢与来源
- 本项目基于 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) 进行二次开发与本地化扩展。
- 感谢原作者团队开源框架与研究成果。

## 项目文档
- 英文原版：`README.en.md`
- 变更记录：`CHANGELOG.md`
- 许可边界：`LICENSING.md`

## 许可说明
- 上游衍生与混合修改部分继续适用 `Apache-2.0`。
- 本仓库新增的前端、部署脚本与部分文档采用 `PolyForm Noncommercial 1.0.0`，仅允许非商业用途。
- 如需商业化运营、托管 SaaS、付费 API 或再分发商业版本，请单独联系维护者授权。

## 新闻
- [2026-02] 发布 **TradingAgents v0.2.0**：支持多 LLM 提供方（GPT-5.x、Gemini 3.x、Claude 4.x、Grok 4.x），并改进系统架构。
- [2026-01] 发布 **Trading-R1** [技术报告](https://arxiv.org/abs/2509.11420)，[Terminal](https://github.com/TauricResearch/Trading-R1) 预计很快上线。

<div align="center">
<a href="https://www.star-history.com/#KylinMountain/TradingAgents-AShare&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=KylinMountain/TradingAgents-AShare&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=KylinMountain/TradingAgents-AShare&type=Date" />
   <img alt="TradingAgents Star History" src="https://api.star-history.com/svg?repos=KylinMountain/TradingAgents-AShare&type=Date" style="width: 80%; height: auto;" />
 </picture>
</a>
</div>

> 🎉 **TradingAgents** 已正式发布。我们收到了大量关于该项目的咨询，感谢社区的关注与支持。
>
> 因此我们决定将框架全面开源，也期待与你一起共建更多有影响力的项目。

<div align="center">

🚀 [框架介绍](#tradingagents-框架) | ⚡ [安装与 CLI](#安装与-cli) | 🎬 [演示视频](https://www.youtube.com/watch?v=90gr5lwjIho) | 📦 [包用法](#tradingagents-包用法) | 🤝 [贡献](#贡献) | 📄 [引用](#引用)

</div>

## TradingAgents 框架

TradingAgents 是一个模拟真实交易机构协作模式的多智能体框架。系统通过 LLM 驱动的专业角色协同工作：从基本面分析师、情绪分析师、新闻分析师、技术分析师，到交易员与风控团队，共同评估市场并形成交易决策；同时，多个角色会进行动态讨论以收敛出更优策略。

<p align="center">
  <img src="assets/schema.png" style="width: 100%; height: auto;">
</p>

> TradingAgents 主要用于研究目的。交易表现会受到模型选择、温度参数、交易区间、数据质量及其他非确定性因素影响。[不构成金融、投资或交易建议。](https://tauric.ai/disclaimer/)

该框架将复杂交易任务拆分为专门角色，以实现更稳健、可扩展的市场分析与决策流程。

### 分析团队（Analyst Team）
- 基本面分析师：评估公司财务与经营指标，识别内在价值与潜在风险。
- 情绪分析师：分析社交媒体与公众情绪，判断短期市场情绪变化。
- 新闻分析师：跟踪全球新闻与宏观事件，评估其对市场的潜在影响。
- 技术分析师：使用技术指标（如 MACD、RSI）识别交易形态并预测价格变化。

<p align="center">
  <img src="assets/analyst.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

### 研究团队（Researcher Team）
- 由多头与空头研究员组成，围绕分析团队结论开展结构化辩论，在潜在收益与风险之间做平衡。

<p align="center">
  <img src="assets/researcher.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### 交易员（Trader Agent）
- 汇总分析与辩论结果，生成交易方案；并基于综合信息决定交易方向、时机与仓位规模。

<p align="center">
  <img src="assets/trader.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### 风险管理与组合经理
- 风控团队持续评估组合风险（波动、流动性等），并对交易方案进行审查与调整，将风险评估报告提交给组合经理。
- 组合经理负责最终批准/驳回交易提案；若批准，订单将发送到模拟交易所执行。

<p align="center">
  <img src="assets/risk.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

## 安装与 CLI

### 安装

克隆项目：
```bash
git clone git@home:KylinMountain/TradingAgents-AShare.git
cd TradingAgents-AShare
```

创建虚拟环境（示例）：
```bash
conda create -n tradingagents python=3.13
conda activate tradingagents
```

安装依赖（推荐使用 uv）：
```bash
uv sync
```

### 必要 API

TradingAgents 支持多种 LLM 提供方。为你使用的提供方设置 API Key：

```bash
export OPENAI_API_KEY=...          # OpenAI (GPT)
export GOOGLE_API_KEY=...          # Google (Gemini)
export ANTHROPIC_API_KEY=...       # Anthropic (Claude)
export XAI_API_KEY=...             # xAI (Grok)
export OPENROUTER_API_KEY=...      # OpenRouter
export ALPHA_VANTAGE_API_KEY=...   # Alpha Vantage
```

如需使用本地模型，可在配置中设置 `llm_provider: "ollama"`。

也可以复制 `.env.example` 到 `.env` 后填写：
```bash
cp .env.example .env
```

### CLI 用法

```bash
uv run python -m cli.main
```

你会看到可选择 ticker、日期、LLM、研究深度等参数的界面。

<p align="center">
  <img src="assets/cli/cli_init.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

运行中界面会持续展示各 agent 的进度与结果。

<p align="center">
  <img src="assets/cli/cli_news.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

<p align="center">
  <img src="assets/cli/cli_transaction.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

## TradingAgents 包用法

### 实现细节

框架基于 LangGraph 构建，以保证灵活性与模块化。当前支持 OpenAI、Google、Anthropic、xAI、OpenRouter、Ollama 等多 LLM 提供方。

### Python 用法

在代码中可以直接导入 `tradingagents` 并初始化 `TradingAgentsGraph()`。`.propagate()` 会返回最终决策。示例：

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG.copy())

# forward propagate
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

你也可以按需调整默认配置（模型、辩论轮次等）：

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"        # openai, google, anthropic, xai, openrouter, ollama
config["deep_think_llm"] = "gpt-5.2"     # 深度推理模型
config["quick_think_llm"] = "gpt-5-mini" # 快速任务模型
config["max_debate_rounds"] = 2

ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

完整配置项见：`tradingagents/default_config.py`。

## 贡献

欢迎社区贡献。无论是修复 bug、改进文档，还是提出新功能建议，都很有价值。

## 引用

如果你觉得 *TradingAgents* 对你的研究有帮助，请引用：

```bibtex
@misc{xiao2025tradingagentsmultiagentsllmfinancial,
      title={TradingAgents: Multi-Agents LLM Financial Trading Framework}, 
      author={Yijia Xiao and Edward Sun and Di Luo and Wei Wang},
      year={2025},
      eprint={2412.20138},
      archivePrefix={arXiv},
      primaryClass={q-fin.TR},
      url={https://arxiv.org/abs/2412.20138}, 
}
```

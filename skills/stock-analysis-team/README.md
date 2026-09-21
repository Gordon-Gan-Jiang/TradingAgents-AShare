# stock-analysis-team（本仓库内嵌）

本目录为 [wudengyao/stock-analysis-team](https://github.com/wudengyao/stock-analysis-team) 的整合副本，用于与 **TradingAgents-AShare** 放在同一仓库中维护，便于 OpenClaw / 本地同时使用：

- **框架与模板**：`references/` 下的分析框架、风控标准、HTML 参考等
- **可执行脚本**：`scripts/` 下行情（**A 股默认 AkShare**，美股 yfinance）、图表与 HTML 报告生成

## 依赖

在仓库根目录执行：

```bash
uv sync --group stock-analysis-team
```

主项目已包含 `yfinance`、`pandas` 等；本组补充 `ta`、`matplotlib`、`jinja2`、`plotly`、`beautifulsoup4` 等脚本专用依赖。

## 运行脚本

```bash
cd skills/stock-analysis-team
python scripts/market_data_fetcher.py --help
```

## 与 `tradingagents-analysis` 的分工

- 需要 **与本站一致的 A 股多智能体分析** → 使用 `../tradingagents-analysis/`，配置 `TRADINGAGENTS_TOKEN` 与 `TRADINGAGENTS_API_URL`
- 需要 **独立 HTML 报告 / 行情快照管线** → 使用本目录脚本与 `SKILL.md` 流程（A 股默认 AkShare，环境变量 `TA_STOCK_SKILL_CN_SOURCE=yfinance` 可切回 Yahoo）

## 已接入后端 API（TradingAgents-AShare）

登录后可用（`Authorization: Bearer`）：

- `GET /v1/skill/stock-team/yfinance-snapshot` — 参数 `mode`=`stock`|`market`，`market`=`cn`|`us`|`both`，`symbol`（个股必填）、`period`、`interval`
- `GET /v1/skill/stock-team/references/{name}` — 白名单见 `api/services/stock_analysis_skill_service.py` 中 `_REFERENCE_ALLOWLIST`

设计说明见仓库根目录 `docs/stock-analysis-team-integration.md`。

### 数据源与 Yahoo 限流

A 股默认 **AkShare**，不占用 Yahoo 配额。若将 `TA_STOCK_SKILL_CN_SOURCE=yfinance` 或拉 **美股**，仍可能遇到 `Too Many Requests`，可在 `.env` 中调整：`YFINANCE_MAX_RETRIES`、`YFINANCE_RETRY_BASE_SEC`、`YFINANCE_INTER_MARKET_SEC`、`TA_STOCK_SKILL_CACHE_TTL` 等，详见 `docs/stock-analysis-team-integration.md` 第 7 节。

## 上游更新

若上游仓库有更新，可从本机同步（示例）：

```bash
rsync -av --delete /path/to/stock-analysis-team/scripts/ ./skills/stock-analysis-team/scripts/
rsync -av --delete /path/to/stock-analysis-team/references/ ./skills/stock-analysis-team/references/
```

并人工合并 `SKILL.md` 变更。

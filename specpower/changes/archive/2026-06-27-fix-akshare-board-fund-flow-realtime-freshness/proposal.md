## Why

历史/当日分析报告出现「数据异常」：`board_fund_flow` 因 akshare API 更名导致 AttributeError；`realtime_quotes` 在非当日盘中分析时被 freshness 误判为「数据源返回空」。

## What Changes

- `get_board_fund_flow` 增加 akshare API 回退链（`stock_sector_fund_flow_rank`）
- 板块资金流向成功响应嵌入 `数据截止 YYYY-MM-DD` 供 freshness 解析
- `intraday_only` 数据源在未采集时标记 `not_applicable`，不再计为 fetch error
- `market_scanner_service` 同步板块资金流 API 回退

## Capabilities

### Modified Capabilities

- `freshness-validator`: intraday_only 源在非适用分析模式下缺失数据不得判 error
- `freshness-contract`: board_fund_flow 提供商需兼容 akshare API 版本差异

## Impact

- `tradingagents/dataflows/providers/cn_akshare_provider.py`
- `tradingagents/dataflows/freshness/validator.py`
- `api/services/market_scanner_service.py`
- `tests/test_freshness_contract.py`, 新增 provider 回归测试

## Why

财报 freshness 将 `20251231` 等列误解析为 `2025-Q1`，导致四表 + fundamentals 在已有 `2026-Q1` 数据时仍显示「部分滞后」。

## What Changes

- `parse_disclosure_period` 优先取最新 `YYYYMMDD` 并正确映射季度
- 财报 markdown 增加 `最新披露期: YYYY-Qn` 显式锚点

## Capabilities

### Modified Capabilities

- `freshness-contract`: 披露期解析必须从 YYYYMMDD 列取最新季度

## Impact

- `tradingagents/dataflows/freshness/parser.py`
- `tradingagents/dataflows/providers/cn_akshare_provider.py`
- `tests/test_disclosure_period_parser.py`

# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- Added Chinese README: `README.zh-CN.md`.
- Added this changelog file.

## [2026-03-02]

### Added
- Added centralized prompt catalog with bilingual templates:
  - `tradingagents/prompts/en.py`
  - `tradingagents/prompts/zh.py`
  - `tradingagents/prompts/catalog.py`
- Added prompt language controls in config:
  - `prompt_language`
  - `prompt_language_by_provider`
- Added runtime artifact ignore rules in `.gitignore`:
  - `eval_results/`
  - `reports/`
  - `results/`

### Changed
- Default prompt language switched to Chinese (`zh`).
- Refactored agents/managers/risk/trader/reflection/signal nodes to load prompts via catalog.
- Improved vendor routing fallback behavior in `tradingagents/dataflows/interface.py`:
  - Provider runtime errors now fallback to next provider in chain.
  - Error message now includes last provider error context.
- Enhanced `cn_akshare` provider:
  - Added in-provider market data fallback chain:
    1. Eastmoney `stock_zh_a_hist`
    2. Sina `stock_zh_a_daily`
    3. Tencent `stock_zh_a_hist_tx`
  - Normalized mixed source columns into unified OHLCV schema.
  - Hardened indicator computation to avoid `KeyError('date')` on non-trading dates.
  - Localized CN news/global news output text.
- Localized yfinance data/indicator textual outputs to Chinese for CN workflow consistency.

### Fixed
- Fixed intermittent indicator failures (`boll_ub`, `atr`) caused by provider-specific date-column mismatch.
- Fixed frequent hard-fail behavior when primary provider throws non-`NotImplementedError` exceptions.


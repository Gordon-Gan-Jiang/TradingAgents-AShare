from __future__ import annotations

from .base import BaseMarketDataProvider


class CnMarketOnlyProvider(BaseMarketDataProvider):
    """Base provider that only implements CN market microstructure tools.

    Many providers only support a subset of methods (e.g. fund flow / 龙虎榜).
    This base class provides explicit, uniform NotImplementedError messages for
    unrelated capabilities while keeping subclasses small and readable.
    """

    _NOT_SUPPORTED = "This provider only supports CN market microstructure tools."

    def get_stock_data(self, symbol: str, start_date: str, end_date: str) -> str:
        raise NotImplementedError(self._NOT_SUPPORTED)

    def get_indicators(self, symbol: str, indicator: str, curr_date: str, look_back_days: int) -> str:
        raise NotImplementedError(self._NOT_SUPPORTED)

    def get_fundamentals(self, ticker: str, curr_date: str = None) -> str:
        raise NotImplementedError(self._NOT_SUPPORTED)

    def get_balance_sheet(self, ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
        raise NotImplementedError(self._NOT_SUPPORTED)

    def get_cashflow(self, ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
        raise NotImplementedError(self._NOT_SUPPORTED)

    def get_income_statement(self, ticker: str, freq: str = "quarterly", curr_date: str = None) -> str:
        raise NotImplementedError(self._NOT_SUPPORTED)

    def get_news(self, ticker: str, start_date: str, end_date: str) -> str:
        raise NotImplementedError(self._NOT_SUPPORTED)

    def get_global_news(self, curr_date: str, look_back_days: int = 7, limit: int = 50) -> str:
        raise NotImplementedError(self._NOT_SUPPORTED)

    def get_insider_transactions(self, symbol: str) -> str:
        raise NotImplementedError(self._NOT_SUPPORTED)


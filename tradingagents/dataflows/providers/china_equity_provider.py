from .base import BaseMarketDataProvider


class ChinaEquityProvider(BaseMarketDataProvider):
    """Placeholder provider for CN market data integration."""

    @property
    def name(self) -> str:
        return "china_equity"

    def _not_implemented(self) -> str:
        raise NotImplementedError(
            "Provider 'china_equity' is not implemented yet. "
            "Implement this provider to connect A-share data sources."
        )

    def get_stock_data(self, symbol: str, start_date: str, end_date: str) -> str:
        return self._not_implemented()

    def get_indicators(
        self, symbol: str, indicator: str, curr_date: str, look_back_days: int
    ) -> str:
        return self._not_implemented()

    def get_fundamentals(self, ticker: str, curr_date: str = None) -> str:
        return self._not_implemented()

    def get_balance_sheet(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        return self._not_implemented()

    def get_cashflow(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        return self._not_implemented()

    def get_income_statement(
        self, ticker: str, freq: str = "quarterly", curr_date: str = None
    ) -> str:
        return self._not_implemented()

    def get_news(self, ticker: str, start_date: str, end_date: str) -> str:
        return self._not_implemented()

    def get_global_news(
        self, curr_date: str, look_back_days: int = 7, limit: int = 50
    ) -> str:
        return self._not_implemented()

    def get_insider_transactions(self, symbol: str) -> str:
        return self._not_implemented()


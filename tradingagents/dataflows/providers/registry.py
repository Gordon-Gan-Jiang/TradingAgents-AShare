from typing import Dict

from .base import BaseMarketDataProvider
from .china_equity_provider import CnStubProvider
from .cn_eastmoney_http_provider import CnEastmoneyHttpProvider
from .cn_sina_moneyflow_provider import CnSinaMoneyflowProvider


class DataProviderRegistry:
    """Simple in-memory provider registry."""

    def __init__(self):
        self._providers: Dict[str, BaseMarketDataProvider] = {}

    def register(self, provider: BaseMarketDataProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, provider_name: str) -> BaseMarketDataProvider | None:
        return self._providers.get(provider_name)

    def list_names(self) -> list[str]:
        return list(self._providers.keys())


def build_default_registry() -> DataProviderRegistry:
    registry = DataProviderRegistry()
    registry.register(CnSinaMoneyflowProvider())
    registry.register(CnEastmoneyHttpProvider())
    # Optional (heavier) providers
    try:
        from .cn_akshare_provider import CnAkshareProvider

        registry.register(CnAkshareProvider())
    except Exception:
        pass

    try:
        from .cn_baostock_provider import CnBaoStockProvider

        registry.register(CnBaoStockProvider())
    except Exception:
        pass
    # Optional providers: import may fail in minimal runtimes.
    try:
        from .yfinance_provider import YFinanceProvider

        registry.register(YFinanceProvider())
    except Exception:
        pass

    try:
        from .alpha_vantage_provider import AlphaVantageProvider

        registry.register(AlphaVantageProvider())
    except Exception:
        pass
    registry.register(CnStubProvider())
    return registry

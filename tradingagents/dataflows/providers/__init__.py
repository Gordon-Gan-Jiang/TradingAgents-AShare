from __future__ import annotations

from typing import TYPE_CHECKING

from .base import BaseMarketDataProvider

# Avoid importing the whole registry (and its optional dependencies) at import time.
# Some environments import provider modules in isolation, and optional deps (e.g. yfinance extras)
# may not be installed in minimal runtimes. We keep these re-exports lazy.
if TYPE_CHECKING:  # pragma: no cover
    from .registry import DataProviderRegistry  # noqa: F401

__all__ = [
    "BaseMarketDataProvider",
    "DataProviderRegistry",
    "build_default_registry",
]


def build_default_registry():
    from .registry import build_default_registry as _build_default_registry

    return _build_default_registry()


def __getattr__(name: str):
    if name == "DataProviderRegistry":
        from .registry import DataProviderRegistry as _DataProviderRegistry

        return _DataProviderRegistry
    raise AttributeError(name)


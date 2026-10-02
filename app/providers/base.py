"""Provider interface and registry. Adding a provider = one module + one line in main.build_registry."""
from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable
from datetime import datetime
from typing import Protocol

from app.domain import Chunk, PermanentError, SymbolInfo
from app.providers.http import ProviderClient


class Provider(Protocol):
    name: str  # registry key, stored in assets.provider
    label: str  # display name
    asset_classes: tuple[str, ...]
    client: ProviderClient

    async def search_symbols(self, query: str) -> list[SymbolInfo]: ...

    async def earliest_available(self, symbol: str) -> datetime: ...

    def available_until(self, now: datetime) -> datetime:
        """Latest minute whose candles are final at `now` (exclusive end for fetching)."""
        ...

    def estimate_requests(self, start: datetime, end: datetime) -> int: ...

    def fetch(self, symbol: str, start: datetime, end: datetime) -> AsyncGenerator[Chunk, None]:
        """Yield ascending chunks covering [start, end); one chunk per request or file."""
        ...


class ProviderRegistry:
    def __init__(self, providers: Iterable[Provider]):
        self._providers = {p.name: p for p in providers}

    def get(self, name: str) -> Provider:
        provider = self._providers.get(name)
        if provider is None:
            raise PermanentError(f"Unknown provider {name!r}")
        return provider

    def find(self, name: str) -> Provider | None:
        return self._providers.get(name)

    def all(self) -> list[Provider]:
        return list(self._providers.values())


def split_label(label: str) -> tuple[str, str]:
    """'Binance (crypto)' -> ('Binance', 'crypto'); a label without parentheses has no coverage part."""
    name, _, coverage = label.partition(" (")
    return name, coverage.rstrip(")")


def _normalise(value: str) -> str:
    return value.upper().replace("-", "").replace("/", "").replace("_", "").strip()


def rank_matches(symbols: Iterable[SymbolInfo], query: str, limit: int = 20) -> list[SymbolInfo]:
    """Case-insensitive substring search: exact match first, then prefix matches, then the rest."""
    q = _normalise(query)
    if not q:
        return []
    matches = [s for s in symbols if q in _normalise(s.provider_symbol) or q in _normalise(s.name)]

    def key(s: SymbolInfo):
        sym = _normalise(s.provider_symbol)
        return (sym != q, not sym.startswith(q), sym)

    return sorted(matches, key=key)[:limit]

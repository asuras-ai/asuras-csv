"""Binance spot klines (crypto). Public API, no key."""
from __future__ import annotations

import math
from collections.abc import AsyncIterator
from datetime import datetime

import httpx

from app.domain import MINUTE, Candle, Chunk, PermanentError, SymbolInfo, floor_minute, from_ms, to_ms
from app.providers.base import rank_matches
from app.providers.http import ProviderClient, RateLimitPolicy

BASE_URL = "https://api.binance.com"
PAGE = 1000
WEIGHT_SOFT_LIMIT = 5400  # stop at 90% of the 6000 weight/min limit


def binance_quota_delay(response: httpx.Response, now: datetime) -> float:
    used = response.headers.get("X-MBX-USED-WEIGHT-1M")
    if used is None or int(used) < WEIGHT_SOFT_LIMIT:
        return 0.0
    return 60.0 - (now.second + now.microsecond / 1_000_000)


# A 1000-candle kline request costs weight 2, so 25 req/s = 3000 weight/min (50% of the limit).
POLICY = RateLimitPolicy(
    name="Binance",
    rate=25.0,
    burst=10,
    concurrency=2,
    throttle_statuses=frozenset({418, 429}),
    permanent_messages={
        400: "request rejected, check the symbol",
        451: "Binance is not available from this server's region",
    },
    quota_delay=binance_quota_delay,
)


class BinanceProvider:
    name = "binance"
    label = "Binance (crypto)"
    asset_classes = ("crypto",)

    def __init__(self, client: ProviderClient, base_url: str = BASE_URL):
        self.client = client
        self._base = base_url
        self._symbols: list[SymbolInfo] | None = None

    async def search_symbols(self, query: str) -> list[SymbolInfo]:
        if self._symbols is None:
            data = (await self.client.get(f"{self._base}/api/v3/exchangeInfo")).json()
            self._symbols = [
                SymbolInfo(
                    provider_symbol=s["symbol"],
                    asset_class="crypto",
                    suggested_jesse_symbol=f"{s['baseAsset']}-{s['quoteAsset']}",
                    name=f"{s['baseAsset']}/{s['quoteAsset']}",
                )
                for s in data["symbols"]
                if s.get("status") == "TRADING"
            ]
        return rank_matches(self._symbols, query)

    async def earliest_available(self, symbol: str) -> datetime:
        rows = await self._klines(symbol, start_ms=0, end_ms=None, limit=1)
        if not rows:
            raise PermanentError(f"Binance: no data for {symbol}")
        return from_ms(rows[0][0])

    def available_until(self, now: datetime) -> datetime:
        return floor_minute(now)

    def estimate_requests(self, start: datetime, end: datetime) -> int:
        if end <= start:
            return 0
        return max(1, math.ceil((end - start) / MINUTE / PAGE))

    async def fetch(self, symbol: str, start: datetime, end: datetime) -> AsyncIterator[Chunk]:
        cursor = start
        while cursor < end:
            rows = await self._klines(symbol, start_ms=to_ms(cursor), end_ms=to_ms(end) - 1, limit=PAGE)
            candles = [
                Candle(from_ms(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                for r in rows
            ]
            covered = candles[-1].ts + MINUTE if len(rows) == PAGE else end
            yield Chunk(candles, covered)
            cursor = covered

    async def _klines(self, symbol: str, *, start_ms: int, end_ms: int | None, limit: int) -> list:
        params = {"symbol": symbol, "interval": "1m", "startTime": start_ms, "limit": limit}
        if end_ms is not None:
            params["endTime"] = end_ms
        return (await self.client.get(f"{self._base}/api/v3/klines", params=params)).json()

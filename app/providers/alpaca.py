"""Alpaca market data (US stocks and ETFs), free IEX feed, regular session only."""
from __future__ import annotations

import math
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.domain import MINUTE, Candle, Chunk, PermanentError, SymbolInfo, floor_minute, utc
from app.providers.base import rank_matches
from app.providers.http import ProviderClient, RateLimitPolicy

DATA_URL = "https://data.alpaca.markets"
NEW_YORK = ZoneInfo("America/New_York")
PAGE = 10_000
FEED = "iex"  # free plan; switch to "sip" with a paid plan
IEX_START = datetime(2016, 1, 1, tzinfo=UTC)
KEY_REJECTED = "API key rejected — check Settings"

Credentials = Callable[[], Awaitable[tuple[str, str]]]


def alpaca_quota_delay(response: httpx.Response, now: datetime) -> float:
    remaining = response.headers.get("X-RateLimit-Remaining")
    reset = response.headers.get("X-RateLimit-Reset")
    if remaining is None or reset is None or int(remaining) > 5:
        return 0.0
    return max(0.0, int(reset) - now.timestamp())


PUBLISH_LAG = 2 * MINUTE  # keep the cursor behind data that may not be published yet

POLICY = RateLimitPolicy(
    name="Alpaca",
    rate=3.0,  # 180 requests/min, 90% of the free plan's 200/min
    burst=5,
    concurrency=1,
    permanent_messages={
        400: "request rejected",
        401: KEY_REJECTED,
        403: KEY_REJECTED,
        404: "not found — check the symbol and ALPACA_TRADING_URL",
        422: "request rejected, check the symbol",
    },
    quota_delay=alpaca_quota_delay,
)

Sessions = dict[date, tuple[datetime, datetime]]


def _rfc3339(dt: datetime) -> str:
    return utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _in_session(ts: datetime, sessions: Sessions) -> bool:
    session = sessions.get(ts.astimezone(NEW_YORK).date())
    return session is not None and session[0] <= ts < session[1]


def _base_url(url: str) -> str:
    """Accept base URLs with or without the /v2 suffix Alpaca's dashboard shows."""
    url = url.strip().rstrip("/")
    return url.removesuffix("/v2")


class AlpacaProvider:
    name = "alpaca"
    label = "Alpaca (US stocks & ETFs)"
    asset_classes = ("stock", "etf")

    def __init__(self, client: ProviderClient, credentials: Credentials, trading_url: str, data_url: str = DATA_URL):
        self.client = client
        self._credentials = credentials
        self._trading = _base_url(trading_url)
        self._data = _base_url(data_url)
        self._symbols: list[SymbolInfo] | None = None

    async def _headers(self) -> dict[str, str]:
        key, secret = await self._credentials()
        if not key or not secret:
            raise PermanentError("Alpaca: API key missing — set it in Settings")
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    async def search_symbols(self, query: str) -> list[SymbolInfo]:
        if self._symbols is None:
            response = await self.client.get(
                f"{self._trading}/v2/assets",
                params={"status": "active", "asset_class": "us_equity"},
                headers=await self._headers(),
            )
            self._symbols = [
                SymbolInfo(
                    provider_symbol=a["symbol"],
                    asset_class="etf" if "ETF" in (a.get("name") or "").upper() else "stock",
                    suggested_jesse_symbol=f"{a['symbol']}-USD",
                    name=a.get("name") or a["symbol"],
                )
                for a in response.json()
                if a.get("tradable")
            ]
        return rank_matches(self._symbols, query)

    async def earliest_available(self, symbol: str) -> datetime:
        return IEX_START

    def available_until(self, now: datetime) -> datetime:
        return floor_minute(now) - PUBLISH_LAG

    def estimate_requests(self, start: datetime, end: datetime) -> int:
        if end <= start:
            return 0
        trading_minutes = (end - start) / timedelta(days=1) * 5 / 7 * 390
        return 1 + max(1, math.ceil(trading_minutes / PAGE))

    async def fetch(self, symbol: str, start: datetime, end: datetime) -> AsyncIterator[Chunk]:
        headers = await self._headers()
        # Widen by a day on each side so every bar's New York date is covered.
        sessions = await self._sessions(start - timedelta(days=1), end + timedelta(days=1), headers)
        cursor = start
        while cursor < end:
            response = await self.client.get(
                f"{self._data}/v2/stocks/{symbol}/bars",
                params={
                    "timeframe": "1Min",
                    "start": _rfc3339(cursor),
                    "end": _rfc3339(end - timedelta(seconds=1)),  # Alpaca's end is inclusive
                    "limit": PAGE,
                    "feed": FEED,
                    "adjustment": "raw",
                    "sort": "asc",
                },
                headers=headers,
            )
            data = response.json()
            bars = data.get("bars") or []
            candles = []
            for b in bars:
                ts = datetime.fromisoformat(b["t"])
                if _in_session(ts, sessions):
                    candles.append(Candle(ts, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b["v"])))
            if data.get("next_page_token") and bars:
                covered = datetime.fromisoformat(bars[-1]["t"]) + MINUTE
            else:
                covered = end
            yield Chunk(candles, covered)
            cursor = covered

    async def _sessions(self, start: datetime, end: datetime, headers: dict[str, str]) -> Sessions:
        response = await self.client.get(
            f"{self._trading}/v2/calendar",
            params={"start": start.date().isoformat(), "end": end.date().isoformat()},
            headers=headers,
        )
        sessions: Sessions = {}
        for day in response.json():
            d = date.fromisoformat(day["date"])
            opens = datetime.combine(d, time.fromisoformat(day["open"]), NEW_YORK).astimezone(UTC)
            closes = datetime.combine(d, time.fromisoformat(day["close"]), NEW_YORK).astimezone(UTC)
            sessions[d] = (opens, closes)
        return sessions

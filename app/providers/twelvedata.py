"""Twelve Data REST time series (US stocks, forex, metals), 1-minute candles. Needs a free API key."""
from __future__ import annotations

import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx

from app.domain import (
    MINUTE,
    Candle,
    Chunk,
    PermanentError,
    SymbolInfo,
    TransientError,
    floor_minute,
)
from app.providers.base import rank_matches
from app.providers.http import ProviderClient, RateLimitPolicy, Throttle

BASE = "https://api.twelvedata.com"
PAGE = 4999  # the API returns at most 5000 rows; one fewer keeps an inclusive end_date under the cap
KEY_REJECTED = "API key rejected — check Settings"
KEY_MISSING = "Twelve Data: API key missing — set it in Settings"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
REQUEST_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
SEARCH_SIZE = 30

COMMODITY_TYPES = frozenset({"Precious Metal", "Industrial Metal", "Energy", "Agricultural Product"})
STOCK_TYPES = {"Common Stock": "stock", "ETF": "etf"}

Credentials = Callable[[], Awaitable[str]]

PUBLISH_LAG = 15 * MINUTE  # free-plan bars can publish late; keep the cursor well behind the live edge
DAILY_PAUSE_CAP = 3600.0  # the daily credit reset time is unverified, so re-probe at least hourly
SEARCH_TTL = timedelta(minutes=5)
EARLIEST_TTL = timedelta(hours=1)
CACHE_LIMIT = 200


def _body(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return None


def _is_throttled(response: httpx.Response) -> bool:
    """Twelve Data reports rate limits as code 429 inside the JSON body, sometimes with another HTTP status."""
    data = _body(response)
    return isinstance(data, dict) and data.get("status") == "error" and data.get("code") == 429


def _throttle_delay(response: httpx.Response, now: datetime) -> Throttle | float | None:
    """Twelve Data words its 429s differently for the per-minute and the daily credit limit."""
    data = _body(response)
    message = str(data.get("message", "")).lower() if isinstance(data, dict) else ""
    if "for the day" in message:
        tomorrow = floor_minute(now).replace(hour=0, minute=0) + timedelta(days=1)
        seconds = min((tomorrow - now).total_seconds() + 60.0, DAILY_PAUSE_CAP)
        return Throttle(seconds, "Twelve Data daily credit limit reached")
    if "minute" in message:
        return (floor_minute(now) + MINUTE - now).total_seconds() + 5.0
    return None


# Free plan: 8 credits/minute and 800/day; /time_series costs 1 credit per request.
POLICY = RateLimitPolicy(
    name="Twelve Data",
    rate=7 / 60,
    burst=1,
    concurrency=1,
    ok_statuses=frozenset({200, 400}),  # 400 carries the "no data for this range" answer; handled in _json
    throttle_statuses=frozenset({429}),
    permanent_messages={
        400: "request rejected",
        401: KEY_REJECTED,
        403: KEY_REJECTED,
        404: "not found — check the symbol",
    },
    is_throttled=_is_throttled,
    throttle_delay=_throttle_delay,
)

# Searching must not eat the download budget: it has its own light client and never sends the key.
SEARCH_POLICY = replace(POLICY, rate=1.0, burst=3, concurrency=2)


class NoData(Exception):
    """Twelve Data reports an empty range as an error; callers treat it as zero candles."""


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=UTC)  # always requested with timezone=UTC


def _jesse_symbol(symbol: str, asset_class: str) -> str:
    base = "".join(ch for ch in symbol.upper().replace("/", "-") if ch.isascii() and (ch.isalnum() or ch == "-"))
    return base if "-" in base and asset_class in ("forex", "commodity") else f"{base}-USD"


def _remember(cache: dict, key: str, expires: datetime, value, now: datetime) -> None:
    for stale in [k for k, (until, _) in cache.items() if until <= now]:
        del cache[stale]
    if len(cache) >= CACHE_LIMIT:
        cache.pop(next(iter(cache)))
    cache[key] = (expires, value)


class TwelveDataProvider:
    name = "twelvedata"
    label = "Twelve Data (US stocks, forex, metals)"
    asset_classes = ("stock", "etf", "forex", "commodity")

    def __init__(self, client: ProviderClient, credentials: Credentials, search_client: ProviderClient | None = None):
        self.client = client
        self.search_client = search_client or client
        self._credentials = credentials
        self._search_cache: dict[str, tuple[datetime, list[SymbolInfo]]] = {}
        self._earliest_cache: dict[str, tuple[datetime, datetime]] = {}

    async def _key(self) -> str:
        """The API key; raises before any request is sent when it is missing."""
        key = (await self._credentials()).strip()
        if not key:
            raise PermanentError(KEY_MISSING)
        return key

    async def _json(self, path: str, params: dict[str, str], client: ProviderClient | None = None) -> dict:
        """GET and decode, turning error bodies (also inside HTTP 200) into domain errors. Raises NoData for empty ranges."""
        key = params.get("apikey", "")
        try:
            response = await (client or self.client).get(f"{BASE}{path}", params=params)
            data = _body(response)
            if response.status_code != 200 or (isinstance(data, dict) and data.get("status") == "error"):
                self._raise_for_error(response, data)
        except (PermanentError, TransientError) as exc:
            if key and key in str(exc):
                raise type(exc)(str(exc).replace(key, "***")) from None
            raise
        if not isinstance(data, dict):
            raise TransientError("Twelve Data: unexpected response")
        return data

    def _raise_for_error(self, response: httpx.Response, data) -> None:
        """Throttling never gets here: the client classifies it first (see _is_throttled)."""
        body = data if isinstance(data, dict) else {}
        try:
            code = int(body.get("code", response.status_code))
        except (TypeError, ValueError):
            code = response.status_code
        message = str(body.get("message", response.text))[:200]
        if code == 400 and "no data is available" in message.lower():
            raise NoData
        if code in POLICY.permanent_messages:
            raise PermanentError(f"Twelve Data: {POLICY.permanent_messages[code]} (HTTP {code}: {message})")
        if code >= 500:
            raise TransientError(f"Twelve Data: HTTP {code}: {message}")
        raise PermanentError(f"Twelve Data: unexpected error {code}: {message}")

    async def search_symbols(self, query: str) -> list[SymbolInfo]:
        query = query.strip()
        if not query:
            return []
        now = self.search_client.clock.now()
        cached = self._search_cache.get(query.lower())
        if cached and cached[0] > now:
            return cached[1]
        try:
            data = await self._json(
                "/symbol_search", {"symbol": query, "outputsize": str(SEARCH_SIZE)}, self.search_client
            )
        except NoData:
            return []
        rows = data.get("data")
        symbols: dict[str, SymbolInfo] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("symbol"):
                continue
            kind = row.get("instrument_type")
            if kind in COMMODITY_TYPES:
                asset_class = "commodity"
            elif kind == "Physical Currency":
                asset_class = "forex"
            elif kind in STOCK_TYPES and row.get("country") == "United States":
                asset_class = STOCK_TYPES[kind]
            else:
                continue
            symbol = str(row["symbol"])
            if symbol in symbols:
                continue
            name = str(row.get("instrument_name") or symbol)
            if row.get("exchange"):
                name = f"{name} · {row['exchange']}"
            symbols[symbol] = SymbolInfo(symbol, asset_class, _jesse_symbol(symbol, asset_class), name)
        ranked = rank_matches(symbols.values(), query)
        _remember(self._search_cache, query.lower(), now + SEARCH_TTL, ranked, now)
        return ranked

    async def earliest_available(self, symbol: str) -> datetime:
        key = await self._key()
        now = self.client.clock.now()
        cached = self._earliest_cache.get(symbol)
        if cached and cached[0] > now:
            return cached[1]
        try:
            data = await self._json(
                "/earliest_timestamp", {"symbol": symbol, "interval": "1min", "timezone": "UTC", "apikey": key}
            )
            earliest = datetime.fromtimestamp(int(data["unix_time"]), UTC)
        except (KeyError, TypeError, ValueError):
            raise PermanentError(f"Twelve Data: no earliest date for {symbol}") from None
        except NoData:
            raise PermanentError(f"Twelve Data: no data for {symbol}") from None
        _remember(self._earliest_cache, symbol, now + EARLIEST_TTL, earliest, now)
        return earliest

    def available_until(self, now: datetime) -> datetime:
        return floor_minute(now) - PUBLISH_LAG

    def estimate_requests(self, start: datetime, end: datetime) -> int:
        if end <= start:
            return 0
        return math.ceil((end - start) / MINUTE / PAGE)

    async def fetch(self, symbol: str, start: datetime, end: datetime) -> AsyncIterator[Chunk]:
        key = await self._key()
        cursor = start
        while cursor < end:
            window_end = min(cursor + PAGE * MINUTE, end)
            params = {
                "symbol": symbol,
                "interval": "1min",
                "start_date": cursor.astimezone(UTC).strftime(REQUEST_TIME_FORMAT),
                # Verified live (2026-09-30): end_date is INCLUSIVE and, like start_date, is read in `timezone`.
                "end_date": (window_end - MINUTE).astimezone(UTC).strftime(REQUEST_TIME_FORMAT),
                "timezone": "UTC",
                "order": "asc",
                "outputsize": "5000",
                "apikey": key,
            }
            try:
                data = await self._json("/time_series", params)
            except NoData:  # weekends and closed markets: nothing to fetch, but the cursor moves on
                data = {"values": []}
            values = data.get("values")
            if not isinstance(values, list):
                raise TransientError(f"Twelve Data: unexpected response for {symbol}")  # retry; the cursor must not move
            try:
                rows = [
                    (_parse_time(v["datetime"]), float(v["open"]), float(v["high"]), float(v["low"]), float(v["close"]), float(v.get("volume") or 0.0))
                    for v in values
                ]
            except (KeyError, TypeError, ValueError):
                raise TransientError(f"Twelve Data: malformed candle for {symbol}") from None
            if any(not cursor <= row[0] < window_end for row in rows):
                # Never clip silently: it would hide a timezone or range misunderstanding and leave a permanent gap.
                raise TransientError("Twelve Data: returned candles outside the requested window")
            found = {row[0]: Candle(*row) for row in rows}
            yield Chunk([found[ts] for ts in sorted(found)], window_end)
            cursor = window_end

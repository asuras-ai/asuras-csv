"""OANDA v20 REST candles (forex, metals and CFDs), 1-minute bid candles. Needs a free practice-account token."""
from __future__ import annotations

import math
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime

from app.domain import MINUTE, Candle, Chunk, PermanentError, SymbolInfo, floor_minute, utc
from app.providers.base import rank_matches
from app.providers.http import ProviderClient, RateLimitPolicy

HOSTS = {"practice": "https://api-fxpractice.oanda.com", "live": "https://api-fxtrade.oanda.com"}
PAGE = 4999  # OANDA allows 5000 candles per request; one fewer so an inclusive `to` can never exceed it
EARLIEST_PROBE = datetime(2000, 1, 1, tzinfo=UTC)
TOKEN_REJECTED = "API token rejected — check Settings"
ASSET_CLASSES = {"CURRENCY": "forex", "METAL": "metal", "CFD": "cfd"}

Credentials = Callable[[], Awaitable[tuple[str, str]]]

PUBLISH_LAG = 2 * MINUTE  # keep the cursor behind candles that may still be forming

# OANDA allows 120 requests/s per IP; 20/s leaves plenty of room. It sends no quota headers.
POLICY = RateLimitPolicy(
    name="OANDA",
    rate=20.0,
    burst=10,
    concurrency=2,
    throttle_statuses=frozenset({429}),
    permanent_messages={
        400: "request rejected",
        401: TOKEN_REJECTED,
        403: TOKEN_REJECTED,
        404: "not found — check the instrument",
    },
)


def _unix(dt: datetime) -> str:
    return str(int(utc(dt).timestamp()))


def _parse_time(value: str) -> datetime:
    return datetime.fromtimestamp(int(value.split(".")[0]), UTC)


class OandaProvider:
    name = "oanda"
    label = "OANDA (forex & CFDs)"
    asset_classes = ("forex", "metal", "cfd")

    def __init__(self, client: ProviderClient, credentials: Credentials):
        self.client = client
        self._credentials = credentials
        self._symbols: list[SymbolInfo] | None = None
        self._symbols_key: tuple[str, str] | None = None  # (token, base URL) the cache was loaded with

    async def _auth(self) -> tuple[str, str]:
        """Validated (token, base URL); raises before any request is sent."""
        token, environment = await self._credentials()
        token = token.strip()
        if not token:
            raise PermanentError("OANDA: API token missing — set it in Settings")
        base = HOSTS.get(environment.strip().lower())
        if base is None:
            raise PermanentError("OANDA: OANDA_ENVIRONMENT must be 'practice' or 'live'")
        return token, base

    async def _request(self, auth: tuple[str, str], path: str, params: dict[str, str] | None = None) -> dict:
        token, base = auth
        headers = {"Authorization": f"Bearer {token}", "Accept-Datetime-Format": "UNIX"}
        return (await self.client.get(f"{base}{path}", params=params, headers=headers)).json()

    async def search_symbols(self, query: str) -> list[SymbolInfo]:
        auth = await self._auth()
        if self._symbols is None or self._symbols_key != auth:
            accounts = (await self._request(auth, "/v3/accounts") or {}).get("accounts")
            account_id = accounts[0].get("id") if isinstance(accounts, list) and accounts else None
            if not account_id:
                raise PermanentError("OANDA: no accounts found for this token")
            data = await self._request(auth, f"/v3/accounts/{account_id}/instruments")
            instruments = data.get("instruments") if isinstance(data, dict) else None
            symbols = [
                SymbolInfo(
                    provider_symbol=i["name"],
                    asset_class=ASSET_CLASSES[i["type"]],
                    suggested_jesse_symbol=i["name"].replace("_", "-").upper(),
                    name=i.get("displayName") or i["name"],
                )
                for i in instruments or []
                if i.get("name") and i.get("type") in ASSET_CLASSES
            ]
            if not symbols:
                raise PermanentError("OANDA: no instruments found for this account")
            self._symbols, self._symbols_key = symbols, auth
        return rank_matches(self._symbols, query)

    async def earliest_available(self, symbol: str) -> datetime:
        data = await self._candles(symbol, {"from": _unix(EARLIEST_PROBE), "count": "1"})
        if not data["candles"]:
            raise PermanentError(f"OANDA: no data for {symbol}")
        return _parse_time(data["candles"][0]["time"])

    def available_until(self, now: datetime) -> datetime:
        return floor_minute(now) - PUBLISH_LAG

    def estimate_requests(self, start: datetime, end: datetime) -> int:
        if end <= start:
            return 0
        return math.ceil((end - start) / MINUTE / PAGE)

    async def fetch(self, symbol: str, start: datetime, end: datetime) -> AsyncIterator[Chunk]:
        cursor = start
        while cursor < end:
            window_end = min(cursor + PAGE * MINUTE, end)
            data = await self._candles(symbol, {"from": _unix(cursor), "to": _unix(window_end)})
            candles: list[Candle] = []
            covered = window_end
            incomplete = False
            for c in data["candles"]:
                ts = _parse_time(c["time"])
                if not cursor <= ts < window_end:
                    continue
                if not c.get("complete", True):
                    covered, incomplete = ts, True  # the rest is still forming; resume from here next time
                    break
                bid = c["bid"]
                candles.append(
                    Candle(ts, float(bid["o"]), float(bid["h"]), float(bid["l"]), float(bid["c"]), float(c["volume"]))
                )
            yield Chunk(candles, covered)
            if incomplete:
                return  # nothing newer is final yet; the next run continues from covered_until
            cursor = covered

    async def _candles(self, symbol: str, params: dict[str, str]) -> dict:
        data = await self._request(
            await self._auth(), f"/v3/instruments/{symbol}/candles", {"granularity": "M1", "price": "B"} | params
        )
        if not isinstance(data, dict) or not isinstance(data.get("candles"), list):
            raise PermanentError(f"OANDA: unexpected response for {symbol}")
        return data

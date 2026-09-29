"""Dukascopy historical tick files (forex), aggregated to 1-minute bid candles."""
from __future__ import annotations

import asyncio
import lzma
import math
import struct
from collections import deque
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, time, timedelta

from app.domain import HOUR, Candle, Chunk, PermanentError, SymbolInfo, TransientError, floor_hour
from app.providers.base import rank_matches
from app.providers.http import ProviderClient, RateLimitPolicy

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
RECORD = struct.Struct(">3I2f")  # ms offset in hour, ask, bid, ask volume, bid volume
WINDOW = 8  # hours scheduled ahead; the client's concurrency (4) limits actual parallel downloads
SATURDAY = 5

# Conservative default start dates; the Add form lets the user choose a later one.
# Hours before a pair's real start simply come back empty.
PAIRS: dict[str, date] = {
    pair: date(2004, 1, 1)
    for pair in [
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
        "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "GBPJPY", "GBPCHF",
        "GBPAUD", "AUDJPY", "AUDNZD", "CADJPY", "CHFJPY", "NZDJPY",
    ]
}

POLICY = RateLimitPolicy(
    name="Dukascopy",
    rate=8.0,
    burst=8,
    concurrency=4,
    ok_statuses=frozenset({200, 404}),  # 404 = no file for that hour (market closed)
    throttle_statuses=frozenset({429, 503}),
)


def point_divisor(pair: str) -> float:
    return 1000.0 if pair.endswith("JPY") else 100000.0


def decode_hour(raw: bytes, hour_start: datetime, divisor: float) -> list[Candle]:
    if not raw:
        return []
    try:
        records = RECORD.iter_unpack(lzma.decompress(raw))
        buckets: dict[int, list[float]] = {}  # minute -> [open, high, low, close, ticks]
        for ms, _ask, bid, _ask_volume, _bid_volume in records:
            price = bid / divisor
            bucket = buckets.get(ms // 60_000)
            if bucket is None:
                buckets[ms // 60_000] = [price, price, price, price, 1]
            else:
                bucket[1] = max(bucket[1], price)
                bucket[2] = min(bucket[2], price)
                bucket[3] = price
                bucket[4] += 1
    except (lzma.LZMAError, struct.error) as exc:
        raise TransientError(f"Dukascopy: corrupt file for {hour_start:%Y-%m-%d %H}:00 ({exc})") from exc
    return [
        Candle(hour_start + timedelta(minutes=m), o, h, low, c, float(n))
        for m, (o, h, low, c, n) in sorted(buckets.items())
    ]


def _hours(first: datetime, end: datetime) -> Iterator[datetime]:
    hour = first
    while hour < end:
        yield hour
        hour += HOUR


class DukascopyProvider:
    name = "dukascopy"
    label = "Dukascopy (forex)"
    asset_classes = ("forex",)

    def __init__(self, client: ProviderClient, base_url: str = BASE_URL):
        self.client = client
        self._base = base_url.rstrip("/")
        self._symbols = [SymbolInfo(p, "forex", f"{p[:3]}-{p[3:]}", f"{p[:3]}/{p[3:]}") for p in PAIRS]

    async def search_symbols(self, query: str) -> list[SymbolInfo]:
        return rank_matches(self._symbols, query)

    async def earliest_available(self, symbol: str) -> datetime:
        if symbol not in PAIRS:
            raise PermanentError(f"Dukascopy: unknown pair {symbol}")
        return datetime.combine(PAIRS[symbol], time(), UTC)

    def available_until(self, now: datetime) -> datetime:
        return floor_hour(now) - HOUR

    def estimate_requests(self, start: datetime, end: datetime) -> int:
        if end <= start:
            return 0
        hours = math.ceil((end - floor_hour(start)) / HOUR)
        return round(hours * 6 / 7)

    async def fetch(self, symbol: str, start: datetime, end: datetime) -> AsyncIterator[Chunk]:
        divisor = point_divisor(symbol)
        hours = _hours(floor_hour(start), end)
        pending: deque[tuple[datetime, asyncio.Task[list[Candle]] | None]] = deque()

        def schedule() -> None:
            while len(pending) < WINDOW:
                hour = next(hours, None)
                if hour is None:
                    return
                task = None if hour.weekday() == SATURDAY else asyncio.create_task(self._hour(symbol, hour, divisor))
                pending.append((hour, task))

        try:
            schedule()
            while pending:
                hour, task = pending.popleft()
                candles = await task if task is not None else []
                schedule()
                yield Chunk([c for c in candles if start <= c.ts < end], min(hour + HOUR, end))
        finally:
            leftovers = [t for _, t in pending if t is not None]
            for t in leftovers:
                t.cancel()
            await asyncio.gather(*leftovers, return_exceptions=True)

    async def _hour(self, symbol: str, hour: datetime, divisor: float) -> list[Candle]:
        url = f"{self._base}/{symbol}/{hour.year}/{hour.month - 1:02d}/{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"
        response = await self.client.get(url)
        if response.status_code == 404:
            return []
        return decode_hour(response.content, hour, divisor)

"""Test doubles shared across the test suite."""
from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.clock import Clock
from app.db import SessionFactory
from app.domain import MINUTE, Candle, Chunk, SymbolInfo, TransientError, floor_minute
from app.models import Asset


class FakeClock(Clock):
    """Deterministic clock: sleep() advances time instantly and records the duration."""

    def __init__(self, start: datetime = datetime(2024, 1, 1, 2, 0, tzinfo=UTC)):
        self._now = start
        self._mono = 0.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds > 0:
            self.advance(seconds)
        await asyncio.sleep(0)


async def make_asset(sf: SessionFactory, **overrides) -> Asset:
    values = {
        "provider": "fake",
        "provider_symbol": "FAKEUSD",
        "asset_class": "crypto",
        "jesse_symbol": "FAKE-USD",
        "start_date": datetime(2024, 1, 1, tzinfo=UTC),
    } | overrides
    asset = Asset(**values)
    async with sf.begin() as s:
        s.add(asset)
    return asset


class FakeProvider:
    """In-memory provider: one chunk per 10 minutes, one candle per minute."""

    name = "fake"
    label = "Fake"
    asset_classes = ("crypto",)
    CHUNK = timedelta(minutes=10)

    def __init__(self, clock=None, *, seconds_per_chunk=0.0, fail_after=None, error=None, listed=None):
        self.client = SimpleNamespace(policy=SimpleNamespace(rate=10.0))
        self.clock = clock
        self.seconds_per_chunk = seconds_per_chunk
        self.fail_after = fail_after  # raise `error` after this many chunks
        self.error = error or TransientError("Fake: boom")
        self.listed = listed  # no candles before this time
        self.calls: list[tuple[datetime, datetime]] = []

    async def search_symbols(self, query):
        return [SymbolInfo("FAKEUSD", "crypto", "FAKE-USD", "Fake/USD")] if query.strip() else []

    async def earliest_available(self, symbol):
        return datetime(2024, 1, 1, tzinfo=UTC)

    def available_until(self, now):
        return floor_minute(now)

    def estimate_requests(self, start, end):
        return max(0, math.ceil((end - start) / self.CHUNK))

    async def fetch(self, symbol, start, end):
        self.calls.append((start, end))
        cursor, sent = start, 0
        while cursor < end:
            if self.fail_after is not None and sent >= self.fail_after:
                raise self.error
            nxt = min(cursor + self.CHUNK, end)
            candles, t = [], cursor
            while t < nxt:
                if self.listed is None or t >= self.listed:
                    candles.append(Candle(t, 1.0, 2.0, 0.5, 1.5, 10.0))
                t += MINUTE
            if self.clock is not None and self.seconds_per_chunk:
                self.clock.advance(self.seconds_per_chunk)
            yield Chunk(candles, nxt)
            cursor, sent = nxt, sent + 1

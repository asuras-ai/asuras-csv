"""Test doubles shared across the test suite."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.clock import Clock
from app.db import SessionFactory
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

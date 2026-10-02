"""Daily closing prices for the Overview sparklines: one query for all assets, cached like the candle stats."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Float, func, select

from app.clock import Clock
from app.db import SessionFactory
from app.models import CandleRow
from app.services.assets import StatsCache
from app.services.export import valid_candle

DAY = timedelta(days=1)


async def daily_closes(sf: SessionFactory, now: datetime, days: int = 30) -> dict[int, list[float]]:
    """{asset_id: [last close of each UTC day, oldest first]} for candles in the last `days` days."""
    day = func.time_bucket(DAY, CandleRow.ts).label("day")
    stmt = (
        select(CandleRow.asset_id, day, func.last(CandleRow.close, CandleRow.ts, type_=Float))
        .where(CandleRow.ts >= now - timedelta(days=days), valid_candle())
        .group_by(CandleRow.asset_id, day)
        .order_by(CandleRow.asset_id, day)
    )
    closes: dict[int, list[float]] = {}
    async with sf() as s:
        for asset_id, _day, close in await s.execute(stmt):
            closes.setdefault(asset_id, []).append(close)
    return closes


class SparklineCache(StatsCache):
    """StatsCache's single-flight, serve-stale caching, holding daily_closes() instead of candle stats."""

    def __init__(self, clock: Clock, ttl_seconds: float = 600.0, days: int = 30):
        super().__init__(clock, ttl_seconds)
        self._days = days

    async def _load_uncached(self, sf: SessionFactory) -> dict[int, list[float]]:  # type: ignore[override]
        return await daily_closes(sf, self._clock.now(), self._days)

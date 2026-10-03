from datetime import UTC, datetime, timedelta

from app.domain import Candle
from app.services.sparklines import SparklineCache, daily_closes
from app.services.sync import insert_candles
from tests.fakes import make_asset

NOW = datetime(2024, 1, 31, 12, 0, tzinfo=UTC)


def candle(ts: datetime, close: float) -> Candle:
    return Candle(ts, 1.0, 10.0, 0.5, close, 1.0)


async def test_daily_closes_take_the_last_close_of_each_day_in_the_window(sf):
    a = await make_asset(sf)
    await make_asset(sf, provider_symbol="B", jesse_symbol="B-USD")  # no candles: absent from the result
    day = datetime(2024, 1, 20, tzinfo=UTC)
    async with sf.begin() as s:
        await insert_candles(
            s,
            a.id,
            [
                candle(NOW - timedelta(days=40), 1.9),  # outside the 30-day window
                candle(day + timedelta(hours=1), 1.1),
                candle(day + timedelta(hours=5), 1.2),  # last of Jan 20
                candle(day + timedelta(days=1, hours=3), 1.4),
            ],
        )
    assert await daily_closes(sf, NOW) == {a.id: [1.2, 1.4]}


async def test_sparkline_cache_reuses_its_result_within_the_ttl(sf, clock):
    a = await make_asset(sf)
    async with sf.begin() as s:
        await insert_candles(s, a.id, [candle(clock.now() - timedelta(hours=30), 1.0), candle(clock.now() - timedelta(hours=2), 1.5)])
    cache = SparklineCache(clock, ttl_seconds=600)
    assert await cache.get(sf) == {a.id: [1.0, 1.5]}
    async with sf.begin() as s:
        await insert_candles(s, a.id, [candle(clock.now() - timedelta(hours=1), 9.0)])
    assert await cache.get(sf) == {a.id: [1.0, 1.5]}

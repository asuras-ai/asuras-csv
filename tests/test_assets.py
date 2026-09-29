from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain import Candle
from app.services.assets import (
    AssetStats,
    DuplicateAsset,
    StatsCache,
    create_asset,
    delete_asset,
    get_asset,
    list_assets,
    update_asset,
)
from app.services.jobs import get_job
from app.services.sync import insert_candles
from tests.fakes import make_asset

T0 = datetime(2024, 1, 1, tzinfo=UTC)


async def seed(sf, asset_id, minutes):
    async with sf.begin() as s:
        await insert_candles(s, asset_id, [Candle(T0 + timedelta(minutes=m), 1, 2, 0.5, 1.5, 10) for m in minutes])


async def test_create_rejects_duplicates_and_bad_jesse_symbols(sf):
    kwargs = dict(provider="fake", provider_symbol="FAKEUSD", asset_class="crypto", jesse_symbol="FAKE-USD", start_date=date(2024, 1, 1))
    asset = await create_asset(sf, **kwargs)
    assert asset.start_date == T0
    with pytest.raises(DuplicateAsset, match="already exists"):
        await create_asset(sf, **kwargs)
    with pytest.raises(ValueError, match="Jesse symbol"):
        await create_asset(sf, **(kwargs | {"provider_symbol": "OTHER", "jesse_symbol": "fakeusd"}))


async def test_update_and_delete(sf):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [0, 1])
    await update_asset(sf, asset.id, jesse_symbol="FOO-USD", enabled=False)
    got = await get_asset(sf, asset.id)
    assert (got.jesse_symbol, got.enabled) == ("FOO-USD", False)
    with pytest.raises(ValueError):
        await update_asset(sf, asset.id, jesse_symbol="bad", enabled=True)
    await delete_asset(sf, asset.id)
    assert await get_asset(sf, asset.id) is None
    assert await StatsCache(None).get(sf) == {}


async def test_list_assets_is_sorted(sf):
    await make_asset(sf, provider_symbol="B", jesse_symbol="B-USD")
    await make_asset(sf, provider_symbol="A", jesse_symbol="A-USD")
    assert [a.jesse_symbol for a in await list_assets(sf)] == ["A-USD", "B-USD"]


async def test_stats_are_cached_until_ttl_or_invalidation(sf, clock):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [0, 1, 2])
    cache = StatsCache(clock, ttl_seconds=60)
    assert (await cache.get(sf))[asset.id] == AssetStats(T0, T0 + timedelta(minutes=2), 3)
    await seed(sf, asset.id, [3])
    assert (await cache.get(sf))[asset.id].count == 3
    clock.advance(61)
    assert (await cache.get(sf))[asset.id].count == 4
    await seed(sf, asset.id, [4])
    cache.invalidate()
    assert (await cache.get(sf))[asset.id].count == 5


async def test_delete_asset_with_active_job_prevents_deadlock(sf):
    from app.models import Job

    asset = await make_asset(sf)
    # Create an active job for the asset
    async with sf.begin() as s:
        job = Job(
            asset_id=asset.id,
            kind="update",
            status="queued",
            priority=10,
            range_start=T0,
            range_end=T0 + timedelta(hours=1),
        )
        s.add(job)

    # Verify job exists before delete
    job_before = await get_job(sf, job.id)
    assert job_before is not None
    assert job_before.status == "queued"

    # Delete asset (which should cancel the job in a separate transaction first)
    await delete_asset(sf, asset.id)

    # Verify asset and job are gone
    assert await get_asset(sf, asset.id) is None
    assert await get_job(sf, job.id) is None

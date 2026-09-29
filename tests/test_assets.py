import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain import Candle
from app.models import Job
from app.services.assets import (
    AssetStats,
    DuplicateAsset,
    StatsCache,
    _cancel_active_jobs,
    create_asset,
    delete_asset,
    get_asset,
    list_assets,
    update_asset,
)
from sqlalchemy import select

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


async def _add_job(sf, asset_id, status, **extra):
    async with sf.begin() as s:
        job = Job(
            asset_id=asset_id, kind="update", status=status, priority=10,
            range_start=T0, range_end=T0 + timedelta(hours=1), **extra,
        )
        s.add(job)
    return job.id


@pytest.mark.parametrize("status", ["queued", "running", "waiting", "paused"])
async def test_cancel_active_jobs_cancels_every_active_status(sf, status):
    asset = await make_asset(sf)
    extra = {"next_attempt_at": T0, "status_detail": "x"} if status in ("waiting", "paused") else {}
    job_id = await _add_job(sf, asset.id, status, **extra)
    async with sf.begin() as s:
        await _cancel_active_jobs(s, asset.id)
    job = await get_job(sf, job_id)
    assert job.status == "cancelled"
    assert job.finished_at is not None
    assert job.next_attempt_at is None
    assert job.status_detail is None


async def test_cancel_active_jobs_leaves_done_jobs_and_other_assets_alone(sf):
    asset = await make_asset(sf)
    other = await make_asset(sf, provider_symbol="OTHER", jesse_symbol="OTHER-USD")
    done_id = await _add_job(sf, asset.id, "done")
    other_id = await _add_job(sf, other.id, "queued")
    async with sf.begin() as s:
        await _cancel_active_jobs(s, asset.id)
    done = await get_job(sf, done_id)
    assert (done.status, done.finished_at) == ("done", None)
    assert (await get_job(sf, other_id)).status == "queued"


async def test_delete_asset_does_not_deadlock_with_sync_commit_chunk(sf):
    """Mimics sync's _commit_chunk: lock the job row, then insert candles (FK key-share lock on the asset)."""
    asset = await make_asset(sf)
    job_id = await _add_job(sf, asset.id, "running")
    await seed(sf, asset.id, [0])

    async with sf() as sync_session:
        async with sync_session.begin():
            await sync_session.execute(select(Job).where(Job.id == job_id).with_for_update())
            deleter = asyncio.create_task(delete_asset(sf, asset.id))
            await asyncio.sleep(0.5)  # let delete_asset reach its lock wait
            assert not deleter.done()
            # Without the cancel step delete holds the asset lock here and this insert deadlocks.
            await insert_candles(sync_session, asset.id, [Candle(T0 + timedelta(minutes=5), 1, 2, 0.5, 1.5, 10)])
        await asyncio.wait_for(deleter, timeout=15)

    assert await get_asset(sf, asset.id) is None
    assert await get_job(sf, job_id) is None
    assert await StatsCache(None).get(sf) == {}


async def test_stats_invalidated_mid_load_is_not_cached_as_fresh(sf, clock):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [0])
    cache = StatsCache(clock, ttl_seconds=60)

    class InvalidatingSession:
        def __init__(self, session):
            self._session = session

        async def __aenter__(self):
            self._inner = await self._session.__aenter__()
            return self

        async def __aexit__(self, *exc):
            return await self._session.__aexit__(*exc)

        async def execute(self, *args, **kwargs):
            result = await self._inner.execute(*args, **kwargs)
            cache.invalidate()  # invalidation lands while the load is in flight
            return result

    await cache.get(lambda: InvalidatingSession(sf()))
    await seed(sf, asset.id, [1])
    assert (await cache.get(sf))[asset.id].count == 2  # reloaded, stale result was not stored

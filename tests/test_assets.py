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
    assert (await cache.get(sf))[asset.id].count == 3  # expired: stale value now, refresh in the background
    await _idle(cache)
    assert (await cache.get(sf))[asset.id].count == 4
    await seed(sf, asset.id, [4])
    cache.invalidate()
    assert (await cache.get(sf))[asset.id].count == 4  # invalidate() expires: stale value served, refresh runs
    await _idle(cache)
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


class CountingSessionFactory:
    """Wraps a session factory, counting sessions and optionally pausing inside the query."""

    def __init__(self, sf, gate=None):
        self._sf = sf
        self.opened = 0
        self.gate = gate

    def __call__(self):
        self.opened += 1
        return GatedSession(self._sf(), self.gate)


class GatedSession:
    def __init__(self, session, gate):
        self._session = session
        self._gate = gate

    async def __aenter__(self):
        self._inner = await self._session.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._session.__aexit__(*exc)

    async def execute(self, *args, **kwargs):
        if self._gate is not None:
            await self._gate.wait()
        return await self._inner.execute(*args, **kwargs)


async def test_concurrent_first_loads_share_one_query(sf, clock):
    import asyncio

    asset = await make_asset(sf)
    await seed(sf, asset.id, [0, 1])
    gate = asyncio.Event()
    counting = CountingSessionFactory(sf, gate)
    cache = StatsCache(clock, ttl_seconds=30)
    callers = [asyncio.create_task(cache.get(counting)) for _ in range(5)]
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(*callers)
    assert counting.opened == 1
    assert all(r[asset.id].count == 2 for r in results)


async def test_expired_value_is_served_stale_while_one_refresh_runs(sf, clock):
    import asyncio

    asset = await make_asset(sf)
    await seed(sf, asset.id, [0])
    cache = StatsCache(clock, ttl_seconds=30)
    assert (await cache.get(sf))[asset.id].count == 1
    await seed(sf, asset.id, [1])
    clock.advance(31)
    gate = asyncio.Event()
    counting = CountingSessionFactory(sf, gate)
    stale = await asyncio.wait_for(asyncio.gather(*[cache.get(counting) for _ in range(3)]), timeout=1)
    assert all(r[asset.id].count == 1 for r in stale)  # returned immediately, without waiting for the query
    gate.set()
    await asyncio.sleep(0.2)
    assert counting.opened == 1
    assert (await cache.get(counting))[asset.id].count == 2
    assert counting.opened == 1


async def test_default_ttl_is_120_seconds():
    assert StatsCache(None)._ttl == 120.0


async def _idle(cache):
    import asyncio

    while cache._task is not None and not cache._task.done():
        await asyncio.wait([cache._task])
    await asyncio.sleep(0.05)  # lets the done-callback start a coalesced follow-up load
    while cache._task is not None and not cache._task.done():
        await asyncio.wait([cache._task])


async def test_many_invalidations_during_a_load_cause_at_most_one_more_load(sf, clock):
    import asyncio

    asset = await make_asset(sf)
    await seed(sf, asset.id, [0])
    gate = asyncio.Event()
    counting = CountingSessionFactory(sf, gate)
    cache = StatsCache(clock, ttl_seconds=60)
    first = asyncio.create_task(cache.get(counting))
    await asyncio.sleep(0.05)
    for _ in range(10):
        cache.invalidate()
        await asyncio.sleep(0)
    gate.set()
    await first
    await _idle(cache)
    assert counting.opened == 2
    assert (await cache.get(counting))[asset.id].count == 1
    await _idle(cache)
    assert counting.opened == 2


async def test_invalidate_serves_stale_value_and_refreshes_in_background(sf, clock):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [0])
    cache = StatsCache(clock, ttl_seconds=60)
    assert (await cache.get(sf))[asset.id].count == 1
    await seed(sf, asset.id, [1])
    cache.invalidate()
    assert (await cache.get(sf))[asset.id].count == 1  # stale, not a blocking reload
    await _idle(cache)
    assert (await cache.get(sf))[asset.id].count == 2


async def test_clear_forces_a_blocking_reload(sf, clock):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [0])
    cache = StatsCache(clock, ttl_seconds=60)
    await cache.get(sf)
    await seed(sf, asset.id, [1])
    cache.clear()
    assert (await cache.get(sf))[asset.id].count == 2


async def test_failed_refresh_is_logged_and_next_get_retries(sf, clock, caplog):
    import logging

    asset = await make_asset(sf)
    await seed(sf, asset.id, [0])
    cache = StatsCache(clock, ttl_seconds=60)
    await cache.get(sf)
    cache.invalidate()

    def broken():
        raise RuntimeError("db down")

    with caplog.at_level(logging.WARNING, logger="app.services.assets"):
        assert (await cache.get(broken))[asset.id].count == 1  # stale value still served
        await _idle(cache)
    assert any("stats refresh failed" in r.getMessage() for r in caplog.records)
    counting = CountingSessionFactory(sf)
    await cache.get(counting)
    await _idle(cache)
    assert counting.opened == 1  # retried
    assert (await cache.get(counting))[asset.id].count == 1


async def test_refresh_soon_loads_fresh_counts_without_waiting_for_a_page_view(sf, clock):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [0])
    cache = StatsCache(clock, ttl_seconds=60)
    assert (await cache.get(sf))[asset.id].count == 1
    await seed(sf, asset.id, [1])
    cache.refresh_soon(sf)  # what the worker calls when a job finishes
    await _idle(cache)
    assert cache._value[asset.id].count == 2  # already fresh before the next poll
    assert (await cache.get(sf))[asset.id].count == 2

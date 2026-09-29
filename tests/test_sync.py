from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.domain import TransientError
from app.models import Asset, CandleRow
from app.providers.base import ProviderRegistry
from app.services import jobs, sync
from app.services.sync import SliceOutcome
from tests.fakes import FakeProvider, make_asset


@pytest.fixture
def provider(clock):
    return FakeProvider(clock)


@pytest.fixture
def registry(provider):
    return ProviderRegistry([provider])


async def start_job(sf, registry, clock, asset_id, kind="backfill"):
    job = await jobs.enqueue(sf, registry, clock, asset_id, kind)
    assert await jobs.claim_next(sf, clock) == job.id
    return job


async def candle_count(sf, asset_id) -> int:
    async with sf() as s:
        return await s.scalar(select(func.count()).select_from(CandleRow).where(CandleRow.asset_id == asset_id))


async def cursor(sf, asset_id):
    async with sf() as s:
        return (await s.get(Asset, asset_id)).fetched_until


async def test_backfill_stores_every_candle_and_advances_the_cursor(sf, registry, clock):
    asset = await make_asset(sf)
    job = await start_job(sf, registry, clock, asset.id)
    assert await sync.run_slice(sf, registry, clock, job.id) is SliceOutcome.DONE
    assert await candle_count(sf, asset.id) == 120
    assert await cursor(sf, asset.id) == clock.now()
    stored = await jobs.get_job(sf, job.id)
    assert (stored.requests_made, stored.candles_added, stored.attempt) == (12, 120, 0)


async def test_update_fetches_only_newer_candles(sf, registry, provider, clock):
    asset = await make_asset(sf)
    job = await start_job(sf, registry, clock, asset.id)
    await sync.run_slice(sf, registry, clock, job.id)
    await jobs.finish(sf, clock, job.id, 0.0)
    first_end = clock.now()
    clock.advance(30 * 60)
    update_job = await start_job(sf, registry, clock, asset.id, "update")
    assert await sync.run_slice(sf, registry, clock, update_job.id) is SliceOutcome.DONE
    assert provider.calls[-1] == (first_end, clock.now())
    assert await candle_count(sf, asset.id) == 150


async def test_overlapping_rerun_inserts_no_duplicates(sf, registry, clock):
    asset = await make_asset(sf)
    job = await start_job(sf, registry, clock, asset.id)
    await sync.run_slice(sf, registry, clock, job.id)
    await jobs.finish(sf, clock, job.id, 0.0)
    async with sf.begin() as s:
        await s.execute(
            update(Asset).where(Asset.id == asset.id).values(fetched_until=clock.now() - timedelta(minutes=20))
        )
    rerun = await start_job(sf, registry, clock, asset.id, "update")
    await sync.run_slice(sf, registry, clock, rerun.id)
    assert await candle_count(sf, asset.id) == 120
    assert (await jobs.get_job(sf, rerun.id)).candles_added == 0


async def test_failure_keeps_progress_and_resume_skips_finished_ranges(sf, clock):
    asset = await make_asset(sf)
    failing = ProviderRegistry([FakeProvider(clock, fail_after=3)])
    job = await start_job(sf, failing, clock, asset.id)
    with pytest.raises(TransientError):
        await sync.run_slice(sf, failing, clock, job.id)
    assert await candle_count(sf, asset.id) == 30
    resume_from = await cursor(sf, asset.id)
    assert resume_from == asset.start_date + timedelta(minutes=30)
    healthy = FakeProvider(clock)
    assert await sync.run_slice(sf, ProviderRegistry([healthy]), clock, job.id) is SliceOutcome.DONE
    assert healthy.calls == [(resume_from, job.range_end)]
    assert await candle_count(sf, asset.id) == 120


async def test_empty_ranges_still_advance_the_cursor(sf, clock):
    asset = await make_asset(sf)
    provider = FakeProvider(clock, listed=asset.start_date + timedelta(minutes=60))
    registry = ProviderRegistry([provider])
    job = await start_job(sf, registry, clock, asset.id)
    assert await sync.run_slice(sf, registry, clock, job.id) is SliceOutcome.DONE
    assert await candle_count(sf, asset.id) == 60
    assert await cursor(sf, asset.id) == job.range_end
    await jobs.finish(sf, clock, job.id, 0.0)
    clock.advance(600)
    later = await start_job(sf, registry, clock, asset.id, "update")
    await sync.run_slice(sf, registry, clock, later.id)
    assert provider.calls[-1][0] == job.range_end


async def test_backfill_yields_after_its_slice(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock, seconds_per_chunk=25)])
    asset = await make_asset(sf)
    job = await start_job(sf, registry, clock, asset.id)
    assert await sync.run_slice(sf, registry, clock, job.id, slice_seconds=60) is SliceOutcome.YIELDED
    assert await candle_count(sf, asset.id) == 30


async def test_update_jobs_are_not_sliced(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock, seconds_per_chunk=25)])
    asset = await make_asset(sf)
    job = await start_job(sf, registry, clock, asset.id, "update")
    assert await sync.run_slice(sf, registry, clock, job.id, slice_seconds=60) is SliceOutcome.DONE


async def test_cancelled_job_stops_without_storing(sf, registry, clock):
    asset = await make_asset(sf)
    job = await start_job(sf, registry, clock, asset.id)
    await jobs.cancel(sf, clock, job.id)
    assert await sync.run_slice(sf, registry, clock, job.id) is SliceOutcome.STOPPED
    assert await candle_count(sf, asset.id) == 0

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.clock import Clock
from app.domain import PermanentError, RateLimited, TransientError, floor_minute
from app.providers.base import ProviderRegistry
from app.services import jobs
from app.worker import Worker
from tests.fakes import FakeProvider, make_asset

RESUME = datetime(2024, 1, 1, 3, 0, tzinfo=UTC)


async def run_one(worker, sf, clock):
    job_id = await jobs.claim_next(sf, clock)
    await worker.run_job(job_id)
    return await jobs.get_job(sf, job_id)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (RateLimited("Fake", RESUME), "waiting"),
        (PermanentError("Fake: bad key"), "failed"),
        (TransientError("Fake: boom"), "paused"),
        (RuntimeError("bug"), "paused"),
    ],
)
async def test_errors_map_to_job_status(sf, clock, error, status):
    registry = ProviderRegistry([FakeProvider(clock, fail_after=0, error=error)])
    asset = await make_asset(sf)
    await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    job = await run_one(Worker(sf, registry, clock), sf, clock)
    assert job.status == status


async def test_rate_limited_job_shows_when_it_resumes(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock, fail_after=0, error=RateLimited("Fake", RESUME))])
    asset = await make_asset(sf)
    await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    job = await run_one(Worker(sf, registry, clock), sf, clock)
    assert job.status_detail == "Fake rate limit, resuming 03:00:00 UTC"
    assert job.next_attempt_at == RESUME


async def test_completed_slice_marks_job_done(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock)])
    asset = await make_asset(sf)
    await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    assert (await run_one(Worker(sf, registry, clock), sf, clock)).status == "done"


async def test_sliced_backfill_lets_a_queued_update_run_first(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock, seconds_per_chunk=25)])
    a = await make_asset(sf, provider_symbol="A")
    b = await make_asset(sf, provider_symbol="B")
    await jobs.enqueue(sf, registry, clock, a.id, "backfill")
    worker = Worker(sf, registry, clock, slice_seconds=60)
    assert (await run_one(worker, sf, clock)).status == "queued"
    update = await jobs.enqueue(sf, registry, clock, b.id, "update")
    assert await jobs.claim_next(sf, clock) == update.id


async def test_worker_runs_queued_jobs_in_the_background(sf):
    clock = Clock()
    registry = ProviderRegistry([FakeProvider()])
    asset = await make_asset(sf, start_date=floor_minute(clock.now()) - timedelta(minutes=30))
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    worker = Worker(sf, registry, clock, concurrency=2, poll_interval=0.05)
    worker.start()
    try:
        for _ in range(100):
            if (await jobs.get_job(sf, job.id)).status == "done":
                break
            await asyncio.sleep(0.05)
    finally:
        await worker.stop()
    assert (await jobs.get_job(sf, job.id)).status == "done"

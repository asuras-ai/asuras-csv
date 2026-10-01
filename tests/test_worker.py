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


async def test_transition_failure_is_retried_until_the_job_settles(sf, clock, monkeypatch):
    registry = ProviderRegistry([FakeProvider(clock)])
    asset = await make_asset(sf)
    await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    real_finish, calls = jobs.finish, []

    async def flaky_finish(*args):
        calls.append(args)
        if len(calls) == 1:
            raise ConnectionError("db down")
        return await real_finish(*args)

    monkeypatch.setattr(jobs, "finish", flaky_finish)
    job = await run_one(Worker(sf, registry, clock, poll_interval=1.0), sf, clock)
    assert job.status == "done"
    assert len(calls) == 2
    assert clock.sleeps == [1.0]


async def test_rate_limited_does_not_count_as_an_attempt(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock, fail_after=0, error=RateLimited("Fake", RESUME))])
    asset = await make_asset(sf)
    await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    job = await run_one(Worker(sf, registry, clock), sf, clock)
    assert (job.status, job.attempt) == ("waiting", 0)


async def test_resize_adds_and_removes_workers(sf):
    worker = Worker(sf, ProviderRegistry([FakeProvider()]), Clock(), concurrency=1, poll_interval=0.01)
    worker.start()
    try:
        worker.resize(3)
        assert len(worker._slots) == 3
        assert not any(t.done() for t in worker._slots.values())
        worker.resize(1)
        await asyncio.sleep(0.2)
        assert [i for i, t in worker._slots.items() if not t.done()] == [0]
    finally:
        await worker.stop()


async def test_stop_mid_job_leaves_it_running_for_recovery(sf, clock):
    started, release = asyncio.Event(), asyncio.Event()

    class Blocking(FakeProvider):
        async def fetch(self, symbol, start, end):
            started.set()
            await release.wait()
            async for chunk in super().fetch(symbol, start, end):
                yield chunk

    registry = ProviderRegistry([Blocking(clock)])
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    worker = Worker(sf, registry, clock, poll_interval=0.01)
    worker.start()
    await asyncio.wait_for(started.wait(), 5)
    await worker.stop()
    assert (await jobs.get_job(sf, job.id)).status == "running"
    assert await jobs.recover(sf) == 1
    assert (await jobs.get_job(sf, job.id)).status == "queued"


async def test_stop_does_not_cancel_a_claim_in_flight(sf, monkeypatch):
    entered, cancelled = asyncio.Event(), []

    async def slow_claim(*args):
        entered.set()
        try:
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(jobs, "claim_next", slow_claim)
    worker = Worker(sf, ProviderRegistry([FakeProvider()]), Clock(), concurrency=1, poll_interval=0.01)
    worker.start()
    await asyncio.wait_for(entered.wait(), 5)
    await worker.stop()
    assert cancelled == []


async def test_stop_is_bounded_when_a_claim_hangs(sf, monkeypatch):
    import time

    from app import worker as worker_mod

    entered = asyncio.Event()

    async def stuck_claim(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(jobs, "claim_next", stuck_claim)
    monkeypatch.setattr(worker_mod, "STOP_TIMEOUT", 0.1, raising=False)
    worker = Worker(sf, ProviderRegistry([FakeProvider()]), Clock(), concurrency=1, poll_interval=0.01)
    worker.start()
    await asyncio.wait_for(entered.wait(), 5)
    t0 = time.monotonic()
    await asyncio.wait_for(worker.stop(), 5)
    assert time.monotonic() - t0 < 1


async def test_on_progress_called_after_finish_fail_and_requeue(sf, clock):
    calls = []
    registry = ProviderRegistry([FakeProvider(clock)])
    asset = await make_asset(sf)
    await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    await run_one(Worker(sf, registry, clock, on_progress=lambda: calls.append("done")), sf, clock)
    assert calls == ["done"]

    bad = ProviderRegistry([FakeProvider(clock, fail_after=0, error=PermanentError("Fake: bad"))])
    other = await make_asset(sf, provider_symbol="B")
    await jobs.enqueue(sf, bad, clock, other.id, "backfill")
    await run_one(Worker(sf, bad, clock, on_progress=lambda: calls.append("failed")), sf, clock)
    assert calls == ["done", "failed"]


async def test_on_progress_called_when_a_backfill_slice_yields(sf, clock):
    calls = []
    registry = ProviderRegistry([FakeProvider(clock, seconds_per_chunk=25)])
    asset = await make_asset(sf)
    await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    job = await run_one(Worker(sf, registry, clock, slice_seconds=60, on_progress=lambda: calls.append(1)), sf, clock)
    assert job.status == "queued"
    assert calls == [1]

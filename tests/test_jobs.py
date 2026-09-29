import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models import ACTIVE_STATUSES, Job
from app.providers.base import ProviderRegistry
from app.services import jobs
from tests.fakes import FakeProvider, make_asset


@pytest.fixture
def registry(clock):
    return ProviderRegistry([FakeProvider(clock)])


async def test_enqueue_backfill_covers_start_date_to_now(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    assert (job.status, job.priority, job.range_start, job.range_end) == (
        "queued", 0, asset.start_date, clock.now(),
    )


async def test_enqueue_returns_the_existing_active_job(sf, registry, clock):
    asset = await make_asset(sf)
    first = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    second = await jobs.enqueue(sf, registry, clock, asset.id, "update")
    assert second.id == first.id and second.kind == "backfill"


async def test_enqueue_starts_from_the_cursor_and_skips_up_to_date_assets(sf, registry, clock):
    asset = await make_asset(sf, fetched_until=clock.now() - timedelta(minutes=30))
    job = await jobs.enqueue(sf, registry, clock, asset.id, "update")
    assert (job.range_start, job.priority) == (clock.now() - timedelta(minutes=30), 10)
    fresh = await make_asset(sf, provider_symbol="FRESH", fetched_until=clock.now())
    done = await jobs.enqueue(sf, registry, clock, fresh.id, "update")
    assert done.status == "done" and done.finished_at == clock.now()


async def test_claim_prefers_updates_then_oldest(sf, registry, clock):
    a = await make_asset(sf, provider_symbol="A")
    b = await make_asset(sf, provider_symbol="B")
    c = await make_asset(sf, provider_symbol="C")
    backfill = await jobs.enqueue(sf, registry, clock, a.id, "backfill")
    update = await jobs.enqueue(sf, registry, clock, b.id, "update")
    later = await jobs.enqueue(sf, registry, clock, c.id, "backfill")
    assert await jobs.claim_next(sf, clock) == update.id
    assert await jobs.claim_next(sf, clock) == backfill.id
    assert await jobs.claim_next(sf, clock) == later.id
    assert await jobs.claim_next(sf, clock) is None
    assert (await jobs.get_job(sf, update.id)).status == "running"


async def test_waiting_job_is_claimed_again_after_resume_time(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    await jobs.claim_next(sf, clock)
    assert await jobs.wait(sf, job.id, clock.now() + timedelta(seconds=30), "Fake rate limit", 1.0)
    assert await jobs.claim_next(sf, clock) is None
    clock.advance(30)
    assert await jobs.claim_next(sf, clock) == job.id


async def test_pause_backs_off_then_gives_up_after_24h_without_progress(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    delays = []
    for _ in range(5):
        assert await jobs.claim_next(sf, clock) == job.id
        await jobs.pause(sf, clock, job.id, "Fake: boom", 0.0)
        paused = await jobs.get_job(sf, job.id)
        assert paused.status == "paused" and "retrying at" in paused.status_detail
        delays.append((paused.next_attempt_at - clock.now()).total_seconds())
        clock.advance(delays[-1])
    assert delays == [60, 300, 900, 3600, 3600]
    clock.advance(24 * 3600)
    assert await jobs.claim_next(sf, clock) == job.id
    await jobs.pause(sf, clock, job.id, "Fake: boom", 0.0)
    failed = await jobs.get_job(sf, job.id)
    assert failed.status == "failed" and "24 h" in failed.error


async def test_transitions_only_apply_to_running_jobs(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    await jobs.claim_next(sf, clock)
    assert await jobs.cancel(sf, clock, job.id)
    assert not await jobs.finish(sf, clock, job.id, 5.0)
    assert (await jobs.get_job(sf, job.id)).status == "cancelled"


async def test_finish_accumulates_run_time(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    await jobs.claim_next(sf, clock)
    assert await jobs.requeue(sf, job.id, 2.0)
    await jobs.claim_next(sf, clock)
    assert await jobs.finish(sf, clock, job.id, 3.0)
    done = await jobs.get_job(sf, job.id)
    assert (done.status, done.run_seconds, done.finished_at) == ("done", 5.0, clock.now())
    assert (await jobs.latest_jobs_by_asset(sf))[asset.id].id == job.id


async def test_fail_records_the_error(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    await jobs.claim_next(sf, clock)
    assert await jobs.fail(sf, clock, job.id, "Alpaca: API key rejected", 1.0)
    failed = await jobs.get_job(sf, job.id)
    assert (failed.status, failed.error) == ("failed", "Alpaca: API key rejected")


async def test_recover_requeues_running_and_waiting_jobs(sf, registry, clock):
    a = await make_asset(sf, provider_symbol="A")
    b = await make_asset(sf, provider_symbol="B")
    ja = await jobs.enqueue(sf, registry, clock, a.id, "backfill")
    jb = await jobs.enqueue(sf, registry, clock, b.id, "backfill")
    await jobs.claim_next(sf, clock)
    await jobs.claim_next(sf, clock)
    await jobs.wait(sf, jb.id, clock.now() + timedelta(minutes=5), "Fake rate limit", 0.0)
    assert await jobs.recover(sf) == 2
    assert {(await jobs.get_job(sf, j.id)).status for j in (ja, jb)} == {"queued"}


async def test_enqueue_all_skips_disabled_assets(sf, registry, clock):
    a = await make_asset(sf, provider_symbol="A")
    await make_asset(sf, provider_symbol="B", enabled=False)
    created = await jobs.enqueue_all(sf, registry, clock)
    assert [j.asset_id for j in created] == [a.id]


async def test_list_recent_joins_assets(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    [(listed_job, listed_asset)] = await jobs.list_recent(sf)
    assert (listed_job.id, listed_asset.jesse_symbol) == (job.id, "FAKE-USD")


async def test_concurrent_claims_get_different_jobs(sf, registry, clock):
    a = await make_asset(sf, provider_symbol="A")
    b = await make_asset(sf, provider_symbol="B")
    ja = await jobs.enqueue(sf, registry, clock, a.id, "backfill")
    jb = await jobs.enqueue(sf, registry, clock, b.id, "backfill")
    claimed = await asyncio.gather(jobs.claim_next(sf, clock), jobs.claim_next(sf, clock))
    assert sorted(claimed) == sorted([ja.id, jb.id])


async def _active_count(sf, asset_id):
    async with sf() as s:
        return await s.scalar(
            select(func.count()).select_from(Job).where(Job.asset_id == asset_id, Job.status.in_(ACTIVE_STATUSES))
        )


async def test_concurrent_enqueues_share_one_active_job(sf, registry, clock):
    asset = await make_asset(sf)
    first, second = await asyncio.gather(
        jobs.enqueue(sf, registry, clock, asset.id, "backfill"),
        jobs.enqueue(sf, registry, clock, asset.id, "backfill"),
    )
    assert first.id == second.id
    assert await _active_count(sf, asset.id) == 1


async def test_enqueue_recovers_from_integrity_error_race(sf, registry, clock, monkeypatch):
    asset = await make_asset(sf)
    existing = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    real = jobs._active_job
    calls = []

    async def blind_first_time(s, asset_id):
        calls.append(asset_id)
        return None if len(calls) == 1 else await real(s, asset_id)

    monkeypatch.setattr(jobs, "_active_job", blind_first_time)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    assert len(calls) == 2  # the insert hit the unique index and the except branch re-read
    assert job.id == existing.id
    assert await _active_count(sf, asset.id) == 1


async def test_enqueue_unknown_kind_raises_value_error(sf, registry, clock):
    asset = await make_asset(sf)
    with pytest.raises(ValueError, match="kind"):
        await jobs.enqueue(sf, registry, clock, asset.id, "bogus")


async def test_recover_leaves_paused_jobs_alone(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    await jobs.claim_next(sf, clock)
    await jobs.pause(sf, clock, job.id, "Fake: boom", 0.0)
    before = await jobs.get_job(sf, job.id)
    assert await jobs.recover(sf) == 0
    after = await jobs.get_job(sf, job.id)
    assert (after.status, after.next_attempt_at) == ("paused", before.next_attempt_at)
    assert after.next_attempt_at is not None


async def test_cancel_works_on_active_states_but_not_done(sf, registry, clock):
    a = await make_asset(sf, provider_symbol="A")
    b = await make_asset(sf, provider_symbol="B")
    c = await make_asset(sf, provider_symbol="C")
    d = await make_asset(sf, provider_symbol="D")
    queued = await jobs.enqueue(sf, registry, clock, a.id, "backfill")
    waiting = await jobs.enqueue(sf, registry, clock, b.id, "backfill")
    paused = await jobs.enqueue(sf, registry, clock, c.id, "backfill")
    done = await jobs.enqueue(sf, registry, clock, d.id, "backfill")
    async with sf.begin() as s:
        (await s.get(Job, waiting.id)).status = "waiting"
        (await s.get(Job, paused.id)).status = "paused"
        (await s.get(Job, done.id)).status = "done"
    for j in (queued, waiting, paused):
        assert await jobs.cancel(sf, clock, j.id)
        assert (await jobs.get_job(sf, j.id)).status == "cancelled"
    assert not await jobs.cancel(sf, clock, done.id)
    assert (await jobs.get_job(sf, done.id)).status == "done"


async def test_pause_is_a_noop_unless_running(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    await jobs.pause(sf, clock, job.id, "Fake: boom", 3.0)
    unchanged = await jobs.get_job(sf, job.id)
    assert (unchanged.status, unchanged.attempt, unchanged.run_seconds, unchanged.error) == ("queued", 0, 0.0, None)

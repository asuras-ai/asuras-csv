from datetime import UTC, datetime, timedelta

import pytest

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

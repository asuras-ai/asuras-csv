from app.providers.base import ProviderRegistry
from app.services import jobs
from tests.fakes import FakeProvider, make_asset


async def test_status_counts(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock)])
    a = await make_asset(sf)
    b = await make_asset(sf, provider_symbol="B", jesse_symbol="B-USD")
    await jobs.enqueue(sf, registry, clock, a.id, "backfill")
    job = await jobs.enqueue(sf, registry, clock, b.id, "backfill")
    await jobs.cancel(sf, clock, job.id)
    assert await jobs.status_counts(sf) == {"queued": 1, "cancelled": 1}


async def test_list_recent_filters_by_status_and_asset(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock)])
    a = await make_asset(sf)
    b = await make_asset(sf, provider_symbol="B", jesse_symbol="B-USD")
    ja = await jobs.enqueue(sf, registry, clock, a.id, "backfill")
    jb = await jobs.enqueue(sf, registry, clock, b.id, "backfill")
    failed = await jobs.claim_next(sf, clock)
    await jobs.fail(sf, clock, failed, "boom", 0.0)
    other = jb.id if failed == ja.id else ja.id
    assert [job.id for job, _ in await jobs.list_recent(sf, status="failed")] == [failed]
    assert [job.id for job, _ in await jobs.list_recent(sf, status="active")] == [other]
    assert [job.id for job, _ in await jobs.list_recent(sf, asset_id=a.id)] == [ja.id]


async def test_last_done_by_asset(sf, clock):
    a = await make_asset(sf)
    job = await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, a.id, "backfill")
    assert await jobs.claim_next(sf, clock) == job.id
    await jobs.finish(sf, clock, job.id, 1.0)
    assert await jobs.last_done_by_asset(sf) == {a.id: clock.now()}

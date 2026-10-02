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

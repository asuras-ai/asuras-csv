from app.providers.base import ProviderRegistry
from app.services import jobs
from tests.fakes import FakeProvider, make_asset


async def test_jobs_tabs_filter_and_count(client, sf, clock):
    registry = ProviderRegistry([FakeProvider(clock)])
    a = await make_asset(sf)
    b = await make_asset(sf, provider_symbol="B", jesse_symbol="B-USD")
    await jobs.enqueue(sf, registry, clock, a.id, "backfill")
    await jobs.enqueue(sf, registry, clock, b.id, "backfill")
    failed_id = await jobs.claim_next(sf, clock)
    await jobs.fail(sf, clock, failed_id, "HTTP 451 from upstream", 0.0)
    active_symbol = "B-USD" if (await jobs.get_job(sf, failed_id)).asset_id == a.id else "FAKE-USD"

    page = (await client.get("/jobs")).text
    assert 'Active <span class="count">1</span>' in page and 'Failed <span class="count">1</span>' in page
    assert 'href="/jobs" aria-current="page"' in page

    failed = (await client.get("/jobs?status=failed")).text
    assert "HTTP 451 from upstream" in failed and active_symbol not in failed

    active = (await client.get("/jobs?status=active")).text
    assert active_symbol in active and "HTTP 451" not in active
    assert 'name="next" value="/jobs?status=active"' in active


async def test_jobs_rows_poll_and_keep_the_filter(client, sf, clock):
    idle = (await client.get("/jobs/rows", params={"status": "failed"})).text
    assert idle.lstrip().startswith('<tbody id="job-rows"') and 'hx-get="/jobs/rows?status=failed"' in idle
    assert 'hx-trigger="every 30s"' in idle and "No failed jobs." in idle
    asset = await make_asset(sf)
    await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, asset.id, "backfill")
    assert 'hx-trigger="every 2s"' in (await client.get("/jobs/rows")).text


async def test_jobs_rejects_an_unknown_filter(client):
    assert (await client.get("/jobs", params={"status": "bogus"})).status_code == 422

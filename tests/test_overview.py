from datetime import UTC, datetime, timedelta

from app.domain import Candle
from app.providers.base import ProviderRegistry
from app.services import jobs
from app.services.sync import insert_candles
from tests.fakes import FakeProvider, make_asset

T = datetime(2023, 12, 30, 12, 0, tzinfo=UTC)  # FakeClock.now() is 2024-01-01 02:00 UTC


async def test_empty_overview_welcomes_and_lists_sources(client):
    r = await client.get("/")
    assert r.status_code == 200 and "Welcome to Asuras CSV" in r.text and 'href="/assets/new"' in r.text
    assert "Data sources" in r.text and "Fake" in r.text and "No key needed" not in r.text
    assert 'href="/" aria-current="page"' in r.text and "<title>Overview · Asuras CSV</title>" in r.text


async def test_overview_kpis_sparklines_and_recent_jobs(client, sf, clock):
    on = await make_asset(sf)
    await make_asset(sf, provider_symbol="OFF", jesse_symbol="OFF-USD", enabled=False)
    async with sf.begin() as s:
        await insert_candles(s, on.id, [Candle(T, 1, 2, 0.5, 1.0, 10), Candle(T + timedelta(days=1), 1, 2, 0.5, 1.5, 10)])
    job = await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, on.id, "backfill")
    assert await jobs.claim_next(sf, clock) == job.id
    await jobs.finish(sf, clock, job.id, 1.0)
    page = (await client.get("/")).text
    assert "1 enabled · 1 disabled" in page
    assert '<div class="kpi-value">1 / 1</div>' in page and "Nothing running" in page
    assert 'class="spark spark-up"' in page
    assert "FAKE-USD" in page and "OFF-USD" in page and "backfill" in page and "badge-success" in page
    assert 'hx-trigger="every 30s"' in page


async def test_overview_live_polls_fast_while_a_job_is_active(client, sf, clock):
    asset = await make_asset(sf)
    await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, asset.id, "backfill")
    live = (await client.get("/overview/live")).text
    assert live.lstrip().startswith('<div id="overview-live"') and 'hx-trigger="every 2s"' in live
    assert "1 queued" in live and "<html" not in live


async def test_overview_lists_at_most_eight_assets(client, sf):
    for i in range(9):
        await make_asset(sf, provider_symbol=f"S{i}", jesse_symbol=f"S{i}-USD")
    page = (await client.get("/")).text
    assert page.count('<tr data-href="/assets/') == 8 and "View all 9" in page


async def test_overview_survives_an_asset_with_an_unknown_provider_and_no_candles(client, sf):
    await make_asset(sf, provider="gone", provider_symbol="OLD", jesse_symbol="OLD-USD")
    r = await client.get("/")
    assert r.status_code == 200 and "OLD-USD" in r.text and ">gone<" in r.text

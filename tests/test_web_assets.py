from datetime import timedelta

from app.services import jobs
from tests.fakes import make_asset

FORM = {
    "provider": "fake",
    "provider_symbol": "FAKEUSD",
    "asset_class": "crypto",
    "jesse_symbol": "FAKE-USD",
    "start_date": "2024-01-01",
}


async def test_empty_assets_page(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "No assets yet" in r.text


async def test_search_details_and_estimate(client):
    r = await client.get("/assets/search", params={"search_provider": "fake", "q": "fake"})
    assert "FAKEUSD" in r.text
    r = await client.get(
        "/assets/new/details",
        params={"provider": "fake", "symbol": "FAKEUSD", "asset_class": "crypto", "jesse_symbol": "FAKE-USD"},
    )
    assert 'value="2024-01-01"' in r.text
    assert "≈ 12 requests" in r.text
    r = await client.get("/assets/estimate", params={"provider": "fake", "start_date": "2024-01-01"})
    assert r.text.startswith("≈ 12 requests")


async def test_adding_an_asset_queues_a_backfill(client, sf):
    r = await client.post("/assets", data=FORM)
    assert r.status_code == 303
    rows = (await client.get("/assets/rows")).text
    assert "FAKE-USD" in rows and "queued" in rows
    [job] = (await jobs.latest_jobs_by_asset(sf)).values()
    assert job.kind == "backfill"


async def test_duplicate_and_invalid_assets_are_rejected(client):
    await client.post("/assets", data=FORM)
    r = await client.post("/assets", data=FORM)
    assert r.status_code == 400 and "already exists" in r.text
    r = await client.post("/assets", data=FORM | {"provider_symbol": "OTHER", "jesse_symbol": "bad symbol"})
    assert r.status_code == 400 and "Jesse symbol" in r.text


async def test_update_buttons_queue_update_jobs(client, sf, clock):
    asset = await make_asset(sf, fetched_until=clock.now() - timedelta(minutes=30))
    await make_asset(
        sf, provider_symbol="OFF", jesse_symbol="OFF-USD", enabled=False, fetched_until=clock.now() - timedelta(minutes=30)
    )
    assert (await client.post(f"/assets/{asset.id}/update")).status_code == 303
    assert (await client.post("/assets/update-all")).status_code == 303
    latest = await jobs.latest_jobs_by_asset(sf)
    assert list(latest) == [asset.id]
    assert latest[asset.id].kind == "update"

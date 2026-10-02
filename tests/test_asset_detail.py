from datetime import UTC, datetime, timedelta

import pytest

from app.domain import Candle
from app.providers.base import ProviderRegistry
from app.services import jobs
from app.services.charts import RANGES
from app.services.sync import insert_candles
from tests.fakes import FakeProvider, make_asset

T0 = datetime(2024, 1, 1, tzinfo=UTC)


async def test_detail_page_without_candles(client, sf):
    asset = await make_asset(sf)
    r = await client.get(f"/assets/{asset.id}")
    assert r.status_code == 200
    t = r.text
    assert "Custom Data · FAKE-USD" in t and "No candles stored yet." in t
    assert 'id="chart"' in t and 'id="export"' in t and 'id="settings"' in t
    assert "/static/vendor/lightweight-charts-4.2.0.standalone.production.js" in t and f"/assets/{asset.id}/candles.json" in t
    for label in RANGES:
        assert f'data-range="{label}"' in t
    assert "Chart library failed to load" in t and t.count('addEventListener("resize"') == 1
    assert 'href="/assets" aria-current="page"' in t and "<title>FAKE-USD · Asuras CSV</title>" in t


async def test_detail_page_export_defaults_to_the_stored_range(client, sf):
    asset = await make_asset(sf)
    async with sf.begin() as s:
        await insert_candles(s, asset.id, [Candle(T0 + timedelta(minutes=m), 1, 2, 0.5, 1.5, 10) for m in (0, 1)])
    t = (await client.get(f"/assets/{asset.id}")).text
    assert 'value="2024-01-01"' in t and f'action="/assets/{asset.id}/export.csv"' in t


async def test_unknown_and_non_numeric_assets(client):
    assert (await client.get("/assets/999")).status_code == 404
    assert (await client.get("/assets/abc")).status_code == 404
    assert (await client.get("/assets/new")).status_code == 200
    assert (await client.get("/assets/rows")).status_code == 200


@pytest.mark.parametrize("old, anchor", [("chart", "chart"), ("export", "export"), ("edit", "settings")])
async def test_old_pages_redirect_to_the_detail_page(client, sf, old, anchor):
    asset = await make_asset(sf)
    r = await client.get(f"/assets/{asset.id}/{old}")
    assert r.status_code == 303 and r.headers["location"] == f"/assets/{asset.id}#{anchor}"
    assert (await client.get(f"/assets/999/{old}")).status_code == 404


async def test_live_fragment_updates_status_details_and_jobs(client, sf, clock):
    asset = await make_asset(sf)
    idle = (await client.get(f"/assets/{asset.id}/live")).text
    assert idle.lstrip().startswith('<span id="asset-status"') and 'hx-trigger="every 30s"' in idle
    assert idle.count('hx-swap-oob="true"') == 2 and "No jobs yet." in idle
    await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, asset.id, "backfill")
    busy = (await client.get(f"/assets/{asset.id}/live")).text
    assert 'hx-trigger="every 2s"' in busy and "backfill" in busy


async def test_edit_saves_and_errors_render_on_the_detail_page(client, sf):
    asset = await make_asset(sf)
    r = await client.post(f"/assets/{asset.id}/edit", data={"jesse_symbol": "NEW-USD"})
    assert r.status_code == 303 and r.headers["location"] == f"/assets/{asset.id}"
    r = await client.post(f"/assets/{asset.id}/edit", data={"jesse_symbol": "bad", "enabled": "on"})
    assert r.status_code == 400 and "alert-danger" in r.text and 'id="chart"' in r.text and 'value="bad"' in r.text


async def test_detail_page_for_an_asset_with_an_unknown_provider(client, sf):
    stale = await make_asset(sf, provider="gone", provider_symbol="OLD", jesse_symbol="OLD-USD")
    r = await client.get(f"/assets/{stale.id}")
    assert r.status_code == 200 and "gone · OLD" in r.text

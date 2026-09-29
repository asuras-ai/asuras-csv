from datetime import UTC, datetime, timedelta

from app.domain import Candle
from app.providers.base import ProviderRegistry
from app.services import jobs
from app.services.assets import get_asset
from app.services.sync import insert_candles
from tests.fakes import FakeProvider, make_asset

T0 = datetime(2024, 1, 1, tzinfo=UTC)


async def test_edit_asset(client, sf):
    asset = await make_asset(sf)
    assert (await client.get(f"/assets/{asset.id}/edit")).status_code == 200
    r = await client.post(f"/assets/{asset.id}/edit", data={"jesse_symbol": "NEW-USD"})
    assert r.status_code == 303
    got = await get_asset(sf, asset.id)
    assert (got.jesse_symbol, got.enabled) == ("NEW-USD", False)
    r = await client.post(f"/assets/{asset.id}/edit", data={"jesse_symbol": "bad", "enabled": "on"})
    assert r.status_code == 400 and "Jesse symbol" in r.text
    assert (await client.get("/assets/999/edit")).status_code == 404


async def test_delete_asset(client, sf):
    asset = await make_asset(sf)
    r = await client.get(f"/assets/{asset.id}/delete")
    assert r.status_code == 200 and "FAKE-USD" in r.text
    assert (await client.post(f"/assets/{asset.id}/delete")).status_code == 303
    assert await get_asset(sf, asset.id) is None


async def test_export_downloads_jesse_csv(client, sf):
    asset = await make_asset(sf)
    async with sf.begin() as s:
        await insert_candles(s, asset.id, [Candle(T0 + timedelta(minutes=m), 1, 2, 0.5, 1.5, 10) for m in (0, 1)])
    page = await client.get(f"/assets/{asset.id}/export")
    assert 'value="2024-01-01"' in page.text
    r = await client.get(f"/assets/{asset.id}/export.csv", params={"start": "2024-01-01", "end": "2024-01-01"})
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="FAKE-USD_2024-01-01_2024-01-01.csv"' in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0] == "timestamp,open,close,high,low,volume" and len(lines) == 3
    assert (await client.get(f"/assets/{asset.id}/export.csv", params={"start": "2025-01-01"})).status_code == 404
    assert (await client.get(f"/assets/{asset.id}/export.csv", params={"start": "nope"})).status_code == 400


async def test_jobs_page_and_cancel(client, sf, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, asset.id, "backfill")
    r = await client.get("/jobs")
    assert "FAKE-USD" in r.text and "queued" in r.text and "Cancel" in r.text
    r = await client.post(f"/jobs/{job.id}/cancel", data={"next": "/jobs"})
    assert r.status_code == 303 and r.headers["location"] == "/jobs"
    assert (await jobs.get_job(sf, job.id)).status == "cancelled"


async def test_settings_roundtrip(client):
    assert (await client.get("/settings")).status_code == 200
    r = await client.post(
        "/settings",
        data={
            "schedule_enabled": "on",
            "schedule_cron": "0 */6 * * *",
            "worker_concurrency": "2",
            "alpaca_key_id": "KEYID1234",
            "alpaca_secret_key": "s3cr3t-value",
        },
    )
    assert r.status_code == 303
    page = (await client.get("/settings")).text
    assert "0 */6 * * *" in page and "1234" in page and "s3cr3t-value" not in page
    r = await client.post("/settings", data={"schedule_cron": "bad", "worker_concurrency": "2"})
    assert r.status_code == 400 and 'class="error"' in r.text


async def test_export_body_matches_filename_range(client, sf):
    asset = await make_asset(sf)
    async with sf.begin() as s:
        await insert_candles(s, asset.id, [Candle(T0 + timedelta(minutes=m), 1, 2, 0.5, 1.5, 10) for m in (0, 1)])
    # a candle arriving after the range end must never leak into a body whose filename excludes it
    late = T0 + timedelta(days=1, minutes=5)
    async with sf.begin() as s:
        await insert_candles(s, asset.id, [Candle(late, 1, 2, 0.5, 1.5, 10)])
    r = await client.get(f"/assets/{asset.id}/export.csv", params={"start": "2024-01-01", "end": "2024-01-01"})
    lines = r.text.splitlines()
    assert "2024-01-01_2024-01-01" in r.headers["content-disposition"]
    assert len(lines) == 3 and int(lines[-1].split(",")[0]) == int((T0 + timedelta(minutes=1)).timestamp() * 1000)

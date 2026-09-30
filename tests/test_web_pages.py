from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.config import EnvConfig
from sqlalchemy import func, select

from app.domain import Candle
from app.models import CandleRow, Setting
from app.main import create_app
from app.services.export import ExportRange
from app.services.settings import SettingsService
from app.web import routes
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
    async with sf.begin() as s:
        await insert_candles(s, asset.id, [Candle(T0, 1, 2, 0.5, 1.5, 10)])
    r = await client.get(f"/assets/{asset.id}/delete")
    assert r.status_code == 200 and "FAKE-USD" in r.text
    assert (await client.post(f"/assets/{asset.id}/delete")).status_code == 303
    assert await get_asset(sf, asset.id) is None
    async with sf() as s:
        remaining = (await s.execute(select(func.count()).select_from(CandleRow).where(CandleRow.asset_id == asset.id))).scalar_one()
    assert remaining == 0


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
    assert "0 */6 * * *" in page and "•••• 1234" in page and "s3cr3t-value" not in page
    r = await client.post("/settings", data={"schedule_cron": "bad", "worker_concurrency": "2"})
    assert r.status_code == 400 and 'class="error"' in r.text


@pytest.mark.parametrize("target", ["//evil.com", "/\\evil.com", "https://evil.com"])
async def test_cancel_rejects_offsite_redirect(client, sf, clock, target):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, asset.id, "backfill")
    r = await client.post(f"/jobs/{job.id}/cancel", data={"next": target})
    assert r.status_code == 303 and r.headers["location"] == "/jobs"


async def test_export_body_bounded_by_range_not_raw_dates(client, sf, monkeypatch):
    asset = await make_asset(sf)
    async with sf.begin() as s:
        await insert_candles(s, asset.id, [Candle(T0 + timedelta(minutes=m), 1, 2, 0.5, 1.5, 10) for m in (0, 1)])
    newer = T0 + timedelta(days=3)
    async with sf.begin() as s:
        await insert_candles(s, asset.id, [Candle(newer, 1, 2, 0.5, 1.5, 10)])

    async def stale_range(sf_, asset_id, start, end):
        return ExportRange(T0, T0 + timedelta(minutes=1))

    monkeypatch.setattr(routes, "export_range", stale_range)
    r = await client.get(f"/assets/{asset.id}/export.csv")  # no end date
    assert "2024-01-01_2024-01-01" in r.headers["content-disposition"]
    stamps = [int(line.split(",")[0]) for line in r.text.splitlines()[1:]]
    assert stamps[-1] == int((T0 + timedelta(minutes=1)).timestamp() * 1000)
    assert int(newer.timestamp() * 1000) not in stamps


@pytest.fixture
async def env_client(sf, clock):
    env = EnvConfig(
        _env_file=None,
        database_url="postgresql+asyncpg://unused@localhost/unused",
        alpaca_key_id="ENVKEY1234",
        alpaca_secret_key="ENVSECRET",
    )
    app = create_app(env, sf=sf, clock=clock, registry=ProviderRegistry([FakeProvider(clock)]), start_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c, SettingsService(sf, env)


async def test_env_keys_are_read_only(env_client, sf):
    c, settings = env_client
    page = (await c.get("/settings")).text
    assert "environment" in page and 'name="alpaca_key_id"' not in page
    r = await c.post(
        "/settings",
        data={"schedule_cron": "0 * * * *", "worker_concurrency": "2", "alpaca_key_id": "OTHER", "alpaca_secret_key": "OTHER2"},
    )
    assert r.status_code == 303
    loaded = await settings.load()
    assert (loaded.alpaca_key_id, loaded.alpaca_secret_key) == ("ENVKEY1234", "ENVSECRET")
    async with sf() as s:
        stored = (await s.scalars(select(Setting.key).where(Setting.key.in_(["alpaca_key_id", "alpaca_secret_key"])))).all()
    assert stored == []  # the posted values were never persisted


async def test_blank_key_fields_keep_stored_values(client, sf):
    base = {"schedule_cron": "0 * * * *", "worker_concurrency": "2"}
    await client.post("/settings", data=base | {"alpaca_key_id": "KEYID1234", "alpaca_secret_key": "sec-1"})
    r = await client.post("/settings", data=base | {"alpaca_key_id": "", "alpaca_secret_key": ""})
    assert r.status_code == 303
    svc = SettingsService(sf, EnvConfig(_env_file=None, database_url="postgresql+asyncpg://u@h/d", alpaca_key_id="", alpaca_secret_key=""))
    assert await svc.alpaca_credentials() == ("KEYID1234", "sec-1")


async def test_oanda_token_is_masked_and_never_rendered(client, sf):
    base = {"schedule_cron": "0 * * * *", "worker_concurrency": "2"}
    r = await client.post("/settings", data=base | {"oanda_api_token": "tok-abcdef-9876", "oanda_environment": "live"})
    assert r.status_code == 303
    page = (await client.get("/settings")).text
    assert "tok-abcdef-9876" not in page and "•••• 9876" in page
    assert 'name="oanda_api_token"' in page and 'name="oanda_environment"' in page
    assert '<option value="live" selected>' in page
    # A blank token keeps the stored one; the environment can change independently.
    r = await client.post("/settings", data=base | {"oanda_api_token": "", "oanda_environment": "practice"})
    assert r.status_code == 303
    svc = SettingsService(sf, EnvConfig(_env_file=None, database_url="postgresql+asyncpg://u@h/d", oanda_api_token=""))
    assert await svc.oanda_credentials() == ("tok-abcdef-9876", "practice")
    r = await client.post("/settings", data=base | {"oanda_environment": "bogus"})
    assert r.status_code == 400 and 'class="error"' in r.text


async def test_oanda_env_token_is_read_only(sf, clock):
    env = EnvConfig(
        _env_file=None, database_url="postgresql+asyncpg://unused@localhost/unused", oanda_api_token="ENVTOK5678", oanda_environment="live"
    )
    app = create_app(env, sf=sf, clock=clock, registry=ProviderRegistry([FakeProvider(clock)]), start_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        page = (await c.get("/settings")).text
        assert "ENVTOK5678" not in page and 'name="oanda_api_token"' not in page and 'name="oanda_environment"' not in page
        r = await c.post(
            "/settings",
            data={"schedule_cron": "0 * * * *", "worker_concurrency": "2", "oanda_api_token": "OTHER", "oanda_environment": "practice"},
        )
        assert r.status_code == 303
    assert await SettingsService(sf, env).oanda_credentials() == ("ENVTOK5678", "live")
    async with sf() as s:
        stored = (await s.scalars(select(Setting.key).where(Setting.key.like("oanda_%")))).all()
    assert stored == []

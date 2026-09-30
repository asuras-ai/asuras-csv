from datetime import timedelta

from app.clock import Clock
from app.config import EnvConfig
from app.main import create_app
from app.providers.base import ProviderRegistry
from app.services import jobs
from app.services.settings import SettingsService
from tests.fakes import FakeProvider, make_asset

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


async def test_lifespan_recovers_jobs_and_starts_worker_and_scheduler(sf, monkeypatch):
    env = EnvConfig(
        _env_file=None, database_url="postgresql+asyncpg://unused@localhost/unused", alpaca_key_id="", alpaca_secret_key=""
    )
    clock = Clock()
    registry = ProviderRegistry([FakeProvider()])
    asset = await make_asset(sf, fetched_until=clock.now() - timedelta(minutes=30))
    await jobs.enqueue(sf, registry, clock, asset.id, "update")
    assert await jobs.claim_next(sf, clock) is not None
    await SettingsService(sf, env).save(
        {"worker_concurrency": "2", "schedule_enabled": "true", "schedule_cron": "0 * * * *"}
    )
    recovered = []
    real_recover = jobs.recover

    async def spy(session_factory):
        recovered.append(await real_recover(session_factory))
        return recovered[-1]

    monkeypatch.setattr(jobs, "recover", spy)
    app = create_app(env, sf=sf, clock=clock, registry=registry, start_background=True)
    async with app.router.lifespan_context(app):
        svc = app.state.services
        assert recovered == [1]
        assert svc.worker is not None and svc.worker._target == 2
        assert svc.scheduler is not None and svc.scheduler.next_run() is not None
    assert not svc.worker._slots


async def test_estimate_errors_render_fragments(client):
    r = await client.get("/assets/estimate", params={"provider": "nope", "start_date": "2024-01-01"})
    assert r.status_code == 200 and "Unknown provider" in r.text
    for params in ({"provider": "fake"}, {"provider": "fake", "start_date": ""}, {"provider": "fake", "start_date": "junk"}):
        r = await client.get("/assets/estimate", params=params)
        assert r.status_code == 200 and "requests" not in r.text


async def test_unknown_provider_renders_error_in_search_and_details(client):
    r = await client.get("/assets/search", params={"search_provider": "nope", "q": "x"})
    assert r.status_code == 200 and "Unknown provider" in r.text
    r = await client.get(
        "/assets/new/details",
        params={"provider": "nope", "symbol": "X", "asset_class": "crypto", "jesse_symbol": "X-USD"},
    )
    assert r.status_code == 200 and "Unknown provider" in r.text


async def test_update_unknown_asset_is_404(client):
    assert (await client.post("/assets/999/update")).status_code == 404


async def test_start_date_bounds_are_enforced(client, clock):
    r = await client.post("/assets", data=FORM | {"start_date": "2024-01-02"})
    assert r.status_code == 400 and "future" in r.text
    r = await client.post("/assets", data=FORM | {"start_date": "2023-12-31"})
    assert r.status_code == 400 and "2024-01-01" in r.text
    r = await client.post("/assets", data=FORM | {"provider_symbol": "OK", "jesse_symbol": "OK-USD", "start_date": "2024-01-01"})
    assert r.status_code == 303


async def test_search_input_blocks_enter_submit(client):
    assert "event.preventDefault()" in (await client.get("/assets/new")).text

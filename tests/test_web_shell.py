from app.providers.base import ProviderRegistry
from app.services import jobs
from app.web import ui
from tests.fakes import FakeProvider, make_asset

STATIC = [
    "/static/app.css",
    "/static/app.js",
    "/static/favicon.svg",
    "/static/vendor/icons.svg",
    "/static/vendor/htmx-2.0.4.min.js",
    "/static/vendor/lightweight-charts-4.2.0.standalone.production.js",
    "/static/vendor/inter/inter-latin-wght-normal.woff2",
]


async def test_static_files_are_served(client):
    for path in STATIC:
        assert (await client.get(path)).status_code == 200, path


async def test_shell_has_sidebar_and_only_local_assets(client):
    page = (await client.get("/settings")).text
    assert "Asuras CSV" in page and 'class="sidebar"' in page
    assert f"/static/app.css?v={ui.STATIC_VERSION}" in page and "/static/vendor/htmx-2.0.4.min.js" in page
    for href in ('href="/"', 'href="/assets"', 'href="/jobs"', 'href="/settings"'):
        assert href in page
    assert "unpkg.com" not in page and "cdn.jsdelivr.net" not in page and "pico" not in page


async def test_nav_status_shows_active_job_count_and_schedule(client, sf, clock):
    idle = (await client.get("/nav/status")).text
    assert 'id="nav-status"' in idle and "Scheduler off" in idle
    assert '<span id="nav-jobs-count" class="nav-count" hx-swap-oob="true" hidden></span>' in idle
    asset = await make_asset(sf)
    await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, asset.id, "backfill")
    busy = (await client.get("/nav/status")).text
    assert '<span id="nav-jobs-count" class="nav-count" hx-swap-oob="true">1</span>' in busy

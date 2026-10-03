from urllib.parse import quote

import pytest

from app.providers.base import ProviderRegistry
from app.services import jobs
from app.web import ui
from tests.fakes import FakeProvider, make_asset


@pytest.mark.parametrize(
    "value, expected",
    [
        ("/assets", "/assets"),
        ("/assets/3#chart", "/assets/3#chart"),
        ("/jobs?status=active", "/jobs?status=active"),
        ("//evil.com", "/d"),
        ("/\\evil.com", "/d"),
        ("https://evil.com", "/d"),
        ("", "/d"),
        (None, "/d"),
    ],
)
def test_safe_next(value, expected):
    assert ui.safe_next(value, "/d") == expected


async def test_post_sets_flash_and_next_page_shows_and_clears_it(client, sf):
    asset = await make_asset(sf)
    r = await client.post(f"/assets/{asset.id}/update", data={"next": "/jobs"})
    assert r.status_code == 303 and r.headers["location"] == "/jobs"
    assert "flash=" in r.headers["set-cookie"] and "Max-Age=30" in r.headers["set-cookie"]
    assert client.cookies.get("flash")
    poll = await client.get("/nav/status", headers={"HX-Request": "true"})
    assert "set-cookie" not in poll.headers  # htmx fragments never consume the toast
    page = await client.get("/jobs")
    assert 'class="toast"' in page.text and "Update queued for FAKE-USD" in page.text
    assert client.cookies.get("flash") is None
    assert 'class="toast"' not in (await client.get("/jobs")).text


async def test_flash_is_escaped(client):
    client.cookies.set("flash", quote("<b>hi</b>", safe=""))
    page = (await client.get("/jobs")).text
    assert "&lt;b&gt;hi&lt;/b&gt;" in page and "<b>hi</b>" not in page


@pytest.mark.parametrize("target", ["//evil.com", "https://evil.com"])
async def test_update_rejects_offsite_next(client, sf, target):
    asset = await make_asset(sf)
    r = await client.post(f"/assets/{asset.id}/update", data={"next": target})
    assert r.headers["location"] == "/assets"
    r = await client.post("/assets/update-all", data={"next": target})
    assert r.headers["location"] == "/"


async def test_update_all_reports_how_many_assets_were_queued(client, sf):
    await make_asset(sf)
    r = await client.post("/assets/update-all")
    assert "Update%20queued%20for%201%20asset" in r.headers["set-cookie"]


async def test_errors_render_as_a_styled_page(client, sf):
    stale = await make_asset(sf, provider="gone", provider_symbol="OLD", jesse_symbol="OLD-USD")
    r = await client.post(f"/assets/{stale.id}/update")
    assert r.status_code == 400 and "alert-danger" in r.text and "gone" in r.text
    assert 'href="/assets"' in r.text and 'class="sidebar"' in r.text


async def test_status_pill_shows_progress_and_errors(client, sf, clock):
    registry = ProviderRegistry([FakeProvider(clock)])
    asset = await make_asset(sf)
    await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    rows = (await client.get("/assets/rows")).text
    assert "badge-info" in rows and ">queued" in rows and 'class="progress"' in rows
    job_id = await jobs.claim_next(sf, clock)
    await jobs.fail(sf, clock, job_id, "HTTP 451 from upstream", 0.0)
    rows = (await client.get("/assets/rows")).text
    assert "badge-danger" in rows and 'class="status-error">HTTP 451 from upstream' in rows

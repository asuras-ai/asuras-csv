import pytest

from app.providers.base import ProviderRegistry
from app.services import jobs
from tests.fakes import FakeProvider, make_asset


def registry(clock):
    return ProviderRegistry([FakeProvider(clock)])


# --- toast wording reports what actually happened ---


async def test_update_one_says_when_an_update_was_queued_running_or_unneeded(client, sf, clock):
    stale = await make_asset(sf)
    r = await client.post(f"/assets/{stale.id}/update")
    assert "Update%20queued%20for%20FAKE-USD" in r.headers["set-cookie"]
    r = await client.post(f"/assets/{stale.id}/update")
    assert "FAKE-USD%20is%20already%20updating" in r.headers["set-cookie"]
    fresh = await make_asset(sf, provider_symbol="NEW", jesse_symbol="NEW-USD", fetched_until=clock.now())
    r = await client.post(f"/assets/{fresh.id}/update")
    assert "NEW-USD%20is%20already%20up%20to%20date" in r.headers["set-cookie"]


async def test_update_all_breaks_down_queued_running_and_up_to_date(client, sf, clock):
    running = await make_asset(sf)
    await jobs.enqueue(sf, registry(clock), clock, running.id, "backfill")
    await make_asset(sf, provider_symbol="B", jesse_symbol="B-USD")
    await make_asset(sf, provider_symbol="C", jesse_symbol="C-USD")
    await make_asset(sf, provider_symbol="D", jesse_symbol="D-USD", fetched_until=clock.now())
    r = await client.post("/assets/update-all")
    assert "Update%20queued%20for%202%20assets%20%C2%B7%201%20already%20running%20%C2%B7%201%20up%20to%20date" in r.headers["set-cookie"]


async def test_update_all_with_nothing_to_do(client, sf, clock):
    await make_asset(sf, fetched_until=clock.now())
    r = await client.post("/assets/update-all")
    assert "No%20updates%20queued%20%C2%B7%201%20up%20to%20date" in r.headers["set-cookie"]


# --- assets list: status groups and the checkbox cell ---


async def test_status_filter_separates_done_from_idle(client, sf, clock):
    done = await make_asset(sf)
    job = await jobs.enqueue(sf, registry(clock), clock, done.id, "backfill")
    assert await jobs.claim_next(sf, clock) == job.id
    await jobs.finish(sf, clock, job.id, 1.0)
    cancelled = await make_asset(sf, provider_symbol="X", jesse_symbol="X-USD")
    other = await jobs.enqueue(sf, registry(clock), clock, cancelled.id, "backfill")
    await jobs.cancel(sf, clock, other.id)
    await make_asset(sf, provider_symbol="N", jesse_symbol="N-USD")  # never ran
    page = (await client.get("/assets")).text
    assert '<option value="done">Done</option>' in page
    assert page.count('data-status="done"') == 1 and page.count('data-status="idle"') == 2


async def test_checkbox_cell_is_a_label_so_near_misses_toggle_instead_of_navigating(client, sf):
    asset = await make_asset(sf)
    rows = (await client.get("/assets/rows")).text
    assert f'<td class="select-cell"><label for="sel-{asset.id}">' in rows
    script = (await client.get("/static/app.js")).text
    assert ".select-cell" in script


# --- jobs tabs refresh with the rows ---


async def test_jobs_rows_refresh_the_tab_counts_out_of_band(client, sf, clock):
    asset = await make_asset(sf)
    await jobs.enqueue(sf, registry(clock), clock, asset.id, "backfill")
    page = (await client.get("/jobs")).text
    assert page.count('id="job-tabs"') == 1 and 'id="job-tabs" class="segmented" hx-swap-oob' not in page
    rows = (await client.get("/jobs/rows", params={"status": "active"})).text
    assert '<nav id="job-tabs" class="segmented" hx-swap-oob="true"' in rows
    assert 'Active <span class="count">1</span>' in rows and 'href="/jobs?status=active" class="active"' in rows


# --- keyboard focus survives polling in browsers without moveBefore ---


async def test_rows_poll_skips_the_swap_while_keyboard_focus_is_inside(client):
    script = (await client.get("/static/app.js")).text
    assert "htmx:beforeSwap" in script and ":focus-visible" in script


# --- 404s ---


@pytest.mark.parametrize("path", ["/assets/abc/chart", "/assets/abc/export", "/assets/abc/edit", "/assets/abc/delete", "/assets/abc/export.csv"])
async def test_non_numeric_asset_paths_are_404(client, path):
    assert (await client.get(path, headers={"Accept": "text/html"})).status_code == 404


async def test_browser_404_renders_the_styled_page(client):
    r = await client.get("/assets/999", headers={"Accept": "text/html,application/xhtml+xml"})
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "Asset not found" in r.text and 'class="sidebar"' in r.text
    r = await client.get("/no-such-page", headers={"Accept": "text/html"})
    assert r.status_code == 404 and "Page not found" in r.text


async def test_api_and_htmx_errors_stay_json(client, sf):
    asset = await make_asset(sf)
    r = await client.get("/assets/999/candles.json", headers={"Accept": "application/json"})
    assert r.status_code == 404 and r.json() == {"detail": "Asset not found"}
    r = await client.get("/assets/999/live", headers={"Accept": "text/html", "HX-Request": "true"})
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    r = await client.get(f"/assets/{asset.id}/candles.json?range=5Y", headers={"Accept": "text/html"})
    assert r.status_code == 422


async def test_old_redirect_targets_still_work(client, sf):
    asset = await make_asset(sf)
    r = await client.get(f"/assets/{asset.id}/chart")
    assert r.status_code == 303 and r.headers["location"] == f"/assets/{asset.id}#chart"
    assert (await client.get(f"/assets/{asset.id}/export.csv", params={"start": "2025-01-01"})).status_code == 404

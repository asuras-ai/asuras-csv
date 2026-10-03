import httpx

from app.config import EnvConfig
from app.main import create_app
from app.providers.base import ProviderRegistry
from tests.fakes import FakeProvider


async def test_add_page_lists_sources_as_radio_cards(client):
    page = (await client.get("/assets/new")).text
    assert 'type="radio" name="search_provider" value="fake" checked' in page and "No key needed" in page
    assert 'hx-include="[name=search_provider]:checked"' in page and "event.preventDefault()" in page
    assert 'id="details"' in page and "Pick a symbol above" in page
    assert '<a class="crumb" href="/assets">' in page


async def test_search_results_are_a_list(client):
    r = await client.get("/assets/search", params={"search_provider": "fake", "q": "fake"})
    assert 'class="result-list"' in r.text and "Fake/USD" in r.text


async def test_add_page_flags_missing_keys(sf, clock):
    keyed = FakeProvider(clock)
    keyed.name, keyed.label = "alpaca", "Alpaca (US stocks & ETFs)"
    env = EnvConfig(_env_file=None, database_url="postgresql+asyncpg://unused@localhost/unused", alpaca_key_id="", alpaca_secret_key="")
    app = create_app(env, sf=sf, clock=clock, registry=ProviderRegistry([keyed]), start_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        page = (await c.get("/assets/new")).text
    assert "Key missing" in page and "US stocks &amp; ETFs" in page


async def test_rejected_asset_shows_the_error_on_the_page(client):
    form = {"provider": "fake", "provider_symbol": "FAKEUSD", "asset_class": "crypto", "jesse_symbol": "bad symbol", "start_date": "2024-01-01"}
    r = await client.post("/assets", data=form)
    assert r.status_code == 400 and "alert-danger" in r.text and "Jesse symbol" in r.text

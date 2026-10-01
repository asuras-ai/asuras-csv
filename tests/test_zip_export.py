import io
import re
import zipfile
from datetime import UTC, datetime, timedelta

from app.domain import Candle
from app.services.sync import insert_candles
from tests.fakes import make_asset

T0 = datetime(2024, 1, 1, tzinfo=UTC)


async def seed(sf, asset_id, days=(0,), minutes=(0, 1)):
    async with sf.begin() as s:
        await insert_candles(
            s, asset_id, [Candle(T0 + timedelta(days=d, minutes=m), 1, 2, 0.5, 1.5, 10 + d) for d in days for m in minutes]
        )


async def two_assets(sf):
    a = await make_asset(sf)
    b = await make_asset(sf, provider_symbol="OTHER", jesse_symbol="OTHER-USD")
    await seed(sf, a.id, days=(0, 1))
    await seed(sf, b.id, days=(1, 2))
    return a, b


def open_zip(r) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(r.content))


async def test_zip_contains_one_csv_per_asset_equal_to_single_export(client, sf):
    a, b = await two_assets(sf)
    r = await client.get("/export.zip", params={"ids": [a.id, b.id]})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = open_zip(r)
    assert sorted(z.namelist()) == ["FAKE-USD_2024-01-01_2024-01-02.csv", "OTHER-USD_2024-01-02_2024-01-03.csv"]
    for asset in (a, b):
        single = await client.get(f"/assets/{asset.id}/export.csv")
        name = re.search(r'filename="(.+)"', single.headers["content-disposition"]).group(1)
        assert z.read(name).decode() == single.text


async def test_zip_respects_date_range(client, sf):
    a, b = await two_assets(sf)
    r = await client.get("/export.zip", params={"ids": [a.id, b.id], "start": "2024-01-02", "end": "2024-01-02"})
    z = open_zip(r)
    assert sorted(z.namelist()) == ["FAKE-USD_2024-01-02_2024-01-02.csv", "OTHER-USD_2024-01-02_2024-01-02.csv"]
    assert len(z.read(z.namelist()[0]).decode().splitlines()) == 3


async def test_zip_skips_assets_without_data(client, sf):
    a, b = await two_assets(sf)
    empty = await make_asset(sf, provider_symbol="EMPTY", jesse_symbol="EMPTY-USD")
    r = await client.get("/export.zip", params={"ids": [a.id, empty.id, b.id], "start": "2024-01-03"})
    assert open_zip(r).namelist() == ["OTHER-USD_2024-01-03_2024-01-03.csv"]


async def test_zip_404_when_no_data_or_unknown_or_bad_input(client, sf):
    a, _ = await two_assets(sf)
    r = await client.get("/export.zip", params={"ids": [a.id], "start": "2030-01-01"})
    assert r.status_code == 404 and "No candles" in r.text
    assert (await client.get("/export.zip", params={"ids": [a.id, 999]})).status_code == 404
    empty = await client.get("/export.zip")
    assert empty.status_code == 400 and empty.headers["content-type"].startswith("text/html")
    assert "Select at least one asset" in empty.text and "<" in empty.text
    assert r.headers["content-type"].startswith("text/html")
    assert (await client.get("/export.zip", params={"ids": [a.id], "start": "nope"})).status_code == 400


async def test_zip_filename_header(client, sf):
    a, _ = await two_assets(sf)
    r = await client.get("/export.zip", params={"ids": [a.id]})
    assert re.fullmatch(r'attachment; filename="ohlcv-export-\d{8}-\d{6}\.zip"', r.headers["content-disposition"])


async def test_zip_duplicate_names_are_disambiguated(client, sf):
    a = await make_asset(sf)
    b = await make_asset(sf, provider_symbol="DUP")
    await seed(sf, a.id)
    await seed(sf, b.id)
    names = open_zip(await client.get("/export.zip", params={"ids": [a.id, b.id]})).namelist()
    assert len(names) == 2 and len(set(names)) == 2


async def test_assets_page_has_selection_checkboxes_that_survive_polling(client, sf):
    a = await make_asset(sf)
    for url in ("/", "/assets/rows"):
        html = (await client.get(url)).text
        assert f'name="ids" value="{a.id}"' in html and f'id="sel-{a.id}"' in html and "hx-preserve" in html
    page = (await client.get("/")).text
    assert 'id="zip-form"' in page and 'action="/export.zip"' in page and 'method="get"' in page


async def test_zip_form_has_optional_date_inputs_and_empty_selection_guard(client, sf):
    await make_asset(sf)
    page = (await client.get("/")).text
    assert 'name="start"' in page and 'name="end"' in page
    assert "Select at least one asset" in page  # inline JS guard message


async def test_zip_temp_file_is_closed_by_a_background_task(client, sf, monkeypatch):
    import tempfile

    created = []
    real = tempfile.SpooledTemporaryFile

    def tracking(*args, **kwargs):
        f = real(*args, **kwargs)
        created.append(f)
        return f

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", tracking)
    a, _ = await two_assets(sf)
    r = await client.get("/export.zip", params={"ids": [a.id]})
    assert r.status_code == 200 and created and all(f.closed for f in created)

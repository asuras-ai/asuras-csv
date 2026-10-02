from datetime import UTC, datetime, timedelta

import pytest

from app.domain import Candle
from app.services.charts import MAX_BARS, RANGES, pick_bucket
from app.services.sync import insert_candles
from tests.fakes import make_asset

T0 = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)


async def seed(sf, asset_id, candles):
    async with sf.begin() as s:
        await insert_candles(s, asset_id, candles)


@pytest.mark.parametrize(
    "rng,minutes", [("1D", 1), ("1W", 5), ("1M", 30), ("6M", 240), ("1Y", 1440), ("All", 1440)]
)
def test_bucket_by_range(rng, minutes):
    assert pick_bucket(rng, span_days=100) == timedelta(minutes=minutes)


def test_all_uses_weekly_buckets_beyond_cap():
    assert pick_bucket("All", span_days=MAX_BARS) == timedelta(days=1)
    assert pick_bucket("All", span_days=MAX_BARS + 1) == timedelta(weeks=1)


def test_every_range_stays_near_the_cap():
    for name, (delta, bucket) in RANGES.items():
        if delta is not None:
            assert delta / bucket <= MAX_BARS * 1.05


async def test_aggregates_ten_minutes_into_one_bucket(client, sf):
    asset = await make_asset(sf)
    t = datetime(2024, 1, 1, 10, 0, tzinfo=UTC)  # on a 5m boundary
    candles = [Candle(t + timedelta(minutes=i), 10 + i, 20 + i, 5 + i, 11 + i, 1.5) for i in range(10)]
    await seed(sf, asset.id, candles)
    r = await client.get(f"/assets/{asset.id}/candles.json", params={"range": "1W"})
    body = r.json()
    assert body["interval"] == "5m"
    first, second = body["candles"]
    assert first == {"time": int(t.timestamp()), "open": 10, "high": 24, "low": 5, "close": 15, "volume": 7.5}
    assert second == {"time": int((t + timedelta(minutes=5)).timestamp()), "open": 15, "high": 29, "low": 10, "close": 20, "volume": 7.5}


async def test_ascending_range_end_and_invalid_rows(client, sf):
    asset = await make_asset(sf)
    end = datetime(2024, 3, 1, tzinfo=UTC)
    await seed(sf, asset.id, [
        Candle(end - timedelta(days=30), 1, 2, 1, 1, 1),  # outside 1W
        Candle(end - timedelta(days=1), 1, 2, 1, 1, 1),
        Candle(end - timedelta(days=2), 1, 2, 1, 1, 1),
        Candle(end, 1, 2, 1, 1, 1),
        Candle(end + timedelta(minutes=1), 1, 0.5, 1, 1, 1),  # invalid: must not define the range end
    ])
    times = [c["time"] for c in (await client.get(f"/assets/{asset.id}/candles.json?range=1W")).json()["candles"]]
    assert times == sorted(times) and len(times) == 3 and times[-1] == int(end.timestamp())
    all_ = (await client.get(f"/assets/{asset.id}/candles.json?range=All")).json()
    assert all_["interval"] == "1d" and len(all_["candles"]) == 4


async def test_all_is_capped_with_weekly_buckets(client, sf):
    asset = await make_asset(sf)
    start = datetime(2015, 1, 1, tzinfo=UTC)
    await seed(sf, asset.id, [Candle(start + timedelta(days=d), 1, 2, 1, 1, 1) for d in range(2500)])
    body = (await client.get(f"/assets/{asset.id}/candles.json?range=All")).json()
    assert body["interval"] == "1w" and 0 < len(body["candles"]) <= MAX_BARS
    assert sum(c["volume"] for c in body["candles"]) == 2500


async def test_chart_json_errors_and_empty(client, sf):
    asset = await make_asset(sf)
    assert (await client.get(f"/assets/{asset.id}/candles.json?range=5Y")).status_code == 422
    assert (await client.get("/assets/999/candles.json?range=1W")).status_code == 404
    assert (await client.get(f"/assets/{asset.id}/candles.json")).json() == {"interval": "5m", "candles": []}


async def test_chart_page(client, sf):
    asset = await make_asset(sf)
    page = await client.get(f"/assets/{asset.id}/chart")
    assert page.status_code == 200 and "/static/vendor/lightweight-charts-4.2.0.standalone.production.js" in page.text and f"/assets/{asset.id}/candles.json" in page.text
    for label in ("1D", "1W", "1M", "6M", "1Y", "All"):
        assert f'data-range="{label}"' in page.text
    assert (await client.get("/assets/999/chart")).status_code == 404
    assert f'/assets/{asset.id}/chart' in (await client.get("/")).text


async def test_one_day_window_has_exactly_1440_full_bars(client, sf):
    asset = await make_asset(sf)
    last = datetime(2024, 3, 3, 12, 0, tzinfo=UTC)
    await seed(sf, asset.id, [Candle(last - timedelta(minutes=m), 1, 2, 1, 1, 1) for m in range(3 * 1440)])
    body = (await client.get(f"/assets/{asset.id}/candles.json?range=1D")).json()
    assert len(body["candles"]) == 1440
    assert body["candles"][0]["time"] == int((last - timedelta(days=1) + timedelta(minutes=1)).timestamp())
    assert body["candles"][-1]["time"] == int(last.timestamp())


async def test_first_bucket_is_full_when_window_start_is_not_on_a_boundary(client, sf):
    asset = await make_asset(sf)
    last = datetime(2024, 3, 8, 10, 3, tzinfo=UTC)  # 1W window start 10:03 falls inside the 10:00 5m bucket
    await seed(sf, asset.id, [Candle(last - timedelta(minutes=m), 1, 2, 1, 1, 1) for m in range(9 * 1440)])
    body = (await client.get(f"/assets/{asset.id}/candles.json?range=1W")).json()
    first = body["candles"][0]
    assert first["time"] % 300 == 0 and first["volume"] == 5
    assert all(c["volume"] == 5 for c in body["candles"][:-1])


def test_valid_candle_is_public():
    from app.services.export import valid_candle

    assert callable(valid_candle)


async def test_chart_page_guards_missing_library_and_resize_listener(client, sf):
    asset = await make_asset(sf)
    text = (await client.get(f"/assets/{asset.id}/chart")).text
    assert "Chart library failed to load" in text
    assert text.count('addEventListener("resize"') == 1  # only the ResizeObserver fallback

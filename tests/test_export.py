from datetime import UTC, date, datetime, timedelta

from app.domain import Candle, to_ms
from app.services.export import day_bounds, export_filename, export_range, format_row, stream_csv
from app.services.sync import insert_candles
from tests.fakes import make_asset

T0 = datetime(2024, 1, 9, 14, 0, tzinfo=UTC)


def c(minute, o=1.0, h=2.0, l=0.5, cl=1.5, v=10.0, base=T0) -> Candle:
    return Candle(base + timedelta(minutes=minute), o, h, l, cl, v)


async def seed(sf, asset_id, candles):
    async with sf.begin() as s:
        await insert_candles(s, asset_id, candles)


async def read(sf, asset_id, start=None, end=None) -> str:
    return "".join([part async for part in stream_csv(sf, asset_id, start, end)])


async def test_csv_matches_jesse_format(sf):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [c(1, 181.40, 181.44, 181.20, 181.31, 9871), c(0, 181.25, 181.55, 181.10, 181.40, 12043)])
    assert await read(sf, asset.id) == (
        "timestamp,open,close,high,low,volume\n"
        "1704808800000,181.25,181.4,181.55,181.1,12043\n"
        "1704808860000,181.4,181.31,181.44,181.2,9871\n"
    )


async def test_invalid_rows_are_skipped_and_logged(sf, caplog):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [c(0), c(1, h=1.0), c(2, l=1.6), c(3, v=-1.0), c(4, o=float("nan")), c(5)])
    lines = (await read(sf, asset.id)).splitlines()
    assert [line.split(",")[0] for line in lines[1:]] == [str(to_ms(T0)), str(to_ms(T0 + timedelta(minutes=5)))]
    assert "skipped 4 invalid candles" in caplog.text


async def test_date_range_is_inclusive_by_day(sf):
    asset = await make_asset(sf)
    midnight = datetime(2024, 1, 2, tzinfo=UTC)
    await seed(sf, asset.id, [c(-1, base=midnight), c(0, base=midnight), c(24 * 60, base=midnight)])
    start, end = day_bounds(date(2024, 1, 2), date(2024, 1, 2))
    lines = (await read(sf, asset.id, start, end)).splitlines()
    assert lines[1:] == [f"{to_ms(midnight)},1,1.5,2,0.5,10"]
    rng = await export_range(sf, asset.id, start, end)
    assert (rng.first, rng.last) == (midnight, midnight)
    assert export_filename("FAKE-USD", rng) == "FAKE-USD_2024-01-02_2024-01-02.csv"
    assert await export_range(sf, asset.id, *day_bounds(date(2025, 1, 1), None)) is None


def test_numbers_never_use_scientific_notation():
    assert format_row(T0, 0.00000812, 0.00000813, 0.00000815, 0.0000081, 12_000_000_000.0) == (
        "1704808800000,0.00000812,0.00000813,0.00000815,0.0000081,12000000000\n"
    )

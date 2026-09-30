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


def test_tiny_prices_keep_their_digits():
    assert format_row(T0, 3.55e-9, 1e-7, 1.5e-7, 3.55e-9, 1.0).split(",")[1:] == [
        "0.00000000355", "0.0000001", "0.00000015", "0.00000000355", "1\n",
    ]


def test_negative_zero_is_never_emitted():
    line = format_row(T0, -0.0, -0.0, 0.0, -0.0, -0.0)
    assert line.split(",")[1:] == ["0", "0", "0", "0", "0\n"]
    assert "-0" not in line


async def test_export_range_uses_first_and_last_valid_candle(sf):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [c(0, h=0.1), c(1), c(2), c(3, v=-1.0), c(4, o=float("nan"))])
    rng = await export_range(sf, asset.id, None, None)
    assert (rng.first, rng.last) == (T0 + timedelta(minutes=1), T0 + timedelta(minutes=2))


async def test_export_range_rejects_infinity(sf):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [c(0), c(1, h=float("inf")), c(2, l=float("-inf"))])
    rng = await export_range(sf, asset.id, None, None)
    assert (rng.first, rng.last) == (T0, T0)


async def test_export_range_none_when_all_invalid(sf):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [c(0, h=0.1), c(1, v=-1.0), c(2, o=float("nan"))])
    assert await export_range(sf, asset.id, None, None) is None


async def test_early_close_releases_session_quietly(sf, caplog):
    import warnings

    asset = await make_asset(sf)
    await seed(sf, asset.id, [c(i) for i in range(5001 + 10)])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        gen = stream_csv(sf, asset.id, None, None)
        assert await anext(gen) == "timestamp,open,close,high,low,volume\n"
        assert len(await anext(gen)) > 0
        await gen.aclose()
        import gc

        gc.collect()
    assert not caught
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


async def test_more_than_one_flush_is_complete_and_ordered(sf):
    asset = await make_asset(sf)
    n = 5001
    await seed(sf, asset.id, [c(i) for i in reversed(range(n))])
    lines = (await read(sf, asset.id)).splitlines()
    stamps = [int(line.split(",")[0]) for line in lines[1:]]
    assert len(stamps) == n
    assert stamps == sorted(stamps) and len(set(stamps)) == n

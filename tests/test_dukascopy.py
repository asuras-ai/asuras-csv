import asyncio
import lzma
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.domain import HOUR, Candle, PermanentError, TransientError
from app.providers.dukascopy import POLICY, RECORD, DukascopyProvider, decode_hour, point_divisor
from app.providers.http import ProviderClient

HOUR0 = datetime(2024, 1, 3, 10, tzinfo=UTC)  # a Wednesday


def bi5(ticks: list[tuple[int, int]]) -> bytes:
    raw = b"".join(RECORD.pack(ms, bid + 2, bid, 1.5, 1.5) for ms, bid in ticks)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


@pytest.fixture
def provider(clock):
    return DukascopyProvider(ProviderClient(POLICY, httpx.AsyncClient(), clock), base_url="https://duka.test/datafeed")


def test_decode_aggregates_bid_ticks_into_minutes():
    data = bi5([(1_000, 110000), (30_000, 110050), (59_000, 109990), (61_000, 110020)])
    assert decode_hour(data, HOUR0, 100000.0) == [
        Candle(HOUR0, 1.1, 1.1005, 1.0999, 1.0999, 3.0),
        Candle(HOUR0 + timedelta(minutes=1), 1.1002, 1.1002, 1.1002, 1.1002, 1.0),
    ]


def test_jpy_pairs_use_three_decimals():
    assert point_divisor("USDJPY") == 1000.0
    assert point_divisor("EURUSD") == 100000.0


def test_empty_file_has_no_candles():
    assert decode_hour(b"", HOUR0, 100000.0) == []


def test_corrupt_file_is_transient():
    with pytest.raises(TransientError, match="corrupt"):
        decode_hour(b"not lzma at all", HOUR0, 100000.0)


async def test_fetch_downloads_hour_files_in_order(respx_mock, provider):
    files = {
        "/datafeed/EURUSD/2024/00/03/10h_ticks.bi5": bi5([(0, 110000)]),
        "/datafeed/EURUSD/2024/00/03/11h_ticks.bi5": bi5([(120_000, 110100)]),
    }
    respx_mock.get(host="duka.test").mock(
        side_effect=lambda r: httpx.Response(200, content=files[r.url.path])
        if r.url.path in files
        else httpx.Response(404)
    )
    chunks = [c async for c in provider.fetch("EURUSD", HOUR0, HOUR0 + 2 * HOUR)]
    assert [c.covered_until for c in chunks] == [HOUR0 + HOUR, HOUR0 + 2 * HOUR]
    assert [c.candles[0].ts for c in chunks] == [HOUR0, HOUR0 + timedelta(hours=1, minutes=2)]


async def test_missing_hours_and_saturdays_still_advance_the_cursor(respx_mock, provider):
    route = respx_mock.get(host="duka.test").mock(return_value=httpx.Response(404))
    start = datetime(2024, 1, 5, 23, tzinfo=UTC)  # Friday 23:00
    end = datetime(2024, 1, 6, 2, tzinfo=UTC)  # Saturday 02:00
    chunks = [c async for c in provider.fetch("EURUSD", start, end)]
    assert [c.covered_until for c in chunks] == [start + HOUR, start + 2 * HOUR, end]
    assert all(c.candles == [] for c in chunks)
    assert route.call_count == 1  # Saturday hours are never requested


def test_available_until_leaves_two_hours_for_publishing(provider):
    now = datetime(2024, 1, 3, 12, 40, tzinfo=UTC)
    assert provider.available_until(now) == datetime(2024, 1, 3, 10, tzinfo=UTC)


def test_estimate_skips_saturdays(provider):
    start = datetime(2024, 1, 1, tzinfo=UTC)
    assert provider.estimate_requests(start, start + timedelta(days=7)) == 144
    assert provider.estimate_requests(start, start) == 0


async def test_search_and_earliest(provider):
    [info] = await provider.search_symbols("eurusd")
    assert (info.provider_symbol, info.suggested_jesse_symbol, info.asset_class) == ("EURUSD", "EUR-USD", "forex")
    assert await provider.earliest_available("EURUSD") == datetime(2004, 1, 1, tzinfo=UTC)
    with pytest.raises(PermanentError):
        await provider.earliest_available("XXXYYY")


def test_tick_offset_past_the_hour_is_corrupt():
    with pytest.raises(TransientError, match="corrupt"):
        decode_hour(bi5([(1_000, 110000), (3_600_000, 110010)]), HOUR0, 100000.0)


def _hour_path(hour: datetime) -> str:
    return f"/datafeed/EURUSD/{hour.year}/{hour.month - 1:02d}/{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"


async def _spin_until(predicate) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never reached")


async def test_out_of_order_completion_keeps_hour_order(respx_mock, provider):
    second_arrived = asyncio.Event()
    files = {
        _hour_path(HOUR0): bi5([(0, 110000)]),
        _hour_path(HOUR0 + HOUR): bi5([(0, 110100)]),
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _hour_path(HOUR0):
            await second_arrived.wait()
        else:
            second_arrived.set()
        return httpx.Response(200, content=files[request.url.path])

    respx_mock.get(host="duka.test").mock(side_effect=handler)
    chunks = [c async for c in provider.fetch("EURUSD", HOUR0, HOUR0 + 2 * HOUR)]
    assert [c.candles[0].ts for c in chunks] == [HOUR0, HOUR0 + HOUR]
    assert [c.candles[0].close for c in chunks] == [1.1, 1.101]
    assert [c.covered_until for c in chunks] == [HOUR0 + HOUR, HOUR0 + 2 * HOUR]


async def test_failing_hour_cancels_the_rest(respx_mock, provider):
    never = asyncio.Event()
    cancelled: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _hour_path(HOUR0):
            return httpx.Response(200, content=b"not lzma at all")
        try:
            await never.wait()
        except asyncio.CancelledError:
            cancelled.append(request.url.path)
            raise
        return httpx.Response(404)

    respx_mock.get(host="duka.test").mock(side_effect=handler)
    before = asyncio.all_tasks()
    with pytest.raises(TransientError, match="corrupt"):
        async for _ in provider.fetch("EURUSD", HOUR0, HOUR0 + 3 * HOUR):
            pass
    assert asyncio.all_tasks() - before == set()
    assert not never.is_set()
    for t in cancelled:
        assert t in {_hour_path(HOUR0 + HOUR), _hour_path(HOUR0 + 2 * HOUR)}


async def test_early_close_cancels_prefetched_downloads(respx_mock, provider):
    never = asyncio.Event()
    started: list[str] = []
    cancelled: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _hour_path(HOUR0):
            return httpx.Response(200, content=bi5([(0, 110000)]))
        started.append(request.url.path)
        try:
            await never.wait()
        except asyncio.CancelledError:
            cancelled.append(request.url.path)
            raise
        return httpx.Response(404)

    respx_mock.get(host="duka.test").mock(side_effect=handler)
    before = asyncio.all_tasks()
    agen = provider.fetch("EURUSD", HOUR0, HOUR0 + 3 * HOUR)
    first = await agen.__anext__()
    assert first.covered_until == HOUR0 + HOUR
    await _spin_until(lambda: len(started) == 2)
    await agen.aclose()
    assert sorted(cancelled) == sorted(started)
    assert asyncio.all_tasks() - before == set()


async def test_start_and_end_clip_the_candles(respx_mock, provider):
    respx_mock.get(host="duka.test").mock(
        return_value=httpx.Response(200, content=bi5([(5 * 60_000, 110000), (40 * 60_000, 110200)]))
    )
    start = HOUR0 + timedelta(minutes=30)
    end = HOUR0 + timedelta(hours=1)
    chunks = [c async for c in provider.fetch("EURUSD", start, end)]
    assert len(chunks) == 1
    assert [c.ts for c in chunks[0].candles] == [HOUR0 + timedelta(minutes=40)]
    assert chunks[0].covered_until == end


async def test_start_and_end_clip_within_a_single_hour(respx_mock, provider):
    respx_mock.get(host="duka.test").mock(
        return_value=httpx.Response(200, content=bi5([(5 * 60_000, 110000), (40 * 60_000, 110200)]))
    )
    start = HOUR0 + timedelta(minutes=30)
    end = HOUR0 + timedelta(minutes=50)
    [chunk] = [c async for c in provider.fetch("EURUSD", start, end)]
    assert [c.ts for c in chunk.candles] == [HOUR0 + timedelta(minutes=40)]
    assert chunk.covered_until == end


async def test_jpy_prices_are_scaled_through_fetch(respx_mock, provider):
    respx_mock.get(host="duka.test").mock(return_value=httpx.Response(200, content=bi5([(0, 150123)])))
    [chunk] = [c async for c in provider.fetch("USDJPY", HOUR0, HOUR0 + HOUR)]
    assert chunk.candles[0].close == 150.123

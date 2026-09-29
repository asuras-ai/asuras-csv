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


def test_available_until_leaves_a_full_hour_for_publishing(provider):
    now = datetime(2024, 1, 3, 12, 40, tzinfo=UTC)
    assert provider.available_until(now) == datetime(2024, 1, 3, 11, tzinfo=UTC)


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

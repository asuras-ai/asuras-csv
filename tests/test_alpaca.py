from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.domain import PermanentError
from app.providers.alpaca import POLICY, AlpacaProvider, alpaca_quota_delay
from app.providers.http import ProviderClient

CALENDAR = [
    {"date": "2024-03-08", "open": "09:30", "close": "16:00"},  # Friday, EST
    {"date": "2024-03-11", "open": "09:30", "close": "16:00"},  # Monday, EDT (DST began 03-10)
    {"date": "2024-11-29", "open": "09:30", "close": "13:00"},  # half-day after Thanksgiving
]


def make_provider(clock, key=("KEY", "SECRET")) -> AlpacaProvider:
    async def credentials():
        return key

    client = ProviderClient(POLICY, httpx.AsyncClient(), clock)
    return AlpacaProvider(client, credentials, trading_url="https://trading.test", data_url="https://data.test")


def bar(t: str) -> dict:
    return {"t": t, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100, "n": 3, "vw": 1.2}


def mock_calendar(respx_mock, status=200, body=None):
    return respx_mock.get(host="trading.test", path="/v2/calendar").mock(
        return_value=httpx.Response(status, json=CALENDAR if body is None else body)
    )


async def test_keeps_only_regular_session_bars_across_dst_and_half_days(respx_mock, clock):
    mock_calendar(respx_mock)
    times = [
        "2024-03-08T14:29:00Z", "2024-03-08T14:30:00Z", "2024-03-08T20:59:00Z", "2024-03-08T21:00:00Z",
        "2024-03-09T15:00:00Z",  # Saturday: not in calendar
        "2024-03-11T13:29:00Z", "2024-03-11T13:30:00Z", "2024-03-11T19:59:00Z", "2024-03-11T20:00:00Z",
        "2024-11-29T17:59:00Z", "2024-11-29T18:00:00Z",
    ]
    respx_mock.get(host="data.test", path="/v2/stocks/AAPL/bars").mock(
        return_value=httpx.Response(200, json={"bars": [bar(t) for t in times], "next_page_token": None})
    )
    end = datetime(2024, 12, 1, tzinfo=UTC)
    chunks = [c async for c in make_provider(clock).fetch("AAPL", datetime(2024, 3, 8, tzinfo=UTC), end)]
    kept = [c.ts.strftime("%Y-%m-%dT%H:%M") for c in chunks[0].candles]
    assert kept == [
        "2024-03-08T14:30", "2024-03-08T20:59",
        "2024-03-11T13:30", "2024-03-11T19:59",
        "2024-11-29T17:59",
    ]
    assert chunks[0].covered_until == end


async def test_follows_pages_from_the_last_bar(respx_mock, clock):
    mock_calendar(respx_mock)
    route = respx_mock.get(host="data.test", path="/v2/stocks/AAPL/bars").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"bars": [bar("2024-03-08T14:30:00Z"), bar("2024-03-08T14:31:00Z")], "next_page_token": "abc"},
            ),
            httpx.Response(200, json={"bars": None, "next_page_token": None}),
        ]
    )
    start, end = datetime(2024, 3, 8, tzinfo=UTC), datetime(2024, 3, 9, tzinfo=UTC)
    chunks = [c async for c in make_provider(clock).fetch("AAPL", start, end)]
    assert [c.covered_until for c in chunks] == [datetime(2024, 3, 8, 14, 32, tzinfo=UTC), end]
    first, second = (call.request.url.params for call in route.calls)
    assert first["start"] == "2024-03-08T00:00:00Z"
    assert first["end"] == "2024-03-08T23:59:59Z"
    assert (first["feed"], first["timeframe"], first["adjustment"], first["limit"]) == ("iex", "1Min", "raw", "10000")
    assert second["start"] == "2024-03-08T14:32:00Z"
    assert route.calls[0].request.headers["APCA-API-KEY-ID"] == "KEY"


async def test_missing_key_is_permanent_and_sends_nothing(respx_mock, clock):
    provider = make_provider(clock, key=("", ""))
    with pytest.raises(PermanentError, match="API key missing"):
        [c async for c in provider.fetch("AAPL", datetime(2024, 3, 8, tzinfo=UTC), datetime(2024, 3, 9, tzinfo=UTC))]
    assert not respx_mock.calls


async def test_rejected_key_is_permanent(respx_mock, clock):
    mock_calendar(respx_mock, status=403, body={"message": "forbidden"})
    with pytest.raises(PermanentError, match="API key rejected"):
        [c async for c in make_provider(clock).fetch("AAPL", datetime(2024, 3, 8, tzinfo=UTC), datetime(2024, 3, 9, tzinfo=UTC))]


async def test_search_classifies_etfs(respx_mock, clock):
    respx_mock.get(host="trading.test", path="/v2/assets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "tradable": True},
                {"symbol": "SPOT", "name": "Spotify Technology S.A.", "tradable": True},
                {"symbol": "SPXX", "name": "Old", "tradable": False},
            ],
        )
    )
    results = await make_provider(clock).search_symbols("sp")
    assert [(s.provider_symbol, s.asset_class, s.suggested_jesse_symbol) for s in results] == [
        ("SPOT", "stock", "SPOT-USD"),
        ("SPY", "etf", "SPY-USD"),
    ]


def test_quota_delay_waits_for_reset_when_nearly_exhausted():
    now = datetime(2024, 1, 1, tzinfo=UTC)
    reset = str(int(now.timestamp()) + 20)
    low = httpx.Response(200, headers={"X-RateLimit-Remaining": "3", "X-RateLimit-Reset": reset})
    plenty = httpx.Response(200, headers={"X-RateLimit-Remaining": "150", "X-RateLimit-Reset": reset})
    assert alpaca_quota_delay(low, now) == 20.0
    assert alpaca_quota_delay(plenty, now) == 0.0


def test_estimate_counts_calendar_plus_bar_pages(clock):
    start = datetime(2016, 1, 1, tzinfo=UTC)
    provider = make_provider(clock)
    assert provider.estimate_requests(start, start + timedelta(days=365 * 8)) == 83
    assert provider.estimate_requests(start, start) == 0


def test_available_until_lags_two_minutes(clock):
    provider = make_provider(clock)
    now = datetime(2024, 5, 1, 15, 30, 45, tzinfo=UTC)
    assert provider.available_until(now) == datetime(2024, 5, 1, 15, 28, tzinfo=UTC)

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.domain import PermanentError, RateLimited, TransientError
from app.providers.http import ProviderClient
from app.providers.twelvedata import PAGE, POLICY, PUBLISH_LAG, SEARCH_POLICY, TwelveDataProvider
from tests.fakes import FakeClock

HOST = "api.twelvedata.com"
T0 = datetime(2024, 1, 8, tzinfo=UTC)  # a Monday
KEY = "KEY123"


def make_provider(clock, key=KEY) -> TwelveDataProvider:
    async def credentials():
        return key

    http = httpx.AsyncClient()
    return TwelveDataProvider(
        ProviderClient(POLICY, http, clock), credentials, search_client=ProviderClient(SEARCH_POLICY, http, clock)
    )


def stamp(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def value(ts: datetime, o="1.1", h="1.3", low="1.0", c="1.2", volume=None) -> dict:
    v = {"datetime": stamp(ts), "open": o, "high": h, "low": low, "close": c}
    if volume is not None:
        v["volume"] = volume
    return v


def series_route(respx_mock, *responses):
    return respx_mock.get(host=HOST, path="/time_series").mock(
        side_effect=[httpx.Response(200, json={"meta": {"symbol": "XAU/USD"}, "values": r, "status": "ok"}) for r in responses]
    )


def error_body(code, message):
    return {"code": code, "message": message, "status": "error"}


async def collect(provider, start, end, symbol="XAU/USD"):
    return [c async for c in provider.fetch(symbol, start, end)]


async def test_windows_of_4999_minutes_with_exact_params(respx_mock, clock):
    route = series_route(respx_mock, [], [], [])
    end = T0 + timedelta(minutes=12_000)
    chunks = await collect(make_provider(clock), T0, end)
    w1, w2 = T0 + timedelta(minutes=PAGE), T0 + timedelta(minutes=2 * PAGE)
    assert [c.covered_until for c in chunks] == [w1, w2, end]
    fmt = "%Y-%m-%dT%H:%M:%S"
    last = timedelta(minutes=1)
    assert [dict(call.request.url.params) for call in route.calls] == [
        {
            "symbol": "XAU/USD",
            "interval": "1min",
            "start_date": s.strftime(fmt),
            "end_date": (e - last).strftime(fmt),
            "timezone": "UTC",
            "order": "asc",
            "outputsize": "5000",
            "apikey": KEY,
        }
        for s, e in [(T0, w1), (w1, w2), (w2, end)]
    ]


async def test_parses_string_prices_naive_utc_and_missing_volume_as_zero(respx_mock, clock):
    series_route(respx_mock, [value(T0, "1.10001", "1.2", "1.0", "1.15"), value(T0 + timedelta(minutes=1), volume="3082")])
    [chunk] = await collect(make_provider(clock), T0, T0 + timedelta(minutes=10))
    a, b = chunk.candles
    assert (a.ts, a.open, a.high, a.low, a.close, a.volume) == (T0, 1.10001, 1.2, 1.0, 1.15, 0.0)
    assert a.ts.tzinfo is not None and a.ts.utcoffset() == timedelta(0)
    assert b.volume == 3082.0


async def test_out_of_order_and_duplicate_rows_inside_the_window_are_sorted_and_deduped(respx_mock, clock):
    end = T0 + timedelta(minutes=10)
    t = [T0 + timedelta(minutes=i) for i in range(3)]
    series_route(respx_mock, [value(t[2]), value(t[1]), value(t[1], c="9.9"), value(t[0])])
    [chunk] = await collect(make_provider(clock), T0, end)
    assert [c.ts for c in chunk.candles] == t
    assert chunk.covered_until == end


@pytest.mark.parametrize("offset", [-1, 10])
async def test_rows_outside_the_window_raise_transient_instead_of_being_clipped(respx_mock, clock, offset):
    end = T0 + timedelta(minutes=10)
    series_route(respx_mock, [value(T0), value(T0 + timedelta(minutes=offset))])
    chunks = []
    with pytest.raises(TransientError, match="outside the requested window"):
        async for chunk in make_provider(clock).fetch("XAU/USD", T0, end):
            chunks.append(chunk)
    assert chunks == []  # nothing yielded, so the cursor cannot move


@pytest.mark.parametrize("as_http_status", [True, False])
async def test_no_data_error_is_an_empty_chunk_that_advances_the_cursor(respx_mock, clock, as_http_status):
    body = error_body(400, "**start_date** ... No data is available on the specified dates. Try setting different start/end dates.")
    route = respx_mock.get(host=HOST, path="/time_series").mock(
        side_effect=[httpx.Response(400 if as_http_status else 200, json=body), httpx.Response(200, json={"values": [value(T0 + timedelta(minutes=PAGE))], "status": "ok"})]
    )
    end = T0 + timedelta(minutes=PAGE + 10)
    chunks = await collect(make_provider(clock), T0, end)
    assert route.call_count == 2
    assert chunks[0].candles == [] and chunks[0].covered_until == T0 + timedelta(minutes=PAGE)
    assert len(chunks[1].candles) == 1 and chunks[1].covered_until == end


async def test_no_data_match_is_case_insensitive(respx_mock, clock):
    respx_mock.get(host=HOST, path="/time_series").mock(
        return_value=httpx.Response(400, json=error_body(400, "no data is available"))
    )
    [chunk] = await collect(make_provider(clock), T0, T0 + timedelta(minutes=5))
    assert chunk.candles == []


async def test_other_400s_are_permanent(respx_mock, clock):
    respx_mock.get(host=HOST, path="/time_series").mock(return_value=httpx.Response(400, json=error_body(400, "bad symbol")))
    with pytest.raises(PermanentError, match="request rejected"):
        await collect(make_provider(clock), T0, T0 + timedelta(minutes=5))


@pytest.mark.parametrize("status", [401, 200])
async def test_rejected_key_is_permanent(respx_mock, clock, status):
    respx_mock.get(host=HOST, path="/time_series").mock(
        return_value=httpx.Response(status, json=error_body(401, "**apikey** parameter is incorrect"))
    )
    with pytest.raises(PermanentError, match="API key rejected"):
        await collect(make_provider(clock), T0, T0 + timedelta(minutes=5))


@pytest.mark.parametrize("status", [404, 200])
async def test_unknown_symbol_404_is_permanent(respx_mock, clock, status):
    respx_mock.get(host=HOST, path="/time_series").mock(return_value=httpx.Response(status, json=error_body(404, "not found")))
    with pytest.raises(PermanentError, match="not found"):
        await collect(make_provider(clock), T0, T0 + timedelta(minutes=5))


DAILY = "You have run out of API credits for the day. 800 API credits were used."


@pytest.mark.parametrize("status", [429, 200, 400])
async def test_daily_limit_pause_is_capped_at_an_hour_with_a_clear_reason(respx_mock, status):
    clock = FakeClock(datetime(2024, 1, 8, 13, 37, 20, tzinfo=UTC))
    route = respx_mock.get(host=HOST, path="/time_series").mock(return_value=httpx.Response(status, json=error_body(429, DAILY)))
    provider = make_provider(clock)
    with pytest.raises(RateLimited) as exc:
        await collect(provider, T0, T0 + timedelta(minutes=5))
    resume = datetime(2024, 1, 8, 14, 37, 20, tzinfo=UTC)
    assert exc.value.resume_at == resume
    assert str(exc.value) == "Twelve Data daily credit limit reached, retrying at 14:37:20 UTC"
    clock.advance(23 * 60)  # a second job, long after the outage threshold, still sees the daily-limit wording
    with pytest.raises(RateLimited) as again:
        await collect(provider, T0, T0 + timedelta(minutes=5))
    assert str(again.value) == str(exc.value) and "unavailable" not in str(again.value)
    assert route.call_count == 1


async def test_daily_limit_pause_ends_at_midnight_plus_a_minute_when_that_is_sooner(respx_mock):
    clock = FakeClock(datetime(2024, 1, 8, 23, 50, tzinfo=UTC))
    respx_mock.get(host=HOST, path="/time_series").mock(return_value=httpx.Response(429, json=error_body(429, DAILY)))
    with pytest.raises(RateLimited) as exc:
        await collect(make_provider(clock), T0, T0 + timedelta(minutes=5))
    assert exc.value.resume_at == datetime(2024, 1, 9, 0, 1, tzinfo=UTC)


@pytest.mark.parametrize("status", [429, 200])
async def test_minute_limit_pauses_until_next_minute_plus_five_seconds(respx_mock, status):
    clock = FakeClock(datetime(2024, 1, 8, 13, 37, 20, tzinfo=UTC))
    respx_mock.get(host=HOST, path="/time_series").mock(
        return_value=httpx.Response(
            status, json=error_body(429, "You have run out of API credits for the current minute. 9 API credits were used, limit is 8.")
        )
    )
    with pytest.raises(RateLimited) as exc:
        await collect(make_provider(clock), T0, T0 + timedelta(minutes=5))
    assert exc.value.resume_at == datetime(2024, 1, 8, 13, 38, 5, tzinfo=UTC)
    assert "unavailable" not in str(exc.value)


async def test_unrecognised_429_message_falls_back_to_the_default_backoff(respx_mock, clock):
    respx_mock.get(host=HOST, path="/time_series").mock(return_value=httpx.Response(429, text="slow down"))
    start = clock.now()
    with pytest.raises(RateLimited) as exc:
        await collect(make_provider(clock), T0, T0 + timedelta(minutes=5))
    assert exc.value.resume_at == start + timedelta(seconds=60)


async def test_malformed_success_body_is_transient(respx_mock, clock):
    respx_mock.get(host=HOST, path="/time_series").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    with pytest.raises(TransientError):
        await collect(make_provider(clock), T0, T0 + timedelta(minutes=5))


@pytest.mark.parametrize("key", ["", "   "])
async def test_missing_key_is_permanent_and_sends_nothing(respx_mock, clock, key):
    route = respx_mock.route(host=HOST).mock(return_value=httpx.Response(200, json={}))
    provider = make_provider(clock, key)
    with pytest.raises(PermanentError, match="API key missing"):
        await collect(provider, T0, T0 + timedelta(minutes=5))
    with pytest.raises(PermanentError, match="API key missing"):
        await provider.earliest_available("XAU/USD")
    assert route.call_count == 0


SEARCH = {
    "data": [
        {"symbol": "XAU/USD", "instrument_name": "Gold Spot / US Dollar", "exchange": "COMMODITY", "instrument_type": "Precious Metal", "country": "", "currency": "USD"},
        {"symbol": "XAU/USD", "instrument_name": "duplicate", "exchange": "OTHER", "instrument_type": "Precious Metal", "country": "", "currency": "USD"},
        {"symbol": "EUR/USD", "instrument_name": "Euro / US Dollar", "exchange": "FOREX", "instrument_type": "Physical Currency", "country": "", "currency": "USD"},
        {"symbol": "AAPL", "instrument_name": "Apple Inc", "exchange": "NASDAQ", "instrument_type": "Common Stock", "country": "United States", "currency": "USD"},
        {"symbol": "BRK.B", "instrument_name": "Berkshire Hathaway", "exchange": "NYSE", "instrument_type": "Common Stock", "country": "United States", "currency": "USD"},
        {"symbol": "SPY", "instrument_name": "SPDR S&P 500 ETF Trust Shares", "exchange": "NYSE", "instrument_type": "ETF", "country": "United States", "currency": "USD"},
        {"symbol": "XAU", "instrument_name": "Canadian Gold Corp", "exchange": "TSX", "instrument_type": "Common Stock", "country": "Canada", "currency": "CAD"},
        {"symbol": "XAUUW", "instrument_name": "Some Warrant", "exchange": "NASDAQ", "instrument_type": "Warrant", "country": "United States", "currency": "USD"},
        {"symbol": "CL", "instrument_name": "Crude Oil WTI Natural", "exchange": "COMMODITY", "instrument_type": "Energy", "country": "", "currency": "USD"},
    ],
    "status": "ok",
}


def search_route(respx_mock, body=SEARCH):
    return respx_mock.get(host=HOST, path="/symbol_search").mock(return_value=httpx.Response(200, json=body))


async def test_search_filters_maps_and_dedupes(respx_mock, clock):
    route = search_route(respx_mock)
    found = await make_provider(clock).search_symbols("a")
    by_symbol = {s.provider_symbol: s for s in found}
    assert set(by_symbol) == {"XAU/USD", "EUR/USD", "AAPL", "BRK.B", "SPY", "CL"}  # Canadian stock and warrant dropped
    assert by_symbol["XAU/USD"].asset_class == "commodity" and by_symbol["XAU/USD"].suggested_jesse_symbol == "XAU-USD"
    assert by_symbol["XAU/USD"].name == "Gold Spot / US Dollar · COMMODITY"  # first duplicate wins
    assert (by_symbol["EUR/USD"].asset_class, by_symbol["EUR/USD"].suggested_jesse_symbol) == ("forex", "EUR-USD")
    assert (by_symbol["AAPL"].asset_class, by_symbol["AAPL"].suggested_jesse_symbol) == ("stock", "AAPL-USD")
    assert by_symbol["BRK.B"].suggested_jesse_symbol == "BRKB-USD"
    assert (by_symbol["SPY"].asset_class, by_symbol["SPY"].suggested_jesse_symbol) == ("etf", "SPY-USD")
    assert by_symbol["CL"].asset_class == "commodity" and by_symbol["CL"].suggested_jesse_symbol == "CL-USD"
    params = route.calls[0].request.url.params
    assert dict(params) == {"symbol": "a", "outputsize": "30"}  # the key is never sent to /symbol_search


async def test_search_ranks_the_compact_query_to_the_slash_symbol(respx_mock, clock):
    search_route(respx_mock)
    found = await make_provider(clock).search_symbols("xauusd")
    assert [s.provider_symbol for s in found] == ["XAU/USD"]


async def test_search_works_without_a_key_and_never_sends_one(respx_mock, clock):
    route = search_route(respx_mock)
    found = await make_provider(clock, "").search_symbols("XAU/USD")
    assert [s.provider_symbol for s in found] == ["XAU/USD"]
    await make_provider(clock, KEY).search_symbols("XAU/USD")
    assert all("apikey" not in call.request.url.params for call in route.calls)


async def test_search_results_are_cached_per_normalised_query_for_five_minutes(respx_mock, clock):
    route = search_route(respx_mock)
    provider = make_provider(clock)
    first = await provider.search_symbols("XAU/USD")
    assert await provider.search_symbols("  xau/usd ") == first
    assert route.call_count == 1
    clock.advance(299)
    await provider.search_symbols("XAU/USD")
    assert route.call_count == 1
    clock.advance(2)
    await provider.search_symbols("XAU/USD")
    assert route.call_count == 2
    await provider.search_symbols("AAPL")
    assert route.call_count == 3


async def test_search_uses_its_own_client_and_does_not_consume_download_pacing(respx_mock, clock):
    search_route(respx_mock)
    series = series_route(respx_mock, [], [])
    provider = make_provider(clock)
    await provider.fetch("XAU/USD", T0, T0 + timedelta(minutes=5)).__anext__()
    before = clock.monotonic()
    for q in ("a", "b", "c"):
        await provider.search_symbols(q)
    assert clock.monotonic() == before  # burst of 3 on the search client; no waiting for the 7/min download budget
    assert provider.search_client is not provider.client and series.call_count == 1


async def test_search_during_a_download_pause_is_not_blocked(respx_mock):
    clock = FakeClock(datetime(2024, 1, 8, 13, 37, 20, tzinfo=UTC))
    respx_mock.get(host=HOST, path="/time_series").mock(return_value=httpx.Response(429, json=error_body(429, DAILY)))
    search_route(respx_mock)
    provider = make_provider(clock)
    with pytest.raises(RateLimited):
        await collect(provider, T0, T0 + timedelta(minutes=5))
    assert await provider.search_symbols("XAU/USD")


async def test_search_429_raises_a_rate_limit_error(respx_mock, clock):
    respx_mock.get(host=HOST, path="/symbol_search").mock(return_value=httpx.Response(429, json=error_body(429, "minute")))
    with pytest.raises(RateLimited, match="Twelve Data"):
        await make_provider(clock).search_symbols("AAPL")


async def test_blank_search_sends_nothing(respx_mock, clock):
    route = search_route(respx_mock)
    assert await make_provider(clock).search_symbols("  ") == []
    assert route.call_count == 0


async def test_search_surfaces_error_bodies(respx_mock, clock):
    search_route(respx_mock, error_body(401, "bad key"))
    with pytest.raises(PermanentError, match="API key rejected"):
        await make_provider(clock).search_symbols("AAPL")


async def test_earliest_available_parses_unix_time(respx_mock, clock):
    route = respx_mock.get(host=HOST, path="/earliest_timestamp").mock(
        return_value=httpx.Response(200, json={"datetime": "2020-02-10 09:30:00", "unix_time": 1581327000})
    )
    assert await make_provider(clock).earliest_available("AAPL") == datetime(2020, 2, 10, 9, 30, tzinfo=UTC)
    params = route.calls[0].request.url.params
    assert dict(params) == {"symbol": "AAPL", "interval": "1min", "timezone": "UTC", "apikey": KEY}


async def test_earliest_available_without_unix_time_is_an_error(respx_mock, clock):
    respx_mock.get(host=HOST, path="/earliest_timestamp").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    with pytest.raises(PermanentError, match="Twelve Data"):
        await make_provider(clock).earliest_available("AAPL")


async def test_earliest_available_is_cached_per_symbol_for_an_hour(respx_mock, clock):
    route = respx_mock.get(host=HOST, path="/earliest_timestamp").mock(
        return_value=httpx.Response(200, json={"datetime": "2020-02-10 09:30:00", "unix_time": 1581327000})
    )
    provider = make_provider(clock)
    assert await provider.earliest_available("AAPL") == await provider.earliest_available("AAPL")
    assert route.call_count == 1
    await provider.earliest_available("MSFT")
    assert route.call_count == 2
    clock.advance(3570)  # MSFT already waited ~9s for the download budget
    await provider.earliest_available("AAPL")
    assert route.call_count == 2
    clock.advance(60)
    await provider.earliest_available("AAPL")
    assert route.call_count == 3


def test_estimate_requests(clock):
    p = make_provider(clock)
    assert p.estimate_requests(T0, T0) == 0
    assert p.estimate_requests(T0, T0 - timedelta(minutes=5)) == 0
    assert p.estimate_requests(T0, T0 + timedelta(minutes=PAGE)) == 1
    assert p.estimate_requests(T0, T0 + timedelta(minutes=PAGE + 1)) == 2
    assert p.estimate_requests(T0, T0 + timedelta(minutes=12_000)) == 3


def test_available_until_lags_fifteen_minutes(clock):
    assert PUBLISH_LAG == timedelta(minutes=15)
    assert make_provider(clock).available_until(datetime(2024, 1, 8, 10, 27, 41, tzinfo=UTC)) == datetime(2024, 1, 8, 10, 12, tzinfo=UTC)


def test_policy_stays_under_eight_requests_a_minute():
    assert POLICY.rate == pytest.approx(7 / 60) and POLICY.burst == 1 and POLICY.concurrency == 1
    assert POLICY.throttle_statuses == frozenset({429})


async def test_server_error_inside_a_200_is_transient(respx_mock, clock):
    respx_mock.get(host=HOST, path="/time_series").mock(return_value=httpx.Response(200, json=error_body(500, "internal")))
    with pytest.raises(TransientError):
        await collect(make_provider(clock), T0, T0 + timedelta(minutes=5))


async def test_non_json_400_is_permanent(respx_mock, clock):
    respx_mock.get(host=HOST, path="/time_series").mock(return_value=httpx.Response(400, text="<html>bad</html>"))
    with pytest.raises(PermanentError, match="request rejected"):
        await collect(make_provider(clock), T0, T0 + timedelta(minutes=5))


@pytest.mark.parametrize("status", [401, 200, 400])
async def test_key_is_scrubbed_from_error_messages(respx_mock, clock, status):
    body = error_body(401 if status != 400 else 400, f"bad key {KEY} in request")
    respx_mock.get(host=HOST, path="/time_series").mock(return_value=httpx.Response(status, json=body))
    with pytest.raises(PermanentError) as exc:
        await collect(make_provider(clock), T0, T0 + timedelta(minutes=5))
    assert KEY not in str(exc.value) and "***" in str(exc.value)

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.domain import PermanentError, TransientError
from app.providers.oanda import PAGE, POLICY, PUBLISH_LAG, OandaProvider
from app.providers.http import ProviderClient

HOST = "api-fxpractice.oanda.com"
T0 = datetime(2024, 1, 8, tzinfo=UTC)  # a Monday
ACCOUNT = "101-004-1234567-001"


def make_provider(clock, token="TOKEN", environment="practice") -> OandaProvider:
    async def credentials():
        return token, environment

    return OandaProvider(ProviderClient(POLICY, httpx.AsyncClient(), clock), credentials)


def unix(dt: datetime) -> str:
    return f"{dt.timestamp():.9f}"


def candle(ts: datetime, complete=True, o="1.1", h="1.3", low="1.0", c="1.2", volume=123) -> dict:
    return {"time": unix(ts), "bid": {"o": o, "h": h, "l": low, "c": c}, "volume": volume, "complete": complete}


def candles_route(respx_mock, *responses, instrument="EUR_USD", host=HOST):
    return respx_mock.get(host=host, path=f"/v3/instruments/{instrument}/candles").mock(
        side_effect=[httpx.Response(200, json={"instrument": instrument, "granularity": "M1", "candles": r}) for r in responses]
    )


async def collect(provider, start, end, symbol="EUR_USD"):
    return [c async for c in provider.fetch(symbol, start, end)]


async def test_windows_of_4999_minutes_with_headers_and_no_count(respx_mock, clock):
    route = candles_route(respx_mock, [], [], [])
    end = T0 + timedelta(minutes=12_000)
    chunks = await collect(make_provider(clock), T0, end)
    w1, w2 = T0 + timedelta(minutes=PAGE), T0 + timedelta(minutes=2 * PAGE)
    assert [c.covered_until for c in chunks] == [w1, w2, end]
    requests = [call.request for call in route.calls]
    assert [(r.url.params["from"], r.url.params["to"]) for r in requests] == [
        (str(int(T0.timestamp())), str(int(w1.timestamp()))),
        (str(int(w1.timestamp())), str(int(w2.timestamp()))),
        (str(int(w2.timestamp())), str(int(end.timestamp()))),
    ]
    for r in requests:
        assert "count" not in r.url.params
        assert r.url.params["granularity"] == "M1" and r.url.params["price"] == "B"
        assert r.headers["Authorization"] == "Bearer TOKEN"
        assert r.headers["Accept-Datetime-Format"] == "UNIX"


async def test_parses_bid_prices_and_volume(respx_mock, clock):
    candles_route(respx_mock, [candle(T0, o="1.10001", h="1.2", low="1.0", c="1.15", volume=42)])
    [chunk] = await collect(make_provider(clock), T0, T0 + timedelta(minutes=10))
    [c] = chunk.candles
    assert (c.ts, c.open, c.high, c.low, c.close, c.volume) == (T0, 1.10001, 1.2, 1.0, 1.15, 42.0)


async def test_candles_outside_the_window_are_ignored(respx_mock, clock):
    end = T0 + timedelta(minutes=10)
    candles_route(respx_mock, [candle(T0 - timedelta(minutes=1)), candle(T0), candle(end)])
    [chunk] = await collect(make_provider(clock), T0, end)
    assert [c.ts for c in chunk.candles] == [T0]


async def test_incomplete_candle_stops_the_cursor_at_its_time_without_another_request(respx_mock, clock):
    t = [T0 + timedelta(minutes=i) for i in range(4)]
    route = candles_route(respx_mock, [candle(t[0]), candle(t[1]), candle(t[2], complete=False), candle(t[3])])
    chunks = await collect(make_provider(clock), T0, T0 + timedelta(minutes=10))
    assert len(chunks) == 1 and route.call_count == 1
    assert [c.ts for c in chunks[0].candles] == t[:2]
    assert chunks[0].covered_until == t[2]


async def test_incomplete_first_candle_ends_the_fetch_instead_of_spinning(respx_mock, clock):
    route = candles_route(respx_mock, [candle(T0, complete=False)])
    chunks = await collect(make_provider(clock), T0, T0 + timedelta(minutes=10))
    assert len(chunks) == 1 and chunks[0].candles == [] and chunks[0].covered_until == T0
    assert route.call_count == 1


async def test_empty_window_still_advances_the_cursor(respx_mock, clock):
    candles_route(respx_mock, [])  # weekend
    end = T0 + timedelta(minutes=60)
    [chunk] = await collect(make_provider(clock), T0, end)
    assert chunk.candles == [] and chunk.covered_until == end


async def test_live_environment_uses_the_fxtrade_host(respx_mock, clock):
    candles_route(respx_mock, [], host="api-fxtrade.oanda.com")
    await collect(make_provider(clock, environment="live"), T0, T0 + timedelta(minutes=5))


async def test_environment_is_normalised(respx_mock, clock):
    candles_route(respx_mock, [], host="api-fxtrade.oanda.com")
    await collect(make_provider(clock, environment=" Live "), T0, T0 + timedelta(minutes=5))


@pytest.mark.parametrize("environment", ["whatever", ""])
async def test_invalid_environment_is_a_permanent_error_without_a_request(respx_mock, clock, environment):
    provider = make_provider(clock, environment=environment)
    with pytest.raises(PermanentError, match="OANDA: OANDA_ENVIRONMENT must be 'practice' or 'live'"):
        await collect(provider, T0, T0 + timedelta(minutes=5))
    with pytest.raises(PermanentError, match="OANDA_ENVIRONMENT"):
        await provider.search_symbols("eur")
    assert len(respx_mock.calls) == 0


@pytest.mark.parametrize("token", ["", "   "])
async def test_missing_token_fails_without_a_request(respx_mock, clock, token):
    provider = make_provider(clock, token=token)
    with pytest.raises(PermanentError, match="OANDA: API token missing — set it in Settings"):
        await collect(provider, T0, T0 + timedelta(minutes=5))
    with pytest.raises(PermanentError, match="token missing"):
        await provider.search_symbols("eur")
    with pytest.raises(PermanentError, match="token missing"):
        await provider.earliest_available("EUR_USD")
    assert len(respx_mock.calls) == 0


async def test_rejected_token_is_a_permanent_error(respx_mock, clock):
    respx_mock.get(host=HOST, path="/v3/instruments/EUR_USD/candles").mock(return_value=httpx.Response(401, text="nope"))
    with pytest.raises(PermanentError, match="API token rejected"):
        await collect(make_provider(clock), T0, T0 + timedelta(minutes=5))


INSTRUMENTS = [
    {"name": "EUR_USD", "type": "CURRENCY", "displayName": "EUR/USD"},
    {"name": "XAU_USD", "type": "METAL", "displayName": "Gold"},
    {"name": "US30_USD", "type": "CFD", "displayName": "US Wall St 30"},
]


def mock_accounts(respx_mock):
    accounts = respx_mock.get(host=HOST, path="/v3/accounts").mock(
        return_value=httpx.Response(200, json={"accounts": [{"id": ACCOUNT, "tags": []}, {"id": "other", "tags": []}]})
    )
    instruments = respx_mock.get(host=HOST, path=f"/v3/accounts/{ACCOUNT}/instruments").mock(
        return_value=httpx.Response(200, json={"instruments": INSTRUMENTS})
    )
    return accounts, instruments


async def test_search_maps_types_and_suggests_jesse_symbols_and_caches(respx_mock, clock):
    accounts, instruments = mock_accounts(respx_mock)
    provider = make_provider(clock)
    everything = await provider.search_symbols("USD")
    info = {s.provider_symbol: s for s in everything}
    assert {k: (v.asset_class, v.suggested_jesse_symbol, v.name) for k, v in info.items()} == {
        "EUR_USD": ("forex", "EUR-USD", "EUR/USD"),
        "XAU_USD": ("metal", "XAU-USD", "Gold"),
        "US30_USD": ("cfd", "US30-USD", "US Wall St 30"),
    }
    assert accounts.calls[0].request.headers["Authorization"] == "Bearer TOKEN"
    await provider.search_symbols("gold")
    assert accounts.call_count == 1 and instruments.call_count == 1


async def test_search_finds_eur_usd_from_eurusd(respx_mock, clock):
    mock_accounts(respx_mock)
    found = await make_provider(clock).search_symbols("eurusd")
    assert [s.provider_symbol for s in found] == ["EUR_USD"]


async def test_earliest_available_asks_for_one_candle_since_2000(respx_mock, clock):
    first = datetime(2005, 5, 17, 13, 0, tzinfo=UTC)
    route = candles_route(respx_mock, [candle(first)])
    assert await make_provider(clock).earliest_available("EUR_USD") == first
    params = route.calls[0].request.url.params
    assert params["from"] == str(int(datetime(2000, 1, 1, tzinfo=UTC).timestamp()))
    assert (params["count"], params["granularity"], params["price"]) == ("1", "M1", "B")
    assert "to" not in params


async def test_earliest_available_without_candles_is_permanent(respx_mock, clock):
    candles_route(respx_mock, [])
    with pytest.raises(PermanentError, match="no data"):
        await make_provider(clock).earliest_available("EUR_USD")


def test_estimate_requests_and_available_until(clock):
    p = make_provider(clock)
    assert p.estimate_requests(T0, T0) == 0
    assert p.estimate_requests(T0, T0 - timedelta(hours=1)) == 0
    assert p.estimate_requests(T0, T0 + timedelta(minutes=1)) == 1
    assert p.estimate_requests(T0, T0 + timedelta(minutes=PAGE)) == 1
    assert p.estimate_requests(T0, T0 + timedelta(minutes=PAGE + 1)) == 2
    assert p.estimate_requests(T0, T0 + timedelta(minutes=12_000)) == 3
    now = datetime(2024, 1, 8, 12, 34, 56, tzinfo=UTC)
    assert p.available_until(now) == datetime(2024, 1, 8, 12, 34, tzinfo=UTC) - PUBLISH_LAG == datetime(2024, 1, 8, 12, 32, tzinfo=UTC)


def test_page_stays_below_oandas_5000_candle_limit_even_if_to_is_inclusive():
    assert PAGE == 4999


def test_policy_and_identity(clock):
    assert (POLICY.name, POLICY.rate, POLICY.burst, POLICY.concurrency) == ("OANDA", 20.0, 10, 2)
    assert POLICY.throttle_statuses == {429}
    p = make_provider(clock)
    assert (p.name, p.label, p.asset_classes) == ("oanda", "OANDA (forex & CFDs)", ("forex", "metal", "cfd"))


async def test_search_cache_is_keyed_by_token_and_environment(respx_mock, clock):
    creds = ["TOKEN", "practice"]

    async def credentials():
        return creds[0], creds[1]

    provider = OandaProvider(ProviderClient(POLICY, httpx.AsyncClient(), clock), credentials)
    accounts, instruments = mock_accounts(respx_mock)
    await provider.search_symbols("eur")
    await provider.search_symbols("eur")
    assert accounts.call_count == 1
    creds[0] = "OTHER"
    await provider.search_symbols("eur")
    assert accounts.call_count == 2 and accounts.calls[1].request.headers["Authorization"] == "Bearer OTHER"
    creds[1] = "live"
    live_accounts = respx_mock.get(host="api-fxtrade.oanda.com", path="/v3/accounts").mock(
        return_value=httpx.Response(200, json={"accounts": [{"id": ACCOUNT}]})
    )
    live_instruments = respx_mock.get(host="api-fxtrade.oanda.com", path=f"/v3/accounts/{ACCOUNT}/instruments").mock(
        return_value=httpx.Response(200, json={"instruments": INSTRUMENTS})
    )
    await provider.search_symbols("eur")
    assert live_accounts.call_count == 1 and live_instruments.call_count == 1


@pytest.mark.parametrize(
    "body", [{}, {"accounts": []}, {"accounts": [{"tags": []}]}, {"accounts": None}, [], [1], "x", {"accounts": ["x"]}, {"accounts": "x"}]
)
async def test_search_without_accounts_is_a_clear_permanent_error(respx_mock, clock, body):
    respx_mock.get(host=HOST, path="/v3/accounts").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(PermanentError, match="OANDA: no accounts found for this token"):
        await make_provider(clock).search_symbols("eur")


@pytest.mark.parametrize(
    "body",
    [{}, {"instruments": []}, {"instruments": None}, [], {"instruments": [{"type": "CFD"}]}, {"instruments": [None, "x"]}, "x"],
)
async def test_search_without_instruments_is_a_clear_permanent_error(respx_mock, clock, body):
    respx_mock.get(host=HOST, path="/v3/accounts").mock(return_value=httpx.Response(200, json={"accounts": [{"id": ACCOUNT}]}))
    respx_mock.get(host=HOST, path=f"/v3/accounts/{ACCOUNT}/instruments").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(PermanentError, match="OANDA: no instruments found for this account"):
        await make_provider(clock).search_symbols("eur")


@pytest.mark.parametrize("body", [{}, {"candles": None}, {"candles": "x"}, [], "x"])
async def test_malformed_candle_response_is_transient_so_the_cursor_does_not_move(respx_mock, clock, body):
    respx_mock.get(host=HOST, path="/v3/instruments/EUR_USD/candles").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(TransientError, match="unexpected response"):
        await collect(make_provider(clock), T0, T0 + timedelta(minutes=5))
    respx_mock.get(host=HOST, path="/v3/instruments/EUR_USD/candles").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(TransientError):
        await make_provider(clock).earliest_available("EUR_USD")

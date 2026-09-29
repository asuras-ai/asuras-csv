from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.domain import Candle, PermanentError, to_ms
from app.providers.binance import POLICY, BinanceProvider, binance_quota_delay
from app.providers.http import ProviderClient

BASE = "https://binance.test"
LISTED = datetime(2024, 1, 1, tzinfo=UTC)


def kline_handler(listed: datetime = LISTED):
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        t = max(int(p["startTime"]), to_ms(listed))
        end = int(p.get("endTime", 2**62))
        limit = int(p["limit"])
        rows = []
        while t <= end and len(rows) < limit:
            rows.append([t, "1.0", "2.0", "0.5", "1.5", "10.0", t + 59_999, "15.0", 5, "5.0", "7.5", "0"])
            t += 60_000
        return httpx.Response(200, json=rows, headers={"X-MBX-USED-WEIGHT-1M": "2"})

    return handler


@pytest.fixture
def provider(clock):
    return BinanceProvider(ProviderClient(POLICY, httpx.AsyncClient(), clock), base_url=BASE)


async def collect(provider, symbol, start, end):
    return [chunk async for chunk in provider.fetch(symbol, start, end)]


async def test_fetch_paginates_in_1000_candle_requests(respx_mock, provider):
    route = respx_mock.get(host="binance.test", path="/api/v3/klines").mock(side_effect=kline_handler())
    end = LISTED + timedelta(minutes=2500)
    chunks = await collect(provider, "BTCUSDT", LISTED, end)
    assert route.call_count == 3
    assert [len(c.candles) for c in chunks] == [1000, 1000, 500]
    assert [c.covered_until for c in chunks] == [
        LISTED + timedelta(minutes=1000),
        LISTED + timedelta(minutes=2000),
        end,
    ]
    assert chunks[0].candles[0] == Candle(LISTED, 1.0, 2.0, 0.5, 1.5, 10.0)
    assert route.calls[1].request.url.params["startTime"] == str(to_ms(LISTED + timedelta(minutes=1000)))


async def test_range_before_listing_costs_no_extra_requests(respx_mock, provider):
    route = respx_mock.get(host="binance.test", path="/api/v3/klines").mock(
        side_effect=kline_handler(listed=LISTED + timedelta(minutes=10))
    )
    chunks = await collect(provider, "NEWUSDT", LISTED, LISTED + timedelta(minutes=20))
    assert route.call_count == 1
    assert len(chunks[0].candles) == 10
    assert chunks[0].candles[0].ts == LISTED + timedelta(minutes=10)
    assert chunks[0].covered_until == LISTED + timedelta(minutes=20)


async def test_earliest_available_is_the_first_kline(respx_mock, provider):
    respx_mock.get(host="binance.test", path="/api/v3/klines").mock(side_effect=kline_handler())
    assert await provider.earliest_available("BTCUSDT") == LISTED


async def test_invalid_symbol_is_permanent(respx_mock, provider):
    respx_mock.get(host="binance.test", path="/api/v3/klines").mock(
        return_value=httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
    )
    with pytest.raises(PermanentError, match="Invalid symbol"):
        await collect(provider, "NOPE", LISTED, LISTED + timedelta(minutes=5))


async def test_search_ranks_matches_and_caches_exchange_info(respx_mock, provider):
    route = respx_mock.get(host="binance.test", path="/api/v3/exchangeInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "symbols": [
                    {"symbol": "BTCUSDC", "baseAsset": "BTC", "quoteAsset": "USDC", "status": "TRADING"},
                    {"symbol": "WBTCBTC", "baseAsset": "WBTC", "quoteAsset": "BTC", "status": "TRADING"},
                    {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"},
                    {"symbol": "BTCOLD", "baseAsset": "BTC", "quoteAsset": "OLD", "status": "BREAK"},
                ]
            },
        )
    )
    assert [s.provider_symbol for s in await provider.search_symbols("btcusdt")] == ["BTCUSDT"]
    results = await provider.search_symbols("BTC")
    assert [s.provider_symbol for s in results] == ["BTCUSDC", "BTCUSDT", "WBTCBTC"]
    assert results[1].suggested_jesse_symbol == "BTC-USDT"
    assert results[1].asset_class == "crypto"
    assert route.call_count == 1


def test_estimate_and_available_until(provider):
    assert provider.estimate_requests(LISTED, LISTED + timedelta(minutes=2500)) == 3
    assert provider.estimate_requests(LISTED, LISTED) == 0
    now = datetime(2024, 5, 1, 12, 30, 45, tzinfo=UTC)
    assert provider.available_until(now) == datetime(2024, 5, 1, 12, 28, tzinfo=UTC)


def test_quota_delay_waits_for_the_next_minute_when_weight_is_high():
    now = datetime(2024, 1, 1, 12, 0, 15, tzinfo=UTC)
    high = httpx.Response(200, headers={"X-MBX-USED-WEIGHT-1M": "5500"})
    low = httpx.Response(200, headers={"X-MBX-USED-WEIGHT-1M": "100"})
    assert binance_quota_delay(high, now) == 45.0
    assert binance_quota_delay(low, now) == 0.0

from datetime import timedelta

import httpx
import pytest

from app.domain import PermanentError, RateLimited, TransientError
from app.providers.http import ProviderClient, RateLimitPolicy

URL = "https://api.test/data"


def make_client(clock, **overrides) -> ProviderClient:
    policy = RateLimitPolicy(**({"name": "Test", "rate": 100.0, "burst": 100, "concurrency": 2} | overrides))
    return ProviderClient(policy, httpx.AsyncClient(), clock)


async def test_paces_requests_to_the_configured_rate(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(200))
    client = make_client(clock, rate=2.0, burst=1, concurrency=1)
    for _ in range(5):
        await client.get(URL)
    assert clock.monotonic() == pytest.approx(2.0)


async def test_low_quota_header_blocks_until_reset(respx_mock, clock):
    respx_mock.get(URL).mock(side_effect=[httpx.Response(200, headers={"X-Low": "1"}), httpx.Response(200)])
    client = make_client(clock, quota_delay=lambda r, now: 10.0 if r.headers.get("X-Low") else 0.0)
    await client.get(URL)
    await client.get(URL)
    assert clock.monotonic() == pytest.approx(10.0)


async def test_429_pauses_every_caller_until_retry_after(respx_mock, clock):
    route = respx_mock.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "30"}), httpx.Response(200)]
    )
    client = make_client(clock)
    start = clock.now()
    with pytest.raises(RateLimited) as first:
        await client.get(URL)
    assert first.value.resume_at == start + timedelta(seconds=30)
    with pytest.raises(RateLimited):
        await client.get(URL)
    assert route.call_count == 1
    clock.advance(31)
    assert (await client.get(URL)).status_code == 200


async def test_429_without_retry_after_backs_off_exponentially(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(429))
    client = make_client(clock)
    delays = []
    for _ in range(3):
        start = clock.now()
        with pytest.raises(RateLimited) as exc:
            await client.get(URL)
        delays.append((exc.value.resume_at - start).total_seconds())
        clock.advance(delays[-1])
    assert delays == [60, 120, 240]


async def test_server_errors_are_retried_then_reported_as_transient(respx_mock, clock):
    route = respx_mock.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(TransientError, match="HTTP 503"):
        await make_client(clock).get(URL)
    assert route.call_count == 4
    assert clock.sleeps == [2.0, 4.0, 8.0]


async def test_server_error_then_success(respx_mock, clock):
    respx_mock.get(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
    assert (await make_client(clock).get(URL)).status_code == 200


async def test_network_errors_are_transient(respx_mock, clock):
    respx_mock.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(TransientError, match="network error"):
        await make_client(clock).get(URL)


async def test_permanent_status_uses_policy_message(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(401, text="nope"))
    client = make_client(clock, permanent_messages={401: "API key rejected — check Settings"})
    with pytest.raises(PermanentError, match="Test: API key rejected — check Settings"):
        await client.get(URL)


async def test_unexpected_client_error_is_permanent(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(418))
    with pytest.raises(PermanentError, match="unexpected HTTP 418"):
        await make_client(clock).get(URL)


async def test_extra_ok_statuses_are_returned(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(404))
    client = make_client(clock, ok_statuses=frozenset({200, 404}))
    assert (await client.get(URL)).status_code == 404

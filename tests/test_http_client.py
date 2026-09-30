import asyncio
from datetime import timedelta

import httpx
import pytest

from app.domain import PermanentError, RateLimited, TransientError
from app.providers.http import ProviderClient, RateLimitPolicy, Throttle

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
    route = respx_mock.get(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
    assert (await make_client(clock).get(URL)).status_code == 200
    assert route.call_count == 2


async def test_network_errors_are_transient(respx_mock, clock):
    route = respx_mock.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(TransientError, match="network error"):
        await make_client(clock).get(URL)
    assert route.call_count == 4
    assert clock.sleeps == [2.0, 4.0, 8.0]


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


def both_in_flight_then(responses):
    """Side effect that holds every request until two are in flight, then answers in order."""
    arrived = 0
    both = asyncio.Event()
    queue = list(responses)

    async def handler(request):
        nonlocal arrived
        arrived += 1
        if arrived >= 2:
            both.set()
        await both.wait()
        return queue.pop(0)

    return handler


async def test_concurrent_429s_share_one_backoff_step(respx_mock, clock):
    respx_mock.get(URL).mock(side_effect=both_in_flight_then([httpx.Response(429)] * 2))
    client = make_client(clock)
    start = clock.now()
    results = await asyncio.gather(client.get(URL), client.get(URL), return_exceptions=True)
    assert all(isinstance(r, RateLimited) for r in results)
    assert {r.resume_at for r in results} == {start + timedelta(seconds=60)}
    clock.advance(60)
    respx_mock.get(URL).mock(return_value=httpx.Response(429))
    now = clock.now()
    with pytest.raises(RateLimited) as exc:
        await client.get(URL)
    assert (exc.value.resume_at - now).total_seconds() == 120


async def test_shorter_retry_after_does_not_shorten_pause(respx_mock, clock):
    respx_mock.get(URL).mock(
        side_effect=both_in_flight_then(
            [httpx.Response(429, headers={"Retry-After": "300"}), httpx.Response(429, headers={"Retry-After": "10"})]
        )
    )
    client = make_client(clock)
    start = clock.now()
    results = await asyncio.gather(client.get(URL), client.get(URL), return_exceptions=True)
    assert all(isinstance(r, RateLimited) for r in results)
    assert client._paused_until == start + timedelta(seconds=300)


@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf"])
async def test_unusable_retry_after_is_treated_as_absent(respx_mock, clock, value):
    respx_mock.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": value}))
    start = clock.now()
    with pytest.raises(RateLimited) as exc:
        await make_client(clock).get(URL)
    assert exc.value.resume_at == start + timedelta(seconds=60)


async def test_concurrency_limit_is_respected(respx_mock, clock):
    in_flight = peak = calls = 0
    release = asyncio.Event()

    async def handler(request):
        nonlocal in_flight, peak, calls
        calls += 1
        in_flight += 1
        peak = max(peak, in_flight)
        await release.wait()
        in_flight -= 1
        return httpx.Response(200)

    respx_mock.get(URL).mock(side_effect=handler)
    client = make_client(clock, concurrency=1)
    tasks = asyncio.gather(client.get(URL), client.get(URL))
    for _ in range(20):
        await asyncio.sleep(0)
    assert calls == 1
    release.set()
    await tasks
    assert calls == 2
    assert peak == 1


async def test_persistent_throttling_reports_the_provider_as_unavailable(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(503))
    client = make_client(clock, throttle_statuses=frozenset({503}))
    started = clock.now()
    with pytest.raises(RateLimited) as exc:
        await client.get(URL)
    assert exc.value.since is None and "rate limit" in str(exc.value)
    for _ in range(3):  # each retry after the pause is refused again, at +60 s, +180 s and +420 s
        clock.advance((exc.value.resume_at - clock.now()).total_seconds())
        with pytest.raises(RateLimited) as exc:
            await client.get(URL)
    assert exc.value.since == started and exc.value.status == 503
    assert f"Test unavailable since {started:%H:%M} UTC (HTTP 503), retrying at" in str(exc.value)
    with pytest.raises(RateLimited) as paused:  # raised from the pause check, not a new request
        await client.get(URL)
    assert paused.value.since == started and paused.value.status == 503


async def test_streak_is_judged_on_elapsed_time_not_the_projected_resume_time(respx_mock, clock):
    respx_mock.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "30"}), httpx.Response(429, headers={"Retry-After": "240"})]
    )
    client = make_client(clock)
    with pytest.raises(RateLimited):
        await client.get(URL)
    clock.advance(180)  # 3 minutes into the streak; the new pause projects to 7 minutes
    with pytest.raises(RateLimited) as exc:
        await client.get(URL)
    assert exc.value.resume_at - clock.now() == timedelta(minutes=4)
    assert str(exc.value) == "Test rate limit, resuming 02:07:00 UTC"
    assert exc.value.since is None


async def test_a_single_long_retry_after_is_still_a_rate_limit(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "600"}))
    with pytest.raises(RateLimited) as exc:
        await make_client(clock).get(URL)
    assert str(exc.value) == "Test rate limit, resuming 02:10:00 UTC"


async def test_unavailable_starts_exactly_after_five_minutes_of_refusals(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "10"}))
    client = make_client(clock)
    with pytest.raises(RateLimited):
        await client.get(URL)
    for _ in range(4):  # a refusal every 50 s keeps the streak unbroken: 50 .. 200 s
        clock.advance(50)
        with pytest.raises(RateLimited) as exc:
            await client.get(URL)
        assert "rate limit" in str(exc.value)
    clock.advance(49)  # 249 s
    with pytest.raises(RateLimited) as just_before:
        await client.get(URL)
    assert "rate limit" in str(just_before.value)
    clock.advance(51)  # 300 s since the first refusal
    with pytest.raises(RateLimited) as at_boundary:
        await client.get(URL)
    assert str(at_boundary.value) == "Test unavailable since 02:00 UTC (HTTP 429), retrying at 02:05:10 UTC"


async def test_refusal_after_a_long_idle_gap_starts_a_new_streak(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(503))
    client = make_client(clock, throttle_statuses=frozenset({503}))
    with pytest.raises(RateLimited):
        await client.get(URL)  # nobody finishes this streak (e.g. the job was cancelled)
    clock.advance(3 * 3600)
    with pytest.raises(RateLimited) as exc:
        await client.get(URL)
    assert "rate limit" in str(exc.value) and exc.value.since is None
    for _ in range(3):  # but a genuinely continuous outage from here on is reported: +60 s, +180 s, +420 s
        clock.advance((exc.value.resume_at - clock.now()).total_seconds())
        with pytest.raises(RateLimited) as exc:
            await client.get(URL)
    assert "unavailable since 05:00 UTC" in str(exc.value)


async def test_a_successful_response_ends_the_throttle_streak(respx_mock, clock):
    respx_mock.get(URL).mock(side_effect=[httpx.Response(429), httpx.Response(200), httpx.Response(429)])
    client = make_client(clock)
    with pytest.raises(RateLimited):
        await client.get(URL)
    clock.advance(61)
    await client.get(URL)
    clock.advance(1000)
    with pytest.raises(RateLimited) as again:
        await client.get(URL)  # a new streak, not 17 minutes of the old one
    assert "rate limit" in str(again.value) and again.value.since is None


async def test_stale_success_during_a_pause_does_not_end_the_streak(clock):
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/slow"):
            await gate.wait()  # still in flight when the throttling response arrives
            return httpx.Response(200)
        gate.set()
        return httpx.Response(429, headers={"Retry-After": "10"})

    client = ProviderClient(
        RateLimitPolicy(name="Test", rate=100.0, burst=100, concurrency=2),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock,
    )
    started = clock.now()
    slow, refused = await asyncio.gather(client.get(f"{URL}/slow"), client.get(f"{URL}/fast"), return_exceptions=True)
    assert isinstance(refused, RateLimited) and slow.status_code == 200
    for _ in range(6):  # a refusal every 50 s keeps the streak unbroken up to 300 s
        clock.advance(50)
        with pytest.raises(RateLimited) as exc:
            await client.get(f"{URL}/fast")
    assert exc.value.since == started


async def test_stale_success_during_a_pause_does_not_reset_the_backoff(clock):
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/slow"):
            await gate.wait()
            return httpx.Response(200)
        gate.set()
        return httpx.Response(429)  # no Retry-After: 60 s, then 120 s

    client = ProviderClient(
        RateLimitPolicy(name="Test", rate=100.0, burst=100, concurrency=2),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock,
    )
    slow, refused = await asyncio.gather(client.get(f"{URL}/slow"), client.get(f"{URL}/fast"), return_exceptions=True)
    assert slow.status_code == 200 and isinstance(refused, RateLimited)
    clock.advance(60)
    with pytest.raises(RateLimited) as again:
        await client.get(f"{URL}/fast")
    assert again.value.resume_at - clock.now() == timedelta(seconds=120)


async def test_throttle_delay_hook_overrides_retry_after_and_backoff(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "30"}, json={"message": "soon"}))
    seen = []

    def hook(response, now):
        seen.append((response.status_code, now))
        return 7.0

    client = make_client(clock, throttle_delay=hook)
    start = clock.now()
    with pytest.raises(RateLimited) as exc:
        await client.get(URL)
    assert exc.value.resume_at == start + timedelta(seconds=7)
    assert seen == [(429, start)]


async def test_throttle_delay_hook_returning_none_falls_back_to_retry_after_then_backoff(respx_mock, clock):
    respx_mock.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "30"}), httpx.Response(429)]
    )
    client = make_client(clock, throttle_delay=lambda r, now: None)
    start = clock.now()
    with pytest.raises(RateLimited) as first:
        await client.get(URL)
    assert first.value.resume_at == start + timedelta(seconds=30)
    clock.advance(31)
    start = clock.now()
    with pytest.raises(RateLimited) as second:
        await client.get(URL)
    assert second.value.resume_at == start + timedelta(seconds=60)


async def test_is_throttled_classifies_a_non_throttle_status_before_success_bookkeeping(respx_mock, clock):
    route = respx_mock.get(URL).mock(return_value=httpx.Response(200, json={"code": 429}))
    client = make_client(clock, is_throttled=lambda r: r.json().get("code") or None, throttle_delay=lambda r, now: 45.0)
    start = clock.now()
    with pytest.raises(RateLimited) as exc:
        await client.get(URL)
    assert exc.value.resume_at == start + timedelta(seconds=45)
    with pytest.raises(RateLimited):
        await client.get(URL)
    assert route.call_count == 1


async def test_is_throttled_status_is_reported_in_a_long_streak(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(200, json={"code": 429}))
    client = make_client(clock, is_throttled=lambda r: r.json().get("code") or None)
    last = None
    for _ in range(4):
        with pytest.raises(RateLimited) as exc:
            await client.get(URL)
        last = exc.value
        clock.advance((last.resume_at - clock.now()).total_seconds())
    assert last.since is not None and last.status == 429 and "(HTTP 429)" in str(last)


async def test_is_throttled_defaults_to_false(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(200, json={"code": 429}))
    assert (await make_client(clock).get(URL)).status_code == 200


async def test_throttle_reason_replaces_both_wordings_even_after_the_outage_threshold(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(429))
    client = make_client(clock, throttle_delay=lambda r, now: Throttle(3600.0, "Test daily limit reached"))
    start = clock.now()
    with pytest.raises(RateLimited) as first:
        await client.get(URL)
    resume = start + timedelta(seconds=3600)
    assert str(first.value) == f"Test daily limit reached, retrying at {resume:%H:%M:%S} UTC"
    clock.advance(23 * 60)  # well past UNAVAILABLE_AFTER, pause still active
    with pytest.raises(RateLimited) as second:
        await client.get(URL)
    assert str(second.value) == str(first.value) and second.value.since is None


async def test_throttle_reason_is_dropped_once_a_plain_throttle_follows(respx_mock, clock):
    respx_mock.get(URL).mock(side_effect=[httpx.Response(429, json={"r": 1}), httpx.Response(429)])
    client = make_client(clock, throttle_delay=lambda r, now: Throttle(10.0, "Why") if r.content else None)
    with pytest.raises(RateLimited) as first:
        await client.get(URL)
    assert "Why" in str(first.value)
    clock.advance(11)
    with pytest.raises(RateLimited) as second:
        await client.get(URL)
    assert "Why" not in str(second.value)

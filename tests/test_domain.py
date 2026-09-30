from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.domain import RateLimited, floor_hour, floor_minute, from_ms, to_ms, utc
from tests.fakes import FakeClock


def test_floor_minute_and_hour():
    dt = datetime(2024, 3, 10, 14, 37, 42, 123456, tzinfo=UTC)
    assert floor_minute(dt) == datetime(2024, 3, 10, 14, 37, tzinfo=UTC)
    assert floor_hour(dt) == datetime(2024, 3, 10, 14, tzinfo=UTC)


def test_ms_roundtrip_matches_jesse_example():
    dt = datetime(2024, 1, 9, 14, 0, tzinfo=UTC)
    assert to_ms(dt) == 1704808800000
    assert from_ms(1704808800000) == dt


def test_utc_normalises_and_rejects_naive():
    cet = timezone(timedelta(hours=1))
    assert utc(datetime(2024, 1, 1, 1, 0, tzinfo=cet)) == datetime(2024, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        utc(datetime(2024, 1, 1))


def test_rate_limited_message():
    err = RateLimited("Binance", datetime(2024, 1, 1, 14, 3, 12, tzinfo=UTC))
    assert str(err) == "Binance rate limit, resuming 14:03:12 UTC"
    assert err.provider == "Binance"


async def test_fake_clock_sleep_advances_time():
    clock = FakeClock()
    wall, mono = clock.now(), clock.monotonic()
    await clock.sleep(1.5)
    assert clock.monotonic() - mono == 1.5
    assert clock.now() - wall == timedelta(seconds=1.5)
    assert clock.sleeps == [1.5]


def test_rate_limited_message_stays_short_without_an_outage_streak():
    err = RateLimited("Binance", datetime(2024, 1, 1, 14, 3, 12, tzinfo=UTC), status=429)
    assert str(err) == "Binance rate limit, resuming 14:03:12 UTC"


def test_rate_limited_message_says_unavailable_after_a_long_streak():
    since = datetime(2024, 1, 1, 14, 0, tzinfo=UTC)
    err = RateLimited("Dukascopy", datetime(2024, 1, 1, 14, 9, 30, tzinfo=UTC), since=since, status=503)
    assert str(err) == "Dukascopy unavailable since 14:00 UTC (HTTP 503), retrying at 14:09:30 UTC"
    assert (err.since, err.status) == (since, 503)

"""Core value types and errors shared by providers, services and the web layer."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
UNAVAILABLE_AFTER = timedelta(minutes=5)  # a throttle streak this long is reported as an outage


@dataclass(frozen=True, slots=True)
class Candle:
    ts: datetime  # open time, on a 1-minute UTC boundary
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Chunk:
    candles: list[Candle]
    covered_until: datetime  # exclusive end of the range this chunk fully covers


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    provider_symbol: str
    asset_class: str
    suggested_jesse_symbol: str
    name: str


class ProviderError(Exception):
    """Base class for errors raised by provider adapters."""


class TransientError(ProviderError):
    """Temporary failure (network, 5xx). The job is paused and retried later."""


class PermanentError(ProviderError):
    """Failure that retrying cannot fix (bad API key, unknown symbol)."""


class RateLimited(ProviderError):
    """The provider throttled us; nothing may be sent to it before resume_at."""

    def __init__(self, provider: str, resume_at: datetime, *, since: datetime | None = None, status: int | None = None):
        if since is None or resume_at - since < UNAVAILABLE_AFTER:
            message = f"{provider} rate limit, resuming {resume_at:%H:%M:%S} UTC"
        else:
            message = (
                f"{provider} unavailable since {since:%H:%M} UTC (HTTP {status}), retrying at {resume_at:%H:%M:%S} UTC"
            )
        super().__init__(message)
        self.provider = provider
        self.resume_at = resume_at
        self.since = since  # start of the current run of throttling responses
        self.status = status


def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime; all datetimes must be timezone-aware")
    return dt.astimezone(UTC)


def floor_minute(dt: datetime) -> datetime:
    return utc(dt).replace(second=0, microsecond=0)


def floor_hour(dt: datetime) -> datetime:
    return utc(dt).replace(minute=0, second=0, microsecond=0)


def to_ms(dt: datetime) -> int:
    return (utc(dt) - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)

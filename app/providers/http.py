"""Shared HTTP client that keeps every provider inside its rate limits."""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from app.clock import Clock
from app.domain import UNAVAILABLE_AFTER, PermanentError, RateLimited, TransientError

log = logging.getLogger(__name__)

QuotaDelay = Callable[[httpx.Response, datetime], float]

RETRY_DELAYS = (2.0, 4.0, 8.0)
DEFAULT_THROTTLE_SECONDS = 60.0
MAX_THROTTLE_SECONDS = 900.0


def no_quota_delay(response: httpx.Response, now: datetime) -> float:
    return 0.0


@dataclass(frozen=True)
class RateLimitPolicy:
    name: str  # display name used in messages, e.g. "Binance"
    rate: float  # sustained requests per second
    burst: int  # token bucket size
    concurrency: int  # max requests in flight
    ok_statuses: frozenset[int] = frozenset({200})
    throttle_statuses: frozenset[int] = frozenset({429})
    permanent_messages: Mapping[int, str] = field(default_factory=dict)
    quota_delay: QuotaDelay = no_quota_delay


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    # Non-finite, negative and zero values are unusable (zero would cause a tight retry loop).
    return seconds if seconds is not None and math.isfinite(seconds) and seconds > 0 else None


class ProviderClient:
    """One instance per provider, shared by every job that uses that provider."""

    def __init__(self, policy: RateLimitPolicy, http: httpx.AsyncClient, clock: Clock):
        self.policy = policy
        self._http = http
        self._clock = clock
        self._tokens = float(policy.burst)
        self._last_refill = clock.monotonic()
        self._blocked_until = 0.0  # monotonic; set from quota headers
        self._paused_until: datetime | None = None  # wall clock; set by throttling responses
        self._throttle_seconds = DEFAULT_THROTTLE_SECONDS
        self._throttled_since: datetime | None = None  # start of the current run of throttling responses
        self._throttle_status: int | None = None
        self._lock = asyncio.Lock()
        self._in_flight = asyncio.Semaphore(policy.concurrency)

    async def get(self, url: str, *, params=None, headers=None) -> httpx.Response:
        problem = ""
        for attempt in range(len(RETRY_DELAYS) + 1):
            await self._acquire()
            try:
                async with self._in_flight:
                    response = await self._http.get(url, params=params, headers=headers, timeout=30.0)
            except httpx.TransportError as exc:
                problem = f"network error: {exc!r}"
            else:
                status = response.status_code
                if status in self.policy.throttle_statuses:
                    raise self._throttle(response)
                self._throttle_seconds = DEFAULT_THROTTLE_SECONDS
                if not self._pause_active(self._clock.now()):  # a stale success must not end the streak
                    self._throttled_since = None
                self._apply_quota(response)
                if status in self.policy.ok_statuses:
                    return response
                if status in self.policy.permanent_messages:
                    raise PermanentError(
                        f"{self.policy.name}: {self.policy.permanent_messages[status]} "
                        f"(HTTP {status}: {response.text[:200]})"
                    )
                if status < 500:
                    raise PermanentError(f"{self.policy.name}: unexpected HTTP {status}: {response.text[:200]}")
                problem = f"HTTP {status}"
            if attempt < len(RETRY_DELAYS):
                log.warning("%s: %s, retrying in %.0fs", self.policy.name, problem, RETRY_DELAYS[attempt])
                await self._clock.sleep(RETRY_DELAYS[attempt])
        raise TransientError(f"{self.policy.name}: {problem}")

    async def _acquire(self) -> None:
        while True:
            async with self._lock:
                if self._pause_active(self._clock.now()):
                    raise self._rate_limited(self._clock.now())
                now = self._clock.monotonic()
                self._tokens = min(
                    float(self.policy.burst), self._tokens + (now - self._last_refill) * self.policy.rate
                )
                self._last_refill = now
                wait = self._blocked_until - now
                if wait <= 0:
                    if self._tokens >= 1:
                        self._tokens -= 1
                        return
                    wait = (1 - self._tokens) / self.policy.rate
            await self._clock.sleep(wait)

    def _throttle(self, response: httpx.Response) -> RateLimited:
        now = self._clock.now()
        if self._throttled_since is None:
            self._throttled_since = now
        self._throttle_status = response.status_code
        pause_active = self._pause_active(now)
        delay = _retry_after(response)
        if delay is None and pause_active:
            resume_at = self._paused_until  # same throttle event: keep the pause, don't escalate again
        else:
            if delay is None:
                delay = self._throttle_seconds
                self._throttle_seconds = min(self._throttle_seconds * 2, MAX_THROTTLE_SECONDS)
            resume_at = now + timedelta(seconds=delay)
        self._paused_until = max(self._paused_until, resume_at) if pause_active else resume_at
        log.warning("%s throttled us (HTTP %s); pausing until %s", self.policy.name, response.status_code, self._paused_until)
        return self._rate_limited(now)

    def _pause_active(self, now: datetime) -> bool:
        return self._paused_until is not None and self._paused_until > now

    def _rate_limited(self, now: datetime) -> RateLimited:
        """Report an outage (not just rate limiting) once refusals have lasted UNAVAILABLE_AFTER."""
        since = self._throttled_since
        outage = since is not None and now - since >= UNAVAILABLE_AFTER
        return RateLimited(
            self.policy.name, self._paused_until, since=since if outage else None, status=self._throttle_status
        )

    def _apply_quota(self, response: httpx.Response) -> None:
        delay = self.policy.quota_delay(response, self._clock.now())
        if delay > 0:
            self._blocked_until = max(self._blocked_until, self._clock.monotonic() + delay)

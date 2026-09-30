"""Progress percentage, ETA and human-readable durations for the UI."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.models import ACTIVE_STATUSES
from app.providers.base import Provider

NOT_RUNNING_STATUSES = ("waiting", "paused")  # no ETA while nothing is being downloaded
MIN_REQUESTS_FOR_MEASURED_RATE = 20
MIN_RUN_SECONDS_FOR_MEASURED_RATE = 1.0


@dataclass(frozen=True)
class JobProgress:
    percent: float
    eta_seconds: float | None


def job_progress(job, fetched_until: datetime | None, provider: Provider) -> JobProgress:
    if job.status == "done":
        return JobProgress(100.0, None)
    span = (job.range_end - job.range_start).total_seconds()
    cursor = max(fetched_until or job.range_start, job.range_start)
    percent = 100.0 if span <= 0 else min(100.0, (cursor - job.range_start).total_seconds() / span * 100)
    eta = None
    if job.status in ACTIVE_STATUSES and job.status not in NOT_RUNNING_STATUSES:
        if span <= 0:
            eta = 0.0
        else:
            remaining = provider.estimate_requests(cursor, job.range_end)
            if job.requests_made >= MIN_REQUESTS_FOR_MEASURED_RATE and job.run_seconds >= MIN_RUN_SECONDS_FOR_MEASURED_RATE:
                rate = job.requests_made / job.run_seconds
            else:
                rate = provider.client.policy.rate
            eta = remaining / rate if rate > 0 else None
    return JobProgress(round(percent, 1), eta)


def format_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "< 1 min"
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def estimate_text(provider: Provider, start: datetime, end: datetime) -> str:
    requests = provider.estimate_requests(start, end)
    if requests <= 0:
        return "Already up to date."
    return f"≈ {requests:,} requests · {format_duration(requests / provider.client.policy.rate)}"

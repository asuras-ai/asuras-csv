"""Job queue stored in the `jobs` table: creation, claiming and state transitions."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.exc import IntegrityError

from app.clock import Clock
from app.db import SessionFactory
from app.models import ACTIVE_STATUSES, Asset, Job
from app.providers.base import ProviderRegistry

log = logging.getLogger(__name__)

PRIORITY = {"update": 10, "backfill": 0}
BACKOFF_SECONDS = (60, 300, 900, 3600)
GIVE_UP_AFTER = timedelta(hours=24)


async def _active_job(s, asset_id: int) -> Job | None:
    return await s.scalar(select(Job).where(Job.asset_id == asset_id, Job.status.in_(ACTIVE_STATUSES)))


async def enqueue(sf: SessionFactory, registry: ProviderRegistry, clock: Clock, asset_id: int, kind: str) -> Job:
    """Create a job for the asset, or return its already active job."""
    if kind not in PRIORITY:
        raise ValueError(f"Unknown job kind {kind!r}")
    try:
        async with sf.begin() as s:
            existing = await _active_job(s, asset_id)
            if existing is not None:
                return existing
            asset = await s.get(Asset, asset_id)
            if asset is None:
                raise ValueError(f"Asset {asset_id} not found")
            now = clock.now()
            start = asset.fetched_until or asset.start_date
            end = max(registry.get(asset.provider).available_until(now), start)
            pending = start < end
            job = Job(
                asset_id=asset_id,
                kind=kind,
                priority=PRIORITY[kind],
                status="queued" if pending else "done",
                range_start=start,
                range_end=end,
                last_progress_at=now,
                queued_at=now,
                finished_at=None if pending else now,
            )
            s.add(job)
        return job
    except IntegrityError:
        # Another request created the active job concurrently.
        async with sf() as s:
            existing = await _active_job(s, asset_id)
        if existing is None:
            raise
        return existing


async def enqueue_all(sf: SessionFactory, registry: ProviderRegistry, clock: Clock, kind: str = "update") -> list[Job]:
    async with sf() as s:
        assets = (await s.execute(select(Asset.id, Asset.provider).where(Asset.enabled).order_by(Asset.id))).all()
    created = []
    for asset_id, provider in assets:
        if registry.find(provider) is None:
            # One stale asset must not abort the scheduled update for everyone else.
            log.warning("skipping asset %d: unknown provider %r", asset_id, provider)
            continue
        created.append(await enqueue(sf, registry, clock, asset_id, kind))
    return created


async def claim_next(sf: SessionFactory, clock: Clock) -> int | None:
    now = clock.now()
    async with sf.begin() as s:
        job = await s.scalar(
            select(Job)
            .where(
                or_(
                    Job.status == "queued",
                    and_(Job.status.in_(("waiting", "paused")), Job.next_attempt_at <= now),
                )
            )
            .order_by(Job.priority.desc(), Job.queued_at, Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        job.status = "running"
        job.started_at = job.started_at or now
        job.next_attempt_at = None
        job.status_detail = None
        if job.attempt == 0:
            job.last_progress_at = now  # the 24 h give-up counts from when the job last ran, not from queueing
        return job.id


async def _transition(sf: SessionFactory, job_id: int, elapsed: float, **values) -> bool:
    async with sf.begin() as s:
        result = await s.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running")
            .values(run_seconds=Job.run_seconds + elapsed, **values)
        )
    return result.rowcount == 1


async def finish(sf: SessionFactory, clock: Clock, job_id: int, elapsed: float) -> bool:
    return await _transition(
        sf, job_id, elapsed, status="done", finished_at=clock.now(), status_detail=None, next_attempt_at=None
    )


async def requeue(sf: SessionFactory, clock: Clock, job_id: int, elapsed: float) -> bool:
    return await _transition(sf, job_id, elapsed, status="queued", queued_at=clock.now())


async def wait(sf: SessionFactory, job_id: int, resume_at: datetime, detail: str, elapsed: float) -> bool:
    return await _transition(sf, job_id, elapsed, status="waiting", next_attempt_at=resume_at, status_detail=detail)


async def fail(sf: SessionFactory, clock: Clock, job_id: int, error: str, elapsed: float) -> bool:
    return await _transition(
        sf, job_id, elapsed, status="failed", error=error, status_detail=None, finished_at=clock.now()
    )


async def pause(sf: SessionFactory, clock: Clock, job_id: int, error: str, elapsed: float) -> None:
    """Transient failure: retry later with backoff, or give up after 24 h without progress."""
    now = clock.now()
    async with sf.begin() as s:
        job = await s.scalar(select(Job).where(Job.id == job_id, Job.status == "running").with_for_update())
        if job is None:
            return
        job.run_seconds += elapsed
        job.attempt += 1
        job.error = error
        if now - (job.last_progress_at or now) >= GIVE_UP_AFTER:
            job.status = "failed"
            job.finished_at = now
            job.status_detail = None
            job.error = f"Gave up after 24 h without progress: {error}"
        else:
            delay = BACKOFF_SECONDS[min(job.attempt, len(BACKOFF_SECONDS)) - 1]
            retry_at = now + timedelta(seconds=delay)
            job.status = "paused"
            job.next_attempt_at = retry_at
            job.status_detail = f"{error} — retrying at {retry_at:%H:%M} UTC"


async def cancel(sf: SessionFactory, clock: Clock, job_id: int) -> bool:
    async with sf.begin() as s:
        result = await s.execute(
            update(Job)
            .where(Job.id == job_id, Job.status.in_(ACTIVE_STATUSES))
            .values(status="cancelled", finished_at=clock.now(), next_attempt_at=None, status_detail=None)
        )
    return result.rowcount == 1


async def recover(sf: SessionFactory) -> int:
    """On startup: jobs interrupted mid-run (or waiting on a rate limit) go back to the queue."""
    async with sf.begin() as s:
        result = await s.execute(
            update(Job)
            .where(Job.status.in_(("running", "waiting")))
            .values(status="queued", next_attempt_at=None, status_detail=None)
        )
    return result.rowcount


async def get_job(sf: SessionFactory, job_id: int) -> Job | None:
    async with sf() as s:
        return await s.get(Job, job_id)


async def latest_jobs_by_asset(sf: SessionFactory) -> dict[int, Job]:
    async with sf() as s:
        rows = await s.scalars(select(Job).ext(distinct_on(Job.asset_id)).order_by(Job.asset_id, Job.id.desc()))
        return {job.asset_id: job for job in rows}


async def status_counts(sf: SessionFactory) -> dict[str, int]:
    async with sf() as s:
        rows = await s.execute(select(Job.status, func.count()).group_by(Job.status))
        return {status: count for status, count in rows}


JOB_FILTERS = {"active": ACTIVE_STATUSES, "failed": ("failed",)}


async def list_recent(
    sf: SessionFactory, limit: int = 200, *, status: str | None = None, asset_id: int | None = None
) -> list[tuple[Job, Asset]]:
    stmt = select(Job, Asset).join(Asset, Asset.id == Job.asset_id).order_by(Job.id.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(Job.status.in_(JOB_FILTERS[status]))
    if asset_id is not None:
        stmt = stmt.where(Job.asset_id == asset_id)
    async with sf() as s:
        return [(job, asset) for job, asset in await s.execute(stmt)]


async def last_done_by_asset(sf: SessionFactory) -> dict[int, datetime]:
    """When each asset's most recent successful job finished."""
    async with sf() as s:
        rows = await s.execute(select(Job.asset_id, func.max(Job.finished_at)).where(Job.status == "done").group_by(Job.asset_id))
        return {asset_id: finished for asset_id, finished in rows}


async def prune(sf: SessionFactory, clock: Clock, keep_days: int = 30) -> int:
    """Delete finished jobs older than `keep_days`, always keeping each asset's latest job. Returns the count."""
    cutoff = clock.now() - timedelta(days=keep_days)
    latest = select(func.max(Job.id)).group_by(Job.asset_id)
    async with sf.begin() as s:
        result = await s.execute(
            delete(Job).where(
                Job.status.in_(("done", "failed", "cancelled")),
                Job.finished_at < cutoff,
                Job.id.not_in(latest),
            )
        )
    return result.rowcount

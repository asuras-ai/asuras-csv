"""Job queue stored in the `jobs` table: creation, claiming and state transitions."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.clock import Clock
from app.db import SessionFactory
from app.models import ACTIVE_STATUSES, Asset, Job
from app.providers.base import ProviderRegistry

PRIORITY = {"update": 10, "backfill": 0}
BACKOFF_SECONDS = (60, 300, 900, 3600)
GIVE_UP_AFTER = timedelta(hours=24)


async def _active_job(s, asset_id: int) -> Job | None:
    return await s.scalar(select(Job).where(Job.asset_id == asset_id, Job.status.in_(ACTIVE_STATUSES)))


async def enqueue(sf: SessionFactory, registry: ProviderRegistry, clock: Clock, asset_id: int, kind: str) -> Job:
    """Create a job for the asset, or return its already active job."""
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
        asset_ids = (await s.scalars(select(Asset.id).where(Asset.enabled).order_by(Asset.id))).all()
    return [await enqueue(sf, registry, clock, asset_id, kind) for asset_id in asset_ids]


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
            .order_by(Job.priority.desc(), Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        job.status = "running"
        job.started_at = job.started_at or now
        job.next_attempt_at = None
        job.status_detail = None
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


async def requeue(sf: SessionFactory, job_id: int, elapsed: float) -> bool:
    return await _transition(sf, job_id, elapsed, status="queued")


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
        rows = await s.scalars(select(Job).distinct(Job.asset_id).order_by(Job.asset_id, Job.id.desc()))
        return {job.asset_id: job for job in rows}


async def list_recent(sf: SessionFactory, limit: int = 200) -> list[tuple[Job, Asset]]:
    async with sf() as s:
        result = await s.execute(
            select(Job, Asset).join(Asset, Asset.id == Job.asset_id).order_by(Job.id.desc()).limit(limit)
        )
        return list(result.tuples())

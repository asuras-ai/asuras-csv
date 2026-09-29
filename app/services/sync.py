"""The update logic: fetch chunks and commit candles together with the cursor."""
from __future__ import annotations

from contextlib import aclosing
from enum import StrEnum

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import Clock
from app.db import SessionFactory
from app.domain import Candle, Chunk
from app.models import Asset, CandleRow, Job
from app.providers.base import ProviderRegistry

SLICE_SECONDS = 60.0
INSERT_BATCH = 4000  # 7 params per row stays below PostgreSQL's 32767-parameter limit


class SliceOutcome(StrEnum):
    DONE = "done"  # reached range_end
    YIELDED = "yielded"  # slice used up; requeue so other jobs get a turn
    STOPPED = "stopped"  # job was cancelled or deleted


async def insert_candles(session: AsyncSession, asset_id: int, candles: list[Candle]) -> int:
    added = 0
    for i in range(0, len(candles), INSERT_BATCH):
        rows = [
            {"asset_id": asset_id, "ts": c.ts, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
            for c in candles[i : i + INSERT_BATCH]
        ]
        stmt = pg_insert(CandleRow).values(rows).on_conflict_do_nothing(index_elements=["asset_id", "ts"])
        added += (await session.execute(stmt)).rowcount
    return added


async def _commit_chunk(sf: SessionFactory, clock: Clock, job_id: int, asset_id: int, chunk: Chunk) -> bool:
    """Store one chunk and advance the cursor atomically. Returns False if the job is no longer running."""
    async with sf.begin() as s:
        running = await s.scalar(select(Job.id).where(Job.id == job_id, Job.status == "running").with_for_update())
        if running is None:
            return False
        added = await insert_candles(s, asset_id, chunk.candles)
        await s.execute(
            update(Asset)
            .where(Asset.id == asset_id)
            .values(
                fetched_until=func.greatest(func.coalesce(Asset.fetched_until, chunk.covered_until), chunk.covered_until)
            )
        )
        await s.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                requests_made=Job.requests_made + 1,
                candles_added=Job.candles_added + added,
                attempt=0,
                last_progress_at=clock.now(),
                status_detail=None,
            )
        )
    return True


async def run_slice(
    sf: SessionFactory,
    registry: ProviderRegistry,
    clock: Clock,
    job_id: int,
    slice_seconds: float = SLICE_SECONDS,
) -> SliceOutcome:
    """Run one job until done or for about `slice_seconds`. Provider errors propagate."""
    async with sf() as s:
        job = await s.get(Job, job_id)
        asset = await s.get(Asset, job.asset_id) if job is not None else None
    if job is None or asset is None:
        return SliceOutcome.STOPPED
    provider = registry.get(asset.provider)
    start = asset.fetched_until or asset.start_date
    if start >= job.range_end:
        return SliceOutcome.DONE
    deadline = clock.monotonic() + slice_seconds
    async with aclosing(provider.fetch(asset.provider_symbol, start, job.range_end)) as chunks:
        async for chunk in chunks:
            if not await _commit_chunk(sf, clock, job.id, asset.id, chunk):
                return SliceOutcome.STOPPED
            if clock.monotonic() >= deadline and chunk.covered_until < job.range_end:
                return SliceOutcome.YIELDED
    return SliceOutcome.DONE

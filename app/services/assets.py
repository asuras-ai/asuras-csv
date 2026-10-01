"""Asset CRUD and cached per-asset candle statistics."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.clock import Clock
from app.db import SessionFactory
from app.models import ACTIVE_STATUSES, Asset, CandleRow, Job

log = logging.getLogger(__name__)

JESSE_SYMBOL = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")


class DuplicateAsset(ValueError):
    pass


def _check_jesse_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not JESSE_SYMBOL.match(symbol):
        raise ValueError(f"Jesse symbol must look like BASE-QUOTE, e.g. BTC-USDT (got {symbol!r})")
    return symbol


async def create_asset(
    sf: SessionFactory, *, provider: str, provider_symbol: str, asset_class: str, jesse_symbol: str, start_date: date
) -> Asset:
    asset = Asset(
        provider=provider,
        provider_symbol=provider_symbol,
        asset_class=asset_class,
        jesse_symbol=_check_jesse_symbol(jesse_symbol),
        start_date=datetime.combine(start_date, time(), UTC),
    )
    try:
        async with sf.begin() as s:
            s.add(asset)
    except IntegrityError:
        raise DuplicateAsset(f"{provider_symbol} from {provider} already exists") from None
    return asset


async def list_assets(sf: SessionFactory) -> list[Asset]:
    async with sf() as s:
        return list(await s.scalars(select(Asset).order_by(Asset.jesse_symbol, Asset.id)))


async def get_asset(sf: SessionFactory, asset_id: int) -> Asset | None:
    async with sf() as s:
        return await s.get(Asset, asset_id)


async def update_asset(sf: SessionFactory, asset_id: int, *, jesse_symbol: str, enabled: bool) -> None:
    symbol = _check_jesse_symbol(jesse_symbol)
    async with sf.begin() as s:
        await s.execute(update(Asset).where(Asset.id == asset_id).values(jesse_symbol=symbol, enabled=enabled))


async def _cancel_active_jobs(s, asset_id: int) -> None:
    await s.execute(
        update(Job)
        .where(Job.asset_id == asset_id, Job.status.in_(ACTIVE_STATUSES))
        .values(status="cancelled", finished_at=func.now(), next_attempt_at=None, status_detail=None)
    )


async def delete_asset(sf: SessionFactory, asset_id: int) -> None:
    # Cancel active jobs first to avoid lock-order deadlock:
    # Sync locks job then touches asset via candle FK; delete locks asset then cascades to jobs.
    async with sf.begin() as s:
        await _cancel_active_jobs(s, asset_id)
    # Then delete candles and asset in a separate transaction
    async with sf.begin() as s:
        await s.execute(delete(CandleRow).where(CandleRow.asset_id == asset_id))
        await s.execute(delete(Asset).where(Asset.id == asset_id))  # jobs cascade


@dataclass(frozen=True)
class AssetStats:
    first: datetime | None
    last: datetime | None
    count: int


EMPTY_STATS = AssetStats(None, None, 0)


class StatsCache:
    """Counting candles scans the whole table, so results are reused for `ttl_seconds`.

    - Single-flight: at most ONE load runs at a time. Concurrent callers share it.
    - `invalidate()` only marks the value expired. Later gets keep serving the stale value while one refresh runs;
      only the first-ever load (or one after `clear()`) makes a caller wait. An invalidation that lands while a
      load is running sets a dirty flag, so exactly one more load follows it (no parallel loads, no storm).
    - `clear()` also drops the value, so the next get blocks on a fresh load. Use it only where a stale value
      would be wrong. Nothing needs it today: after delete_asset a stale entry is harmless because rows for
      missing assets are never rendered, so the delete route just calls `invalidate()`.
    - A refresh that raises is logged as a warning; the value stays expired so the next get retries.
    """

    def __init__(self, clock: Clock | None, ttl_seconds: float = 120.0):
        self._clock = clock
        self._ttl = ttl_seconds
        self._value: dict[int, AssetStats] | None = None
        self._loaded_at = float("-inf")
        self._generation = 0  # bumped by invalidate() and clear()
        self._epoch = 0  # bumped by clear(): a load started before it must not store its result
        self._dirty = False
        self._task: asyncio.Task | None = None

    def invalidate(self) -> None:
        self._generation += 1
        self._loaded_at = float("-inf")
        if self._task is not None and not self._task.done():
            self._dirty = True

    def clear(self) -> None:
        self.invalidate()
        self._epoch += 1
        self._value = None

    def _now(self) -> float:
        return self._clock.monotonic() if self._clock else 0.0

    async def _load(self, sf: SessionFactory) -> dict[int, AssetStats]:
        started = self._now()
        generation, epoch = self._generation, self._epoch
        value = await self._load_uncached(sf)
        if epoch == self._epoch:
            self._value = value
            # Invalidated mid-load: keep the value (better than nothing) but leave it expired.
            if generation == self._generation:
                self._loaded_at = started
        return value

    def _refresh(self, sf: SessionFactory) -> asyncio.Task:
        if self._task is not None and not self._task.done():
            return self._task
        self._dirty = False
        task = asyncio.create_task(self._load(sf), name="stats-refresh")
        task.add_done_callback(lambda t: self._finished(sf, t))
        self._task = task
        return task

    def _finished(self, sf: SessionFactory, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()  # always retrieved, so asyncio never reports it as unretrieved
        if exc is not None:
            log.warning("stats refresh failed; will retry on the next request", exc_info=exc)
            return
        if self._dirty and self._task is task:
            self._refresh(sf)  # exactly one coalesced follow-up for all invalidations that landed mid-load

    async def get(self, sf: SessionFactory) -> dict[int, AssetStats]:
        if self._clock is None:
            return await self._load_uncached(sf)
        while True:
            if self._value is not None:
                if self._now() - self._loaded_at > self._ttl:
                    self._refresh(sf)
                return self._value
            await asyncio.shield(self._refresh(sf))

    async def _load_uncached(self, sf: SessionFactory) -> dict[int, AssetStats]:
        async with sf() as s:
            rows = await s.execute(
                select(CandleRow.asset_id, func.min(CandleRow.ts), func.max(CandleRow.ts), func.count()).group_by(
                    CandleRow.asset_id
                )
            )
            return {asset_id: AssetStats(first, last, count) for asset_id, first, last, count in rows}

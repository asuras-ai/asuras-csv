"""Asset CRUD and cached per-asset candle statistics."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.clock import Clock
from app.db import SessionFactory
from app.models import ACTIVE_STATUSES, Asset, CandleRow, Job

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


async def delete_asset(sf: SessionFactory, asset_id: int) -> None:
    # Cancel active jobs first to avoid lock-order deadlock:
    # Sync locks job then touches asset via candle FK; delete locks asset then cascades to jobs.
    async with sf.begin() as s:
        await s.execute(
            update(Job)
            .where(Job.asset_id == asset_id, Job.status.in_(ACTIVE_STATUSES))
            .values(status="cancelled", finished_at=func.now(), next_attempt_at=None, status_detail=None)
        )
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
    """Counting candles scans the whole table, so results are reused for `ttl_seconds`."""

    def __init__(self, clock: Clock | None, ttl_seconds: float = 120.0):
        self._clock = clock
        self._ttl = ttl_seconds
        self._value: dict[int, AssetStats] | None = None
        self._loaded_at = 0.0

    def invalidate(self) -> None:
        self._value = None

    async def get(self, sf: SessionFactory) -> dict[int, AssetStats]:
        now = self._clock.monotonic() if self._clock else 0.0
        if self._value is None or self._clock is None or now - self._loaded_at > self._ttl:
            async with sf() as s:
                rows = await s.execute(
                    select(CandleRow.asset_id, func.min(CandleRow.ts), func.max(CandleRow.ts), func.count()).group_by(
                        CandleRow.asset_id
                    )
                )
                self._value = {asset_id: AssetStats(first, last, count) for asset_id, first, last, count in rows}
            self._loaded_at = now
        return self._value

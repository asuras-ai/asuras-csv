"""Jesse "Custom Data" CSV export, streamed straight from the database."""
from __future__ import annotations

import logging
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select

from app.db import SessionFactory
from app.domain import to_ms
from app.models import CandleRow

log = logging.getLogger(__name__)

HEADER = "timestamp,open,close,high,low,volume\n"
FLUSH_ROWS = 5000


@dataclass(frozen=True)
class ExportRange:
    first: datetime
    last: datetime


def day_bounds(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    """Dates to a [start, end) datetime range; `end` is inclusive by day."""
    return (
        datetime.combine(start, time(), UTC) if start else None,
        datetime.combine(end + timedelta(days=1), time(), UTC) if end else None,
    )


def is_valid(o, h, l, c, v) -> bool:
    if any(x is None or math.isnan(x) or math.isinf(x) for x in (o, h, l, c, v)):
        return False
    return h >= max(o, c, l) and l <= min(o, c, h) and v >= 0


def _num(x: float) -> str:
    text = f"{x:.10f}".rstrip("0").rstrip(".")
    return text or "0"


def format_row(ts: datetime, open_: float, close: float, high: float, low: float, volume: float) -> str:
    return f"{to_ms(ts)},{_num(open_)},{_num(close)},{_num(high)},{_num(low)},{_num(volume)}\n"


def _filters(asset_id: int, start: datetime | None, end: datetime | None) -> list:
    conditions = [CandleRow.asset_id == asset_id]
    if start is not None:
        conditions.append(CandleRow.ts >= start)
    if end is not None:
        conditions.append(CandleRow.ts < end)
    return conditions


async def export_range(sf: SessionFactory, asset_id: int, start: datetime | None, end: datetime | None) -> ExportRange | None:
    async with sf() as s:
        first, last = (
            await s.execute(select(func.min(CandleRow.ts), func.max(CandleRow.ts)).where(*_filters(asset_id, start, end)))
        ).one()
    return ExportRange(first, last) if first is not None else None


async def stream_csv(sf: SessionFactory, asset_id: int, start: datetime | None, end: datetime | None) -> AsyncIterator[str]:
    yield HEADER
    skipped = 0
    buffer: list[str] = []
    stmt = (
        select(CandleRow.ts, CandleRow.open, CandleRow.close, CandleRow.high, CandleRow.low, CandleRow.volume)
        .where(*_filters(asset_id, start, end))
        .order_by(CandleRow.ts)
        .execution_options(yield_per=FLUSH_ROWS)
    )
    async with sf() as s:
        result = await s.stream(stmt)
        async for ts, o, c, h, l, v in result:
            if not is_valid(o, h, l, c, v):
                skipped += 1
                continue
            buffer.append(format_row(ts, o, c, h, l, v))
            if len(buffer) >= FLUSH_ROWS:
                yield "".join(buffer)
                buffer.clear()
    if buffer:
        yield "".join(buffer)
    if skipped:
        log.warning("export of asset %s: skipped %d invalid candles", asset_id, skipped)


def export_filename(jesse_symbol: str, rng: ExportRange) -> str:
    return f"{jesse_symbol}_{rng.first:%Y-%m-%d}_{rng.last:%Y-%m-%d}.csv"

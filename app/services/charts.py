"""OHLCV-only candle aggregation for the chart page, done in SQL with TimescaleDB time_bucket."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Float, func, select

from app.db import SessionFactory
from app.models import CandleRow
from app.services.export import _valid

MAX_BARS = 2000

# range -> (window length or None for everything, bucket width)
RANGES: dict[str, tuple[timedelta | None, timedelta]] = {
    "1D": (timedelta(days=1), timedelta(minutes=1)),
    "1W": (timedelta(weeks=1), timedelta(minutes=5)),
    "1M": (timedelta(days=30), timedelta(minutes=30)),
    "6M": (timedelta(days=183), timedelta(hours=4)),
    "1Y": (timedelta(days=365), timedelta(days=1)),
    "All": (None, timedelta(days=1)),
}
DEFAULT_RANGE = "1W"


def pick_bucket(range_name: str, span_days: float) -> timedelta:
    bucket = RANGES[range_name][1]
    if range_name == "All" and span_days > MAX_BARS:
        return timedelta(weeks=1)
    return bucket


def interval_label(bucket: timedelta) -> str:
    seconds = int(bucket.total_seconds())
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


async def candles_for_chart(sf: SessionFactory, asset_id: int, range_name: str) -> tuple[timedelta, list[dict]]:
    window, _ = RANGES[range_name]
    async with sf() as s:
        first, last = (
            await s.execute(
                select(func.min(CandleRow.ts), func.max(CandleRow.ts)).where(CandleRow.asset_id == asset_id, _valid())
            )
        ).one()
        if last is None:
            return pick_bucket(range_name, 0), []
        start: datetime = first if window is None else last - window
        bucket = pick_bucket(range_name, (last - start).total_seconds() / 86400 + 1)
        b = func.time_bucket(bucket, CandleRow.ts).label("b")
        stmt = (
            select(
                b,
                func.first(CandleRow.open, CandleRow.ts, type_=Float),
                func.max(CandleRow.high),
                func.min(CandleRow.low),
                func.last(CandleRow.close, CandleRow.ts, type_=Float),
                func.sum(CandleRow.volume),
            )
            .where(CandleRow.asset_id == asset_id, CandleRow.ts >= start, CandleRow.ts <= last, _valid())
            .group_by(b)
            .order_by(b.desc())
            .limit(MAX_BARS + 50)
        )
        rows = (await s.execute(stmt)).all()
    candles = [
        {"time": int(t.timestamp()), "open": o, "high": h, "low": lo, "close": c, "volume": v}
        for t, o, h, lo, c, v in reversed(rows)
    ]
    return bucket, candles

"""Multi-asset ZIP of Jesse CSVs, built incrementally in a spooled temp file (bounded memory)."""
from __future__ import annotations

import asyncio
import tempfile
import zipfile
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import IO

from app.db import SessionFactory
from app.models import Asset
from app.services.export import export_filename, export_range, stream_csv

SPOOL_BYTES = 50 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


def _unique(name: str, taken: set[str]) -> str:
    candidate, n = name, 1
    while candidate in taken:
        n += 1
        stem, dot, ext = name.rpartition(".")
        candidate = f"{stem}_{n}{dot}{ext}"
    taken.add(candidate)
    return candidate


async def build_zip(sf: SessionFactory, assets: list[Asset], start: datetime | None, end: datetime | None) -> IO[bytes] | None:
    """Return a rewound temp file holding the ZIP, or None when no asset has candles in the range."""
    tmp = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)  # noqa: SIM115 - closed by the caller / on error
    try:
        names: set[str] = set()
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for asset in assets:
                rng = await export_range(sf, asset.id, start, end)
                if rng is None:
                    continue
                name = _unique(export_filename(asset.jesse_symbol, rng), names)
                with zf.open(name, "w", force_zip64=True) as entry:
                    async for chunk in stream_csv(sf, asset.id, rng.first, rng.last + timedelta(minutes=1)):
                        await asyncio.to_thread(entry.write, chunk.encode("utf-8"))
        if not names:
            tmp.close()
            return None
        tmp.seek(0)
        return tmp
    except BaseException:
        tmp.close()
        raise


def iter_and_close(f: IO[bytes]) -> Iterator[bytes]:
    try:
        while chunk := f.read(CHUNK_BYTES):
            yield chunk
    finally:
        f.close()

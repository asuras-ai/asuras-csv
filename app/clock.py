"""Time source, injectable so tests can control wall and monotonic time."""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime


class Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

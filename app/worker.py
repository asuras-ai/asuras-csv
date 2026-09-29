"""In-process worker pool that runs queued jobs."""
from __future__ import annotations

import asyncio
import logging

from app.clock import Clock
from app.db import SessionFactory
from app.domain import PermanentError, RateLimited, TransientError
from app.providers.base import ProviderRegistry
from app.services import jobs, sync
from app.services.sync import SLICE_SECONDS, SliceOutcome

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        sf: SessionFactory,
        registry: ProviderRegistry,
        clock: Clock,
        concurrency: int = 3,
        *,
        poll_interval: float = 1.0,
        slice_seconds: float = SLICE_SECONDS,
    ):
        self._sf = sf
        self._registry = registry
        self._clock = clock
        self._target = concurrency
        self._poll_interval = poll_interval
        self._slice_seconds = slice_seconds
        self._slots: dict[int, asyncio.Task] = {}
        self._running = False

    def start(self) -> None:
        self._running = True
        self._fill()

    def resize(self, concurrency: int) -> None:
        """Change the number of parallel workers; extra workers exit after their current job."""
        self._target = concurrency
        if self._running:
            self._fill()

    async def stop(self) -> None:
        # Jobs interrupted here stay 'running' and are re-queued by jobs.recover() on the next start.
        self._running = False
        tasks = list(self._slots.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._slots.clear()

    def _fill(self) -> None:
        for index in range(self._target):
            task = self._slots.get(index)
            if task is None or task.done():
                self._slots[index] = asyncio.create_task(self._loop(index), name=f"worker-{index}")

    async def _loop(self, index: int) -> None:
        while self._running and index < self._target:
            try:
                job_id = await jobs.claim_next(self._sf, self._clock)
                if job_id is None:
                    await self._clock.sleep(self._poll_interval)
                    continue
                await self.run_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker %d: unexpected error", index)
                await self._clock.sleep(self._poll_interval)

    async def _settle(self, transition, *args) -> None:
        """Retry a job transition until it succeeds, so a DB outage never leaves the job stuck 'running'."""
        delay = self._poll_interval
        while True:
            try:
                await transition(*args)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job transition %s failed; retrying in %.1fs", transition.__name__, delay)
                await self._clock.sleep(delay)
                delay = min(delay * 2, 60.0)

    async def run_job(self, job_id: int) -> None:
        began = self._clock.monotonic()

        def elapsed() -> float:
            return self._clock.monotonic() - began

        try:
            outcome = await sync.run_slice(self._sf, self._registry, self._clock, job_id, self._slice_seconds)
        except RateLimited as exc:
            await self._settle(jobs.wait, self._sf, job_id, exc.resume_at, str(exc), elapsed())
        except PermanentError as exc:
            log.warning("job %d failed: %s", job_id, exc)
            await self._settle(jobs.fail, self._sf, self._clock, job_id, str(exc), elapsed())
        except TransientError as exc:
            log.warning("job %d paused: %s", job_id, exc)
            await self._settle(jobs.pause, self._sf, self._clock, job_id, str(exc), elapsed())
        except Exception as exc:
            log.exception("job %d: unexpected error", job_id)
            await self._settle(jobs.pause, self._sf, self._clock, job_id, f"Unexpected error: {exc!r}", elapsed())
        else:
            if outcome is SliceOutcome.DONE:
                await self._settle(jobs.finish, self._sf, self._clock, job_id, elapsed())
            elif outcome is SliceOutcome.YIELDED:
                await self._settle(jobs.requeue, self._sf, job_id, elapsed())

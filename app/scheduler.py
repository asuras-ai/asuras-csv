"""Cron schedule that queues an update for every enabled asset."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.clock import Clock
from app.db import SessionFactory
from app.providers.base import ProviderRegistry
from app.services import jobs
from app.services.settings import AppSettings

log = logging.getLogger(__name__)
JOB_ID = "update-all"
PRUNE_JOB_ID = "prune-jobs"


class UpdateScheduler:
    def __init__(self, sf: SessionFactory, registry: ProviderRegistry, clock: Clock):
        self._sf = sf
        self._registry = registry
        self._clock = clock
        self._scheduler = AsyncIOScheduler(timezone=UTC)

    def start(self) -> None:
        self._scheduler.add_job(
            self.prune,
            CronTrigger(hour=3, minute=30, timezone=UTC),
            id=PRUNE_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def apply(self, settings: AppSettings) -> None:
        # Build the trigger first - if cron is invalid, ValueError propagates before modifying state
        if settings.schedule_enabled:
            trigger = CronTrigger.from_crontab(settings.schedule_cron, timezone=UTC)

        # Now it's safe to modify the job
        if self._scheduler.get_job(JOB_ID):
            self._scheduler.remove_job(JOB_ID)

        if settings.schedule_enabled:
            self._scheduler.add_job(
                self.run_now,
                trigger,
                id=JOB_ID,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )

    async def prune(self) -> int:
        deleted = await jobs.prune(self._sf, self._clock)
        log.info("pruned %d old finished jobs", deleted)
        return deleted

    async def run_now(self) -> int:
        created = await jobs.enqueue_all(self._sf, self._registry, self._clock)
        log.info("scheduled update queued %d jobs", len(created))
        return len(created)

    def next_run(self) -> datetime | None:
        job = self._scheduler.get_job(JOB_ID)
        return getattr(job, "next_run_time", None) if job else None

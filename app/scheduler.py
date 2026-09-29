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


class UpdateScheduler:
    def __init__(self, sf: SessionFactory, registry: ProviderRegistry, clock: Clock):
        self._sf = sf
        self._registry = registry
        self._clock = clock
        self._scheduler = AsyncIOScheduler(timezone=UTC)

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    def apply(self, settings: AppSettings) -> None:
        if self._scheduler.get_job(JOB_ID):
            self._scheduler.remove_job(JOB_ID)
        if settings.schedule_enabled:
            self._scheduler.add_job(
                self.run_now,
                CronTrigger.from_crontab(settings.schedule_cron, timezone=UTC),
                id=JOB_ID,
                max_instances=1,
                coalesce=True,
            )

    async def run_now(self) -> int:
        created = await jobs.enqueue_all(self._sf, self._registry, self._clock)
        log.info("scheduled update queued %d jobs", len(created))
        return len(created)

    def next_run(self) -> datetime | None:
        job = self._scheduler.get_job(JOB_ID)
        return job.next_run_time if job else None

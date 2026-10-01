from datetime import UTC

import pytest

from app.providers.base import ProviderRegistry
from app.scheduler import UpdateScheduler
from app.services import jobs
from app.services.settings import AppSettings
from tests.fakes import FakeProvider, make_asset


def settings(enabled: bool, cron: str = "0 */6 * * *") -> AppSettings:
    return AppSettings(enabled, cron, 3, "", "", False)


async def test_apply_adds_or_removes_the_cron_job(sf, clock):
    scheduler = UpdateScheduler(sf, ProviderRegistry([FakeProvider(clock)]), clock)
    scheduler.start()
    try:
        scheduler.apply(settings(True))
        next_run = scheduler.next_run()
        assert next_run is not None and next_run.hour % 6 == 0 and next_run.minute == 0
        scheduler.apply(settings(False))
        assert scheduler.next_run() is None
    finally:
        scheduler.shutdown()


async def test_run_now_queues_updates_for_enabled_assets(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock)])
    enabled = await make_asset(sf, provider_symbol="A")
    await make_asset(sf, provider_symbol="B", enabled=False)
    assert await UpdateScheduler(sf, registry, clock).run_now() == 1
    assert list((await jobs.latest_jobs_by_asset(sf)).keys()) == [enabled.id]


async def test_next_run_safe_before_start(sf, clock):
    """next_run() must not crash when apply() runs before start()."""
    scheduler = UpdateScheduler(sf, ProviderRegistry([FakeProvider(clock)]), clock)
    # apply() before start() - job may not have next_run_time attribute yet
    scheduler.apply(settings(True))
    # next_run() should not crash
    result = scheduler.next_run()
    # After start(), next_run() should be available and in UTC
    scheduler.start()
    try:
        next_run = scheduler.next_run()
        assert next_run is not None
        assert next_run.tzinfo.utcoffset(None) == UTC.utcoffset(None)
    finally:
        scheduler.shutdown()


async def test_apply_is_atomic(sf, clock):
    """apply() must not partially update on invalid cron."""
    scheduler = UpdateScheduler(sf, ProviderRegistry([FakeProvider(clock)]), clock)
    scheduler.start()
    try:
        # Apply a valid cron
        scheduler.apply(settings(True, "0 */6 * * *"))
        first_run = scheduler.next_run()
        assert first_run is not None

        # Apply an invalid cron - should raise ValueError and not modify the job
        with pytest.raises(ValueError):
            scheduler.apply(settings(True, "invalid cron"))

        # The original job should still be there with the same schedule
        assert scheduler.next_run() == first_run
    finally:
        scheduler.shutdown()


async def test_shutdown_safe_before_start(sf, clock):
    """shutdown() must not raise if scheduler was never started."""
    scheduler = UpdateScheduler(sf, ProviderRegistry([FakeProvider(clock)]), clock)
    # shutdown() without start() should not raise
    scheduler.shutdown()


async def test_prune_job_is_registered_regardless_of_update_schedule(sf, clock):
    scheduler = UpdateScheduler(sf, ProviderRegistry([FakeProvider(clock)]), clock)
    scheduler.start()
    try:
        for enabled in (False, True, False):
            scheduler.apply(settings(enabled))
            job = scheduler._scheduler.get_job("prune-jobs")
            assert job is not None
            assert job.coalesce and job.misfire_grace_time == 3600
            assert (job.next_run_time.hour, job.next_run_time.minute) == (3, 30)
    finally:
        scheduler.shutdown()


async def test_prune_job_is_registered_by_start_even_without_apply(sf, clock):
    scheduler = UpdateScheduler(sf, ProviderRegistry([FakeProvider(clock)]), clock)
    scheduler.start()
    try:
        assert scheduler._scheduler.get_job("prune-jobs") is not None
    finally:
        scheduler.shutdown()

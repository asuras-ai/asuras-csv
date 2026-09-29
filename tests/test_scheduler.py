from app.providers.base import ProviderRegistry
from app.scheduler import UpdateScheduler
from app.services import jobs
from app.services.settings import AppSettings
from tests.fakes import FakeProvider, make_asset


def settings(enabled: bool) -> AppSettings:
    return AppSettings(enabled, "0 */6 * * *", 3, "", "", False)


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

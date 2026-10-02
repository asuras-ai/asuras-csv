from app.clock import Clock
from app.config import EnvConfig
from app.main import create_app
from app.providers.base import ProviderRegistry
from app.services.assets import StatsCache
from tests.fakes import FakeProvider


async def test_finished_jobs_refresh_both_stats_and_sparklines(sf, monkeypatch):
    calls = []
    monkeypatch.setattr(StatsCache, "refresh_soon", lambda self, session_factory: calls.append(type(self).__name__))
    env = EnvConfig(_env_file=None, database_url="postgresql+asyncpg://unused@localhost/unused", alpaca_key_id="", alpaca_secret_key="")
    app = create_app(env, sf=sf, clock=Clock(), registry=ProviderRegistry([FakeProvider()]), start_background=True)
    async with app.router.lifespan_context(app):
        app.state.services.worker._on_progress()
    assert sorted(calls) == ["SparklineCache", "StatsCache"]

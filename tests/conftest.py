from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

from app.config import EnvConfig
from app.db import make_engine, make_session_factory
from app.main import create_app
from app.providers.base import ProviderRegistry
from tests.fakes import FakeClock, FakeProvider

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(scope="session")
def database_url():
    with PostgresContainer("timescale/timescaledb:2.30.2-pg16", driver=None) as pg:
        url = pg.get_connection_url().replace("postgresql://", "postgresql+asyncpg://", 1)
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        yield url


@pytest.fixture
async def sf(database_url):
    engine = make_engine(database_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE candles, jobs, assets, settings RESTART IDENTITY CASCADE"))
    yield make_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def client(sf, clock):
    env = EnvConfig(
        _env_file=None, database_url="postgresql+asyncpg://unused@localhost/unused", alpaca_key_id="", alpaca_secret_key=""
    )
    app = create_app(env, sf=sf, clock=clock, registry=ProviderRegistry([FakeProvider(clock)]), start_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c

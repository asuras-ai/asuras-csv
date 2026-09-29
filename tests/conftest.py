from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

from app.db import make_engine, make_session_factory
from tests.fakes import FakeClock

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(scope="session")
def database_url():
    with PostgresContainer("timescale/timescaledb:latest-pg16", driver=None) as pg:
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

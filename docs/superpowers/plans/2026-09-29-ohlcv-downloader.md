# OHLCV 1m Downloader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-hosted Docker Compose app with a web GUI that downloads 1-minute OHLCV candles (crypto via Binance, US stocks/ETFs via Alpaca, forex via Dukascopy) into TimescaleDB. It updates incrementally, paces itself to rate limits, resumes on its own, and exports Jesse "Custom Data" CSVs.

**Architecture:** One FastAPI process serves a Jinja2 + HTMX UI and runs an in-process async worker pool plus an APScheduler cron. Jobs live in a `jobs` table (the queue). Each provider adapter makes HTTP calls through a shared rate-limited `ProviderClient` and yields `Chunk`s. After each chunk, the candles and the asset's `fetched_until` cursor are committed together in one transaction, so any interruption loses at most one request.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic, TimescaleDB (PostgreSQL 16), httpx, APScheduler 3.x, Jinja2, HTMX 2, Pico.css 2, pytest + pytest-asyncio + respx + testcontainers.

**Spec:** `docs/superpowers/specs/2026-09-29-ohlcv-downloader-design.md`

## Global Constraints

- Python `>=3.12`. Database image `timescale/timescaledb:latest-pg16`.
- All datetimes are timezone-aware UTC. A naive datetime is a bug (`app.domain.utc` raises on it).
- Candle `ts` = candle **open** time, always on a 1-minute UTC boundary.
- CSV: UTF-8, header exactly `timestamp,open,close,high,low,volume`, timestamp = Unix **milliseconds**, rows ascending and unique, no scientific notation.
- Jesse symbols are `BASE-QUOTE`, uppercase letters/digits only (regex `^[A-Z0-9]+-[A-Z0-9]+$`).
- Stocks/ETFs: regular session only (Alpaca calendar). Crypto and forex: every candle.
- Provider budgets: Binance 25 req/s (= 3000 weight/min, 50% of the limit), concurrency 2. Alpaca 3 req/s (180/min), concurrency 1. Dukascopy 8 files/s, concurrency 4.
- Job statuses: `queued`, `running`, `waiting`, `paused`, `done`, `failed`, `cancelled`. Active = `queued|running|waiting|paused`.
- Pause backoff: 60 s, 300 s, 900 s, 3600 s, then 3600 s. Give up after 24 h without a successful chunk.
- Backfill slice = 60 s. Priorities: update 10, backfill 0.
- No authentication. The UI has no JS build step (HTMX and Pico come from a CDN).
- Tests need a running Docker daemon (testcontainers starts TimescaleDB).
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## File Structure

```
pyproject.toml, alembic.ini, Dockerfile, docker-compose.yml, .env.example, .dockerignore, .gitignore, README.md
migrations/env.py, migrations/versions/0001_initial.py
app/
  domain.py            Candle, Chunk, SymbolInfo, error types, time helpers
  clock.py             Clock (now/monotonic/sleep), injectable for tests
  config.py            EnvConfig (environment variables)
  db.py                engine + session factory helpers
  models.py            SQLAlchemy models: Asset, CandleRow, Job, Setting
  state.py             Services container stored on app.state
  main.py              create_app() factory, lifespan, provider registry wiring
  worker.py            async worker pool
  scheduler.py         APScheduler wrapper for scheduled "update all"
  providers/
    http.py            RateLimitPolicy + ProviderClient (pacing, throttling, retries)
    base.py            Provider protocol, ProviderRegistry, rank_matches
    binance.py, alpaca.py, dukascopy.py
  services/
    settings.py        DB-backed settings with env overrides
    jobs.py            job queue: enqueue, claim, state transitions, recovery
    sync.py            run_slice(): fetch chunks, commit candles + cursor
    export.py          Jesse CSV streaming
    assets.py          asset CRUD + cached stats
    progress.py        progress %, ETA, human durations, estimates
  web/
    routes.py          all HTTP routes
    templates/*.html
tests/
  conftest.py, fakes.py, test_*.py
```

---

### Task 1: Project scaffold, domain types, clock

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `app/__init__.py`, `app/domain.py`, `app/clock.py`
- Create: `tests/__init__.py`, `tests/conftest.py`, `tests/fakes.py`
- Test: `tests/test_domain.py`

**Interfaces:**
- Produces: `app.domain`: `Candle(ts, open, high, low, close, volume)`, `Chunk(candles: list[Candle], covered_until: datetime)`, `SymbolInfo(provider_symbol, asset_class, suggested_jesse_symbol, name)`, `ProviderError`, `TransientError`, `PermanentError`, `RateLimited(provider: str, resume_at: datetime)`, `MINUTE`, `HOUR`, `utc()`, `floor_minute()`, `floor_hour()`, `to_ms()`, `from_ms()`.
- Produces: `app.clock.Clock` with `now() -> datetime`, `monotonic() -> float`, `async sleep(seconds)`.
- Produces: `tests.fakes.FakeClock(start=2024-01-01 02:00 UTC)` with `advance(seconds)` and a `sleeps: list[float]` record. `clock` pytest fixture.

- [ ] **Step 1: Create project files**

`pyproject.toml`:
```toml
[project]
name = "asuras-csv"
version = "0.1.0"
description = "Self-hosted 1-minute OHLCV downloader with Jesse CSV export"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "sqlalchemy[asyncio]>=2.0.30",
  "asyncpg>=0.29",
  "alembic>=1.13",
  "httpx>=0.27",
  "jinja2>=3.1",
  "python-multipart>=0.0.9",
  "pydantic-settings>=2.3",
  "apscheduler>=3.10,<4",
  "tzdata>=2024.1",
]

[project.optional-dependencies]
dev = [
  "pytest>=8",
  "pytest-asyncio>=0.24",
  "respx>=0.21",
  "testcontainers[postgres]>=4.7",
]

[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["app*"]

[tool.setuptools.package-data]
app = ["web/templates/*.html"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
testpaths = ["tests"]
pythonpath = ["."]
```

`.gitignore`:
```
.venv/
__pycache__/
*.egg-info/
.pytest_cache/
.env
```

`app/__init__.py` and `tests/__init__.py`: empty files.

- [ ] **Step 2: Create the virtualenv and install**

Run: `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"`
Expected: ends with `Successfully installed ...`.

- [ ] **Step 3: Write the failing tests**

`tests/fakes.py`:
```python
"""Test doubles shared across the test suite."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.clock import Clock


class FakeClock(Clock):
    """Deterministic clock: sleep() advances time instantly and records the duration."""

    def __init__(self, start: datetime = datetime(2024, 1, 1, 2, 0, tzinfo=UTC)):
        self._now = start
        self._mono = 0.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds > 0:
            self.advance(seconds)
        await asyncio.sleep(0)
```

`tests/conftest.py`:
```python
import pytest

from tests.fakes import FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
```

`tests/test_domain.py`:
```python
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.domain import RateLimited, floor_hour, floor_minute, from_ms, to_ms, utc
from tests.fakes import FakeClock


def test_floor_minute_and_hour():
    dt = datetime(2024, 3, 10, 14, 37, 42, 123456, tzinfo=UTC)
    assert floor_minute(dt) == datetime(2024, 3, 10, 14, 37, tzinfo=UTC)
    assert floor_hour(dt) == datetime(2024, 3, 10, 14, tzinfo=UTC)


def test_ms_roundtrip_matches_jesse_example():
    dt = datetime(2024, 1, 9, 14, 0, tzinfo=UTC)
    assert to_ms(dt) == 1704808800000
    assert from_ms(1704808800000) == dt


def test_utc_normalises_and_rejects_naive():
    cet = timezone(timedelta(hours=1))
    assert utc(datetime(2024, 1, 1, 1, 0, tzinfo=cet)) == datetime(2024, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        utc(datetime(2024, 1, 1))


def test_rate_limited_message():
    err = RateLimited("Binance", datetime(2024, 1, 1, 14, 3, 12, tzinfo=UTC))
    assert str(err) == "Binance rate limit, resuming 14:03:12 UTC"
    assert err.provider == "Binance"


async def test_fake_clock_sleep_advances_time():
    clock = FakeClock()
    wall, mono = clock.now(), clock.monotonic()
    await clock.sleep(1.5)
    assert clock.monotonic() - mono == 1.5
    assert clock.now() - wall == timedelta(seconds=1.5)
    assert clock.sleeps == [1.5]
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_domain.py -v`
Expected: collection error `ModuleNotFoundError: No module named 'app.clock'`.

- [ ] **Step 5: Implement**

`app/clock.py`:
```python
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
```

`app/domain.py`:
```python
"""Core value types and errors shared by providers, services and the web layer."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class Candle:
    ts: datetime  # open time, on a 1-minute UTC boundary
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class Chunk:
    candles: list[Candle]
    covered_until: datetime  # exclusive end of the range this chunk fully covers


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    provider_symbol: str
    asset_class: str
    suggested_jesse_symbol: str
    name: str


class ProviderError(Exception):
    """Base class for errors raised by provider adapters."""


class TransientError(ProviderError):
    """Temporary failure (network, 5xx). The job is paused and retried later."""


class PermanentError(ProviderError):
    """Failure that retrying cannot fix (bad API key, unknown symbol)."""


class RateLimited(ProviderError):
    """The provider throttled us; nothing may be sent to it before resume_at."""

    def __init__(self, provider: str, resume_at: datetime):
        super().__init__(f"{provider} rate limit, resuming {resume_at:%H:%M:%S} UTC")
        self.provider = provider
        self.resume_at = resume_at


def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime; all datetimes must be timezone-aware")
    return dt.astimezone(UTC)


def floor_minute(dt: datetime) -> datetime:
    return utc(dt).replace(second=0, microsecond=0)


def floor_hour(dt: datetime) -> datetime:
    return utc(dt).replace(minute=0, second=0, microsecond=0)


def to_ms(dt: datetime) -> int:
    return (utc(dt) - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_domain.py -v`
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore app tests
git commit -m "feat: project scaffold, domain types and clock

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Database models, migration, test database fixture

**Files:**
- Create: `app/config.py`, `app/db.py`, `app/models.py`, `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/0001_initial.py`
- Modify: `tests/conftest.py` (full replacement below), `tests/fakes.py` (append `make_asset`)
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: nothing beyond Task 1.
- Produces: `app.config.EnvConfig` (fields `database_url`, `alpaca_key_id`, `alpaca_secret_key`, `alpaca_trading_url`). `app.db.SessionFactory` (= `async_sessionmaker[AsyncSession]`), `make_engine(url, **kw)`, `make_session_factory(engine)`. `app.models`: `Base`, `Asset`, `CandleRow`, `Job`, `Setting`, `ACTIVE_STATUSES`.
- Produces (tests): `sf` fixture (a session factory against a fresh, truncated TimescaleDB) and `tests.fakes.make_asset(sf, **overrides) -> Asset`.

- [ ] **Step 1: Write config, db and models**

`app/config.py`:
```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvConfig(BaseSettings):
    """Configuration from environment variables (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://ohlcv:ohlcv@db:5432/ohlcv"
    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    alpaca_trading_url: str = "https://paper-api.alpaca.markets"
```

`app/db.py`:
```python
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

SessionFactory = async_sessionmaker[AsyncSession]


def make_engine(url: str, **kwargs) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True, **kwargs)


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)
```

`app/models.py`:
```python
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TS = DateTime(timezone=True)
ACTIVE_STATUSES = ("queued", "running", "waiting", "paused")


class Base(DeclarativeBase):
    pass


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("provider", "provider_symbol"),)
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    provider_symbol: Mapped[str] = mapped_column(String(64))
    asset_class: Mapped[str] = mapped_column(String(16))
    jesse_symbol: Mapped[str] = mapped_column(String(64))
    start_date: Mapped[datetime] = mapped_column(TS)
    fetched_until: Mapped[datetime | None] = mapped_column(TS)
    enabled: Mapped[bool] = mapped_column(default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class CandleRow(Base):
    __tablename__ = "candles"

    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    open: Mapped[float] = mapped_column(Float(53))
    high: Mapped[float] = mapped_column(Float(53))
    low: Mapped[float] = mapped_column(Float(53))
    close: Mapped[float] = mapped_column(Float(53))
    volume: Mapped[float] = mapped_column(Float(53))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "jobs_one_active_per_asset",
            "asset_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'waiting', 'paused')"),
        ),
    )
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    priority: Mapped[int]
    range_start: Mapped[datetime] = mapped_column(TS)
    range_end: Mapped[datetime] = mapped_column(TS)
    requests_made: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    candles_added: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    run_seconds: Mapped[float] = mapped_column(Float(53), default=0.0, server_default=text("0"))
    status_detail: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(TS)
    attempt: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    last_progress_at: Mapped[datetime | None] = mapped_column(TS)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
```

- [ ] **Step 2: Write Alembic config and the initial migration**

`alembic.ini`:
```ini
[alembic]
script_location = migrations

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
```

`migrations/env.py`:
```python
import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import EnvConfig
from app.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL") or EnvConfig().database_url


def run_sync(connection) -> None:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async() -> None:
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(run_sync)
    await engine.dispose()


asyncio.run(run_async())
```

`migrations/script.py.mako`:
```mako
"""${message}"""
import sqlalchemy as sa
from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

`migrations/versions/0001_initial.py`:
```python
"""initial schema"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.create_table(
        "assets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_symbol", sa.String(64), nullable=False),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("jesse_symbol", sa.String(64), nullable=False),
        sa.Column("start_date", TS, nullable=False),
        sa.Column("fetched_until", TS),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("provider", "provider_symbol"),
    )
    op.create_table(
        "candles",
        sa.Column("asset_id", sa.Integer, sa.ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("ts", TS, primary_key=True),
        sa.Column("open", sa.Float(53), nullable=False),
        sa.Column("high", sa.Float(53), nullable=False),
        sa.Column("low", sa.Float(53), nullable=False),
        sa.Column("close", sa.Float(53), nullable=False),
        sa.Column("volume", sa.Float(53), nullable=False),
    )
    op.execute("SELECT create_hypertable('candles', 'ts', chunk_time_interval => INTERVAL '7 days')")
    op.execute(
        "ALTER TABLE candles SET (timescaledb.compress, "
        "timescaledb.compress_segmentby = 'asset_id', timescaledb.compress_orderby = 'ts')"
    )
    op.execute("SELECT add_compression_policy('candles', INTERVAL '30 days')")
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("asset_id", sa.Integer, sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("range_start", TS, nullable=False),
        sa.Column("range_end", TS, nullable=False),
        sa.Column("requests_made", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("candles_added", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("run_seconds", sa.Float(53), nullable=False, server_default=sa.text("0")),
        sa.Column("status_detail", sa.Text),
        sa.Column("next_attempt_at", TS),
        sa.Column("attempt", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_progress_at", TS),
        sa.Column("error", sa.Text),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", TS),
        sa.Column("finished_at", TS),
    )
    op.create_index(
        "jobs_one_active_per_asset",
        "jobs",
        ["asset_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'waiting', 'paused')"),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("settings")
    op.drop_table("jobs")
    op.drop_table("candles")
    op.drop_table("assets")
```

- [ ] **Step 3: Write test fixtures and the failing tests**

`tests/conftest.py` (replace the whole file):
```python
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
```

Append to `tests/fakes.py` (merge the imports into the top of the file):
```python
from app.db import SessionFactory
from app.models import Asset


async def make_asset(sf: SessionFactory, **overrides) -> Asset:
    values = {
        "provider": "fake",
        "provider_symbol": "FAKEUSD",
        "asset_class": "crypto",
        "jesse_symbol": "FAKE-USD",
        "start_date": datetime(2024, 1, 1, tzinfo=UTC),
    } | overrides
    asset = Asset(**values)
    async with sf.begin() as s:
        s.add(asset)
    return asset
```

`tests/test_db.py`:
```python
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models import Job
from tests.fakes import make_asset


async def test_candles_is_a_compressed_hypertable(sf):
    async with sf() as s:
        row = (
            await s.execute(
                text(
                    "SELECT compression_enabled FROM timescaledb_information.hypertables "
                    "WHERE hypertable_name = 'candles'"
                )
            )
        ).one()
    assert row.compression_enabled is True


async def test_only_one_active_job_per_asset(sf):
    asset = await make_asset(sf)
    t = datetime(2024, 1, 1, tzinfo=UTC)

    def job(status: str) -> Job:
        return Job(asset_id=asset.id, kind="update", status=status, priority=10, range_start=t, range_end=t)

    async with sf.begin() as s:
        s.add_all([job("done"), job("queued")])
    with pytest.raises(IntegrityError):
        async with sf.begin() as s:
            s.add(job("running"))


async def test_asset_defaults_are_loaded(sf):
    asset = await make_asset(sf)
    assert asset.id == 1 and asset.enabled is True and asset.created_at is not None
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_db.py -v` (Docker must be running; the first run pulls the TimescaleDB image)
Expected: 3 passed. If the migration fails, fix it; the tests don't need changing.

- [ ] **Step 5: Commit**

```bash
git add app alembic.ini migrations tests
git commit -m "feat: database models, TimescaleDB migration and test fixtures

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Rate-limited provider HTTP client

**Files:**
- Create: `app/providers/__init__.py` (empty), `app/providers/http.py`
- Test: `tests/test_http_client.py`

**Interfaces:**
- Consumes: `app.clock.Clock`, `app.domain.{PermanentError, RateLimited, TransientError}`.
- Produces: `RateLimitPolicy(name, rate, burst, concurrency, ok_statuses=frozenset({200}), throttle_statuses=frozenset({429}), permanent_messages={}, quota_delay=no_quota_delay)`; `ProviderClient(policy, http: httpx.AsyncClient, clock)` with `async get(url, *, params=None, headers=None) -> httpx.Response` and attribute `policy`. `quota_delay` signature: `(response: httpx.Response, now: datetime) -> float` (seconds to block the provider).
- Behaviour: token bucket (`rate` req/s, `burst`), semaphore (`concurrency`), quota-header blocking, throttle → `RateLimited` for all callers until the resume time (`Retry-After`, else 60 s doubling to 900 s), network/5xx → 3 retries (2, 4, 8 s) then `TransientError`, status in `permanent_messages` or other 4xx → `PermanentError`.

- [ ] **Step 1: Write the failing tests**

`tests/test_http_client.py`:
```python
from datetime import timedelta

import httpx
import pytest

from app.domain import PermanentError, RateLimited, TransientError
from app.providers.http import ProviderClient, RateLimitPolicy

URL = "https://api.test/data"


def make_client(clock, **overrides) -> ProviderClient:
    policy = RateLimitPolicy(**({"name": "Test", "rate": 100.0, "burst": 100, "concurrency": 2} | overrides))
    return ProviderClient(policy, httpx.AsyncClient(), clock)


async def test_paces_requests_to_the_configured_rate(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(200))
    client = make_client(clock, rate=2.0, burst=1, concurrency=1)
    for _ in range(5):
        await client.get(URL)
    assert clock.monotonic() == pytest.approx(2.0)


async def test_low_quota_header_blocks_until_reset(respx_mock, clock):
    respx_mock.get(URL).mock(side_effect=[httpx.Response(200, headers={"X-Low": "1"}), httpx.Response(200)])
    client = make_client(clock, quota_delay=lambda r, now: 10.0 if r.headers.get("X-Low") else 0.0)
    await client.get(URL)
    await client.get(URL)
    assert clock.monotonic() == pytest.approx(10.0)


async def test_429_pauses_every_caller_until_retry_after(respx_mock, clock):
    route = respx_mock.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "30"}), httpx.Response(200)]
    )
    client = make_client(clock)
    start = clock.now()
    with pytest.raises(RateLimited) as first:
        await client.get(URL)
    assert first.value.resume_at == start + timedelta(seconds=30)
    with pytest.raises(RateLimited):
        await client.get(URL)
    assert route.call_count == 1
    clock.advance(31)
    assert (await client.get(URL)).status_code == 200


async def test_429_without_retry_after_backs_off_exponentially(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(429))
    client = make_client(clock)
    delays = []
    for _ in range(3):
        start = clock.now()
        with pytest.raises(RateLimited) as exc:
            await client.get(URL)
        delays.append((exc.value.resume_at - start).total_seconds())
        clock.advance(delays[-1])
    assert delays == [60, 120, 240]


async def test_server_errors_are_retried_then_reported_as_transient(respx_mock, clock):
    route = respx_mock.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(TransientError, match="HTTP 503"):
        await make_client(clock).get(URL)
    assert route.call_count == 4
    assert clock.sleeps == [2.0, 4.0, 8.0]


async def test_server_error_then_success(respx_mock, clock):
    respx_mock.get(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
    assert (await make_client(clock).get(URL)).status_code == 200


async def test_network_errors_are_transient(respx_mock, clock):
    respx_mock.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(TransientError, match="network error"):
        await make_client(clock).get(URL)


async def test_permanent_status_uses_policy_message(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(401, text="nope"))
    client = make_client(clock, permanent_messages={401: "API key rejected — check Settings"})
    with pytest.raises(PermanentError, match="Test: API key rejected — check Settings"):
        await client.get(URL)


async def test_unexpected_client_error_is_permanent(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(418))
    with pytest.raises(PermanentError, match="unexpected HTTP 418"):
        await make_client(clock).get(URL)


async def test_extra_ok_statuses_are_returned(respx_mock, clock):
    respx_mock.get(URL).mock(return_value=httpx.Response(404))
    client = make_client(clock, ok_statuses=frozenset({200, 404}))
    assert (await client.get(URL)).status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_http_client.py -v`
Expected: `ModuleNotFoundError: No module named 'app.providers'`.

- [ ] **Step 3: Implement**

`app/providers/__init__.py`: empty.

`app/providers/http.py`:
```python
"""Shared HTTP client that keeps every provider inside its rate limits."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from app.clock import Clock
from app.domain import PermanentError, RateLimited, TransientError

log = logging.getLogger(__name__)

QuotaDelay = Callable[[httpx.Response, datetime], float]

RETRY_DELAYS = (2.0, 4.0, 8.0)
DEFAULT_THROTTLE_SECONDS = 60.0
MAX_THROTTLE_SECONDS = 900.0


def no_quota_delay(response: httpx.Response, now: datetime) -> float:
    return 0.0


@dataclass(frozen=True)
class RateLimitPolicy:
    name: str  # display name used in messages, e.g. "Binance"
    rate: float  # sustained requests per second
    burst: int  # token bucket size
    concurrency: int  # max requests in flight
    ok_statuses: frozenset[int] = frozenset({200})
    throttle_statuses: frozenset[int] = frozenset({429})
    permanent_messages: Mapping[int, str] = field(default_factory=dict)
    quota_delay: QuotaDelay = no_quota_delay


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class ProviderClient:
    """One instance per provider, shared by every job that uses that provider."""

    def __init__(self, policy: RateLimitPolicy, http: httpx.AsyncClient, clock: Clock):
        self.policy = policy
        self._http = http
        self._clock = clock
        self._tokens = float(policy.burst)
        self._last_refill = clock.monotonic()
        self._blocked_until = 0.0  # monotonic; set from quota headers
        self._paused_until: datetime | None = None  # wall clock; set by throttling responses
        self._throttle_seconds = DEFAULT_THROTTLE_SECONDS
        self._lock = asyncio.Lock()
        self._in_flight = asyncio.Semaphore(policy.concurrency)

    async def get(self, url: str, *, params=None, headers=None) -> httpx.Response:
        problem = ""
        for attempt in range(len(RETRY_DELAYS) + 1):
            await self._acquire()
            try:
                async with self._in_flight:
                    response = await self._http.get(url, params=params, headers=headers, timeout=30.0)
            except httpx.TransportError as exc:
                problem = f"network error: {exc!r}"
            else:
                status = response.status_code
                if status in self.policy.throttle_statuses:
                    raise self._throttle(response)
                self._throttle_seconds = DEFAULT_THROTTLE_SECONDS
                self._apply_quota(response)
                if status in self.policy.ok_statuses:
                    return response
                if status in self.policy.permanent_messages:
                    raise PermanentError(
                        f"{self.policy.name}: {self.policy.permanent_messages[status]} "
                        f"(HTTP {status}: {response.text[:200]})"
                    )
                if status < 500:
                    raise PermanentError(f"{self.policy.name}: unexpected HTTP {status}: {response.text[:200]}")
                problem = f"HTTP {status}"
            if attempt < len(RETRY_DELAYS):
                log.warning("%s: %s, retrying in %.0fs", self.policy.name, problem, RETRY_DELAYS[attempt])
                await self._clock.sleep(RETRY_DELAYS[attempt])
        raise TransientError(f"{self.policy.name}: {problem}")

    async def _acquire(self) -> None:
        while True:
            async with self._lock:
                if self._paused_until is not None and self._paused_until > self._clock.now():
                    raise RateLimited(self.policy.name, self._paused_until)
                now = self._clock.monotonic()
                self._tokens = min(
                    float(self.policy.burst), self._tokens + (now - self._last_refill) * self.policy.rate
                )
                self._last_refill = now
                wait = self._blocked_until - now
                if wait <= 0:
                    if self._tokens >= 1:
                        self._tokens -= 1
                        return
                    wait = (1 - self._tokens) / self.policy.rate
            await self._clock.sleep(wait)

    def _throttle(self, response: httpx.Response) -> RateLimited:
        delay = _retry_after(response)
        if delay is None:
            delay = self._throttle_seconds
            self._throttle_seconds = min(self._throttle_seconds * 2, MAX_THROTTLE_SECONDS)
        self._paused_until = self._clock.now() + timedelta(seconds=delay)
        log.warning("%s throttled us (HTTP %s); pausing until %s", self.policy.name, response.status_code, self._paused_until)
        return RateLimited(self.policy.name, self._paused_until)

    def _apply_quota(self, response: httpx.Response) -> None:
        delay = self.policy.quota_delay(response, self._clock.now())
        if delay > 0:
            self._blocked_until = max(self._blocked_until, self._clock.monotonic() + delay)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_http_client.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add app/providers tests/test_http_client.py
git commit -m "feat: rate-limited provider HTTP client

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Provider protocol, registry, fake provider, Binance adapter

**Files:**
- Create: `app/providers/base.py`, `app/providers/binance.py`
- Modify: `tests/fakes.py` (append `FakeProvider`)
- Test: `tests/test_registry.py`, `tests/test_binance.py`

**Interfaces:**
- Consumes: `ProviderClient`, `RateLimitPolicy` (Task 3), domain types (Task 1).
- Produces: `app.providers.base.Provider` (Protocol with `name`, `label`, `asset_classes: tuple[str, ...]`, `client: ProviderClient`, `async search_symbols(query) -> list[SymbolInfo]`, `async earliest_available(symbol) -> datetime`, `available_until(now) -> datetime`, `estimate_requests(start, end) -> int`, `fetch(symbol, start, end) -> AsyncIterator[Chunk]`). `ProviderRegistry(providers)` with `get(name)` (raises `PermanentError`), `find(name) -> Provider | None`, `all() -> list[Provider]`. `rank_matches(symbols, query, limit=20)`.
- Produces: `app.providers.binance.BinanceProvider(client, base_url=BASE_URL)`, `POLICY`, `binance_quota_delay`.
- Produces (tests): `tests.fakes.FakeProvider(clock=None, *, seconds_per_chunk=0.0, fail_after=None, error=None, listed=None)`, `name="fake"`, one chunk per 10 minutes with one candle per minute, `calls: list[tuple[start, end]]`, `client.policy.rate == 10.0`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/fakes.py` (merge imports at top: `import math`, `from types import SimpleNamespace`, `from app.domain import MINUTE, Candle, Chunk, SymbolInfo, TransientError, floor_minute`):
```python
class FakeProvider:
    """In-memory provider: one chunk per 10 minutes, one candle per minute."""

    name = "fake"
    label = "Fake"
    asset_classes = ("crypto",)
    CHUNK = timedelta(minutes=10)

    def __init__(self, clock=None, *, seconds_per_chunk=0.0, fail_after=None, error=None, listed=None):
        self.client = SimpleNamespace(policy=SimpleNamespace(rate=10.0))
        self.clock = clock
        self.seconds_per_chunk = seconds_per_chunk
        self.fail_after = fail_after  # raise `error` after this many chunks
        self.error = error or TransientError("Fake: boom")
        self.listed = listed  # no candles before this time
        self.calls: list[tuple[datetime, datetime]] = []

    async def search_symbols(self, query):
        return [SymbolInfo("FAKEUSD", "crypto", "FAKE-USD", "Fake/USD")] if query.strip() else []

    async def earliest_available(self, symbol):
        return datetime(2024, 1, 1, tzinfo=UTC)

    def available_until(self, now):
        return floor_minute(now)

    def estimate_requests(self, start, end):
        return max(0, math.ceil((end - start) / self.CHUNK))

    async def fetch(self, symbol, start, end):
        self.calls.append((start, end))
        cursor, sent = start, 0
        while cursor < end:
            if self.fail_after is not None and sent >= self.fail_after:
                raise self.error
            nxt = min(cursor + self.CHUNK, end)
            candles, t = [], cursor
            while t < nxt:
                if self.listed is None or t >= self.listed:
                    candles.append(Candle(t, 1.0, 2.0, 0.5, 1.5, 10.0))
                t += MINUTE
            if self.clock is not None and self.seconds_per_chunk:
                self.clock.advance(self.seconds_per_chunk)
            yield Chunk(candles, nxt)
            cursor, sent = nxt, sent + 1
```

`tests/test_registry.py`:
```python
import pytest

from app.domain import PermanentError, SymbolInfo
from app.providers.base import ProviderRegistry, rank_matches
from tests.fakes import FakeProvider


def test_registry_lookup():
    provider = FakeProvider()
    registry = ProviderRegistry([provider])
    assert registry.get("fake") is provider
    assert registry.find("nope") is None
    assert registry.all() == [provider]
    with pytest.raises(PermanentError, match="Unknown provider"):
        registry.get("nope")


def test_rank_matches_prefers_exact_then_prefix():
    symbols = [SymbolInfo(s, "crypto", s, s) for s in ["WBTCBTC", "BTCUSDT", "ETHBTC", "BTC"]]
    assert [s.provider_symbol for s in rank_matches(symbols, "btc")] == ["BTC", "BTCUSDT", "ETHBTC", "WBTCBTC"]
    assert rank_matches(symbols, "  ") == []
    assert [s.provider_symbol for s in rank_matches(symbols, "BTC-USDT")] == ["BTCUSDT"]
```

`tests/test_binance.py`:
```python
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.domain import Candle, PermanentError, to_ms
from app.providers.binance import POLICY, BinanceProvider, binance_quota_delay
from app.providers.http import ProviderClient

BASE = "https://binance.test"
LISTED = datetime(2024, 1, 1, tzinfo=UTC)


def kline_handler(listed: datetime = LISTED):
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        t = max(int(p["startTime"]), to_ms(listed))
        end = int(p.get("endTime", 2**62))
        limit = int(p["limit"])
        rows = []
        while t <= end and len(rows) < limit:
            rows.append([t, "1.0", "2.0", "0.5", "1.5", "10.0", t + 59_999, "15.0", 5, "5.0", "7.5", "0"])
            t += 60_000
        return httpx.Response(200, json=rows, headers={"X-MBX-USED-WEIGHT-1M": "2"})

    return handler


@pytest.fixture
def provider(clock):
    return BinanceProvider(ProviderClient(POLICY, httpx.AsyncClient(), clock), base_url=BASE)


async def collect(provider, symbol, start, end):
    return [chunk async for chunk in provider.fetch(symbol, start, end)]


async def test_fetch_paginates_in_1000_candle_requests(respx_mock, provider):
    route = respx_mock.get(host="binance.test", path="/api/v3/klines").mock(side_effect=kline_handler())
    end = LISTED + timedelta(minutes=2500)
    chunks = await collect(provider, "BTCUSDT", LISTED, end)
    assert route.call_count == 3
    assert [len(c.candles) for c in chunks] == [1000, 1000, 500]
    assert [c.covered_until for c in chunks] == [
        LISTED + timedelta(minutes=1000),
        LISTED + timedelta(minutes=2000),
        end,
    ]
    assert chunks[0].candles[0] == Candle(LISTED, 1.0, 2.0, 0.5, 1.5, 10.0)
    assert route.calls[1].request.url.params["startTime"] == str(to_ms(LISTED + timedelta(minutes=1000)))


async def test_range_before_listing_costs_no_extra_requests(respx_mock, provider):
    route = respx_mock.get(host="binance.test", path="/api/v3/klines").mock(
        side_effect=kline_handler(listed=LISTED + timedelta(minutes=10))
    )
    chunks = await collect(provider, "NEWUSDT", LISTED, LISTED + timedelta(minutes=20))
    assert route.call_count == 1
    assert len(chunks[0].candles) == 10
    assert chunks[0].candles[0].ts == LISTED + timedelta(minutes=10)
    assert chunks[0].covered_until == LISTED + timedelta(minutes=20)


async def test_earliest_available_is_the_first_kline(respx_mock, provider):
    respx_mock.get(host="binance.test", path="/api/v3/klines").mock(side_effect=kline_handler())
    assert await provider.earliest_available("BTCUSDT") == LISTED


async def test_invalid_symbol_is_permanent(respx_mock, provider):
    respx_mock.get(host="binance.test", path="/api/v3/klines").mock(
        return_value=httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})
    )
    with pytest.raises(PermanentError, match="Invalid symbol"):
        await collect(provider, "NOPE", LISTED, LISTED + timedelta(minutes=5))


async def test_search_ranks_matches_and_caches_exchange_info(respx_mock, provider):
    route = respx_mock.get(host="binance.test", path="/api/v3/exchangeInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "symbols": [
                    {"symbol": "BTCUSDC", "baseAsset": "BTC", "quoteAsset": "USDC", "status": "TRADING"},
                    {"symbol": "WBTCBTC", "baseAsset": "WBTC", "quoteAsset": "BTC", "status": "TRADING"},
                    {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING"},
                    {"symbol": "BTCOLD", "baseAsset": "BTC", "quoteAsset": "OLD", "status": "BREAK"},
                ]
            },
        )
    )
    assert [s.provider_symbol for s in await provider.search_symbols("btcusdt")] == ["BTCUSDT"]
    results = await provider.search_symbols("BTC")
    assert [s.provider_symbol for s in results] == ["BTCUSDC", "BTCUSDT", "WBTCBTC"]
    assert results[1].suggested_jesse_symbol == "BTC-USDT"
    assert results[1].asset_class == "crypto"
    assert route.call_count == 1


def test_estimate_and_available_until(provider):
    assert provider.estimate_requests(LISTED, LISTED + timedelta(minutes=2500)) == 3
    assert provider.estimate_requests(LISTED, LISTED) == 0
    now = datetime(2024, 5, 1, 12, 30, 45, tzinfo=UTC)
    assert provider.available_until(now) == datetime(2024, 5, 1, 12, 30, tzinfo=UTC)


def test_quota_delay_waits_for_the_next_minute_when_weight_is_high():
    now = datetime(2024, 1, 1, 12, 0, 15, tzinfo=UTC)
    high = httpx.Response(200, headers={"X-MBX-USED-WEIGHT-1M": "5500"})
    low = httpx.Response(200, headers={"X-MBX-USED-WEIGHT-1M": "100"})
    assert binance_quota_delay(high, now) == 45.0
    assert binance_quota_delay(low, now) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_registry.py tests/test_binance.py -v`
Expected: `ModuleNotFoundError: No module named 'app.providers.base'`.

- [ ] **Step 3: Implement**

`app/providers/base.py`:
```python
"""Provider interface and registry. Adding a provider = one module + one line in main.build_registry."""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from typing import Protocol

from app.domain import Chunk, PermanentError, SymbolInfo
from app.providers.http import ProviderClient


class Provider(Protocol):
    name: str  # registry key, stored in assets.provider
    label: str  # display name
    asset_classes: tuple[str, ...]
    client: ProviderClient

    async def search_symbols(self, query: str) -> list[SymbolInfo]: ...

    async def earliest_available(self, symbol: str) -> datetime: ...

    def available_until(self, now: datetime) -> datetime:
        """Latest minute whose candles are final at `now` (exclusive end for fetching)."""
        ...

    def estimate_requests(self, start: datetime, end: datetime) -> int: ...

    def fetch(self, symbol: str, start: datetime, end: datetime) -> AsyncIterator[Chunk]:
        """Yield ascending chunks covering [start, end); one chunk per request or file."""
        ...


class ProviderRegistry:
    def __init__(self, providers: Iterable[Provider]):
        self._providers = {p.name: p for p in providers}

    def get(self, name: str) -> Provider:
        provider = self._providers.get(name)
        if provider is None:
            raise PermanentError(f"Unknown provider {name!r}")
        return provider

    def find(self, name: str) -> Provider | None:
        return self._providers.get(name)

    def all(self) -> list[Provider]:
        return list(self._providers.values())


def _normalise(value: str) -> str:
    return value.upper().replace("-", "").replace("/", "").strip()


def rank_matches(symbols: Iterable[SymbolInfo], query: str, limit: int = 20) -> list[SymbolInfo]:
    """Case-insensitive substring search: exact match first, then prefix matches, then the rest."""
    q = _normalise(query)
    if not q:
        return []
    matches = [s for s in symbols if q in _normalise(s.provider_symbol) or q in _normalise(s.name)]

    def key(s: SymbolInfo):
        sym = _normalise(s.provider_symbol)
        return (sym != q, not sym.startswith(q), sym)

    return sorted(matches, key=key)[:limit]
```

`app/providers/binance.py`:
```python
"""Binance spot klines (crypto). Public API, no key."""
from __future__ import annotations

import math
from collections.abc import AsyncIterator
from datetime import datetime

import httpx

from app.domain import MINUTE, Candle, Chunk, PermanentError, SymbolInfo, floor_minute, from_ms, to_ms
from app.providers.base import rank_matches
from app.providers.http import ProviderClient, RateLimitPolicy

BASE_URL = "https://api.binance.com"
PAGE = 1000
WEIGHT_SOFT_LIMIT = 5400  # stop at 90% of the 6000 weight/min limit


def binance_quota_delay(response: httpx.Response, now: datetime) -> float:
    used = response.headers.get("X-MBX-USED-WEIGHT-1M")
    if used is None or int(used) < WEIGHT_SOFT_LIMIT:
        return 0.0
    return 60.0 - (now.second + now.microsecond / 1_000_000)


# A 1000-candle kline request costs weight 2, so 25 req/s = 3000 weight/min (50% of the limit).
POLICY = RateLimitPolicy(
    name="Binance",
    rate=25.0,
    burst=10,
    concurrency=2,
    throttle_statuses=frozenset({418, 429}),
    permanent_messages={
        400: "request rejected, check the symbol",
        451: "Binance is not available from this server's region",
    },
    quota_delay=binance_quota_delay,
)


class BinanceProvider:
    name = "binance"
    label = "Binance (crypto)"
    asset_classes = ("crypto",)

    def __init__(self, client: ProviderClient, base_url: str = BASE_URL):
        self.client = client
        self._base = base_url
        self._symbols: list[SymbolInfo] | None = None

    async def search_symbols(self, query: str) -> list[SymbolInfo]:
        if self._symbols is None:
            data = (await self.client.get(f"{self._base}/api/v3/exchangeInfo")).json()
            self._symbols = [
                SymbolInfo(
                    provider_symbol=s["symbol"],
                    asset_class="crypto",
                    suggested_jesse_symbol=f"{s['baseAsset']}-{s['quoteAsset']}",
                    name=f"{s['baseAsset']}/{s['quoteAsset']}",
                )
                for s in data["symbols"]
                if s.get("status") == "TRADING"
            ]
        return rank_matches(self._symbols, query)

    async def earliest_available(self, symbol: str) -> datetime:
        rows = await self._klines(symbol, start_ms=0, end_ms=None, limit=1)
        if not rows:
            raise PermanentError(f"Binance: no data for {symbol}")
        return from_ms(rows[0][0])

    def available_until(self, now: datetime) -> datetime:
        return floor_minute(now)

    def estimate_requests(self, start: datetime, end: datetime) -> int:
        if end <= start:
            return 0
        return max(1, math.ceil((end - start) / MINUTE / PAGE))

    async def fetch(self, symbol: str, start: datetime, end: datetime) -> AsyncIterator[Chunk]:
        cursor = start
        while cursor < end:
            rows = await self._klines(symbol, start_ms=to_ms(cursor), end_ms=to_ms(end) - 1, limit=PAGE)
            candles = [
                Candle(from_ms(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                for r in rows
            ]
            covered = candles[-1].ts + MINUTE if len(rows) == PAGE else end
            yield Chunk(candles, covered)
            cursor = covered

    async def _klines(self, symbol: str, *, start_ms: int, end_ms: int | None, limit: int) -> list:
        params = {"symbol": symbol, "interval": "1m", "startTime": start_ms, "limit": limit}
        if end_ms is not None:
            params["endTime"] = end_ms
        return (await self.client.get(f"{self._base}/api/v3/klines", params=params)).json()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_registry.py tests/test_binance.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add app/providers tests/fakes.py tests/test_registry.py tests/test_binance.py
git commit -m "feat: provider protocol, registry and Binance adapter

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Alpaca adapter (US stocks and ETFs, regular session)

**Files:**
- Create: `app/providers/alpaca.py`
- Test: `tests/test_alpaca.py`

**Interfaces:**
- Consumes: `ProviderClient`, `RateLimitPolicy`, `rank_matches`, domain types.
- Produces: `AlpacaProvider(client, credentials, trading_url, data_url=DATA_URL)`, where `credentials: Callable[[], Awaitable[tuple[str, str]]]` returns `(key_id, secret)`. Also `POLICY` and `alpaca_quota_delay`. `name = "alpaca"`, `asset_classes = ("stock", "etf")`.

- [ ] **Step 1: Write the failing tests**

`tests/test_alpaca.py`:
```python
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.domain import PermanentError
from app.providers.alpaca import POLICY, AlpacaProvider, alpaca_quota_delay
from app.providers.http import ProviderClient

CALENDAR = [
    {"date": "2024-03-08", "open": "09:30", "close": "16:00"},  # Friday, EST
    {"date": "2024-03-11", "open": "09:30", "close": "16:00"},  # Monday, EDT (DST began 03-10)
    {"date": "2024-11-29", "open": "09:30", "close": "13:00"},  # half-day after Thanksgiving
]


def make_provider(clock, key=("KEY", "SECRET")) -> AlpacaProvider:
    async def credentials():
        return key

    client = ProviderClient(POLICY, httpx.AsyncClient(), clock)
    return AlpacaProvider(client, credentials, trading_url="https://trading.test", data_url="https://data.test")


def bar(t: str) -> dict:
    return {"t": t, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 100, "n": 3, "vw": 1.2}


def mock_calendar(respx_mock, status=200, body=None):
    return respx_mock.get(host="trading.test", path="/v2/calendar").mock(
        return_value=httpx.Response(status, json=CALENDAR if body is None else body)
    )


async def test_keeps_only_regular_session_bars_across_dst_and_half_days(respx_mock, clock):
    mock_calendar(respx_mock)
    times = [
        "2024-03-08T14:29:00Z", "2024-03-08T14:30:00Z", "2024-03-08T20:59:00Z", "2024-03-08T21:00:00Z",
        "2024-03-09T15:00:00Z",  # Saturday: not in calendar
        "2024-03-11T13:29:00Z", "2024-03-11T13:30:00Z", "2024-03-11T19:59:00Z", "2024-03-11T20:00:00Z",
        "2024-11-29T17:59:00Z", "2024-11-29T18:00:00Z",
    ]
    respx_mock.get(host="data.test", path="/v2/stocks/AAPL/bars").mock(
        return_value=httpx.Response(200, json={"bars": [bar(t) for t in times], "next_page_token": None})
    )
    end = datetime(2024, 12, 1, tzinfo=UTC)
    chunks = [c async for c in make_provider(clock).fetch("AAPL", datetime(2024, 3, 8, tzinfo=UTC), end)]
    kept = [c.ts.strftime("%Y-%m-%dT%H:%M") for c in chunks[0].candles]
    assert kept == [
        "2024-03-08T14:30", "2024-03-08T20:59",
        "2024-03-11T13:30", "2024-03-11T19:59",
        "2024-11-29T17:59",
    ]
    assert chunks[0].covered_until == end


async def test_follows_pages_from_the_last_bar(respx_mock, clock):
    mock_calendar(respx_mock)
    route = respx_mock.get(host="data.test", path="/v2/stocks/AAPL/bars").mock(
        side_effect=[
            httpx.Response(
                200,
                json={"bars": [bar("2024-03-08T14:30:00Z"), bar("2024-03-08T14:31:00Z")], "next_page_token": "abc"},
            ),
            httpx.Response(200, json={"bars": None, "next_page_token": None}),
        ]
    )
    start, end = datetime(2024, 3, 8, tzinfo=UTC), datetime(2024, 3, 9, tzinfo=UTC)
    chunks = [c async for c in make_provider(clock).fetch("AAPL", start, end)]
    assert [c.covered_until for c in chunks] == [datetime(2024, 3, 8, 14, 32, tzinfo=UTC), end]
    first, second = (call.request.url.params for call in route.calls)
    assert first["start"] == "2024-03-08T00:00:00Z"
    assert first["end"] == "2024-03-08T23:59:59Z"
    assert (first["feed"], first["timeframe"], first["adjustment"], first["limit"]) == ("iex", "1Min", "raw", "10000")
    assert second["start"] == "2024-03-08T14:32:00Z"
    assert route.calls[0].request.headers["APCA-API-KEY-ID"] == "KEY"


async def test_missing_key_is_permanent_and_sends_nothing(respx_mock, clock):
    provider = make_provider(clock, key=("", ""))
    with pytest.raises(PermanentError, match="API key missing"):
        [c async for c in provider.fetch("AAPL", datetime(2024, 3, 8, tzinfo=UTC), datetime(2024, 3, 9, tzinfo=UTC))]
    assert not respx_mock.calls


async def test_rejected_key_is_permanent(respx_mock, clock):
    mock_calendar(respx_mock, status=403, body={"message": "forbidden"})
    with pytest.raises(PermanentError, match="API key rejected"):
        [c async for c in make_provider(clock).fetch("AAPL", datetime(2024, 3, 8, tzinfo=UTC), datetime(2024, 3, 9, tzinfo=UTC))]


async def test_search_classifies_etfs(respx_mock, clock):
    respx_mock.get(host="trading.test", path="/v2/assets").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust", "tradable": True},
                {"symbol": "SPOT", "name": "Spotify Technology S.A.", "tradable": True},
                {"symbol": "SPXX", "name": "Old", "tradable": False},
            ],
        )
    )
    results = await make_provider(clock).search_symbols("sp")
    assert [(s.provider_symbol, s.asset_class, s.suggested_jesse_symbol) for s in results] == [
        ("SPOT", "stock", "SPOT-USD"),
        ("SPY", "etf", "SPY-USD"),
    ]


def test_quota_delay_waits_for_reset_when_nearly_exhausted():
    now = datetime(2024, 1, 1, tzinfo=UTC)
    reset = str(int(now.timestamp()) + 20)
    low = httpx.Response(200, headers={"X-RateLimit-Remaining": "3", "X-RateLimit-Reset": reset})
    plenty = httpx.Response(200, headers={"X-RateLimit-Remaining": "150", "X-RateLimit-Reset": reset})
    assert alpaca_quota_delay(low, now) == 20.0
    assert alpaca_quota_delay(plenty, now) == 0.0


def test_estimate_counts_calendar_plus_bar_pages(clock):
    start = datetime(2016, 1, 1, tzinfo=UTC)
    provider = make_provider(clock)
    assert provider.estimate_requests(start, start + timedelta(days=365 * 8)) == 83
    assert provider.estimate_requests(start, start) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_alpaca.py -v`
Expected: `ModuleNotFoundError: No module named 'app.providers.alpaca'`.

- [ ] **Step 3: Implement**

`app/providers/alpaca.py`:
```python
"""Alpaca market data (US stocks and ETFs), free IEX feed, regular session only."""
from __future__ import annotations

import math
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.domain import MINUTE, Candle, Chunk, PermanentError, SymbolInfo, floor_minute, utc
from app.providers.base import rank_matches
from app.providers.http import ProviderClient, RateLimitPolicy

DATA_URL = "https://data.alpaca.markets"
NEW_YORK = ZoneInfo("America/New_York")
PAGE = 10_000
FEED = "iex"  # free plan; switch to "sip" with a paid plan
IEX_START = datetime(2016, 1, 1, tzinfo=UTC)
KEY_REJECTED = "API key rejected — check Settings"

Credentials = Callable[[], Awaitable[tuple[str, str]]]


def alpaca_quota_delay(response: httpx.Response, now: datetime) -> float:
    remaining = response.headers.get("X-RateLimit-Remaining")
    reset = response.headers.get("X-RateLimit-Reset")
    if remaining is None or reset is None or int(remaining) > 5:
        return 0.0
    return max(0.0, int(reset) - now.timestamp())


POLICY = RateLimitPolicy(
    name="Alpaca",
    rate=3.0,  # 180 requests/min, 90% of the free plan's 200/min
    burst=5,
    concurrency=1,
    permanent_messages={
        400: "request rejected",
        401: KEY_REJECTED,
        403: KEY_REJECTED,
        404: "symbol not found",
        422: "request rejected, check the symbol",
    },
    quota_delay=alpaca_quota_delay,
)

Sessions = dict[date, tuple[datetime, datetime]]


def _rfc3339(dt: datetime) -> str:
    return utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _in_session(ts: datetime, sessions: Sessions) -> bool:
    session = sessions.get(ts.astimezone(NEW_YORK).date())
    return session is not None and session[0] <= ts < session[1]


class AlpacaProvider:
    name = "alpaca"
    label = "Alpaca (US stocks & ETFs)"
    asset_classes = ("stock", "etf")

    def __init__(self, client: ProviderClient, credentials: Credentials, trading_url: str, data_url: str = DATA_URL):
        self.client = client
        self._credentials = credentials
        self._trading = trading_url.rstrip("/")
        self._data = data_url.rstrip("/")
        self._symbols: list[SymbolInfo] | None = None

    async def _headers(self) -> dict[str, str]:
        key, secret = await self._credentials()
        if not key or not secret:
            raise PermanentError("Alpaca: API key missing — set it in Settings")
        return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    async def search_symbols(self, query: str) -> list[SymbolInfo]:
        if self._symbols is None:
            response = await self.client.get(
                f"{self._trading}/v2/assets",
                params={"status": "active", "asset_class": "us_equity"},
                headers=await self._headers(),
            )
            self._symbols = [
                SymbolInfo(
                    provider_symbol=a["symbol"],
                    asset_class="etf" if "ETF" in (a.get("name") or "").upper() else "stock",
                    suggested_jesse_symbol=f"{a['symbol']}-USD",
                    name=a.get("name") or a["symbol"],
                )
                for a in response.json()
                if a.get("tradable")
            ]
        return rank_matches(self._symbols, query)

    async def earliest_available(self, symbol: str) -> datetime:
        return IEX_START

    def available_until(self, now: datetime) -> datetime:
        return floor_minute(now)

    def estimate_requests(self, start: datetime, end: datetime) -> int:
        if end <= start:
            return 0
        trading_minutes = (end - start) / timedelta(days=1) * 5 / 7 * 390
        return 1 + max(1, math.ceil(trading_minutes / PAGE))

    async def fetch(self, symbol: str, start: datetime, end: datetime) -> AsyncIterator[Chunk]:
        headers = await self._headers()
        # Widen by a day on each side so every bar's New York date is covered.
        sessions = await self._sessions(start - timedelta(days=1), end + timedelta(days=1), headers)
        cursor = start
        while cursor < end:
            response = await self.client.get(
                f"{self._data}/v2/stocks/{symbol}/bars",
                params={
                    "timeframe": "1Min",
                    "start": _rfc3339(cursor),
                    "end": _rfc3339(end - timedelta(seconds=1)),  # Alpaca's end is inclusive
                    "limit": PAGE,
                    "feed": FEED,
                    "adjustment": "raw",
                    "sort": "asc",
                },
                headers=headers,
            )
            data = response.json()
            bars = data.get("bars") or []
            candles = []
            for b in bars:
                ts = datetime.fromisoformat(b["t"])
                if _in_session(ts, sessions):
                    candles.append(Candle(ts, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b["v"])))
            if data.get("next_page_token") and bars:
                covered = datetime.fromisoformat(bars[-1]["t"]) + MINUTE
            else:
                covered = end
            yield Chunk(candles, covered)
            cursor = covered

    async def _sessions(self, start: datetime, end: datetime, headers: dict[str, str]) -> Sessions:
        response = await self.client.get(
            f"{self._trading}/v2/calendar",
            params={"start": start.date().isoformat(), "end": end.date().isoformat()},
            headers=headers,
        )
        sessions: Sessions = {}
        for day in response.json():
            d = date.fromisoformat(day["date"])
            opens = datetime.combine(d, time.fromisoformat(day["open"]), NEW_YORK).astimezone(UTC)
            closes = datetime.combine(d, time.fromisoformat(day["close"]), NEW_YORK).astimezone(UTC)
            sessions[d] = (opens, closes)
        return sessions
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_alpaca.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add app/providers/alpaca.py tests/test_alpaca.py
git commit -m "feat: Alpaca adapter with regular-session filtering

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Dukascopy adapter (forex)

**Files:**
- Create: `app/providers/dukascopy.py`
- Test: `tests/test_dukascopy.py`

**Interfaces:**
- Consumes: `ProviderClient`, `RateLimitPolicy`, `rank_matches`, domain types.
- Produces: `DukascopyProvider(client, base_url=BASE_URL)`, `POLICY`, `RECORD` (a `struct.Struct(">3I2f")`), `decode_hour(raw: bytes, hour_start: datetime, divisor: float) -> list[Candle]`, `point_divisor(pair) -> float`, `PAIRS: dict[str, date]`. `name = "dukascopy"`, `asset_classes = ("forex",)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_dukascopy.py`:
```python
import lzma
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.domain import HOUR, Candle, PermanentError, TransientError
from app.providers.dukascopy import POLICY, RECORD, DukascopyProvider, decode_hour, point_divisor
from app.providers.http import ProviderClient

HOUR0 = datetime(2024, 1, 3, 10, tzinfo=UTC)  # a Wednesday


def bi5(ticks: list[tuple[int, int]]) -> bytes:
    raw = b"".join(RECORD.pack(ms, bid + 2, bid, 1.5, 1.5) for ms, bid in ticks)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


@pytest.fixture
def provider(clock):
    return DukascopyProvider(ProviderClient(POLICY, httpx.AsyncClient(), clock), base_url="https://duka.test/datafeed")


def test_decode_aggregates_bid_ticks_into_minutes():
    data = bi5([(1_000, 110000), (30_000, 110050), (59_000, 109990), (61_000, 110020)])
    assert decode_hour(data, HOUR0, 100000.0) == [
        Candle(HOUR0, 1.1, 1.1005, 1.0999, 1.0999, 3.0),
        Candle(HOUR0 + timedelta(minutes=1), 1.1002, 1.1002, 1.1002, 1.1002, 1.0),
    ]


def test_jpy_pairs_use_three_decimals():
    assert point_divisor("USDJPY") == 1000.0
    assert point_divisor("EURUSD") == 100000.0


def test_empty_file_has_no_candles():
    assert decode_hour(b"", HOUR0, 100000.0) == []


def test_corrupt_file_is_transient():
    with pytest.raises(TransientError, match="corrupt"):
        decode_hour(b"not lzma at all", HOUR0, 100000.0)


async def test_fetch_downloads_hour_files_in_order(respx_mock, provider):
    files = {
        "/datafeed/EURUSD/2024/00/03/10h_ticks.bi5": bi5([(0, 110000)]),
        "/datafeed/EURUSD/2024/00/03/11h_ticks.bi5": bi5([(120_000, 110100)]),
    }
    respx_mock.get(host="duka.test").mock(
        side_effect=lambda r: httpx.Response(200, content=files[r.url.path])
        if r.url.path in files
        else httpx.Response(404)
    )
    chunks = [c async for c in provider.fetch("EURUSD", HOUR0, HOUR0 + 2 * HOUR)]
    assert [c.covered_until for c in chunks] == [HOUR0 + HOUR, HOUR0 + 2 * HOUR]
    assert [c.candles[0].ts for c in chunks] == [HOUR0, HOUR0 + timedelta(hours=1, minutes=2)]


async def test_missing_hours_and_saturdays_still_advance_the_cursor(respx_mock, provider):
    route = respx_mock.get(host="duka.test").mock(return_value=httpx.Response(404))
    start = datetime(2024, 1, 5, 23, tzinfo=UTC)  # Friday 23:00
    end = datetime(2024, 1, 6, 2, tzinfo=UTC)  # Saturday 02:00
    chunks = [c async for c in provider.fetch("EURUSD", start, end)]
    assert [c.covered_until for c in chunks] == [start + HOUR, start + 2 * HOUR, end]
    assert all(c.candles == [] for c in chunks)
    assert route.call_count == 1  # Saturday hours are never requested


def test_available_until_leaves_a_full_hour_for_publishing(provider):
    now = datetime(2024, 1, 3, 12, 40, tzinfo=UTC)
    assert provider.available_until(now) == datetime(2024, 1, 3, 11, tzinfo=UTC)


def test_estimate_skips_saturdays(provider):
    start = datetime(2024, 1, 1, tzinfo=UTC)
    assert provider.estimate_requests(start, start + timedelta(days=7)) == 144
    assert provider.estimate_requests(start, start) == 0


async def test_search_and_earliest(provider):
    [info] = await provider.search_symbols("eurusd")
    assert (info.provider_symbol, info.suggested_jesse_symbol, info.asset_class) == ("EURUSD", "EUR-USD", "forex")
    assert await provider.earliest_available("EURUSD") == datetime(2004, 1, 1, tzinfo=UTC)
    with pytest.raises(PermanentError):
        await provider.earliest_available("XXXYYY")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dukascopy.py -v`
Expected: `ModuleNotFoundError: No module named 'app.providers.dukascopy'`.

- [ ] **Step 3: Implement**

`app/providers/dukascopy.py`:
```python
"""Dukascopy historical tick files (forex), aggregated to 1-minute bid candles."""
from __future__ import annotations

import asyncio
import lzma
import math
import struct
from collections import deque
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, time, timedelta

from app.domain import HOUR, Candle, Chunk, PermanentError, SymbolInfo, TransientError, floor_hour
from app.providers.base import rank_matches
from app.providers.http import ProviderClient, RateLimitPolicy

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
RECORD = struct.Struct(">3I2f")  # ms offset in hour, ask, bid, ask volume, bid volume
WINDOW = 8  # hours scheduled ahead; the client's concurrency (4) limits actual parallel downloads
SATURDAY = 5

# Conservative default start dates; the Add form lets the user choose a later one.
# Hours before a pair's real start simply come back empty.
PAIRS: dict[str, date] = {
    pair: date(2004, 1, 1)
    for pair in [
        "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
        "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "GBPJPY", "GBPCHF",
        "GBPAUD", "AUDJPY", "AUDNZD", "CADJPY", "CHFJPY", "NZDJPY",
    ]
}

POLICY = RateLimitPolicy(
    name="Dukascopy",
    rate=8.0,
    burst=8,
    concurrency=4,
    ok_statuses=frozenset({200, 404}),  # 404 = no file for that hour (market closed)
    throttle_statuses=frozenset({429, 503}),
)


def point_divisor(pair: str) -> float:
    return 1000.0 if pair.endswith("JPY") else 100000.0


def decode_hour(raw: bytes, hour_start: datetime, divisor: float) -> list[Candle]:
    if not raw:
        return []
    try:
        records = RECORD.iter_unpack(lzma.decompress(raw))
        buckets: dict[int, list[float]] = {}  # minute -> [open, high, low, close, ticks]
        for ms, _ask, bid, _ask_volume, _bid_volume in records:
            price = bid / divisor
            bucket = buckets.get(ms // 60_000)
            if bucket is None:
                buckets[ms // 60_000] = [price, price, price, price, 1]
            else:
                bucket[1] = max(bucket[1], price)
                bucket[2] = min(bucket[2], price)
                bucket[3] = price
                bucket[4] += 1
    except (lzma.LZMAError, struct.error) as exc:
        raise TransientError(f"Dukascopy: corrupt file for {hour_start:%Y-%m-%d %H}:00 ({exc})") from exc
    return [
        Candle(hour_start + timedelta(minutes=m), o, h, low, c, float(n))
        for m, (o, h, low, c, n) in sorted(buckets.items())
    ]


def _hours(first: datetime, end: datetime) -> Iterator[datetime]:
    hour = first
    while hour < end:
        yield hour
        hour += HOUR


class DukascopyProvider:
    name = "dukascopy"
    label = "Dukascopy (forex)"
    asset_classes = ("forex",)

    def __init__(self, client: ProviderClient, base_url: str = BASE_URL):
        self.client = client
        self._base = base_url.rstrip("/")
        self._symbols = [SymbolInfo(p, "forex", f"{p[:3]}-{p[3:]}", f"{p[:3]}/{p[3:]}") for p in PAIRS]

    async def search_symbols(self, query: str) -> list[SymbolInfo]:
        return rank_matches(self._symbols, query)

    async def earliest_available(self, symbol: str) -> datetime:
        if symbol not in PAIRS:
            raise PermanentError(f"Dukascopy: unknown pair {symbol}")
        return datetime.combine(PAIRS[symbol], time(), UTC)

    def available_until(self, now: datetime) -> datetime:
        return floor_hour(now) - HOUR

    def estimate_requests(self, start: datetime, end: datetime) -> int:
        if end <= start:
            return 0
        hours = math.ceil((end - floor_hour(start)) / HOUR)
        return round(hours * 6 / 7)

    async def fetch(self, symbol: str, start: datetime, end: datetime) -> AsyncIterator[Chunk]:
        divisor = point_divisor(symbol)
        hours = _hours(floor_hour(start), end)
        pending: deque[tuple[datetime, asyncio.Task[list[Candle]] | None]] = deque()

        def schedule() -> None:
            while len(pending) < WINDOW:
                hour = next(hours, None)
                if hour is None:
                    return
                task = None if hour.weekday() == SATURDAY else asyncio.create_task(self._hour(symbol, hour, divisor))
                pending.append((hour, task))

        try:
            schedule()
            while pending:
                hour, task = pending.popleft()
                candles = await task if task is not None else []
                schedule()
                yield Chunk([c for c in candles if start <= c.ts < end], min(hour + HOUR, end))
        finally:
            leftovers = [t for _, t in pending if t is not None]
            for t in leftovers:
                t.cancel()
            await asyncio.gather(*leftovers, return_exceptions=True)

    async def _hour(self, symbol: str, hour: datetime, divisor: float) -> list[Candle]:
        url = f"{self._base}/{symbol}/{hour.year}/{hour.month - 1:02d}/{hour.day:02d}/{hour.hour:02d}h_ticks.bi5"
        response = await self.client.get(url)
        if response.status_code == 404:
            return []
        return decode_hour(response.content, hour, divisor)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dukascopy.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add app/providers/dukascopy.py tests/test_dukascopy.py
git commit -m "feat: Dukascopy forex adapter

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Settings service

**Files:**
- Create: `app/services/__init__.py` (empty), `app/services/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: `EnvConfig`, `SessionFactory`, `Setting` model.
- Produces: `AppSettings(schedule_enabled: bool, schedule_cron: str, worker_concurrency: int, alpaca_key_id: str, alpaca_secret_key: str, alpaca_from_env: bool)` (frozen dataclass, positional in that order). `SettingsService(sf, env)` with `async load() -> AppSettings`, `async save(values: Mapping[str, str])` (raises `ValueError` with a user-facing message), and `async alpaca_credentials() -> tuple[str, str]`. `DEFAULTS` dict.

- [ ] **Step 1: Write the failing tests**

`tests/test_settings.py`:
```python
import pytest

from app.config import EnvConfig
from app.services.settings import AppSettings, SettingsService


def env(**overrides) -> EnvConfig:
    values = {"database_url": "unused", "alpaca_key_id": "", "alpaca_secret_key": ""} | overrides
    return EnvConfig(_env_file=None, **values)


async def test_defaults(sf):
    assert await SettingsService(sf, env()).load() == AppSettings(False, "0 * * * *", 3, "", "", False)


async def test_save_and_load(sf):
    svc = SettingsService(sf, env())
    await svc.save(
        {
            "schedule_enabled": "true",
            "schedule_cron": "0 */6 * * *",
            "worker_concurrency": "5",
            "alpaca_key_id": "K",
            "alpaca_secret_key": "S",
        }
    )
    await svc.save({"worker_concurrency": "4"})
    s = await svc.load()
    assert (s.schedule_enabled, s.schedule_cron, s.worker_concurrency) == (True, "0 */6 * * *", 4)
    assert await svc.alpaca_credentials() == ("K", "S")


async def test_environment_keys_override_saved_keys(sf):
    svc = SettingsService(sf, env(alpaca_key_id="ENVK", alpaca_secret_key="ENVS"))
    await svc.save({"alpaca_key_id": "K", "alpaca_secret_key": "S"})
    s = await svc.load()
    assert s.alpaca_from_env is True
    assert (s.alpaca_key_id, s.alpaca_secret_key) == ("ENVK", "ENVS")


@pytest.mark.parametrize(
    "values",
    [
        {"schedule_cron": "not a cron"},
        {"worker_concurrency": "0"},
        {"worker_concurrency": "x"},
        {"schedule_enabled": "maybe"},
        {"nope": "1"},
    ],
)
async def test_invalid_values_are_rejected(sf, values):
    with pytest.raises(ValueError):
        await SettingsService(sf, env()).save(values)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_settings.py -v`
Expected: `ModuleNotFoundError: No module named 'app.services'`.

- [ ] **Step 3: Implement**

`app/services/__init__.py`: empty.

`app/services/settings.py`:
```python
"""User settings stored in the database; Alpaca keys from the environment take precedence."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import EnvConfig
from app.db import SessionFactory
from app.models import Setting

DEFAULTS = {
    "schedule_enabled": "false",
    "schedule_cron": "0 * * * *",
    "worker_concurrency": "3",
    "alpaca_key_id": "",
    "alpaca_secret_key": "",
}


@dataclass(frozen=True)
class AppSettings:
    schedule_enabled: bool
    schedule_cron: str
    worker_concurrency: int
    alpaca_key_id: str
    alpaca_secret_key: str
    alpaca_from_env: bool


def _validate(values: Mapping[str, str]) -> None:
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
    if "schedule_enabled" in values and values["schedule_enabled"] not in ("true", "false"):
        raise ValueError("schedule_enabled must be 'true' or 'false'")
    if "schedule_cron" in values:
        try:
            CronTrigger.from_crontab(values["schedule_cron"], timezone=UTC)
        except ValueError as exc:
            raise ValueError(f"Invalid cron expression: {exc}") from exc
    if "worker_concurrency" in values:
        try:
            n = int(values["worker_concurrency"])
        except ValueError:
            raise ValueError("Parallel downloads must be a whole number") from None
        if not 1 <= n <= 10:
            raise ValueError("Parallel downloads must be between 1 and 10")


class SettingsService:
    def __init__(self, sf: SessionFactory, env: EnvConfig):
        self._sf = sf
        self._env = env

    async def load(self) -> AppSettings:
        async with self._sf() as s:
            stored = dict((await s.execute(select(Setting.key, Setting.value))).tuples().all())
        v = DEFAULTS | stored
        from_env = bool(self._env.alpaca_key_id and self._env.alpaca_secret_key)
        return AppSettings(
            schedule_enabled=v["schedule_enabled"] == "true",
            schedule_cron=v["schedule_cron"],
            worker_concurrency=int(v["worker_concurrency"]),
            alpaca_key_id=self._env.alpaca_key_id if from_env else v["alpaca_key_id"],
            alpaca_secret_key=self._env.alpaca_secret_key if from_env else v["alpaca_secret_key"],
            alpaca_from_env=from_env,
        )

    async def save(self, values: Mapping[str, str]) -> None:
        _validate(values)
        if not values:
            return
        stmt = pg_insert(Setting).values([{"key": k, "value": v} for k, v in values.items()])
        stmt = stmt.on_conflict_do_update(index_elements=[Setting.key], set_={"value": stmt.excluded.value})
        async with self._sf.begin() as s:
            await s.execute(stmt)

    async def alpaca_credentials(self) -> tuple[str, str]:
        s = await self.load()
        return s.alpaca_key_id, s.alpaca_secret_key
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_settings.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services tests/test_settings.py
git commit -m "feat: settings service with environment overrides

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Job queue service

**Files:**
- Create: `app/services/jobs.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Consumes: `ProviderRegistry` (Task 4), models, `Clock`.
- Produces (all in `app.services.jobs`):
  - `PRIORITY = {"update": 10, "backfill": 0}`, `BACKOFF_SECONDS = (60, 300, 900, 3600)`, `GIVE_UP_AFTER = timedelta(hours=24)`
  - `async enqueue(sf, registry, clock, asset_id: int, kind: str) -> Job`
  - `async enqueue_all(sf, registry, clock, kind="update") -> list[Job]`
  - `async claim_next(sf, clock) -> int | None`
  - `async finish(sf, clock, job_id, elapsed: float) -> bool`
  - `async requeue(sf, job_id, elapsed: float) -> bool`
  - `async wait(sf, job_id, resume_at: datetime, detail: str, elapsed: float) -> bool`
  - `async pause(sf, clock, job_id, error: str, elapsed: float) -> None`
  - `async fail(sf, clock, job_id, error: str, elapsed: float) -> bool`
  - `async cancel(sf, clock, job_id) -> bool`
  - `async recover(sf) -> int`
  - `async get_job(sf, job_id) -> Job | None`
  - `async latest_jobs_by_asset(sf) -> dict[int, Job]`
  - `async list_recent(sf, limit=200) -> list[tuple[Job, Asset]]`
- The transitions `finish`, `requeue`, `wait`, `pause` and `fail` only apply while the job is `running`.

- [ ] **Step 1: Write the failing tests**

`tests/test_jobs.py`:
```python
from datetime import UTC, datetime, timedelta

import pytest

from app.providers.base import ProviderRegistry
from app.services import jobs
from tests.fakes import FakeProvider, make_asset


@pytest.fixture
def registry(clock):
    return ProviderRegistry([FakeProvider(clock)])


async def test_enqueue_backfill_covers_start_date_to_now(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    assert (job.status, job.priority, job.range_start, job.range_end) == (
        "queued", 0, asset.start_date, clock.now(),
    )


async def test_enqueue_returns_the_existing_active_job(sf, registry, clock):
    asset = await make_asset(sf)
    first = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    second = await jobs.enqueue(sf, registry, clock, asset.id, "update")
    assert second.id == first.id and second.kind == "backfill"


async def test_enqueue_starts_from_the_cursor_and_skips_up_to_date_assets(sf, registry, clock):
    asset = await make_asset(sf, fetched_until=clock.now() - timedelta(minutes=30))
    job = await jobs.enqueue(sf, registry, clock, asset.id, "update")
    assert (job.range_start, job.priority) == (clock.now() - timedelta(minutes=30), 10)
    fresh = await make_asset(sf, provider_symbol="FRESH", fetched_until=clock.now())
    done = await jobs.enqueue(sf, registry, clock, fresh.id, "update")
    assert done.status == "done" and done.finished_at == clock.now()


async def test_claim_prefers_updates_then_oldest(sf, registry, clock):
    a = await make_asset(sf, provider_symbol="A")
    b = await make_asset(sf, provider_symbol="B")
    c = await make_asset(sf, provider_symbol="C")
    backfill = await jobs.enqueue(sf, registry, clock, a.id, "backfill")
    update = await jobs.enqueue(sf, registry, clock, b.id, "update")
    later = await jobs.enqueue(sf, registry, clock, c.id, "backfill")
    assert await jobs.claim_next(sf, clock) == update.id
    assert await jobs.claim_next(sf, clock) == backfill.id
    assert await jobs.claim_next(sf, clock) == later.id
    assert await jobs.claim_next(sf, clock) is None
    assert (await jobs.get_job(sf, update.id)).status == "running"


async def test_waiting_job_is_claimed_again_after_resume_time(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    await jobs.claim_next(sf, clock)
    assert await jobs.wait(sf, job.id, clock.now() + timedelta(seconds=30), "Fake rate limit", 1.0)
    assert await jobs.claim_next(sf, clock) is None
    clock.advance(30)
    assert await jobs.claim_next(sf, clock) == job.id


async def test_pause_backs_off_then_gives_up_after_24h_without_progress(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    delays = []
    for _ in range(5):
        assert await jobs.claim_next(sf, clock) == job.id
        await jobs.pause(sf, clock, job.id, "Fake: boom", 0.0)
        paused = await jobs.get_job(sf, job.id)
        assert paused.status == "paused" and "retrying at" in paused.status_detail
        delays.append((paused.next_attempt_at - clock.now()).total_seconds())
        clock.advance(delays[-1])
    assert delays == [60, 300, 900, 3600, 3600]
    clock.advance(24 * 3600)
    assert await jobs.claim_next(sf, clock) == job.id
    await jobs.pause(sf, clock, job.id, "Fake: boom", 0.0)
    failed = await jobs.get_job(sf, job.id)
    assert failed.status == "failed" and "24 h" in failed.error


async def test_transitions_only_apply_to_running_jobs(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    await jobs.claim_next(sf, clock)
    assert await jobs.cancel(sf, clock, job.id)
    assert not await jobs.finish(sf, clock, job.id, 5.0)
    assert (await jobs.get_job(sf, job.id)).status == "cancelled"


async def test_finish_accumulates_run_time(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    await jobs.claim_next(sf, clock)
    assert await jobs.requeue(sf, job.id, 2.0)
    await jobs.claim_next(sf, clock)
    assert await jobs.finish(sf, clock, job.id, 3.0)
    done = await jobs.get_job(sf, job.id)
    assert (done.status, done.run_seconds, done.finished_at) == ("done", 5.0, clock.now())
    assert (await jobs.latest_jobs_by_asset(sf))[asset.id].id == job.id


async def test_fail_records_the_error(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    await jobs.claim_next(sf, clock)
    assert await jobs.fail(sf, clock, job.id, "Alpaca: API key rejected", 1.0)
    failed = await jobs.get_job(sf, job.id)
    assert (failed.status, failed.error) == ("failed", "Alpaca: API key rejected")


async def test_recover_requeues_running_and_waiting_jobs(sf, registry, clock):
    a = await make_asset(sf, provider_symbol="A")
    b = await make_asset(sf, provider_symbol="B")
    ja = await jobs.enqueue(sf, registry, clock, a.id, "backfill")
    jb = await jobs.enqueue(sf, registry, clock, b.id, "backfill")
    await jobs.claim_next(sf, clock)
    await jobs.claim_next(sf, clock)
    await jobs.wait(sf, jb.id, clock.now() + timedelta(minutes=5), "Fake rate limit", 0.0)
    assert await jobs.recover(sf) == 2
    assert {(await jobs.get_job(sf, j.id)).status for j in (ja, jb)} == {"queued"}


async def test_enqueue_all_skips_disabled_assets(sf, registry, clock):
    a = await make_asset(sf, provider_symbol="A")
    await make_asset(sf, provider_symbol="B", enabled=False)
    created = await jobs.enqueue_all(sf, registry, clock)
    assert [j.asset_id for j in created] == [a.id]


async def test_list_recent_joins_assets(sf, registry, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    [(listed_job, listed_asset)] = await jobs.list_recent(sf)
    assert (listed_job.id, listed_asset.jesse_symbol) == (job.id, "FAKE-USD")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_jobs.py -v`
Expected: `ImportError: cannot import name 'jobs' from 'app.services'`.

- [ ] **Step 3: Implement**

`app/services/jobs.py`:
```python
"""Job queue stored in the `jobs` table: creation, claiming and state transitions."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.clock import Clock
from app.db import SessionFactory
from app.models import ACTIVE_STATUSES, Asset, Job
from app.providers.base import ProviderRegistry

PRIORITY = {"update": 10, "backfill": 0}
BACKOFF_SECONDS = (60, 300, 900, 3600)
GIVE_UP_AFTER = timedelta(hours=24)


async def _active_job(s, asset_id: int) -> Job | None:
    return await s.scalar(select(Job).where(Job.asset_id == asset_id, Job.status.in_(ACTIVE_STATUSES)))


async def enqueue(sf: SessionFactory, registry: ProviderRegistry, clock: Clock, asset_id: int, kind: str) -> Job:
    """Create a job for the asset, or return its already active job."""
    try:
        async with sf.begin() as s:
            existing = await _active_job(s, asset_id)
            if existing is not None:
                return existing
            asset = await s.get(Asset, asset_id)
            if asset is None:
                raise ValueError(f"Asset {asset_id} not found")
            now = clock.now()
            start = asset.fetched_until or asset.start_date
            end = max(registry.get(asset.provider).available_until(now), start)
            pending = start < end
            job = Job(
                asset_id=asset_id,
                kind=kind,
                priority=PRIORITY[kind],
                status="queued" if pending else "done",
                range_start=start,
                range_end=end,
                last_progress_at=now,
                finished_at=None if pending else now,
            )
            s.add(job)
        return job
    except IntegrityError:
        # Another request created the active job concurrently.
        async with sf() as s:
            existing = await _active_job(s, asset_id)
        if existing is None:
            raise
        return existing


async def enqueue_all(sf: SessionFactory, registry: ProviderRegistry, clock: Clock, kind: str = "update") -> list[Job]:
    async with sf() as s:
        asset_ids = (await s.scalars(select(Asset.id).where(Asset.enabled).order_by(Asset.id))).all()
    return [await enqueue(sf, registry, clock, asset_id, kind) for asset_id in asset_ids]


async def claim_next(sf: SessionFactory, clock: Clock) -> int | None:
    now = clock.now()
    async with sf.begin() as s:
        job = await s.scalar(
            select(Job)
            .where(
                or_(
                    Job.status == "queued",
                    and_(Job.status.in_(("waiting", "paused")), Job.next_attempt_at <= now),
                )
            )
            .order_by(Job.priority.desc(), Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        job.status = "running"
        job.started_at = job.started_at or now
        job.next_attempt_at = None
        job.status_detail = None
        return job.id


async def _transition(sf: SessionFactory, job_id: int, elapsed: float, **values) -> bool:
    async with sf.begin() as s:
        result = await s.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running")
            .values(run_seconds=Job.run_seconds + elapsed, **values)
        )
    return result.rowcount == 1


async def finish(sf: SessionFactory, clock: Clock, job_id: int, elapsed: float) -> bool:
    return await _transition(
        sf, job_id, elapsed, status="done", finished_at=clock.now(), status_detail=None, next_attempt_at=None
    )


async def requeue(sf: SessionFactory, job_id: int, elapsed: float) -> bool:
    return await _transition(sf, job_id, elapsed, status="queued")


async def wait(sf: SessionFactory, job_id: int, resume_at: datetime, detail: str, elapsed: float) -> bool:
    return await _transition(sf, job_id, elapsed, status="waiting", next_attempt_at=resume_at, status_detail=detail)


async def fail(sf: SessionFactory, clock: Clock, job_id: int, error: str, elapsed: float) -> bool:
    return await _transition(
        sf, job_id, elapsed, status="failed", error=error, status_detail=None, finished_at=clock.now()
    )


async def pause(sf: SessionFactory, clock: Clock, job_id: int, error: str, elapsed: float) -> None:
    """Transient failure: retry later with backoff, or give up after 24 h without progress."""
    now = clock.now()
    async with sf.begin() as s:
        job = await s.scalar(select(Job).where(Job.id == job_id, Job.status == "running").with_for_update())
        if job is None:
            return
        job.run_seconds += elapsed
        job.attempt += 1
        job.error = error
        if now - (job.last_progress_at or now) >= GIVE_UP_AFTER:
            job.status = "failed"
            job.finished_at = now
            job.status_detail = None
            job.error = f"Gave up after 24 h without progress: {error}"
        else:
            delay = BACKOFF_SECONDS[min(job.attempt, len(BACKOFF_SECONDS)) - 1]
            retry_at = now + timedelta(seconds=delay)
            job.status = "paused"
            job.next_attempt_at = retry_at
            job.status_detail = f"{error} — retrying at {retry_at:%H:%M} UTC"


async def cancel(sf: SessionFactory, clock: Clock, job_id: int) -> bool:
    async with sf.begin() as s:
        result = await s.execute(
            update(Job)
            .where(Job.id == job_id, Job.status.in_(ACTIVE_STATUSES))
            .values(status="cancelled", finished_at=clock.now(), next_attempt_at=None, status_detail=None)
        )
    return result.rowcount == 1


async def recover(sf: SessionFactory) -> int:
    """On startup: jobs interrupted mid-run (or waiting on a rate limit) go back to the queue."""
    async with sf.begin() as s:
        result = await s.execute(
            update(Job)
            .where(Job.status.in_(("running", "waiting")))
            .values(status="queued", next_attempt_at=None, status_detail=None)
        )
    return result.rowcount


async def get_job(sf: SessionFactory, job_id: int) -> Job | None:
    async with sf() as s:
        return await s.get(Job, job_id)


async def latest_jobs_by_asset(sf: SessionFactory) -> dict[int, Job]:
    async with sf() as s:
        rows = await s.scalars(select(Job).distinct(Job.asset_id).order_by(Job.asset_id, Job.id.desc()))
        return {job.asset_id: job for job in rows}


async def list_recent(sf: SessionFactory, limit: int = 200) -> list[tuple[Job, Asset]]:
    async with sf() as s:
        result = await s.execute(
            select(Job, Asset).join(Asset, Asset.id == Job.asset_id).order_by(Job.id.desc()).limit(limit)
        )
        return list(result.tuples())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_jobs.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/jobs.py tests/test_jobs.py
git commit -m "feat: job queue with priorities, backoff and recovery

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Sync (update logic) and worker pool

**Files:**
- Create: `app/services/sync.py`, `app/worker.py`
- Test: `tests/test_sync.py`, `tests/test_worker.py`

**Interfaces:**
- Consumes: `jobs` service (Task 8), `ProviderRegistry`, models, domain errors.
- Produces: `app.services.sync`: `SLICE_SECONDS = 60.0`, `SliceOutcome` (StrEnum with `DONE`, `YIELDED`, `STOPPED`), `async run_slice(sf, registry, clock, job_id, slice_seconds=SLICE_SECONDS) -> SliceOutcome` (raises provider errors), `async insert_candles(session, asset_id, candles) -> int` (the number of rows inserted).
- Produces: `app.worker.Worker(sf, registry, clock, concurrency=3, *, poll_interval=1.0, slice_seconds=SLICE_SECONDS)` with `start()`, `resize(n)`, `async stop()`, `async run_job(job_id)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_sync.py`:
```python
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.domain import TransientError
from app.models import Asset, CandleRow
from app.providers.base import ProviderRegistry
from app.services import jobs, sync
from app.services.sync import SliceOutcome
from tests.fakes import FakeProvider, make_asset


@pytest.fixture
def provider(clock):
    return FakeProvider(clock)


@pytest.fixture
def registry(provider):
    return ProviderRegistry([provider])


async def start_job(sf, registry, clock, asset_id, kind="backfill"):
    job = await jobs.enqueue(sf, registry, clock, asset_id, kind)
    assert await jobs.claim_next(sf, clock) == job.id
    return job


async def candle_count(sf, asset_id) -> int:
    async with sf() as s:
        return await s.scalar(select(func.count()).select_from(CandleRow).where(CandleRow.asset_id == asset_id))


async def cursor(sf, asset_id):
    async with sf() as s:
        return (await s.get(Asset, asset_id)).fetched_until


async def test_backfill_stores_every_candle_and_advances_the_cursor(sf, registry, clock):
    asset = await make_asset(sf)
    job = await start_job(sf, registry, clock, asset.id)
    assert await sync.run_slice(sf, registry, clock, job.id) is SliceOutcome.DONE
    assert await candle_count(sf, asset.id) == 120
    assert await cursor(sf, asset.id) == clock.now()
    stored = await jobs.get_job(sf, job.id)
    assert (stored.requests_made, stored.candles_added, stored.attempt) == (12, 120, 0)


async def test_update_fetches_only_newer_candles(sf, registry, provider, clock):
    asset = await make_asset(sf)
    job = await start_job(sf, registry, clock, asset.id)
    await sync.run_slice(sf, registry, clock, job.id)
    await jobs.finish(sf, clock, job.id, 0.0)
    first_end = clock.now()
    clock.advance(30 * 60)
    update_job = await start_job(sf, registry, clock, asset.id, "update")
    assert await sync.run_slice(sf, registry, clock, update_job.id) is SliceOutcome.DONE
    assert provider.calls[-1] == (first_end, clock.now())
    assert await candle_count(sf, asset.id) == 150


async def test_overlapping_rerun_inserts_no_duplicates(sf, registry, clock):
    asset = await make_asset(sf)
    job = await start_job(sf, registry, clock, asset.id)
    await sync.run_slice(sf, registry, clock, job.id)
    await jobs.finish(sf, clock, job.id, 0.0)
    async with sf.begin() as s:
        await s.execute(
            update(Asset).where(Asset.id == asset.id).values(fetched_until=clock.now() - timedelta(minutes=20))
        )
    rerun = await start_job(sf, registry, clock, asset.id, "update")
    await sync.run_slice(sf, registry, clock, rerun.id)
    assert await candle_count(sf, asset.id) == 120
    assert (await jobs.get_job(sf, rerun.id)).candles_added == 0


async def test_failure_keeps_progress_and_resume_skips_finished_ranges(sf, clock):
    asset = await make_asset(sf)
    failing = ProviderRegistry([FakeProvider(clock, fail_after=3)])
    job = await start_job(sf, failing, clock, asset.id)
    with pytest.raises(TransientError):
        await sync.run_slice(sf, failing, clock, job.id)
    assert await candle_count(sf, asset.id) == 30
    resume_from = await cursor(sf, asset.id)
    assert resume_from == asset.start_date + timedelta(minutes=30)
    healthy = FakeProvider(clock)
    assert await sync.run_slice(sf, ProviderRegistry([healthy]), clock, job.id) is SliceOutcome.DONE
    assert healthy.calls == [(resume_from, job.range_end)]
    assert await candle_count(sf, asset.id) == 120


async def test_empty_ranges_still_advance_the_cursor(sf, clock):
    asset = await make_asset(sf)
    provider = FakeProvider(clock, listed=asset.start_date + timedelta(minutes=60))
    registry = ProviderRegistry([provider])
    job = await start_job(sf, registry, clock, asset.id)
    assert await sync.run_slice(sf, registry, clock, job.id) is SliceOutcome.DONE
    assert await candle_count(sf, asset.id) == 60
    assert await cursor(sf, asset.id) == job.range_end
    await jobs.finish(sf, clock, job.id, 0.0)
    clock.advance(600)
    later = await start_job(sf, registry, clock, asset.id, "update")
    await sync.run_slice(sf, registry, clock, later.id)
    assert provider.calls[-1][0] == job.range_end


async def test_backfill_yields_after_its_slice(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock, seconds_per_chunk=25)])
    asset = await make_asset(sf)
    job = await start_job(sf, registry, clock, asset.id)
    assert await sync.run_slice(sf, registry, clock, job.id, slice_seconds=60) is SliceOutcome.YIELDED
    assert await candle_count(sf, asset.id) == 30


async def test_update_jobs_are_not_sliced(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock, seconds_per_chunk=25)])
    asset = await make_asset(sf)
    job = await start_job(sf, registry, clock, asset.id, "update")
    assert await sync.run_slice(sf, registry, clock, job.id, slice_seconds=60) is SliceOutcome.DONE


async def test_cancelled_job_stops_without_storing(sf, registry, clock):
    asset = await make_asset(sf)
    job = await start_job(sf, registry, clock, asset.id)
    await jobs.cancel(sf, clock, job.id)
    assert await sync.run_slice(sf, registry, clock, job.id) is SliceOutcome.STOPPED
    assert await candle_count(sf, asset.id) == 0
```

`tests/test_worker.py`:
```python
import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.clock import Clock
from app.domain import PermanentError, RateLimited, TransientError, floor_minute
from app.providers.base import ProviderRegistry
from app.services import jobs
from app.worker import Worker
from tests.fakes import FakeProvider, make_asset

RESUME = datetime(2024, 1, 1, 3, 0, tzinfo=UTC)


async def run_one(worker, sf, clock):
    job_id = await jobs.claim_next(sf, clock)
    await worker.run_job(job_id)
    return await jobs.get_job(sf, job_id)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (RateLimited("Fake", RESUME), "waiting"),
        (PermanentError("Fake: bad key"), "failed"),
        (TransientError("Fake: boom"), "paused"),
        (RuntimeError("bug"), "paused"),
    ],
)
async def test_errors_map_to_job_status(sf, clock, error, status):
    registry = ProviderRegistry([FakeProvider(clock, fail_after=0, error=error)])
    asset = await make_asset(sf)
    await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    job = await run_one(Worker(sf, registry, clock), sf, clock)
    assert job.status == status


async def test_rate_limited_job_shows_when_it_resumes(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock, fail_after=0, error=RateLimited("Fake", RESUME))])
    asset = await make_asset(sf)
    await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    job = await run_one(Worker(sf, registry, clock), sf, clock)
    assert job.status_detail == "Fake rate limit, resuming 03:00:00 UTC"
    assert job.next_attempt_at == RESUME


async def test_completed_slice_marks_job_done(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock)])
    asset = await make_asset(sf)
    await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    assert (await run_one(Worker(sf, registry, clock), sf, clock)).status == "done"


async def test_sliced_backfill_lets_a_queued_update_run_first(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock, seconds_per_chunk=25)])
    a = await make_asset(sf, provider_symbol="A")
    b = await make_asset(sf, provider_symbol="B")
    await jobs.enqueue(sf, registry, clock, a.id, "backfill")
    worker = Worker(sf, registry, clock, slice_seconds=60)
    assert (await run_one(worker, sf, clock)).status == "queued"
    update = await jobs.enqueue(sf, registry, clock, b.id, "update")
    assert await jobs.claim_next(sf, clock) == update.id


async def test_worker_runs_queued_jobs_in_the_background(sf):
    clock = Clock()
    registry = ProviderRegistry([FakeProvider()])
    asset = await make_asset(sf, start_date=floor_minute(clock.now()) - timedelta(minutes=30))
    job = await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    worker = Worker(sf, registry, clock, concurrency=2, poll_interval=0.05)
    worker.start()
    try:
        for _ in range(100):
            if (await jobs.get_job(sf, job.id)).status == "done":
                break
            await asyncio.sleep(0.05)
    finally:
        await worker.stop()
    assert (await jobs.get_job(sf, job.id)).status == "done"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_sync.py tests/test_worker.py -v`
Expected: `ImportError: cannot import name 'sync' from 'app.services'`.

- [ ] **Step 3: Implement sync**

`app/services/sync.py`:
```python
"""The update logic: fetch chunks and commit candles together with the cursor."""
from __future__ import annotations

from contextlib import aclosing
from enum import StrEnum

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import Clock
from app.db import SessionFactory
from app.domain import Candle, Chunk
from app.models import Asset, CandleRow, Job
from app.providers.base import ProviderRegistry

SLICE_SECONDS = 60.0
INSERT_BATCH = 4000  # 7 params per row stays below PostgreSQL's 32767-parameter limit


class SliceOutcome(StrEnum):
    DONE = "done"  # reached range_end
    YIELDED = "yielded"  # backfill slice used up; requeue so other jobs get a turn
    STOPPED = "stopped"  # job was cancelled or deleted


async def insert_candles(session: AsyncSession, asset_id: int, candles: list[Candle]) -> int:
    added = 0
    for i in range(0, len(candles), INSERT_BATCH):
        rows = [
            {"asset_id": asset_id, "ts": c.ts, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
            for c in candles[i : i + INSERT_BATCH]
        ]
        stmt = pg_insert(CandleRow).values(rows).on_conflict_do_nothing(index_elements=["asset_id", "ts"])
        added += (await session.execute(stmt)).rowcount
    return added


async def _commit_chunk(sf: SessionFactory, clock: Clock, job_id: int, asset_id: int, chunk: Chunk) -> bool:
    """Store one chunk and advance the cursor atomically. Returns False if the job is no longer running."""
    async with sf.begin() as s:
        running = await s.scalar(select(Job.id).where(Job.id == job_id, Job.status == "running").with_for_update())
        if running is None:
            return False
        added = await insert_candles(s, asset_id, chunk.candles)
        await s.execute(
            update(Asset)
            .where(Asset.id == asset_id)
            .values(
                fetched_until=func.greatest(func.coalesce(Asset.fetched_until, chunk.covered_until), chunk.covered_until)
            )
        )
        await s.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                requests_made=Job.requests_made + 1,
                candles_added=Job.candles_added + added,
                attempt=0,
                last_progress_at=clock.now(),
                status_detail=None,
            )
        )
    return True


async def run_slice(
    sf: SessionFactory,
    registry: ProviderRegistry,
    clock: Clock,
    job_id: int,
    slice_seconds: float = SLICE_SECONDS,
) -> SliceOutcome:
    """Run one job until done (updates) or for about `slice_seconds` (backfills). Provider errors propagate."""
    async with sf() as s:
        job = await s.get(Job, job_id)
        asset = await s.get(Asset, job.asset_id) if job is not None else None
    if job is None or asset is None:
        return SliceOutcome.STOPPED
    provider = registry.get(asset.provider)
    start = asset.fetched_until or asset.start_date
    if start >= job.range_end:
        return SliceOutcome.DONE
    deadline = clock.monotonic() + slice_seconds if job.kind == "backfill" else None
    async with aclosing(provider.fetch(asset.provider_symbol, start, job.range_end)) as chunks:
        async for chunk in chunks:
            if not await _commit_chunk(sf, clock, job.id, asset.id, chunk):
                return SliceOutcome.STOPPED
            if deadline is not None and clock.monotonic() >= deadline and chunk.covered_until < job.range_end:
                return SliceOutcome.YIELDED
    return SliceOutcome.DONE
```

- [ ] **Step 4: Implement the worker**

`app/worker.py`:
```python
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

    async def run_job(self, job_id: int) -> None:
        began = self._clock.monotonic()

        def elapsed() -> float:
            return self._clock.monotonic() - began

        try:
            outcome = await sync.run_slice(self._sf, self._registry, self._clock, job_id, self._slice_seconds)
        except RateLimited as exc:
            await jobs.wait(self._sf, job_id, exc.resume_at, str(exc), elapsed())
        except PermanentError as exc:
            log.warning("job %d failed: %s", job_id, exc)
            await jobs.fail(self._sf, self._clock, job_id, str(exc), elapsed())
        except TransientError as exc:
            log.warning("job %d paused: %s", job_id, exc)
            await jobs.pause(self._sf, self._clock, job_id, str(exc), elapsed())
        except Exception as exc:
            log.exception("job %d: unexpected error", job_id)
            await jobs.pause(self._sf, self._clock, job_id, f"Unexpected error: {exc!r}", elapsed())
        else:
            if outcome is SliceOutcome.DONE:
                await jobs.finish(self._sf, self._clock, job_id, elapsed())
            elif outcome is SliceOutcome.YIELDED:
                await jobs.requeue(self._sf, job_id, elapsed())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_sync.py tests/test_worker.py -v`
Expected: 16 passed.

- [ ] **Step 6: Commit**

```bash
git add app/services/sync.py app/worker.py tests/test_sync.py tests/test_worker.py
git commit -m "feat: resumable chunked sync and worker pool

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Jesse CSV export

**Files:**
- Create: `app/services/export.py`
- Test: `tests/test_export.py`

**Interfaces:**
- Consumes: `insert_candles` (Task 9, tests only), `CandleRow`, `to_ms`.
- Produces: `HEADER`, `ExportRange(first: datetime, last: datetime)`, `day_bounds(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]` (end is inclusive by day, so the result is exclusive at the next midnight), `is_valid(o, h, l, c, v) -> bool`, `format_row(ts, open, close, high, low, volume) -> str`, `async export_range(sf, asset_id, start, end) -> ExportRange | None`, `stream_csv(sf, asset_id, start, end) -> AsyncIterator[str]`, `export_filename(jesse_symbol, rng) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/test_export.py`:
```python
from datetime import UTC, date, datetime, timedelta

from app.domain import Candle, to_ms
from app.services.export import day_bounds, export_filename, export_range, format_row, stream_csv
from app.services.sync import insert_candles
from tests.fakes import make_asset

T0 = datetime(2024, 1, 9, 14, 0, tzinfo=UTC)


def c(minute, o=1.0, h=2.0, l=0.5, cl=1.5, v=10.0, base=T0) -> Candle:
    return Candle(base + timedelta(minutes=minute), o, h, l, cl, v)


async def seed(sf, asset_id, candles):
    async with sf.begin() as s:
        await insert_candles(s, asset_id, candles)


async def read(sf, asset_id, start=None, end=None) -> str:
    return "".join([part async for part in stream_csv(sf, asset_id, start, end)])


async def test_csv_matches_jesse_format(sf):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [c(1, 181.40, 181.44, 181.20, 181.31, 9871), c(0, 181.25, 181.55, 181.10, 181.40, 12043)])
    assert await read(sf, asset.id) == (
        "timestamp,open,close,high,low,volume\n"
        "1704808800000,181.25,181.4,181.55,181.1,12043\n"
        "1704808860000,181.4,181.31,181.44,181.2,9871\n"
    )


async def test_invalid_rows_are_skipped_and_logged(sf, caplog):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [c(0), c(1, h=1.0), c(2, l=1.6), c(3, v=-1.0), c(4, o=float("nan")), c(5)])
    lines = (await read(sf, asset.id)).splitlines()
    assert [line.split(",")[0] for line in lines[1:]] == [str(to_ms(T0)), str(to_ms(T0 + timedelta(minutes=5)))]
    assert "skipped 4 invalid candles" in caplog.text


async def test_date_range_is_inclusive_by_day(sf):
    asset = await make_asset(sf)
    midnight = datetime(2024, 1, 2, tzinfo=UTC)
    await seed(sf, asset.id, [c(-1, base=midnight), c(0, base=midnight), c(24 * 60, base=midnight)])
    start, end = day_bounds(date(2024, 1, 2), date(2024, 1, 2))
    lines = (await read(sf, asset.id, start, end)).splitlines()
    assert lines[1:] == [f"{to_ms(midnight)},1,1.5,2,0.5,10"]
    rng = await export_range(sf, asset.id, start, end)
    assert (rng.first, rng.last) == (midnight, midnight)
    assert export_filename("FAKE-USD", rng) == "FAKE-USD_2024-01-02_2024-01-02.csv"
    assert await export_range(sf, asset.id, *day_bounds(date(2025, 1, 1), None)) is None


def test_numbers_never_use_scientific_notation():
    assert format_row(T0, 0.00000812, 0.00000813, 0.00000815, 0.0000081, 12_000_000_000.0) == (
        "1704808800000,0.00000812,0.00000813,0.00000815,0.0000081,12000000000\n"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_export.py -v`
Expected: `ModuleNotFoundError: No module named 'app.services.export'`.

- [ ] **Step 3: Implement**

`app/services/export.py`:
```python
"""Jesse "Custom Data" CSV export, streamed straight from the database."""
from __future__ import annotations

import logging
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select

from app.db import SessionFactory
from app.domain import to_ms
from app.models import CandleRow

log = logging.getLogger(__name__)

HEADER = "timestamp,open,close,high,low,volume\n"
FLUSH_ROWS = 5000


@dataclass(frozen=True)
class ExportRange:
    first: datetime
    last: datetime


def day_bounds(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    """Dates to a [start, end) datetime range; `end` is inclusive by day."""
    return (
        datetime.combine(start, time(), UTC) if start else None,
        datetime.combine(end + timedelta(days=1), time(), UTC) if end else None,
    )


def is_valid(o, h, l, c, v) -> bool:
    if any(x is None or math.isnan(x) or math.isinf(x) for x in (o, h, l, c, v)):
        return False
    return h >= max(o, c, l) and l <= min(o, c, h) and v >= 0


def _num(x: float) -> str:
    text = f"{x:.10f}".rstrip("0").rstrip(".")
    return text or "0"


def format_row(ts: datetime, open_: float, close: float, high: float, low: float, volume: float) -> str:
    return f"{to_ms(ts)},{_num(open_)},{_num(close)},{_num(high)},{_num(low)},{_num(volume)}\n"


def _filters(asset_id: int, start: datetime | None, end: datetime | None) -> list:
    conditions = [CandleRow.asset_id == asset_id]
    if start is not None:
        conditions.append(CandleRow.ts >= start)
    if end is not None:
        conditions.append(CandleRow.ts < end)
    return conditions


async def export_range(sf: SessionFactory, asset_id: int, start: datetime | None, end: datetime | None) -> ExportRange | None:
    async with sf() as s:
        first, last = (
            await s.execute(select(func.min(CandleRow.ts), func.max(CandleRow.ts)).where(*_filters(asset_id, start, end)))
        ).one()
    return ExportRange(first, last) if first is not None else None


async def stream_csv(sf: SessionFactory, asset_id: int, start: datetime | None, end: datetime | None) -> AsyncIterator[str]:
    yield HEADER
    skipped = 0
    buffer: list[str] = []
    stmt = (
        select(CandleRow.ts, CandleRow.open, CandleRow.close, CandleRow.high, CandleRow.low, CandleRow.volume)
        .where(*_filters(asset_id, start, end))
        .order_by(CandleRow.ts)
        .execution_options(yield_per=FLUSH_ROWS)
    )
    async with sf() as s:
        result = await s.stream(stmt)
        async for ts, o, c, h, l, v in result:
            if not is_valid(o, h, l, c, v):
                skipped += 1
                continue
            buffer.append(format_row(ts, o, c, h, l, v))
            if len(buffer) >= FLUSH_ROWS:
                yield "".join(buffer)
                buffer.clear()
    if buffer:
        yield "".join(buffer)
    if skipped:
        log.warning("export of asset %s: skipped %d invalid candles", asset_id, skipped)


def export_filename(jesse_symbol: str, rng: ExportRange) -> str:
    return f"{jesse_symbol}_{rng.first:%Y-%m-%d}_{rng.last:%Y-%m-%d}.csv"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_export.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/export.py tests/test_export.py
git commit -m "feat: streaming Jesse CSV export with row validation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Asset service, stats cache, progress/ETA helpers

**Files:**
- Create: `app/services/assets.py`, `app/services/progress.py`
- Test: `tests/test_assets.py`, `tests/test_progress.py`

**Interfaces:**
- Consumes: models, `insert_candles` (tests only), `ACTIVE_STATUSES`, `Provider`.
- Produces: `app.services.assets`: `DuplicateAsset(ValueError)`, `JESSE_SYMBOL` regex, `async create_asset(sf, *, provider, provider_symbol, asset_class, jesse_symbol, start_date: date) -> Asset`, `async list_assets(sf) -> list[Asset]` (sorted by jesse_symbol), `async get_asset(sf, asset_id) -> Asset | None`, `async update_asset(sf, asset_id, *, jesse_symbol, enabled)`, `async delete_asset(sf, asset_id)`, `AssetStats(first, last, count)`, `StatsCache(clock, ttl_seconds=120)` with `async get(sf) -> dict[int, AssetStats]` and `invalidate()`.
- Produces: `app.services.progress`: `JobProgress(percent: float, eta_seconds: float | None)`, `job_progress(job, fetched_until, provider) -> JobProgress`, `format_duration(seconds) -> str`, `estimate_text(provider, start, end) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/test_assets.py`:
```python
from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain import Candle
from app.services.assets import (
    AssetStats,
    DuplicateAsset,
    StatsCache,
    create_asset,
    delete_asset,
    get_asset,
    list_assets,
    update_asset,
)
from app.services.sync import insert_candles
from tests.fakes import make_asset

T0 = datetime(2024, 1, 1, tzinfo=UTC)


async def seed(sf, asset_id, minutes):
    async with sf.begin() as s:
        await insert_candles(s, asset_id, [Candle(T0 + timedelta(minutes=m), 1, 2, 0.5, 1.5, 10) for m in minutes])


async def test_create_rejects_duplicates_and_bad_jesse_symbols(sf):
    kwargs = dict(provider="fake", provider_symbol="FAKEUSD", asset_class="crypto", jesse_symbol="FAKE-USD", start_date=date(2024, 1, 1))
    asset = await create_asset(sf, **kwargs)
    assert asset.start_date == T0
    with pytest.raises(DuplicateAsset, match="already exists"):
        await create_asset(sf, **kwargs)
    with pytest.raises(ValueError, match="Jesse symbol"):
        await create_asset(sf, **(kwargs | {"provider_symbol": "OTHER", "jesse_symbol": "fakeusd"}))


async def test_update_and_delete(sf):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [0, 1])
    await update_asset(sf, asset.id, jesse_symbol="FOO-USD", enabled=False)
    got = await get_asset(sf, asset.id)
    assert (got.jesse_symbol, got.enabled) == ("FOO-USD", False)
    with pytest.raises(ValueError):
        await update_asset(sf, asset.id, jesse_symbol="bad", enabled=True)
    await delete_asset(sf, asset.id)
    assert await get_asset(sf, asset.id) is None
    assert await StatsCache(None).get(sf) == {}


async def test_list_assets_is_sorted(sf):
    await make_asset(sf, provider_symbol="B", jesse_symbol="B-USD")
    await make_asset(sf, provider_symbol="A", jesse_symbol="A-USD")
    assert [a.jesse_symbol for a in await list_assets(sf)] == ["A-USD", "B-USD"]


async def test_stats_are_cached_until_ttl_or_invalidation(sf, clock):
    asset = await make_asset(sf)
    await seed(sf, asset.id, [0, 1, 2])
    cache = StatsCache(clock, ttl_seconds=60)
    assert (await cache.get(sf))[asset.id] == AssetStats(T0, T0 + timedelta(minutes=2), 3)
    await seed(sf, asset.id, [3])
    assert (await cache.get(sf))[asset.id].count == 3
    clock.advance(61)
    assert (await cache.get(sf))[asset.id].count == 4
    await seed(sf, asset.id, [4])
    cache.invalidate()
    assert (await cache.get(sf))[asset.id].count == 5
```

`tests/test_progress.py`:
```python
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.progress import estimate_text, format_duration, job_progress
from tests.fakes import FakeProvider

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def job(**overrides):
    values = {
        "status": "running",
        "range_start": T0,
        "range_end": T0 + timedelta(minutes=100),
        "requests_made": 0,
        "run_seconds": 0.0,
    } | overrides
    return SimpleNamespace(**values)


def test_progress_and_eta_use_policy_rate_at_start():
    p = job_progress(job(), T0 + timedelta(minutes=50), FakeProvider())
    assert p.percent == 50.0
    assert p.eta_seconds == pytest.approx(0.5)  # 5 chunks left at 10 requests/s


def test_eta_uses_measured_rate_after_20_requests():
    p = job_progress(job(requests_made=40, run_seconds=80.0), T0 + timedelta(minutes=50), FakeProvider())
    assert p.eta_seconds == pytest.approx(10.0)  # 5 chunks left at 0.5 requests/s


def test_finished_jobs_have_full_progress_and_no_eta():
    p = job_progress(job(status="done"), None, FakeProvider())
    assert (p.percent, p.eta_seconds) == (100.0, None)


def test_cursor_before_range_counts_as_zero():
    assert job_progress(job(), None, FakeProvider()).percent == 0.0


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(30, "< 1 min"), (120, "2 min"), (3600, "1 h"), (7800, "2 h 10 min"), (100_000, "1 d 3 h"), (172_800, "2 d")],
)
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


def test_estimate_text():
    assert estimate_text(FakeProvider(), T0, T0 + timedelta(minutes=100)) == "≈ 10 requests · < 1 min"
    assert estimate_text(FakeProvider(), T0, T0 + timedelta(days=7)) == "≈ 1,008 requests · 2 min"
    assert estimate_text(FakeProvider(), T0, T0) == "Already up to date."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_assets.py tests/test_progress.py -v`
Expected: `ModuleNotFoundError: No module named 'app.services.assets'`.

- [ ] **Step 3: Implement**

`app/services/assets.py`:
```python
"""Asset CRUD and cached per-asset candle statistics."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.clock import Clock
from app.db import SessionFactory
from app.models import Asset, CandleRow

JESSE_SYMBOL = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")


class DuplicateAsset(ValueError):
    pass


def _check_jesse_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not JESSE_SYMBOL.match(symbol):
        raise ValueError(f"Jesse symbol must look like BASE-QUOTE, e.g. BTC-USDT (got {symbol!r})")
    return symbol


async def create_asset(
    sf: SessionFactory, *, provider: str, provider_symbol: str, asset_class: str, jesse_symbol: str, start_date: date
) -> Asset:
    asset = Asset(
        provider=provider,
        provider_symbol=provider_symbol,
        asset_class=asset_class,
        jesse_symbol=_check_jesse_symbol(jesse_symbol),
        start_date=datetime.combine(start_date, time(), UTC),
    )
    try:
        async with sf.begin() as s:
            s.add(asset)
    except IntegrityError:
        raise DuplicateAsset(f"{provider_symbol} from {provider} already exists") from None
    return asset


async def list_assets(sf: SessionFactory) -> list[Asset]:
    async with sf() as s:
        return list(await s.scalars(select(Asset).order_by(Asset.jesse_symbol, Asset.id)))


async def get_asset(sf: SessionFactory, asset_id: int) -> Asset | None:
    async with sf() as s:
        return await s.get(Asset, asset_id)


async def update_asset(sf: SessionFactory, asset_id: int, *, jesse_symbol: str, enabled: bool) -> None:
    symbol = _check_jesse_symbol(jesse_symbol)
    async with sf.begin() as s:
        await s.execute(update(Asset).where(Asset.id == asset_id).values(jesse_symbol=symbol, enabled=enabled))


async def delete_asset(sf: SessionFactory, asset_id: int) -> None:
    async with sf.begin() as s:
        await s.execute(delete(CandleRow).where(CandleRow.asset_id == asset_id))
        await s.execute(delete(Asset).where(Asset.id == asset_id))  # jobs cascade


@dataclass(frozen=True)
class AssetStats:
    first: datetime | None
    last: datetime | None
    count: int


EMPTY_STATS = AssetStats(None, None, 0)


class StatsCache:
    """Counting candles scans the whole table, so results are reused for `ttl_seconds`."""

    def __init__(self, clock: Clock | None, ttl_seconds: float = 120.0):
        self._clock = clock
        self._ttl = ttl_seconds
        self._value: dict[int, AssetStats] | None = None
        self._loaded_at = 0.0

    def invalidate(self) -> None:
        self._value = None

    async def get(self, sf: SessionFactory) -> dict[int, AssetStats]:
        now = self._clock.monotonic() if self._clock else 0.0
        if self._value is None or self._clock is None or now - self._loaded_at > self._ttl:
            async with sf() as s:
                rows = await s.execute(
                    select(CandleRow.asset_id, func.min(CandleRow.ts), func.max(CandleRow.ts), func.count()).group_by(
                        CandleRow.asset_id
                    )
                )
                self._value = {asset_id: AssetStats(first, last, count) for asset_id, first, last, count in rows}
            self._loaded_at = now
        return self._value
```

`app/services/progress.py`:
```python
"""Progress percentage, ETA and human-readable durations for the UI."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.models import ACTIVE_STATUSES
from app.providers.base import Provider

MIN_REQUESTS_FOR_MEASURED_RATE = 20


@dataclass(frozen=True)
class JobProgress:
    percent: float
    eta_seconds: float | None


def job_progress(job, fetched_until: datetime | None, provider: Provider) -> JobProgress:
    if job.status == "done":
        return JobProgress(100.0, None)
    span = (job.range_end - job.range_start).total_seconds()
    cursor = max(fetched_until or job.range_start, job.range_start)
    percent = 100.0 if span <= 0 else min(100.0, (cursor - job.range_start).total_seconds() / span * 100)
    eta = None
    if job.status in ACTIVE_STATUSES:
        remaining = provider.estimate_requests(cursor, job.range_end)
        if job.requests_made >= MIN_REQUESTS_FOR_MEASURED_RATE and job.run_seconds > 0:
            rate = job.requests_made / job.run_seconds
        else:
            rate = provider.client.policy.rate
        eta = remaining / rate
    return JobProgress(round(percent, 1), eta)


def format_duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "< 1 min"
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def estimate_text(provider: Provider, start: datetime, end: datetime) -> str:
    requests = provider.estimate_requests(start, end)
    if requests <= 0:
        return "Already up to date."
    return f"≈ {requests:,} requests · {format_duration(requests / provider.client.policy.rate)}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_assets.py tests/test_progress.py -v`
Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/assets.py app/services/progress.py tests/test_assets.py tests/test_progress.py
git commit -m "feat: asset service, stats cache and progress helpers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Scheduler

**Files:**
- Create: `app/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `jobs.enqueue_all` (Task 8), `AppSettings` (Task 7).
- Produces: `UpdateScheduler(sf, registry, clock)` with `start()`, `shutdown()`, `apply(settings: AppSettings)`, `async run_now() -> int`, `next_run() -> datetime | None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_scheduler.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_scheduler.py -v`
Expected: `ModuleNotFoundError: No module named 'app.scheduler'`.

- [ ] **Step 3: Implement**

`app/scheduler.py`:
```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_scheduler.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/scheduler.py tests/test_scheduler.py
git commit -m "feat: cron scheduler for update-all

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Web app: app factory, assets page, add-asset flow

**Files:**
- Create: `app/state.py`, `app/main.py`, `app/web/__init__.py` (empty), `app/web/routes.py`
- Create templates: `app/web/templates/base.html`, `_macros.html`, `assets.html`, `_asset_rows.html`, `asset_new.html`, `_search_results.html`, `_asset_details.html`
- Modify: `tests/conftest.py` (append the `client` fixture)
- Test: `tests/test_web_assets.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `app.state.Services` dataclass (`env, sf, clock, registry, settings, stats, worker=None, scheduler=None`). `app.main.create_app(env=None, *, sf=None, clock=None, registry=None, start_background=True) -> FastAPI` and `build_registry(http, clock, settings, env)`. `app.web.routes.router`, `templates`, and helper `services(request) -> Services`.
- Routes: `GET /`, `GET /assets/rows`, `GET /assets/new`, `GET /assets/search?search_provider=&q=`, `GET /assets/new/details?provider=&symbol=&asset_class=&jesse_symbol=`, `GET /assets/estimate?provider=&start_date=`, `POST /assets`, `POST /assets/{id}/update`, `POST /assets/update-all`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/conftest.py`:
```python
import httpx

from app.config import EnvConfig
from app.main import create_app
from app.providers.base import ProviderRegistry
from tests.fakes import FakeProvider


@pytest.fixture
async def client(sf, clock):
    env = EnvConfig(
        _env_file=None, database_url="postgresql+asyncpg://unused@localhost/unused", alpaca_key_id="", alpaca_secret_key=""
    )
    app = create_app(env, sf=sf, clock=clock, registry=ProviderRegistry([FakeProvider(clock)]), start_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
```

`tests/test_web_assets.py`:
```python
from datetime import timedelta

from app.services import jobs
from tests.fakes import make_asset

FORM = {
    "provider": "fake",
    "provider_symbol": "FAKEUSD",
    "asset_class": "crypto",
    "jesse_symbol": "FAKE-USD",
    "start_date": "2024-01-01",
}


async def test_empty_assets_page(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "No assets yet" in r.text


async def test_search_details_and_estimate(client):
    r = await client.get("/assets/search", params={"search_provider": "fake", "q": "fake"})
    assert "FAKEUSD" in r.text
    r = await client.get(
        "/assets/new/details",
        params={"provider": "fake", "symbol": "FAKEUSD", "asset_class": "crypto", "jesse_symbol": "FAKE-USD"},
    )
    assert 'value="2024-01-01"' in r.text
    assert "≈ 12 requests" in r.text
    r = await client.get("/assets/estimate", params={"provider": "fake", "start_date": "2024-01-01"})
    assert r.text.startswith("≈ 12 requests")


async def test_adding_an_asset_queues_a_backfill(client, sf):
    r = await client.post("/assets", data=FORM)
    assert r.status_code == 303
    rows = (await client.get("/assets/rows")).text
    assert "FAKE-USD" in rows and "queued" in rows
    [job] = (await jobs.latest_jobs_by_asset(sf)).values()
    assert job.kind == "backfill"


async def test_duplicate_and_invalid_assets_are_rejected(client):
    await client.post("/assets", data=FORM)
    r = await client.post("/assets", data=FORM)
    assert r.status_code == 400 and "already exists" in r.text
    r = await client.post("/assets", data=FORM | {"provider_symbol": "OTHER", "jesse_symbol": "bad symbol"})
    assert r.status_code == 400 and "Jesse symbol" in r.text


async def test_update_buttons_queue_update_jobs(client, sf, clock):
    asset = await make_asset(sf, fetched_until=clock.now() - timedelta(minutes=30))
    await make_asset(
        sf, provider_symbol="OFF", jesse_symbol="OFF-USD", enabled=False, fetched_until=clock.now() - timedelta(minutes=30)
    )
    assert (await client.post(f"/assets/{asset.id}/update")).status_code == 303
    assert (await client.post("/assets/update-all")).status_code == 303
    latest = await jobs.latest_jobs_by_asset(sf)
    assert list(latest) == [asset.id]
    assert latest[asset.id].kind == "update"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_assets.py -v`
Expected: `ModuleNotFoundError: No module named 'app.main'`.

- [ ] **Step 3: Implement the app factory**

`app/state.py`:
```python
from __future__ import annotations

from dataclasses import dataclass

from app.clock import Clock
from app.config import EnvConfig
from app.db import SessionFactory
from app.providers.base import ProviderRegistry
from app.scheduler import UpdateScheduler
from app.services.assets import StatsCache
from app.services.settings import SettingsService
from app.worker import Worker


@dataclass
class Services:
    env: EnvConfig
    sf: SessionFactory
    clock: Clock
    registry: ProviderRegistry
    settings: SettingsService
    stats: StatsCache
    worker: Worker | None = None
    scheduler: UpdateScheduler | None = None
```

`app/main.py`:
```python
"""Application factory. Run with: uvicorn app.main:create_app --factory"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.clock import Clock
from app.config import EnvConfig
from app.db import SessionFactory, make_engine, make_session_factory
from app.providers import alpaca, binance, dukascopy
from app.providers.base import ProviderRegistry
from app.providers.http import ProviderClient
from app.scheduler import UpdateScheduler
from app.services import jobs
from app.services.assets import StatsCache
from app.services.settings import SettingsService
from app.state import Services
from app.web.routes import router
from app.worker import Worker

log = logging.getLogger(__name__)


def build_registry(http: httpx.AsyncClient, clock: Clock, settings: SettingsService, env: EnvConfig) -> ProviderRegistry:
    return ProviderRegistry(
        [
            binance.BinanceProvider(ProviderClient(binance.POLICY, http, clock)),
            alpaca.AlpacaProvider(
                ProviderClient(alpaca.POLICY, http, clock), settings.alpaca_credentials, env.alpaca_trading_url
            ),
            dukascopy.DukascopyProvider(ProviderClient(dukascopy.POLICY, http, clock)),
        ]
    )


def create_app(
    env: EnvConfig | None = None,
    *,
    sf: SessionFactory | None = None,
    clock: Clock | None = None,
    registry: ProviderRegistry | None = None,
    start_background: bool = True,
) -> FastAPI:
    env = env or EnvConfig()
    clock = clock or Clock()
    engine = None
    if sf is None:
        engine = make_engine(env.database_url)
        sf = make_session_factory(engine)
    http = None
    settings = SettingsService(sf, env)
    if registry is None:
        http = httpx.AsyncClient(headers={"User-Agent": "asuras-csv/0.1"}, follow_redirects=True)
        registry = build_registry(http, clock, settings, env)
    services = Services(env=env, sf=sf, clock=clock, registry=registry, settings=settings, stats=StatsCache(clock))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        if start_background:
            recovered = await jobs.recover(sf)
            if recovered:
                log.info("re-queued %d interrupted jobs", recovered)
            current = await settings.load()
            services.worker = Worker(sf, registry, clock, current.worker_concurrency)
            services.worker.start()
            services.scheduler = UpdateScheduler(sf, registry, clock)
            services.scheduler.apply(current)
            services.scheduler.start()
        yield
        if services.scheduler:
            services.scheduler.shutdown()
        if services.worker:
            await services.worker.stop()
        if http:
            await http.aclose()
        if engine:
            await engine.dispose()

    app = FastAPI(title="OHLCV Downloader", lifespan=lifespan)
    app.state.services = services
    app.include_router(router)
    return app
```

- [ ] **Step 4: Implement the routes**

`app/web/__init__.py`: empty.

`app/web/routes.py`:
```python
"""HTML routes (Jinja2 + HTMX)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.domain import ProviderError
from app.models import Asset, Job
from app.services import assets as asset_service
from app.services import jobs
from app.services.assets import EMPTY_STATS, AssetStats
from app.services.progress import JobProgress, estimate_text, format_duration, job_progress

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["dt"] = lambda value: value.strftime("%Y-%m-%d %H:%M") if value else "—"
templates.env.filters["duration"] = format_duration


def services(request: Request):
    return request.app.state.services


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time(), UTC)


@dataclass(frozen=True)
class AssetRow:
    asset: Asset
    provider_label: str
    stats: AssetStats
    job: Job | None
    progress: JobProgress | None


async def _asset_rows(svc) -> list[AssetRow]:
    all_assets = await asset_service.list_assets(svc.sf)
    stats = await svc.stats.get(svc.sf)
    latest = await jobs.latest_jobs_by_asset(svc.sf)
    rows = []
    for asset in all_assets:
        provider = svc.registry.find(asset.provider)
        job = latest.get(asset.id)
        progress = job_progress(job, asset.fetched_until, provider) if job and provider else None
        label = provider.label if provider else asset.provider
        rows.append(AssetRow(asset, label, stats.get(asset.id, EMPTY_STATS), job, progress))
    return rows


@router.get("/", response_class=HTMLResponse)
async def assets_page(request: Request):
    return templates.TemplateResponse(request, "assets.html", {"rows": await _asset_rows(services(request))})


@router.get("/assets/rows", response_class=HTMLResponse)
async def asset_rows(request: Request):
    return templates.TemplateResponse(request, "_asset_rows.html", {"rows": await _asset_rows(services(request))})


def _new_page(request: Request, error: str | None = None, status_code: int = 200):
    context = {"providers": services(request).registry.all(), "error": error}
    return templates.TemplateResponse(request, "asset_new.html", context, status_code=status_code)


@router.get("/assets/new", response_class=HTMLResponse)
async def new_asset(request: Request):
    return _new_page(request)


@router.get("/assets/search", response_class=HTMLResponse)
async def search(request: Request, search_provider: str, q: str = ""):
    symbols, error = [], None
    if q.strip():
        try:
            symbols = await services(request).registry.get(search_provider).search_symbols(q)
        except ProviderError as exc:
            error = str(exc)
    context = {"symbols": symbols, "error": error, "q": q, "provider": search_provider}
    return templates.TemplateResponse(request, "_search_results.html", context)


@router.get("/assets/new/details", response_class=HTMLResponse)
async def new_asset_details(request: Request, provider: str, symbol: str, asset_class: str, jesse_symbol: str):
    svc = services(request)
    context = {"provider": provider, "symbol": symbol, "asset_class": asset_class, "jesse_symbol": jesse_symbol, "error": None}
    try:
        p = svc.registry.get(provider)
        earliest = (await p.earliest_available(symbol)).date()
        context |= {
            "asset_classes": p.asset_classes,
            "earliest": earliest.isoformat(),
            "estimate": estimate_text(p, _start_of(earliest), p.available_until(svc.clock.now())),
        }
    except ProviderError as exc:
        context["error"] = str(exc)
    return templates.TemplateResponse(request, "_asset_details.html", context)


@router.get("/assets/estimate", response_class=HTMLResponse)
async def estimate(request: Request, provider: str, start_date: date):
    svc = services(request)
    p = svc.registry.get(provider)
    return HTMLResponse(estimate_text(p, _start_of(start_date), p.available_until(svc.clock.now())))


@router.post("/assets")
async def create(
    request: Request,
    provider: Annotated[str, Form()],
    provider_symbol: Annotated[str, Form()],
    asset_class: Annotated[str, Form()],
    jesse_symbol: Annotated[str, Form()],
    start_date: Annotated[date, Form()],
):
    svc = services(request)
    p = svc.registry.find(provider)
    if p is None or asset_class not in p.asset_classes:
        return _new_page(request, "Unknown provider or asset class.", 400)
    try:
        asset = await asset_service.create_asset(
            svc.sf,
            provider=provider,
            provider_symbol=provider_symbol,
            asset_class=asset_class,
            jesse_symbol=jesse_symbol,
            start_date=start_date,
        )
    except ValueError as exc:
        return _new_page(request, str(exc), 400)
    await jobs.enqueue(svc.sf, svc.registry, svc.clock, asset.id, "backfill")
    svc.stats.invalidate()
    return redirect("/")


@router.post("/assets/update-all")
async def update_all(request: Request):
    svc = services(request)
    await jobs.enqueue_all(svc.sf, svc.registry, svc.clock)
    return redirect("/")


@router.post("/assets/{asset_id}/update")
async def update_one(request: Request, asset_id: int):
    svc = services(request)
    await jobs.enqueue(svc.sf, svc.registry, svc.clock, asset_id, "update")
    return redirect("/")
```

- [ ] **Step 5: Write the templates**

`app/web/templates/base.html`:
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}OHLCV Downloader{% endblock %}</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
  <script src="https://unpkg.com/htmx.org@2.0.4"></script>
  <style>
    table td, table th { white-space: nowrap; font-size: .9rem; vertical-align: top; }
    .error { color: var(--pico-del-color); }
    .muted { color: var(--pico-muted-color); font-size: .85rem; }
    .actions form { display: inline; }
    .actions button, .actions a[role=button] { padding: .2rem .5rem; font-size: .8rem; margin: 0 .1rem 0 0; }
    .toolbar { display: flex; gap: .5rem; margin-bottom: 1rem; }
    .toolbar form { margin: 0; }
    progress { margin: 0; width: 8rem; }
  </style>
</head>
<body>
<main class="container-fluid">
  <nav>
    <ul><li><strong>OHLCV Downloader</strong></li></ul>
    <ul>
      <li><a href="/">Assets</a></li>
      <li><a href="/jobs">Jobs</a></li>
      <li><a href="/settings">Settings</a></li>
    </ul>
  </nav>
  {% block content %}{% endblock %}
</main>
</body>
</html>
```

`app/web/templates/_macros.html`:
```html
{% macro job_status(job, progress) %}
{% if job is none %}<span class="muted">idle</span>
{% else %}
  <strong>{{ job.status }}</strong>
  {% if progress and job.status in ('queued', 'running', 'waiting', 'paused') %}
    <br><progress value="{{ progress.percent }}" max="100"></progress> {{ progress.percent }}%
    {% if progress.eta_seconds is not none %}· ~{{ progress.eta_seconds|duration }} left{% endif %}
  {% endif %}
  {% if job.status_detail %}<br><span class="muted">{{ job.status_detail }}</span>{% endif %}
  {% if job.status == 'failed' and job.error %}<br><span class="error">{{ job.error }}</span>{% endif %}
{% endif %}
{% endmacro %}
```

`app/web/templates/assets.html`:
```html
{% extends "base.html" %}
{% block content %}
<h2>Assets</h2>
<div class="toolbar">
  <a href="/assets/new" role="button">Add asset</a>
  <form method="post" action="/assets/update-all"><button class="secondary">Update all</button></form>
</div>
<div class="overflow-auto">
<table class="striped">
  <thead>
    <tr>
      <th>Jesse symbol</th><th>Provider</th><th>Class</th><th>First candle</th><th>Last candle</th>
      <th>Candles</th><th>Fetched until</th><th>Status</th><th>Actions</th>
    </tr>
  </thead>
  <tbody hx-get="/assets/rows" hx-trigger="every 2s" hx-swap="innerHTML">
    {% include "_asset_rows.html" %}
  </tbody>
</table>
</div>
<p class="muted">Times are UTC. Candle counts refresh every couple of minutes.</p>
{% endblock %}
```

`app/web/templates/_asset_rows.html`:
```html
{% from "_macros.html" import job_status %}
{% for row in rows %}
<tr>
  <td>{{ row.asset.jesse_symbol }}{% if not row.asset.enabled %} <span class="muted">(disabled)</span>{% endif %}</td>
  <td>{{ row.provider_label }}<br><span class="muted">{{ row.asset.provider_symbol }}</span></td>
  <td>{{ row.asset.asset_class }}</td>
  <td>{{ row.stats.first|dt }}</td>
  <td>{{ row.stats.last|dt }}</td>
  <td>{{ "{:,}".format(row.stats.count) }}</td>
  <td>{{ row.asset.fetched_until|dt }}</td>
  <td>{{ job_status(row.job, row.progress) }}</td>
  <td class="actions">
    <form method="post" action="/assets/{{ row.asset.id }}/update"><button>Update</button></form>
    <a href="/assets/{{ row.asset.id }}/export" role="button" class="secondary">Export</a>
    <a href="/assets/{{ row.asset.id }}/edit" role="button" class="secondary outline">Edit</a>
    <a href="/assets/{{ row.asset.id }}/delete" role="button" class="contrast outline">Delete</a>
  </td>
</tr>
{% else %}
<tr><td colspan="9">No assets yet. <a href="/assets/new">Add one</a>.</td></tr>
{% endfor %}
```

`app/web/templates/asset_new.html`:
```html
{% extends "base.html" %}
{% block content %}
<h2>Add asset</h2>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post" action="/assets">
  <label>Provider
    <select name="search_provider" hx-get="/assets/search" hx-target="#results" hx-include="[name=q]" hx-trigger="change">
      {% for p in providers %}<option value="{{ p.name }}">{{ p.label }}</option>{% endfor %}
    </select>
  </label>
  <label>Search symbol
    <input type="search" name="q" placeholder="e.g. BTCUSDT, AAPL, EURUSD" autocomplete="off"
           hx-get="/assets/search" hx-trigger="input changed delay:300ms, search"
           hx-target="#results" hx-include="[name=search_provider]">
  </label>
  <div id="results"></div>
  <div id="details"></div>
</form>
{% endblock %}
```

`app/web/templates/_search_results.html`:
```html
{% if error %}<p class="error">{{ error }}</p>
{% elif symbols %}
<ul>
  {% for s in symbols %}
  <li>
    <a href="#" hx-get="/assets/new/details" hx-target="#details"
       hx-vals='{{ {"provider": provider, "symbol": s.provider_symbol, "asset_class": s.asset_class, "jesse_symbol": s.suggested_jesse_symbol}|tojson }}'>{{ s.provider_symbol }}</a>
    <span class="muted">{{ s.name }} · {{ s.asset_class }}</span>
  </li>
  {% endfor %}
</ul>
{% elif q %}<p class="muted">No matches.</p>
{% endif %}
```

`app/web/templates/_asset_details.html`:
```html
{% if error %}<p class="error">{{ error }}</p>
{% else %}
<h3>{{ symbol }}</h3>
<input type="hidden" name="provider" value="{{ provider }}">
<input type="hidden" name="provider_symbol" value="{{ symbol }}">
<label>Asset class
  <select name="asset_class">
    {% for c in asset_classes %}<option value="{{ c }}" {% if c == asset_class %}selected{% endif %}>{{ c }}</option>{% endfor %}
  </select>
</label>
<label>Jesse symbol
  <input name="jesse_symbol" value="{{ jesse_symbol }}" required pattern="[A-Z0-9]+-[A-Z0-9]+">
  <small>Use this symbol with exchange "Custom Data" when importing into Jesse.</small>
</label>
<label>Start date
  <input type="date" name="start_date" value="{{ earliest }}" min="{{ earliest }}" required
         hx-get="/assets/estimate" hx-trigger="change" hx-target="#estimate" hx-include="[name=provider]">
</label>
<p id="estimate" class="muted">{{ estimate }}</p>
<button type="submit">Add and start download</button>
{% endif %}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_assets.py -v`
Expected: 5 passed.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add app/state.py app/main.py app/web tests/conftest.py tests/test_web_assets.py
git commit -m "feat: web app with assets page and add-asset flow

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: Web pages: edit, delete, export, jobs, settings

**Files:**
- Modify: `app/web/routes.py` (append the routes below and add the imports)
- Create templates: `app/web/templates/asset_edit.html`, `asset_delete.html`, `export.html`, `jobs.html`, `settings.html`
- Test: `tests/test_web_pages.py`

**Interfaces:**
- Consumes: Task 13's `router`, `templates`, `services()`, `redirect()`, plus the export, assets, jobs and settings services.
- Routes: `GET/POST /assets/{id}/edit`, `GET/POST /assets/{id}/delete`, `GET /assets/{id}/export`, `GET /assets/{id}/export.csv?start=&end=`, `GET /jobs`, `POST /jobs/{id}/cancel` (form field `next`), `GET /settings`, `POST /settings`.

- [ ] **Step 1: Write the failing tests**

`tests/test_web_pages.py`:
```python
from datetime import UTC, datetime, timedelta

from app.domain import Candle
from app.providers.base import ProviderRegistry
from app.services import jobs
from app.services.assets import get_asset
from app.services.sync import insert_candles
from tests.fakes import FakeProvider, make_asset

T0 = datetime(2024, 1, 1, tzinfo=UTC)


async def test_edit_asset(client, sf):
    asset = await make_asset(sf)
    assert (await client.get(f"/assets/{asset.id}/edit")).status_code == 200
    r = await client.post(f"/assets/{asset.id}/edit", data={"jesse_symbol": "NEW-USD"})
    assert r.status_code == 303
    got = await get_asset(sf, asset.id)
    assert (got.jesse_symbol, got.enabled) == ("NEW-USD", False)
    r = await client.post(f"/assets/{asset.id}/edit", data={"jesse_symbol": "bad", "enabled": "on"})
    assert r.status_code == 400 and "Jesse symbol" in r.text
    assert (await client.get("/assets/999/edit")).status_code == 404


async def test_delete_asset(client, sf):
    asset = await make_asset(sf)
    r = await client.get(f"/assets/{asset.id}/delete")
    assert r.status_code == 200 and "FAKE-USD" in r.text
    assert (await client.post(f"/assets/{asset.id}/delete")).status_code == 303
    assert await get_asset(sf, asset.id) is None


async def test_export_downloads_jesse_csv(client, sf):
    asset = await make_asset(sf)
    async with sf.begin() as s:
        await insert_candles(s, asset.id, [Candle(T0 + timedelta(minutes=m), 1, 2, 0.5, 1.5, 10) for m in (0, 1)])
    page = await client.get(f"/assets/{asset.id}/export")
    assert 'value="2024-01-01"' in page.text
    r = await client.get(f"/assets/{asset.id}/export.csv", params={"start": "2024-01-01", "end": "2024-01-01"})
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="FAKE-USD_2024-01-01_2024-01-01.csv"' in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0] == "timestamp,open,close,high,low,volume" and len(lines) == 3
    assert (await client.get(f"/assets/{asset.id}/export.csv", params={"start": "2025-01-01"})).status_code == 404
    assert (await client.get(f"/assets/{asset.id}/export.csv", params={"start": "nope"})).status_code == 400


async def test_jobs_page_and_cancel(client, sf, clock):
    asset = await make_asset(sf)
    job = await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, asset.id, "backfill")
    r = await client.get("/jobs")
    assert "FAKE-USD" in r.text and "queued" in r.text and "Cancel" in r.text
    r = await client.post(f"/jobs/{job.id}/cancel", data={"next": "/jobs"})
    assert r.status_code == 303 and r.headers["location"] == "/jobs"
    assert (await jobs.get_job(sf, job.id)).status == "cancelled"


async def test_settings_roundtrip(client):
    assert (await client.get("/settings")).status_code == 200
    r = await client.post(
        "/settings",
        data={
            "schedule_enabled": "on",
            "schedule_cron": "0 */6 * * *",
            "worker_concurrency": "2",
            "alpaca_key_id": "KEYID1234",
            "alpaca_secret_key": "s3cr3t-value",
        },
    )
    assert r.status_code == 303
    page = (await client.get("/settings")).text
    assert "0 */6 * * *" in page and "1234" in page and "s3cr3t-value" not in page
    r = await client.post("/settings", data={"schedule_cron": "bad", "worker_concurrency": "2"})
    assert r.status_code == 400 and 'class="error"' in r.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_pages.py -v`
Expected: failures with 404/405 status codes (the routes don't exist yet).

- [ ] **Step 3: Implement the routes**

Add these imports at the top of `app/web/routes.py` (merge with the existing ones):
```python
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from app.models import ACTIVE_STATUSES
from app.services.export import day_bounds, export_filename, export_range, stream_csv
```

Append to `app/web/routes.py`:
```python
async def _asset_or_404(svc, asset_id: int) -> Asset:
    asset = await asset_service.get_asset(svc.sf, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    return asset


@router.get("/assets/{asset_id}/edit", response_class=HTMLResponse)
async def edit_page(request: Request, asset_id: int):
    asset = await _asset_or_404(services(request), asset_id)
    return templates.TemplateResponse(request, "asset_edit.html", {"asset": asset, "error": None})


@router.post("/assets/{asset_id}/edit")
async def edit(
    request: Request,
    asset_id: int,
    jesse_symbol: Annotated[str, Form()],
    enabled: Annotated[str | None, Form()] = None,
):
    svc = services(request)
    asset = await _asset_or_404(svc, asset_id)
    try:
        await asset_service.update_asset(svc.sf, asset_id, jesse_symbol=jesse_symbol, enabled=enabled is not None)
    except ValueError as exc:
        return templates.TemplateResponse(request, "asset_edit.html", {"asset": asset, "error": str(exc)}, status_code=400)
    return redirect("/")


@router.get("/assets/{asset_id}/delete", response_class=HTMLResponse)
async def delete_page(request: Request, asset_id: int):
    svc = services(request)
    asset = await _asset_or_404(svc, asset_id)
    count = (await svc.stats.get(svc.sf)).get(asset_id, EMPTY_STATS).count
    return templates.TemplateResponse(request, "asset_delete.html", {"asset": asset, "count": count})


@router.post("/assets/{asset_id}/delete")
async def delete(request: Request, asset_id: int):
    svc = services(request)
    await _asset_or_404(svc, asset_id)
    await asset_service.delete_asset(svc.sf, asset_id)
    svc.stats.invalidate()
    return redirect("/")


@router.get("/assets/{asset_id}/export", response_class=HTMLResponse)
async def export_page(request: Request, asset_id: int):
    svc = services(request)
    asset = await _asset_or_404(svc, asset_id)
    rng = await export_range(svc.sf, asset_id, None, None)
    return templates.TemplateResponse(request, "export.html", {"asset": asset, "rng": rng})


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"Invalid date: {value!r}") from None


@router.get("/assets/{asset_id}/export.csv")
async def export_csv(request: Request, asset_id: int, start: str | None = None, end: str | None = None):
    svc = services(request)
    asset = await _asset_or_404(svc, asset_id)
    start_dt, end_dt = day_bounds(_parse_day(start), _parse_day(end))
    rng = await export_range(svc.sf, asset_id, start_dt, end_dt)
    if rng is None:
        raise HTTPException(404, "No candles in this range")
    filename = export_filename(asset.jesse_symbol, rng)
    return StreamingResponse(
        stream_csv(svc.sf, asset_id, start_dt, end_dt),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@dataclass(frozen=True)
class JobRow:
    job: Job
    asset: Asset
    progress: JobProgress | None


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    svc = services(request)
    rows = []
    for job, asset in await jobs.list_recent(svc.sf):
        provider = svc.registry.find(asset.provider)
        progress = job_progress(job, asset.fetched_until, provider) if provider else None
        rows.append(JobRow(job, asset, progress))
    return templates.TemplateResponse(request, "jobs.html", {"rows": rows, "active_statuses": ACTIVE_STATUSES})


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: int, next: Annotated[str, Form()] = "/jobs"):
    svc = services(request)
    await jobs.cancel(svc.sf, svc.clock, job_id)
    return redirect(next if next.startswith("/") else "/jobs")


CRON_PRESETS = [
    ("0 * * * *", "hourly"),
    ("0 */6 * * *", "every 6 hours"),
    ("0 2 * * *", "daily at 02:00 UTC"),
]


def _key_hint(key_id: str) -> str:
    return f"•••• {key_id[-4:]}" if key_id else "not set"


async def _settings_page(request: Request, error: str | None = None, status_code: int = 200):
    svc = services(request)
    current = await svc.settings.load()
    context = {
        "s": current,
        "error": error,
        "saved": request.query_params.get("saved") == "1",
        "presets": CRON_PRESETS,
        "next_run": svc.scheduler.next_run() if svc.scheduler else None,
        "key_hint": _key_hint(current.alpaca_key_id),
    }
    return templates.TemplateResponse(request, "settings.html", context, status_code=status_code)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return await _settings_page(request)


@router.post("/settings")
async def save_settings(
    request: Request,
    schedule_cron: Annotated[str, Form()],
    worker_concurrency: Annotated[str, Form()],
    schedule_enabled: Annotated[str | None, Form()] = None,
    alpaca_key_id: Annotated[str, Form()] = "",
    alpaca_secret_key: Annotated[str, Form()] = "",
):
    svc = services(request)
    current = await svc.settings.load()
    values = {
        "schedule_enabled": "true" if schedule_enabled else "false",
        "schedule_cron": schedule_cron.strip(),
        "worker_concurrency": worker_concurrency.strip(),
    }
    if not current.alpaca_from_env:
        if alpaca_key_id.strip():
            values["alpaca_key_id"] = alpaca_key_id.strip()
        if alpaca_secret_key.strip():
            values["alpaca_secret_key"] = alpaca_secret_key.strip()
    try:
        await svc.settings.save(values)
    except ValueError as exc:
        return await _settings_page(request, str(exc), 400)
    updated = await svc.settings.load()
    if svc.worker:
        svc.worker.resize(updated.worker_concurrency)
    if svc.scheduler:
        svc.scheduler.apply(updated)
    return redirect("/settings?saved=1")
```

- [ ] **Step 4: Write the templates**

`app/web/templates/asset_edit.html`:
```html
{% extends "base.html" %}
{% block content %}
<h2>Edit {{ asset.jesse_symbol }}</h2>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post">
  <label>Jesse symbol <input name="jesse_symbol" value="{{ asset.jesse_symbol }}" required></label>
  <label><input type="checkbox" role="switch" name="enabled" {% if asset.enabled %}checked{% endif %}>
    Include in "Update all" and scheduled updates</label>
  <p class="muted">{{ asset.provider }} · {{ asset.provider_symbol }} · start date {{ asset.start_date|dt }} UTC (can't be changed)</p>
  <button type="submit">Save</button>
  <a href="/" role="button" class="secondary">Cancel</a>
</form>
{% endblock %}
```

`app/web/templates/asset_delete.html`:
```html
{% extends "base.html" %}
{% block content %}
<h2>Delete {{ asset.jesse_symbol }}?</h2>
<p>This removes the asset, its jobs and all {{ "{:,}".format(count) }} stored candles. This cannot be undone.</p>
<form method="post">
  <button type="submit" class="contrast">Delete</button>
  <a href="/" role="button" class="secondary">Cancel</a>
</form>
{% endblock %}
```

`app/web/templates/export.html`:
```html
{% extends "base.html" %}
{% block content %}
<h2>Export {{ asset.jesse_symbol }}</h2>
{% if rng is none %}
<p>No candles stored yet.</p>
{% else %}
<p class="muted">Stored: {{ rng.first|dt }} → {{ rng.last|dt }} UTC.
  In Jesse, import with exchange <strong>Custom Data</strong> and symbol <strong>{{ asset.jesse_symbol }}</strong>.</p>
<form method="get" action="/assets/{{ asset.id }}/export.csv">
  <div class="grid">
    <label>From <input type="date" name="start" value="{{ rng.first.date().isoformat() }}"></label>
    <label>To (inclusive) <input type="date" name="end" value="{{ rng.last.date().isoformat() }}"></label>
  </div>
  <button type="submit">Download CSV</button>
</form>
{% endif %}
<p><a href="/">Back to assets</a></p>
{% endblock %}
```

`app/web/templates/jobs.html`:
```html
{% extends "base.html" %}
{% from "_macros.html" import job_status %}
{% block content %}
<h2>Jobs</h2>
<div class="overflow-auto">
<table class="striped">
  <thead>
    <tr>
      <th>#</th><th>Asset</th><th>Kind</th><th>Status</th><th>Range (UTC)</th><th>Requests</th>
      <th>Candles added</th><th>Run time</th><th>Created</th><th>Finished</th><th></th>
    </tr>
  </thead>
  <tbody>
  {% for row in rows %}
    <tr>
      <td>{{ row.job.id }}</td>
      <td>{{ row.asset.jesse_symbol }}</td>
      <td>{{ row.job.kind }}</td>
      <td>{{ job_status(row.job, row.progress) }}</td>
      <td>{{ row.job.range_start|dt }} → {{ row.job.range_end|dt }}</td>
      <td>{{ "{:,}".format(row.job.requests_made) }}</td>
      <td>{{ "{:,}".format(row.job.candles_added) }}</td>
      <td>{{ row.job.run_seconds|duration }}</td>
      <td>{{ row.job.created_at|dt }}</td>
      <td>{{ row.job.finished_at|dt }}</td>
      <td class="actions">
        {% if row.job.status in active_statuses %}
        <form method="post" action="/jobs/{{ row.job.id }}/cancel">
          <input type="hidden" name="next" value="/jobs">
          <button class="secondary outline">Cancel</button>
        </form>
        {% endif %}
      </td>
    </tr>
  {% else %}
    <tr><td colspan="11">No jobs yet.</td></tr>
  {% endfor %}
  </tbody>
</table>
</div>
{% endblock %}
```

`app/web/templates/settings.html`:
```html
{% extends "base.html" %}
{% block content %}
<h2>Settings</h2>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
{% if saved %}<p><ins>Saved.</ins></p>{% endif %}
<form method="post">
  <fieldset>
    <legend><strong>Scheduled updates</strong></legend>
    <label><input type="checkbox" role="switch" name="schedule_enabled" {% if s.schedule_enabled %}checked{% endif %}>
      Update all enabled assets on a schedule</label>
    <label>Schedule (cron, UTC)
      <input name="schedule_cron" value="{{ s.schedule_cron }}" list="cron-presets" required>
      <datalist id="cron-presets">
        {% for value, label in presets %}<option value="{{ value }}">{{ label }}</option>{% endfor %}
      </datalist>
    </label>
    <small class="muted">
      Presets: {% for value, label in presets %}<code>{{ value }}</code> {{ label }}{% if not loop.last %} · {% endif %}{% endfor %}.
      {% if next_run %}Next run: {{ next_run|dt }} UTC.{% endif %}
    </small>
  </fieldset>
  <label>Parallel downloads (1–10)
    <input type="number" name="worker_concurrency" min="1" max="10" value="{{ s.worker_concurrency }}" required>
  </label>
  <fieldset>
    <legend><strong>Alpaca API key</strong> <span class="muted">(free account at alpaca.markets)</span></legend>
    {% if s.alpaca_from_env %}
    <p class="muted">Set through environment variables (ALPACA_KEY_ID / ALPACA_SECRET_KEY). Edit your .env file to change it.</p>
    {% else %}
    <p class="muted">Current key: {{ key_hint }}. Leave the fields blank to keep it.</p>
    <label>Key ID <input name="alpaca_key_id" autocomplete="off"></label>
    <label>Secret key <input type="password" name="alpaca_secret_key" autocomplete="off"></label>
    {% endif %}
  </fieldset>
  <button type="submit">Save</button>
</form>
{% endblock %}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_pages.py -v`
Expected: 5 passed.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/web tests/test_web_pages.py
git commit -m "feat: edit, delete, export, jobs and settings pages

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: Docker Compose deployment, README, end-to-end check

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `.env.example`, `.dockerignore`, `README.md`

**Interfaces:**
- Consumes: `app.main:create_app` (factory), Alembic config.
- Produces: `docker compose up -d --build` → UI at `http://localhost:8000`.

- [ ] **Step 1: Write the deployment files**

`Dockerfile`:
```dockerfile
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY migrations ./migrations
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000"]
```

`docker-compose.yml`:
```yaml
services:
  db:
    image: timescale/timescaledb:latest-pg16
    environment:
      POSTGRES_USER: ohlcv
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-ohlcv}
      POSTGRES_DB: ohlcv
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ohlcv -d ohlcv"]
      interval: 5s
      timeout: 5s
      retries: 20
    restart: unless-stopped

  app:
    build: .
    environment:
      DATABASE_URL: postgresql+asyncpg://ohlcv:${POSTGRES_PASSWORD:-ohlcv}@db:5432/ohlcv
      ALPACA_KEY_ID: ${ALPACA_KEY_ID:-}
      ALPACA_SECRET_KEY: ${ALPACA_SECRET_KEY:-}
      ALPACA_TRADING_URL: ${ALPACA_TRADING_URL:-https://paper-api.alpaca.markets}
    ports:
      - "${APP_PORT:-8000}:8000"
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped

volumes:
  pgdata:
```

`.env.example`:
```bash
# Copy to .env and adjust. Everything is optional.
POSTGRES_PASSWORD=ohlcv
APP_PORT=8000

# Alpaca (US stocks & ETFs). Free account: https://alpaca.markets
# You can also enter the key in the Settings page instead.
ALPACA_KEY_ID=
ALPACA_SECRET_KEY=
# Paper-account keys (the free default) use the paper trading API; live keys use https://api.alpaca.markets
ALPACA_TRADING_URL=https://paper-api.alpaca.markets
```

`.dockerignore`:
```
.venv
.git
.pytest_cache
**/__pycache__
tests
docs
.env
```

`README.md`:
````markdown
# OHLCV Downloader

Self-hosted tool that downloads 1-minute OHLCV candles into TimescaleDB and exports them as
[Jesse "Custom Data" CSVs](https://docs.jesse.trade/docs/traditional-markets/importing-data#custom-data-csv).

| Asset class | Provider | Key | Notes |
|---|---|---|---|
| Crypto | Binance | none | Full history. Binance blocks some regions (HTTP 451), e.g. US servers. |
| US stocks & ETFs | Alpaca | free | IEX feed from 2016, regular session only (09:30–16:00 ET, early closes respected). IEX volume is lower than consolidated volume. Prices are unadjusted. |
| Forex | Dukascopy | none | Bid prices; volume = tick count. Slowest source (~10–15 min per year per pair). |

## Run

```bash
cp .env.example .env   # optional
docker compose up -d --build
```

Open http://localhost:8000, click **Add asset**, search a symbol, choose a start date and save.
The download runs in the background. You can close the browser, and restarts resume where they stopped.
Enable scheduled updates under **Settings**.

## Export to Jesse

Click **Export** on an asset, choose a date range and download the CSV. In Jesse's import form use
exchange **Custom Data** and the asset's Jesse symbol (e.g. `BTC-USDT`, `AAPL-USD`, `EUR-USD`).

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest            # needs a running Docker daemon (testcontainers)
```

Adding a provider: implement the `Provider` protocol in `app/providers/<name>.py` and register it in
`app/main.py:build_registry`.
````

- [ ] **Step 2: Build and start**

Run: `docker compose up -d --build && sleep 15 && docker compose ps`
Expected: `db` is `healthy` and `app` is `running`.

- [ ] **Step 3: Check the app responds and migrations ran**

Run: `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/ && docker compose logs app | grep -i "alembic"`
Expected: `200`, and the logs show `Running upgrade  -> 0001, initial schema`.

- [ ] **Step 4: End-to-end download check (Binance, no key needed)**

Open http://localhost:8000/assets/new, pick "Binance (crypto)", search `BTCUSDT`, click it, set the start date to 3 days ago and submit.
Expected: the Assets page shows the `BTC-USDT` row going from `queued` to `running` to `done` within about a minute. (If the server is in a region Binance blocks, the job shows `failed` with the "not available from this server's region" message instead.) Then click Export → Download CSV and check that the first line is `timestamp,open,close,high,low,volume` and the following lines are ascending Unix-ms rows.

Then test that downloads resume after a restart. Add `ETHUSDT` with a start date one year back, run `docker compose restart app` while the job is running, and confirm the job returns to `running` and continues from its previous `Fetched until` value.

- [ ] **Step 5: Stop**

Run: `docker compose down`
Expected: containers removed. The `pgdata` volume is kept.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile docker-compose.yml .env.example .dockerignore README.md
git commit -m "feat: Docker Compose deployment and README

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Docker Compose with TimescaleDB + app, migrations on startup | 2, 15 |
| assets / candles hypertable + compression / jobs / settings tables | 2 |
| Provider interface, registry, pluggable | 4 |
| Binance, Alpaca (regular session, calendar), Dukascopy (bi5, bid, tick volume) | 4, 5, 6 |
| Small requests, per-provider pacing, quota headers | 3, 4, 5, 6 |
| Throttling = waiting (all callers), not failing | 3, 9 |
| Transient retries + pause backoff + 24 h give-up; permanent fail fast | 3, 8, 9 |
| Cursor (`fetched_until`) committed with candles; empty ranges advance | 9 |
| Restart recovery | 8, 13 |
| Slicing + update priority | 8, 9 |
| Progress, ETA, add-form estimate | 11, 13 |
| One active job per asset; cancel keeps data | 2, 8, 14 |
| Scheduler with presets / custom cron | 12, 14 |
| Jesse CSV export (streaming, validation, filename, inclusive dates) | 10, 14 |
| GUI pages: assets, add, edit, export, delete, jobs, settings | 13, 14 |
| Alpaca keys via env override Settings | 7, 14 |

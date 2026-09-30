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

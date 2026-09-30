"""User settings stored in the database; Alpaca and OANDA credentials from the environment take precedence."""
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
    "oanda_api_token": "",
    "oanda_environment": "practice",
}


@dataclass(frozen=True)
class AppSettings:
    schedule_enabled: bool
    schedule_cron: str
    worker_concurrency: int
    alpaca_key_id: str
    alpaca_secret_key: str
    alpaca_from_env: bool
    oanda_api_token: str = ""
    oanda_environment: str = "practice"
    oanda_from_env: bool = False
    oanda_environment_from_env: bool = False


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
    if "oanda_environment" in values and values["oanda_environment"] not in ("practice", "live"):
        raise ValueError("oanda_environment must be 'practice' or 'live'")
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
            stored = {row[0]: row[1] for row in (await s.execute(select(Setting.key, Setting.value))).all()}
        v = DEFAULTS | stored
        from_env = bool(self._env.alpaca_key_id and self._env.alpaca_secret_key)
        oanda_from_env = bool(self._env.oanda_api_token)
        env_environment = self._env.oanda_environment.strip().lower()
        return AppSettings(
            schedule_enabled=v["schedule_enabled"] == "true",
            schedule_cron=v["schedule_cron"],
            worker_concurrency=int(v["worker_concurrency"]),
            alpaca_key_id=self._env.alpaca_key_id if from_env else v["alpaca_key_id"],
            alpaca_secret_key=self._env.alpaca_secret_key if from_env else v["alpaca_secret_key"],
            alpaca_from_env=from_env,
            oanda_api_token=self._env.oanda_api_token if oanda_from_env else v["oanda_api_token"],
            oanda_environment=env_environment or v["oanda_environment"],  # an invalid env value is reported by the provider
            oanda_from_env=oanda_from_env,
            oanda_environment_from_env=bool(env_environment),
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

    async def oanda_credentials(self) -> tuple[str, str]:
        s = await self.load()
        return s.oanda_api_token, s.oanda_environment

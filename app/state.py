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

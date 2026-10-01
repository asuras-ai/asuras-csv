"""Application factory. Run with: uvicorn app.main:create_app --factory"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.clock import Clock
from app.config import EnvConfig
from app.db import SessionFactory, make_engine, make_session_factory
from app.providers import alpaca, binance, twelvedata
from app.providers.base import ProviderRegistry
from app.providers.http import ProviderClient
from app.scheduler import UpdateScheduler
from app.services import jobs
from app.services.assets import StatsCache
from app.services.settings import SettingsService
from app.state import Services
from app.web.routes import router
from app.web.security import CrossSiteGuard
from app.worker import Worker

log = logging.getLogger(__name__)


def build_registry(http: httpx.AsyncClient, clock: Clock, settings: SettingsService, env: EnvConfig) -> ProviderRegistry:
    return ProviderRegistry(
        [
            binance.BinanceProvider(ProviderClient(binance.POLICY, http, clock)),
            alpaca.AlpacaProvider(
                ProviderClient(alpaca.POLICY, http, clock), settings.alpaca_credentials, env.alpaca_trading_url
            ),
            twelvedata.TwelveDataProvider(
                ProviderClient(twelvedata.POLICY, http, clock),
                settings.twelvedata_credentials,
                search_client=ProviderClient(twelvedata.SEARCH_POLICY, http, clock),
            ),
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
        logging.getLogger("httpx").setLevel(logging.WARNING)
        try:
            if start_background:
                recovered = await jobs.recover(sf)
                if recovered:
                    log.info("re-queued %d interrupted jobs", recovered)
                current = await settings.load()
                services.worker = Worker(
                    sf, registry, clock, current.worker_concurrency, on_progress=services.stats.invalidate
                )
                services.worker.start()
                services.scheduler = UpdateScheduler(sf, registry, clock)
                services.scheduler.apply(current)
                services.scheduler.start()
            yield
        finally:
            if services.scheduler is not None:
                services.scheduler.shutdown()
            if services.worker is not None:
                await services.worker.stop()
            if http is not None:
                await http.aclose()
            if engine is not None:
                await engine.dispose()

    app = FastAPI(title="OHLCV Downloader", lifespan=lifespan)
    app.state.services = services
    app.add_middleware(CrossSiteGuard)
    app.include_router(router)
    return app

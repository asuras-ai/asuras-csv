import logging

from app.config import EnvConfig
from app.main import create_app
from app.providers.base import ProviderRegistry
from tests.fakes import FakeProvider


async def test_startup_quiets_httpx_request_logs(sf, clock):
    httpx_log = logging.getLogger("httpx")
    httpx_log.setLevel(logging.NOTSET)
    env = EnvConfig(_env_file=None, database_url="postgresql+asyncpg://u@h/d", alpaca_key_id="", alpaca_secret_key="")
    app = create_app(env, sf=sf, clock=clock, registry=ProviderRegistry([FakeProvider(clock)]), start_background=False)
    async with app.router.lifespan_context(app):
        assert httpx_log.level == logging.WARNING

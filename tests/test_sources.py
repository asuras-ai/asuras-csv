from types import SimpleNamespace

from app.providers.base import ProviderRegistry
from app.services.settings import AppSettings
from app.services.sources import source_statuses

REGISTRY = ProviderRegistry(
    [
        SimpleNamespace(name="binance", label="Binance (crypto)"),
        SimpleNamespace(name="alpaca", label="Alpaca (US stocks & ETFs)"),
        SimpleNamespace(name="twelvedata", label="Twelve Data (US stocks, forex, metals)"),
    ]
)


def settings(**overrides) -> AppSettings:
    values = dict(
        schedule_enabled=False, schedule_cron="0 * * * *", worker_concurrency=3,
        alpaca_key_id="", alpaca_secret_key="", alpaca_from_env=False, twelvedata_api_key="",
    )
    return AppSettings(**(values | overrides))


def test_sources_without_keys():
    got = [(s.name, s.label, s.coverage, s.needs_key, s.ready) for s in source_statuses(REGISTRY, settings())]
    assert got == [
        ("binance", "Binance", "crypto", False, True),
        ("alpaca", "Alpaca", "US stocks & ETFs", True, False),
        ("twelvedata", "Twelve Data", "US stocks, forex, metals", True, False),
    ]


def test_keys_make_sources_ready():
    assert not source_statuses(REGISTRY, settings(alpaca_key_id="K"))[1].ready  # both parts needed
    assert source_statuses(REGISTRY, settings(alpaca_key_id="K", alpaca_secret_key="S"))[1].ready
    assert source_statuses(REGISTRY, settings(twelvedata_api_key="T"))[2].ready

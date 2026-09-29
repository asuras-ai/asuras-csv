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

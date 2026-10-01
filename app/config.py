from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvConfig(BaseSettings):
    """Configuration from environment variables (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://ohlcv:ohlcv@db:5432/ohlcv"
    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    alpaca_trading_url: str = "https://paper-api.alpaca.markets"
    twelvedata_api_key: str = ""

# OHLCV Downloader

Self-hosted tool that downloads 1-minute OHLCV candles into TimescaleDB and exports them as
[Jesse "Custom Data" CSVs](https://docs.jesse.trade/docs/traditional-markets/importing-data#custom-data-csv).

| Asset class | Provider | Key | Notes |
|---|---|---|---|
| Crypto | Binance | none | Full history. Binance blocks some regions (HTTP 451), e.g. US servers. |
| US stocks & ETFs | Alpaca | free | IEX feed from 2016, regular session only (09:30–16:00 ET, early closes respected). IEX volume is lower than consolidated volume. Prices are unadjusted. |
| Forex | Dukascopy | none | Bid prices; volume = tick count. Slowest source (about 1 hour or more per year per pair, roughly 7,500 files at 2 per second). Dukascopy may refuse requests (HTTP 503) when it decides it has been asked too much; the job then waits and shows "unavailable" until it recovers. Use OANDA if this keeps happening. |
| Forex, metals & CFDs | OANDA | free practice account | 1m bid candles; volume = tick count. Up to 5000 candles per request, so it is far faster than Dukascopy and the more reliable forex source. Create the token on your OANDA account's "Manage API Access" page, then set `OANDA_API_TOKEN` in `.env` or in Settings. `OANDA_ENVIRONMENT` (`practice` or `live`) is optional: when set it overrides the Settings page, otherwise the Settings value (default `practice`) is used. |
| Stocks, forex & metals | Twelve Data | free Basic plan (email signup) | 1m candles, regular session only for US stocks. Free limits are 800 requests/day and 8/min; history goes back to about 2020 (the depth varies by symbol, and the asset form shows it via earliest available). Gold is `XAU/USD`. Forex and metals have no volume, so it is 0. Get a key at twelvedata.com, then set `TWELVEDATA_API_KEY` in `.env` (wins over Settings) or in Settings. Speed: about 105 requests per year of 1-minute data, roughly 15 minutes per year at 8/min. The daily cap allows about 7 years of 1-minute history per day in total across all assets. |

## Run

```bash
cp .env.example .env   # optional
docker compose up -d --build
```

Open http://localhost:8000 (or the port set by `APP_PORT`), click **Add asset**, search a symbol, choose a start date and save.
The download runs in the background. You can close the browser, and restarts resume where they stopped.
Enable scheduled updates under **Settings**.

`POSTGRES_PASSWORD` only takes effect the first time the database volume is created. To change it later,
change the password inside Postgres (`ALTER USER ohlcv PASSWORD '...'`) or recreate the volume
(`docker compose down -v`, which deletes all stored candles).

## Security

The app has no authentication. Keep it on your LAN and do not expose it to the internet. It also rejects
cross-site browser POST/PUT/PATCH/DELETE requests as a guard against forged requests from other web pages.

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

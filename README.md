# OHLCV Downloader

Self-hosted tool that downloads 1-minute OHLCV candles from Binance (crypto), Alpaca (US stocks & ETFs) and Twelve Data (US stocks, forex, metals and commodities such as XAU/USD) into TimescaleDB and exports them as
[Jesse "Custom Data" CSVs](https://docs.jesse.trade/docs/traditional-markets/importing-data#custom-data-csv).

| Asset class | Provider | Key | Notes |
|---|---|---|---|
| Crypto | Binance | none | Full history. Binance blocks some regions (HTTP 451), e.g. US servers. |
| US stocks & ETFs | Alpaca | free | IEX feed from 2016, regular session only (09:30–16:00 ET, early closes respected). IEX volume is lower than consolidated volume. Prices are unadjusted. |
| US stocks, forex, metals & commodities | Twelve Data | free Basic plan (email signup) | 1m candles, regular session only for US stocks. Free limits are 800 requests/day and 8/min; history goes back to about 2020 (the depth varies by symbol, and the asset form shows it via earliest available). Gold is `XAU/USD`. Forex and metals have no volume, so it is 0. Get a key at twelvedata.com, then set `TWELVEDATA_API_KEY` in `.env` (wins over Settings) or in Settings. Speed: about 105 requests per year of 1-minute data, roughly 15 minutes per year at 8/min. The daily cap allows about 7 years of 1-minute history per day in total across all assets, and adding an asset costs about 1 credit (its earliest-date lookup). When the daily cap is hit, the job shows "daily credit limit reached" and retries hourly. The newest 15 minutes are not downloaded yet, because free-plan bars can publish late. |

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

## Backup & restore

```bash
scripts/backup.sh                      # writes backups/ohlcv-YYYYmmdd-HHMMSS.dump, keeps the newest 7
KEEP=14 scripts/backup.sh              # keep the newest 14 instead
PROJECT=myproject scripts/backup.sh    # target a non-default compose project
```

The scripts work from any directory and need the `db` container running. Nightly backup at 03:00 via cron:

```
0 3 * * * /path/to/repo/scripts/backup.sh
```

Restore:

```bash
scripts/restore.sh backups/ohlcv-20261001-030000.dump          # asks you to type "restore"
scripts/restore.sh backups/ohlcv-20261001-030000.dump --yes    # no prompt
```

**Warning:** restoring REPLACES all current data in the database with the dump. The script stops the `app` service, runs `timescaledb_pre_restore()`, `pg_restore --clean --if-exists`, then `timescaledb_post_restore()`, and starts the app again. Copy dumps off the machine too; `backups/` lives next to the database.

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

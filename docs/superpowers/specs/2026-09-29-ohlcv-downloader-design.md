# OHLCV 1m Downloader — Design

**Date:** 2026-09-29
**Status:** Approved (brainstorming)

## Goal

A self-hosted tool, run with `docker compose`, with a web GUI to download 1-minute OHLCV candles for crypto, stocks, ETFs and forex into a database. Updates download only candles newer than the last stored one. Any asset can be exported as a CSV in the Jesse "Custom Data" format (https://docs.jesse.trade/docs/traditional-markets/importing-data#custom-data-csv).

Single user, runs on a local machine or LAN, no authentication.

## Stack

- Python 3.12, FastAPI, SQLAlchemy 2 (async) + asyncpg, Alembic
- PostgreSQL 16 + TimescaleDB
- UI: Jinja2 templates + HTMX + Pico.css (no JS build step)
- APScheduler (in-process) for scheduled updates
- httpx for provider HTTP calls
- pytest, respx, testcontainers

## Deployment

`docker-compose.yml` with two services:

- `db`: `timescale/timescaledb:latest-pg16`, named volume for data, `restart: unless-stopped`.
- `app`: built from repo `Dockerfile`, exposes port 8000, depends on `db`, `restart: unless-stopped`. Runs Alembic migrations on startup, then serves uvicorn.

`.env.example` provides `DATABASE_URL`, `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`, `TZ`.

## Data Model

### `assets`
| column | type | notes |
|---|---|---|
| id | serial PK | |
| provider | text | `binance` \| `alpaca` \| `dukascopy` |
| provider_symbol | text | e.g. `BTCUSDT`, `AAPL`, `EURUSD` |
| asset_class | text | `crypto` \| `stock` \| `etf` \| `forex` |
| jesse_symbol | text | `BASE-QUOTE`, e.g. `BTC-USDT`, `AAPL-USD`, `EUR-USD`; auto-suggested, user-editable |
| start_date | timestamptz | first candle to backfill from |
| enabled | bool | included in "Update all" and scheduled runs |
| created_at | timestamptz | |

Unique on `(provider, provider_symbol)`.

### `candles` (TimescaleDB hypertable on `ts`)
| column | type |
|---|---|
| asset_id | int FK → assets (ON DELETE CASCADE) |
| ts | timestamptz, candle open time, on a 1-minute UTC boundary |
| open, high, low, close | double precision |
| volume | double precision |

Primary key `(asset_id, ts)`. All inserts use `ON CONFLICT DO NOTHING`. Compression is enabled with `segmentby = asset_id`, and a policy compresses chunks older than 30 days.

### `jobs`
`id, asset_id, kind (backfill|update), status (queued|running|done|failed), progress_ts (last inserted candle ts), target_ts, candles_added, error, created_at, started_at, finished_at`.

### `settings`
Key/value rows: `schedule_enabled`, `schedule_cron`, `worker_concurrency`, `alpaca_key_id`, `alpaca_secret_key`. Environment variables override DB values for the Alpaca keys.

## Providers

Pluggable interface, one module per provider, registered in a provider registry:

```python
class Provider(Protocol):
    name: str
    asset_classes: set[str]
    async def search_symbols(self, query: str) -> list[SymbolInfo]
    async def earliest_available(self, symbol: str) -> datetime
    async def fetch(self, symbol: str, start: datetime, end: datetime
                    ) -> AsyncIterator[list[Candle]]   # yields ascending chunks
```

`SymbolInfo` contains `provider_symbol`, `asset_class`, `suggested_jesse_symbol`, and a display name.

Each provider has its own rate limiter (a token bucket) shared by all jobs that use it.

### Binance (crypto)
- Public `GET /api/v3/klines?interval=1m&limit=1000`, no key.
- Symbol list from `/api/v3/exchangeInfo` (cached). Jesse symbol = `baseAsset-quoteAsset`.
- Earliest available: first kline returned from `startTime=0`.
- Respects `X-MBX-USED-WEIGHT` and backs off on HTTP 429/418.

### Alpaca (stocks, ETFs)
- `GET /v2/stocks/{symbol}/bars?timeframe=1Min&feed=iex&limit=10000`, follows `next_page_token`. Requires a free API key.
- **Regular session only:** keeps a bar only if its open time, converted to `America/New_York`, is in [09:30, 16:00). DST is handled through zoneinfo.
- Symbol search from `/v2/assets` (cached), which gives the stock/ETF class. Jesse symbol = `SYMBOL-USD`.
- Earliest available: 2016-01-01 (IEX feed history start). The actual first bar is found by the first fetch.
- Note: the free IEX feed only covers IEX exchange trades, so volume is lower than consolidated volume and some thinly traded minutes have no bar. The feed is a single constant in the adapter, so it can be switched to `sip` if a paid plan is added later.

### Dukascopy (forex)
- Free hourly tick files: `https://datafeed.dukascopy.com/datafeed/{PAIR}/{YYYY}/{MM-1:02d}/{DD:02d}/{HH:02d}h_ticks.bi5` (LZMA-compressed; 20-byte big-endian records: ms offset, ask, bid, ask vol, bid vol; prices scaled by point size, 1e5 or 1e3 for JPY pairs).
- Aggregated into 1m **bid** candles. Volume = tick count in the minute.
- Missing or empty hour files (weekends, holidays) produce no candles.
- Concurrency limited to 4 in-flight file downloads.
- Symbol list: a built-in list of major and minor pairs. Jesse symbol = `EUR-USD` etc.

## Update Logic

For an asset:
1. `start = max(candles.ts) + 1 minute`, or `asset.start_date` if no candles exist.
2. `end = now` floored to the minute (the last *closed* candle is `end - 1 minute`).
3. If `start >= end`, the job finishes with 0 candles.
4. Iterate `provider.fetch(...)`. Each chunk is bulk-inserted in its own transaction, and `jobs.progress_ts` / `candles_added` are updated after each chunk.

Because progress is saved per chunk and inserts ignore conflicts, an interrupted or failed job loses no data. Re-running it continues from the last stored candle.

Gaps inside the stored range are not filled (out of scope).

## Job Runner

- An in-process async worker pool with `worker_concurrency` workers (default 3). The workers take `queued` jobs from the `jobs` table.
- At most one queued or running job per asset. Requesting another returns the existing job.
- Per-request retries: 3 attempts with exponential backoff on network errors and 5xx/429. Once retries are exhausted, the job becomes `failed` with the error message, and the candles already inserted are kept.
- On startup, any `running` job is marked `failed` with error `interrupted`.
- Adding an asset queues a `backfill` job. Update buttons and scheduled runs queue `update` jobs. Both kinds run the same update logic; `kind` is for display only.

## Scheduler

APScheduler runs in the app process. When `schedule_enabled` is set, it runs on `schedule_cron` (UI presets: hourly, every 6h, daily at HH:MM UTC; custom cron allowed) and queues an `update` job for every enabled asset. The schedule reloads when settings are saved.

## CSV Export (Jesse format)

`GET /assets/{id}/export.csv?start=YYYY-MM-DD&end=YYYY-MM-DD` (both optional, default = full stored range, `end` inclusive by day).

- Streaming response using a server-side cursor, so memory use stays constant.
- UTF-8, header `timestamp,open,close,high,low,volume` (this column order).
- `timestamp` = Unix milliseconds of the candle open. Rows are ascending and unique, which the primary key and `ORDER BY ts` guarantee.
- Row validation: rows with any null/NaN value, `high < max(open, close, low)`, `low > min(open, close, high)`, or `volume < 0` are skipped, and the skip count is logged.
- Gaps are exported as gaps (Jesse treats them as market closures).
- Filename: `{jesse_symbol}_{first_date}_{last_date}.csv`, dates being the first and last exported candle.

## GUI

- **Assets (home):** a table with Jesse symbol, provider, class, first candle, last candle, candle count, last update, job status badge with progress, and the actions Update / Export / Edit / Delete. There's also an "Update all" button. Active job rows refresh by HTMX polling every 2s.
- **Add asset:** provider select, then symbol search (HTMX type-ahead backed by `search_symbols`), then Jesse symbol (pre-filled, editable) and start date (pre-filled with earliest available). Saving queues a backfill job.
- **Edit asset:** jesse_symbol, enabled. Changing `start_date` is not supported, because the stored range only extends forward.
- **Export dialog:** start/end date inputs, pre-filled with the stored range, and a Download button.
- **Delete:** an in-page confirmation, which then deletes the asset and all its candles.
- **Jobs:** the last 200 jobs with asset, kind, status, candles added, duration, error.
- **Settings:** schedule on/off plus cron preset, worker concurrency, and Alpaca key/secret (masked; shows "set via environment" when env vars are present, which makes them read-only in the UI).

Candle count and first/last candle per asset come from one aggregate query per asset list render, cached for a short time if it is slow.

## Testing

- **Provider unit tests** with recorded HTTP fixtures (respx):
  - Binance pagination and kline parsing.
  - Alpaca pagination and the regular-session filter, including days on both sides of a DST switch and a half-day.
  - Dukascopy `.bi5` decoding, point-size scaling (JPY vs non-JPY), 1m aggregation, and empty/missing hour files.
- **Integration tests** against TimescaleDB via testcontainers, using a fake provider:
  - A first run backfills from `start_date`. A second run fetches only newer candles.
  - A re-run over an overlapping range inserts no duplicates.
  - A job failing midway keeps its inserted candles, and the next run resumes after them.
  - The one-active-job-per-asset rule.
- **Export tests:** header and column order, ms timestamps, ascending and unique, all on 1m boundaries, invalid rows skipped, date-range filtering.
- **Route smoke tests:** pages render, and add/update/delete actions queue or delete correctly.

## Out of Scope (v1)

Authentication, timeframes other than 1m, multi-asset ZIP export, charts, detecting or filling gaps inside the stored range, changing an asset's start date after creation, and paid providers (the provider interface supports adding them later).

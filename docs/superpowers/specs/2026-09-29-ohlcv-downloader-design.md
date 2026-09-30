# OHLCV 1m Downloader — Design

**Date:** 2026-09-29
**Status:** Approved (brainstorming)

## Goal

A self-hosted tool, run with `docker compose`, with a web GUI to download 1-minute OHLCV candles for crypto, stocks, ETFs and forex into a database. Updates download only candles newer than the last stored one. Any asset can be exported as a CSV in the Jesse "Custom Data" format (https://docs.jesse.trade/docs/traditional-markets/importing-data#custom-data-csv).

Single user, runs on a local machine or LAN, no authentication. The app has no login, so a small middleware rejects state-changing requests (POST/PUT/PATCH/DELETE) that a browser marks `Sec-Fetch-Site: cross-site` or whose `Origin` host differs from `Host`; requests with neither header (curl, tests) pass. Keep it off the public internet.

Free APIs have rate limits, and years of 1m history take thousands of requests. The user never has to manage this. They add an asset and walk away. The app splits the download into small requests, paces them to each provider's limits, waits out throttling, resumes after restarts or network outages, and shows progress with an ETA (see **Rate Limits and Long Downloads**).

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

`.env.example` provides `POSTGRES_PASSWORD`, `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`, `ALPACA_TRADING_URL`, `OANDA_API_TOKEN`, `OANDA_ENVIRONMENT`, `APP_PORT`. Compose builds `DATABASE_URL` from `POSTGRES_PASSWORD`.

## Data Model

### `assets`
| column | type | notes |
|---|---|---|
| id | serial PK | |
| provider | text | `binance` \| `alpaca` \| `dukascopy` \| `oanda` |
| provider_symbol | text | e.g. `BTCUSDT`, `AAPL`, `EURUSD` |
| asset_class | text | `crypto` \| `stock` \| `etf` \| `forex` \| `metal` \| `cfd` |
| jesse_symbol | text | `BASE-QUOTE`, e.g. `BTC-USDT`, `AAPL-USD`, `EUR-USD`; auto-suggested, user-editable |
| start_date | timestamptz | first candle to backfill from |
| fetched_until | timestamptz null | exclusive end of the range already fetched from the provider (NULL = nothing fetched yet). Advances even when a window has no candles (weekends, holidays, pre-listing), so empty ranges are never requested twice |
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
| column | notes |
|---|---|
| id, asset_id | |
| kind | `backfill` \| `update` |
| status | `queued` \| `running` \| `waiting` (rate-limited, resumes automatically) \| `paused` (transient error, retries automatically) \| `done` \| `failed` (permanent error) \| `cancelled` |
| priority | `update` = 10, `backfill` = 0 (higher runs first) |
| range_start, range_end | range this job covers, fixed when the job is created |
| requests_made, candles_added | counters |
| status_detail | human-readable reason for `waiting`/`paused`, e.g. "Alpaca rate limit, resuming 14:03:12" |
| next_attempt_at | when a `waiting`/`paused` job becomes eligible again |
| attempt | consecutive failed attempts (reset after any successful chunk) |
| last_progress_at | time of the last successful chunk; also reset whenever a job with `attempt = 0` is claimed, so the 24 h give-up rule counts from when the job last ran, not from queueing |
| queued_at | when the job last entered the queue (set at creation and on every slice requeue); claim order is `priority DESC, queued_at, id` |
| run_seconds | accumulated time spent running; used for the measured request rate |
| error | last error message |
| created_at, started_at, finished_at | |

Progress is `(assets.fetched_until − range_start) / (range_end − range_start)`.

### `settings`
Key/value rows: `schedule_enabled`, `schedule_cron`, `worker_concurrency`, `alpaca_key_id`, `alpaca_secret_key`, `oanda_api_token`, `oanda_environment`. Environment variables override DB values for the Alpaca keys and the OANDA token ; `OANDA_ENVIRONMENT`, when non-empty, overrides `oanda_environment` independently of the token.

## Providers

Pluggable interface, one module per provider, registered in a provider registry:

```python
class Provider(Protocol):
    name: str
    asset_classes: set[str]
    async def search_symbols(self, query: str) -> list[SymbolInfo]
    async def earliest_available(self, symbol: str) -> datetime
    def available_until(self, now: datetime) -> datetime   # latest safely-final minute (includes each provider's publish lag)
    def estimate_requests(self, start: datetime, end: datetime) -> int
    async def fetch(self, symbol: str, start: datetime, end: datetime
                    ) -> AsyncIterator[Chunk]   # ascending chunks, one per request/file
```

`SymbolInfo` contains `provider_symbol`, `asset_class`, `suggested_jesse_symbol`, and a display name.

`Chunk` contains `candles: list[Candle]` and `covered_until: datetime`, the exclusive end of the time range this chunk fully covers. `covered_until` can advance even when `candles` is empty. Providers make every HTTP call through a shared `ProviderClient`, which applies the provider's rate-limit policy (see **Rate Limits and Long Downloads**).

### Binance (crypto)
- Public `GET /api/v3/klines?interval=1m&limit=1000`, no key.
- Symbol list from `/api/v3/exchangeInfo` (cached). Jesse symbol = `baseAsset-quoteAsset`.
- Earliest available: first kline returned from `startTime=0`.
- Rate limit: 6000 request weight/min per IP; a 1000-candle kline request costs weight 2. `X-MBX-USED-WEIGHT-1M` is read after every response. HTTP 429/418 carry `Retry-After`.
- `available_until` = now floored to the minute, minus a 2-minute publish lag (`PUBLISH_LAG`), so the cursor never passes unpublished data.

### Alpaca (stocks, ETFs)
- `GET /v2/stocks/{symbol}/bars?timeframe=1Min&feed=iex&limit=10000`, follows `next_page_token`. Requires a free API key.
- **Regular session only:** each fetch loads the Alpaca market calendar (`GET {trading}/v2/calendar`, one request) for the range and keeps a bar only if its open time is in [open, close) of its New York trading day. This handles DST, holidays and early closes (e.g. 13:00 half-days). Trading API base URL comes from `ALPACA_TRADING_URL` (default `https://paper-api.alpaca.markets`, which works with free paper-account keys).
- Prices are requested unadjusted (`adjustment=raw`), so incremental updates never disagree with earlier stored candles after a split.
- Symbol search from `/v2/assets` (cached), which gives the stock/ETF class. Jesse symbol = `SYMBOL-USD`.
- Earliest available: 2016-01-01 (IEX feed history start). A request whose range starts before a symbol's first bar simply returns that first bar, so pre-listing years cost no extra requests.
- Rate limit: 200 requests/min on the free plan. `X-RateLimit-Remaining` / `X-RateLimit-Reset` are read after every response.
- `covered_until` = timestamp after the last bar of the page, or the request `end` once `next_page_token` is empty.
- `available_until` = now floored to the minute, minus a 2-minute publish lag (`PUBLISH_LAG`), so the cursor never passes unpublished data.
- Note: the free IEX feed only covers IEX exchange trades, so volume is lower than consolidated volume and some thinly traded minutes have no bar. The feed is a single constant in the adapter, so it can be switched to `sip` if a paid plan is added later.

### Dukascopy (forex)
- Free hourly tick files: `https://datafeed.dukascopy.com/datafeed/{PAIR}/{YYYY}/{MM-1:02d}/{DD:02d}/{HH:02d}h_ticks.bi5` (LZMA-compressed; 20-byte big-endian records: ms offset, ask, bid, ask vol, bid vol; prices scaled by point size, 1e5 or 1e3 for JPY pairs).
- Aggregated into 1m **bid** candles. Volume = tick count in the minute.
- Missing or empty hour files (weekends, holidays) produce no candles.
- No published rate limit. The app self-throttles to 2 in-flight downloads and 2 files/s (kept low because Dukascopy blocks fast clients), and treats 429/503 as throttling. One file = one hour, so this is the slowest source (about 1 hour or more per year (about 7,500 files a year at 2/s) of history per pair).
- Each hour file yields one `Chunk` with `covered_until` = end of that hour.
- `available_until` = start of the current UTC hour minus a 2-hour publish lag (`PUBLISH_LAG`). The current hour's file is not final, and finished hours are often published late. Requesting it too early would return 404 and move the cursor past it, leaving a permanent gap.
- Symbol list: a built-in list of major and minor pairs, each with its first available date. Jesse symbol = `EUR-USD` etc.

### OANDA (forex, metals, CFDs)
- OANDA v20 REST API. Hosts: practice `https://api-fxpractice.oanda.com` (free demo account), live `https://api-fxtrade.oanda.com`, chosen by `OANDA_ENVIRONMENT` (non-empty values win over the Settings page, even when the token comes from Settings; the value is trimmed and lower-cased, and anything other than `practice`/`live` makes the provider fail with "OANDA_ENVIRONMENT must be 'practice' or 'live'"; unset means the Settings value, default practice). Auth: `Authorization: Bearer <token>`; the token is created on the account's "Manage API Access" page and is required (missing token = permanent error before any request). Every request sends `Accept-Datetime-Format: UNIX`, so `from`/`to` are UNIX seconds and each candle `time` is a string like `"1476717360.000000000"`.
- Candles: `GET /v3/instruments/{instrument}/candles?granularity=M1&price=B&from=..&to=..` (bid, consistent with Dukascopy). `count` is never sent together with `from`+`to`; a range may hold at most 5000 candles, so `fetch` walks windows `[cursor, min(cursor + 4999 min, end))` (`PAGE = 4999`, so a window can never exceed 5000 candles even if `to` were inclusive), one request and one `Chunk` each. Prices are strings; volume is tick volume.
- A candle with `complete: false` is dropped along with everything after it, and `covered_until` is that candle's time. After such a chunk the fetch stops without another request (the next run continues from `covered_until`), so it cannot spin. Otherwise `covered_until` is the window end, so empty windows (weekends) still advance the cursor.
- Symbols: `GET /v3/accounts`, then `GET /v3/accounts/{id}/instruments` of the first account, cached per (token, environment). An empty or malformed accounts/instruments response is a permanent error ("no accounts found for this token", "no instruments found for this account"). Types CURRENCY, METAL, CFD map to classes forex, metal, cfd. Jesse symbol = name with `_` replaced by `-` (`EUR-USD`, `XAU-USD`, `US30-USD`). Search ignores `_`, `-` and `/`.
- `earliest_available`: one request from 2000-01-01 with `count=1`; the first candle's time.
- `available_until` = current minute minus a 2-minute lag (`PUBLISH_LAG`). `estimate_requests` = ceil(minutes / 4999).
- Budget: OANDA allows 120 requests/s per IP and answers 429 beyond it. The app uses 20 req/s with 2 in flight. There are no quota headers. Errors 400/401/403/404 are permanent; 429 is throttling.

## Update Logic

When a job is created for an asset:
1. `range_start = asset.fetched_until`, or `asset.start_date` if NULL.
2. `range_end = provider.available_until(now)`.
3. If `range_start >= range_end`, the job is created as `done` with 0 candles.

When a job runs (including every resume):
1. `start = asset.fetched_until or asset.start_date`. The job always continues from the stored cursor, never from `range_start`.
2. Iterate `provider.fetch(symbol, start, range_end)`. For each chunk, **in one transaction**: bulk-insert the candles (`ON CONFLICT DO NOTHING`), set `assets.fetched_until = chunk.covered_until`, and update the job counters.
3. When the iterator ends, the job is `done`.

Because the cursor and the candles are committed together after every request, stopping at any point loses at most one in-flight request. Resuming never re-fetches finished ranges, including empty ones.

Gaps inside the stored range are not filled (out of scope).

## Rate Limits and Long Downloads

Goal: a multi-year backfill runs unattended to completion, with the user only watching the progress bar.

**1. Small requests.** Every fetch is broken into requests the provider can serve: 1000 candles for Binance, one 10,000-bar page for Alpaca, one hour file for Dukascopy, one window of at most 4999 one-minute candles for OANDA. A large download is simply many small requests, each committed on its own.

**2. Pacing per provider.** Each provider has a `RateLimitPolicy` that the shared `ProviderClient` enforces for all jobs using that provider:

| provider | budget used by the app | concurrency |
|---|---|---|
| Binance | 3000 weight/min (50% of the 6000 limit, leaving room for other tools on the same IP) | 2 |
| Alpaca | 180 req/min (90% of 200) | 1 |
| Dukascopy | 2 files/s | 2 |
| OANDA | 20 req/s (of 120) | 2 |

The client uses a token bucket for the budget. It also reads the provider's rate-limit headers after each response: when they report remaining quota near zero, it sleeps until the reported reset time instead of hitting the limit. Budgets are constants in each adapter.

**3. Throttling means waiting, not failing.** On HTTP 429/418 (or 503 from Dukascopy), the client pauses **all** requests to that provider until `Retry-After` (or 60 s if absent, doubling on repeats up to 15 min). Affected jobs show status `waiting` with a message like "Binance rate limit, resuming 14:03:12". This never counts toward the job's error attempts.

**Honest status.** The client remembers when the current streak of throttling responses began (any non-throttled response ends it, except a stale one that arrives while a pause is still active). If the refusals have lasted 5 minutes or more of elapsed time (measured from the first refusal to now, not from the projected resume time, so one long `Retry-After` alone never counts), the message changes to "Dukascopy unavailable since 14:00 UTC (HTTP 503), retrying at 14:09:30 UTC" so a provider that refuses everything is not presented as merely rate limited. Jobs in `waiting` or `paused` show no ETA, since nothing is being downloaded.

**4. Transient errors retry automatically.** Network errors, timeouts and 5xx responses are retried 3 times within the request (backoff 2 s, 4 s, 8 s). If a request still fails, the job becomes `paused` and is re-queued with `next_attempt_at` after 1 min, 5 min, 15 min, 1 h, then hourly. After 24 h of consecutive failures without a successful chunk, the job becomes `failed`. Any successful chunk resets the counter. Candles already stored are always kept.

**5. Permanent errors fail fast.** Bad or missing API key (401/403) and unknown symbol (400/404 or provider "invalid symbol") mark the job `failed` immediately with an actionable message such as "Alpaca API key rejected — check Settings". Other jobs keep running.

**6. Restarts resume.** On startup, jobs left `running` or `waiting` return to `queued` and continue from `fetched_until`. `paused` jobs keep their `next_attempt_at`. Stopping the container, rebooting the host or a crash just pauses downloads.

**7. Fair scheduling.** Every job (backfill or update) works in **slices**: after about 60 s of work it commits and returns itself to the queue with a fresh `queued_at`. Workers pick the highest-priority eligible job (`updates` before `backfills`), then the oldest `queued_at`. Short updates never wait behind a multi-hour Dukascopy backfill, and several jobs share the providers in round-robin.

**8. Visibility.** Progress and ETA come from the cursor. ETA = `estimate_requests(fetched_until, range_end)` divided by the job's measured average rate (`requests_made / run_seconds`) once it has made 20 requests, and by the policy budget before that. The Add Asset form shows an estimate before saving, e.g. "≈ 2,600 requests, about 2 minutes" or "≈ 120,000 files, about 4 hours".

## Job Runner

- An in-process async worker pool with `worker_concurrency` workers (default 3). Workers take the highest-priority eligible job (`queued`, or `waiting`/`paused` with `next_attempt_at <= now`), using `SELECT ... FOR UPDATE SKIP LOCKED`.
- At most one active (not `done`/`failed`/`cancelled`) job per asset. Requesting another returns the existing job. An active backfill already runs to `range_end`, and the next update continues from its cursor.
- Retry, throttling, restart and slicing behaviour are specified in **Rate Limits and Long Downloads**.
- Adding an asset queues a `backfill` job. Update buttons and scheduled runs queue `update` jobs. Both kinds run the same update logic and differ only in priority.
- The UI can cancel an active job. Stored candles and the cursor are kept, so a later update continues from there.

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

- **Assets (home):** a table with Jesse symbol, provider, class, first candle, last candle, candle count, last update, job status, and the actions Update / Export / Edit / Delete. There's also an "Update all" button. The job status shows a progress bar, percentage, ETA and `status_detail` (e.g. "waiting: Alpaca rate limit, resuming 14:03", "paused: network error, retrying in 5 min"). Active job rows refresh by HTMX polling every 2s.
- Export is available at any time, including during a backfill; it exports what is stored so far.
- **Add asset:** provider select, then symbol search (HTMX type-ahead backed by `search_symbols`), then Jesse symbol (pre-filled, editable) and start date (pre-filled with earliest available). An estimate of requests and download time updates as the start date changes. Saving queues a backfill job.
- **Edit asset:** jesse_symbol, enabled. Changing `start_date` is not supported, because the stored range only extends forward.
- **Export dialog:** start/end date inputs, pre-filled with the stored range, and a Download button.
- **Delete:** an in-page confirmation, which then deletes the asset and all its candles.
- **Jobs:** the last 200 jobs with asset, kind, status, progress, requests made, candles added, duration, error, and a Cancel action for active jobs.
- **Settings:** schedule on/off plus cron preset, worker concurrency, and Alpaca key/secret and OANDA token plus practice/live environment (masked; shows "set via environment" when env vars are present, which makes them read-only in the UI).

Candle count and first/last candle per asset come from one aggregate query per asset list render, cached for a short time if it is slow.

## Testing

- **Provider unit tests** with recorded HTTP fixtures (respx):
  - Binance pagination and kline parsing.
  - Alpaca pagination and the regular-session filter, including days on both sides of a DST switch and a half-day.
  - Dukascopy `.bi5` decoding, point-size scaling (JPY vs non-JPY), 1m aggregation, and empty/missing hour files.
  - `covered_until` advancing over empty ranges (weekend for Dukascopy, pre-listing range for Alpaca and Binance).
- **Rate limiting** (`ProviderClient` with a fake clock and respx):
  - The token bucket keeps the request rate under budget with concurrent callers.
  - Low-quota headers cause a sleep until reset.
  - 429 with `Retry-After` pauses all callers for that provider and does not count as an error attempt.
  - 5xx/network errors are retried, then raise a transient error; 401/404 raise a permanent error immediately.
- **Integration tests** against TimescaleDB via testcontainers, using a fake provider:
  - A first run backfills from `start_date`. A second run fetches only newer candles.
  - A re-run over an overlapping range inserts no duplicates.
  - A job failing midway keeps its inserted candles and cursor, and the next run resumes after them without re-requesting finished (including empty) ranges.
  - Transient failures move a job to `paused` with the backoff schedule, then `failed` after 24 h without progress; permanent errors fail immediately.
  - Startup recovery re-queues `running`/`waiting` jobs.
  - Slicing: a long backfill yields after its slice, and a queued update runs before it continues.
  - The one-active-job-per-asset rule, and cancellation keeping the cursor.
- **Export tests:** header and column order, ms timestamps, ascending and unique, all on 1m boundaries, invalid rows skipped, date-range filtering.
- **Route smoke tests:** pages render, and add/update/delete actions queue or delete correctly.

## Out of Scope (v1)

Authentication, timeframes other than 1m, multi-asset ZIP export, charts, detecting or filling gaps inside the stored range, changing an asset's start date after creation, and paid providers (the provider interface supports adding them later).

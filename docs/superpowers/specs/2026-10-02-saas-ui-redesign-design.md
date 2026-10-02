# SaaS UI Redesign — Design

**Date:** 2026-10-02
**Status:** Approved (brainstorming)
**Mockups:** `.superpowers/brainstorm/18632-1790961161/content/` (`visual-direction.html`, `overview-v1.html`, `assets-detail-v1.html`, `rest-v1.html`; git-ignored, local only)

## Goal

Make the web UI look and feel like a professional SaaS product. Other people will see and use the tool (demos, shared or self-hosted instances), so it must look credible to outsiders on first open. The product name shown in the UI is **Asuras CSV**.

Not in scope: login or multiple users, branding beyond the name and a logo mark, and features beyond those described here. The download, export and job machinery stay as they are.

## Visual direction

Dark sidebar layout in the style of Linear (mockup direction A):

- A left sidebar with the logo mark and "Asuras CSV", nav items Overview / Assets / Jobs (badge with the active job count) / Settings. The footer shows scheduler status, the next run, and a theme toggle.
- A neutral near-black palette in dark mode and white cards on light gray in light mode. One indigo accent (`#5b6cff` dark / `#4f5bff` light).
- Typography: Inter, 12.5–13px base, tabular numerals in tables and KPIs.
- Status pills with colored backgrounds. Green for up to date or done, blue for running (with an inline progress bar and percent), amber for waiting or rate limited, red for failed, gray for idle or disabled.
- The theme follows `prefers-color-scheme`. The sidebar toggle overrides it, and the choice is stored in `localStorage` (wrapped in try/catch). The theme is applied before first paint by a small inline script in `<head>` so the page doesn't flash.
- Responsive: below ~900px the sidebar collapses into a top bar with a menu button. Wide tables scroll horizontally inside their card, and the page itself never scrolls sideways.

## Pages

### Overview — `/` (new home page)

- Header: title, **Update all**, **+ Add asset** (primary).
- Four KPI cards:
  - **Assets:** total, with "N enabled · M disabled".
  - **Candles stored:** total, abbreviated (48.2M), subtitle "1-minute bars".
  - **Active jobs:** count, with a breakdown by status (running / waiting / queued / paused).
  - **Freshness:** "X / Y". X is the number of enabled assets whose latest job finished as `done` within the last 24 h. Y is the number of enabled assets. This counts the last successful update, not the last candle, so stocks and forex don't look stale over weekends or holidays.
- **Assets card:** the first 8 assets by Jesse symbol with columns Symbol, Provider, History (first → last date), Last 30 days (sparkline), Last candle (relative time), Status. Links to `/assets` ("View all →").
- **Recent jobs card:** the latest 5 jobs (asset, kind, status, candles added, finished as relative time). Links to `/jobs`.
- **Data sources card:** one row per registered provider. Each shows its label, whether it needs a key, and "Ready" (key not required or configured) or "Key missing" (links to Settings). The last row shows the scheduled updates (cron expression, On or Off).
- Empty state (no assets): a centered card with a short explanation and **Add your first asset**.

### Assets — `/assets`

- The current assets table moves here. Header: title with the asset count, **Update all**, **+ Add asset**.
- Filter bar: a text filter on the symbol and dropdowns for Provider and Status. These filter rows in the browser over the polled table. The filter state is re-applied after each htmx swap.
- Columns: checkbox, Symbol (provider symbol muted below, plus a "disabled" tag), Provider, Class, First candle, Last candle, Candles (right-aligned), Status pill, ⋯ menu.
- The ⋯ menu holds Update now, Open detail, Export CSV (links to the detail page's export card), Edit (links to the detail page's settings card), and Delete… (opens the confirmation page).
- Clicking a row outside the checkbox and menu opens `/assets/{id}`.
- Bulk-action bar: appears while at least one checkbox is ticked and shows "N selected", From and To dates, **Export ZIP** and **Clear**. It submits to the existing `/export.zip`. Checkboxes keep `form="zip-form"` and `hx-preserve`, as today.
- Polling stays as it is now: `/assets/rows` every 2 s while a job is active, otherwise every 30 s.

### Asset detail — `/assets/{id}` (new; merges chart, export and edit)

- Header: breadcrumb "Assets /", Jesse symbol, live status pill, **Update now**, **Delete**.
- Price card: the Lightweight Charts candlestick and volume chart with range tabs (1D 1W 1M 6M 1Y All), and the interval and bar count in the card header. The chart colors follow the active theme, including when the theme is toggled.
- Details card: Provider · provider symbol, Class, Stored range, Candles, Fetched until, Jesse "Custom Data · SYMBOL".
- Export CSV card: From and To dates (default: the full stored range) and **Download CSV**, submitting to the existing `/assets/{id}/export.csv`. If nothing is stored yet: "No candles stored yet."
- Job history card: the latest 10 jobs for this asset.
- Settings card: the Jesse symbol and an "Include in scheduled updates" (enabled) checkbox, posting to the existing edit handler. Validation errors show inline. On success it redirects back to the detail page with a toast.
- The status pill and job history refresh from `/assets/{id}/live` (2 s while a job is active, otherwise 30 s).
- Old GET pages `/assets/{id}/chart`, `/assets/{id}/export` and `/assets/{id}/edit` respond with 303 to `/assets/{id}`. Their anchors are `#chart`, `#export` and `#settings`, and the ⋯ menu links use the same anchors.

### Add asset — `/assets/new`

- One page that reads as three steps (Source ✓ → Symbol → Configure), with a step indicator.
- Source: a segmented control of the providers, replacing the `<select>`. It keeps `name="search_provider"` through radio inputs. Under it, a hint gives the provider's coverage and its key status ("free key required · configured ✓" or "Key missing → Settings").
- Symbol: a search input with results as a list (symbol bold; name · class muted), with htmx behavior unchanged.
- Configure: the details card (Jesse symbol, asset class, start date) in a two-column grid. The existing estimate text is shown in an accent callout, with its wording unchanged. Primary button: **Add and start download**.

### Jobs — `/jobs`

- Segmented tabs: All / Active N / Failed N, implemented as `?status=active|failed`.
- Columns: #, Asset (links to the detail page), Kind, Status pill (detail and error text below it, error in red), Range, Requests, Candles added, Run time, Created, Finished, Cancel (for active jobs).
- The table body polls `/jobs/rows?status=…` (2 s while any job is active, otherwise 30 s).

### Settings — `/settings`

- Header with a **Save changes** primary button. It submits the single existing form, and saving is unchanged.
- Cards: **Scheduled updates** (switch, cron field with the presets datalist, "next run" hint, parallel downloads 1–10), **Alpaca** and **Twelve Data**. For keys that come from env vars, a lock callout replaces the inputs, with the current wording.

### Delete confirmation and message pages

- Delete confirmation: a centered card with a danger heading and the candle count about to be removed, plus **Delete** (danger) and **Cancel**.
- `message_page`: a centered card with the message and a **Back** link.

## Feedback

- **Toasts:** state-changing POSTs that redirect (Update all, Update now, Cancel job, Add asset, Save settings, Edit asset, Delete asset) set a `flash` cookie (`Max-Age=30`, `Path=/`, `SameSite=Lax`, `HttpOnly`) holding a short URL-encoded message. `base.html` renders it as a toast that dismisses itself after 4 s, and the response that renders it deletes the cookie. The cookie is unsigned: the message only reaches the same browser and Jinja escapes it.
- **Poll failures:** an `htmx:sendError` or `htmx:responseError` shows a "Reconnecting…" badge in the header, and the next successful poll hides it. The last good content stays visible.
- **Form errors:** shown inline under the relevant field or at the top of the card, styled in the danger color.

## Architecture

### Static assets

- New `app/web/static/`:
  - `app.css`: design tokens as CSS custom properties on `:root`, with a dark set under `[data-theme=dark]` and `@media (prefers-color-scheme: dark)` for `:root:not([data-theme=light])`. Components: shell, sidebar, page header, card, KPI, table, badge or pill, progress, button variants (primary, secondary, ghost, danger, small), fields, segmented control, menu, toast, empty state, callout, step indicator.
  - `app.js`: theme toggle, ⋯ menus (click to open, close on outside click or Esc), bulk-select bar, browser-side filters, toast dismissal, mobile sidebar, reconnecting badge. Plain JS, no framework.
  - `vendor/htmx-2.0.4.min.js`, `vendor/lightweight-charts-4.2.0.standalone.production.js`, `vendor/inter/*.woff2` (+ license), `vendor/icons.svg` (an SVG sprite of the Lucide icons used, + license).
- `create_app` mounts `StaticFiles` at `/static`. `pyproject.toml` package-data adds `web/static/**/*` so the Docker image ships the files.
- Cache busting: templates reference `/static/app.css?v={{ static_version }}` (and the same for `app.js`). `static_version` is a short hash of `app.css` + `app.js`, computed once at startup (the package version is fixed at 0.1.0, so it can't be used).
- Pico.css and every CDN reference are removed.

### Templates

- `base.html` is the shell (sidebar, header block, toast slot, content block). Pages set `title`, `header_actions` and `content` blocks. The browser title is "<Page> · Asuras CSV".
- `_macros.html` becomes the component library: `btn`, `badge`, `status_pill(job, progress)` (replaces `job_status`), `kpi`, `card`, `empty_state`, `field`, `sparkline(points)`, `rel_time`.
- New partials: `_overview_live.html`, `_asset_live.html`, `_jobs_rows.html`.
- `chart.html`, `export.html` and `asset_edit.html` are removed (their contents move into `asset_detail.html`).

### Server

- **Routes:** `/` → Overview, and `/assets` → the assets list (it currently renders at `/`). New: `/overview/live`, `/assets/{id}`, `/assets/{id}/live`, `/jobs/rows`. Old chart, export and edit GET pages respond with 303 redirects. All POST, CSV, ZIP and JSON endpoints are unchanged, except for where they redirect. Their redirects (currently all to `/`) go to the page the user came from and carry a flash. Forms send a hidden `next` field, as the job-cancel form already does. It's accepted only if it is a local path (starts with `/`, not `//`); otherwise it falls back to `/assets`. After a delete, the redirect always goes to `/assets`.
- **`redirect(url, notice=None)`:** extends the existing helper to set the flash cookie.
- **Overview data** comes from existing services: `StatsCache.get`, `latest_jobs_by_asset`, `list_recent(limit=5)`, the settings and `scheduler.next_run()`. Provider key status comes from `SettingsService` credentials and each provider's key requirement.
- **Sparklines:** a new `app/services/sparklines.py`.
  - `daily_closes(sf, days=30) -> dict[int, list[float]]` runs one query: `time_bucket('1 day', ts)`, `last(close, ts)`, grouped by asset and day, over the last 30 days.
  - It's cached with the same coalescing and stale-while-refresh pattern as `StatsCache`, with a 10-minute TTL.
  - A Jinja helper normalizes the points to an SVG `polyline`. It's green if the last value ≥ the first, red otherwise. Fewer than 2 points renders "—".
- **Jobs filter:** `list_recent` gains an optional status filter (`active` = `ACTIVE_STATUSES`, `failed` = `failed`).

## Testing

- **Update `test_web_pages.py` and `test_web_assets.py`:**
  - Overview renders, both empty and with assets and jobs.
  - `/assets` lists rows.
  - The detail page renders with and without candles.
  - Old URLs redirect with 303 to the detail page.
  - The flash cookie is set after POST and cleared after render.
  - The Jobs status filter works.
  - `/static/app.css` and the vendored scripts are served.
  - No page references an external CDN.
- **New `test_sparklines.py`** against the testcontainers TimescaleDB: the last close per day, the 30-day window, and an asset without candles.
- **Visual check before completion:** run under a separate compose project (`-p ohlcv-e2e`, a port other than 9016, never the live deployment). Use Playwright to screenshot every page in dark, light and ~390px mobile width, and check there's no horizontal page scroll and no console errors.

## Deployment

The live deployment is updated with `docker compose up -d --build app` only. The database and its volume are not touched.

# SaaS UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Pico.css UI with a Linear-style "Asuras CSV" SaaS interface (sidebar shell, light/dark themes, Overview dashboard with sparklines, asset detail page, toasts) without changing the download/export machinery.

**Architecture:** Server-rendered Jinja2 + htmx stays. A hand-written design system (`app/web/static/app.css` + `app.js`, no build step) replaces Pico; all third-party assets are vendored under `app/web/static/vendor/`. Shared view models move to `app/web/rows.py`, template helpers and flash/redirect helpers live in `app/web/ui.py`, the Overview gets its own router (`app/web/overview.py`), and two small services are added (`sparklines.py`, `sources.py`).

**Tech Stack:** Python 3.12, FastAPI 0.142 / Starlette 1.7 (`Jinja2Templates(context_processors=...)`, `StaticFiles`), Jinja2 3.1, htmx 2.0.4, Lightweight Charts 4.2.0, Inter (variable woff2), Lucide icons 0.544.0, TimescaleDB `time_bucket` + `last()`, pytest + testcontainers.

**Spec:** `docs/superpowers/specs/2026-10-02-saas-ui-redesign-design.md`

## Global Constraints

- Product name shown in the UI: **Asuras CSV**; browser title is `"<Page> · Asuras CSV"`.
- No CDN references in any rendered page: htmx 2.0.4, lightweight-charts 4.2.0, Inter and Lucide icons are served from `/static/vendor/`.
- No JS build step and no new Python dependencies.
- All POST, CSV (`/assets/{id}/export.csv`), ZIP (`/export.zip`) and JSON (`/assets/{id}/candles.json`) endpoints keep their URLs, parameters and response formats; only their redirect targets change.
- Times displayed are UTC.
- Polling rule everywhere: every 2 s while a relevant job is active (`queued`, `running`, `waiting`, `paused`), otherwise every 30 s.
- Flash cookie: name `flash`, `Max-Age=30`, `Path=/`, `SameSite=Lax`, `HttpOnly`, unsigned, URL-encoded, escaped on render, cleared by the next full HTML page (never by htmx requests).
- `next` redirect targets are accepted only if they start with `/` and not `//` or `/\`.
- Accent colors: `#5b6cff` (dark) / `#4f5bff` (light). Dark palette per mockup direction A.
- Never touch the live deployment (port 9016, project `asuras-csv`) during development; visual checks use `docker compose -p ohlcv-e2e` on port 9116.
- Run tests with `.venv/bin/pytest` (Docker must be running for testcontainers). Baseline: 290 passed.

## Review Focus

1. **Polling while the user interacts** — a 2 s poll of `/assets/rows` must not close an open ⋯ menu, untick checkboxes or undo filters. Pinned by Task 3's test that rows carry `id="menu-N" hx-preserve` and `id="sel-N" ... hx-preserve`; filters/selection are re-applied on `htmx:afterSwap` (checked in Task 10's browser pass).
2. **Flash cookie abuse/leaks** — a crafted cookie must render escaped, and htmx polls must not consume a toast before the page shows it. Pinned in Task 2 (`test_flash_is_escaped`, `test_post_sets_flash_and_next_page_shows_and_clears_it`).
3. **Open redirects through the new `next` fields** (Update now, Update all). Pinned in Task 2 (`test_update_rejects_offsite_next`, `test_safe_next`).
4. **Route collisions** — `/assets/new`, `/assets/rows`, `/assets/search` must keep working next to `/assets/{id}`, and `/assets/abc` must be a 404, not a 422. Pinned in Task 6 (`test_unknown_and_non_numeric_assets`).
5. **Assets with no candles or a removed provider** must render on Overview, Assets and detail pages without a 500. Pinned in Task 5 (`test_overview_survives_an_asset_with_an_unknown_provider_and_no_candles`) and Task 6 (`test_detail_page_for_an_asset_with_an_unknown_provider`).

---

## File Structure

| File | Responsibility |
|---|---|
| `app/web/ui.py` (new) | `templates` object, filters (`dt`, `duration`, `num`, `compact`, `ago`, `short_label`), globals (`icon`, `sparkline`, `ACTIVE_STATUSES`), `STATIC_VERSION`, context processor, `redirect`, `safe_next`, `FlashCleaner` middleware |
| `app/web/rows.py` (new) | `AssetRow`, `JobRow`, `load_asset_rows`, `load_asset_row`, `make_asset_row`, `job_rows` |
| `app/web/overview.py` (new) | `/` and `/overview/live` routes |
| `app/web/routes.py` | all other HTML routes (assets, detail, jobs, settings, nav status) |
| `app/services/sparklines.py` (new) | `daily_closes`, `SparklineCache` |
| `app/services/sources.py` (new) | `SourceStatus`, `source_statuses` |
| `app/services/jobs.py` | + `status_counts`, `last_done_by_asset`, filters on `list_recent` |
| `app/providers/base.py` | + `split_label` |
| `app/web/static/app.css`, `app.js`, `favicon.svg` (new) | design system and behaviour |
| `app/web/static/vendor/*` (new) | htmx, lightweight-charts, Inter, icon sprite, licenses |
| `scripts/build_icon_sprite.py` (new) | regenerates `vendor/icons.svg` |
| `app/web/templates/*` | rewritten; `chart.html`, `export.html`, `asset_edit.html` deleted; new `message.html`, `overview.html`, `_overview_live.html`, `_sources_card.html`, `_nav_status.html`, `asset_detail.html`, `_asset_parts.html`, `_asset_live.html`, `_jobs_tbody.html` |

---

### Task 1: Static foundation and app shell

**Files:**
- Create: `scripts/build_icon_sprite.py`, `app/web/static/app.css`, `app/web/static/app.js`, `app/web/static/favicon.svg`, `app/web/static/vendor/` (downloaded files), `app/web/ui.py`, `app/web/templates/_nav_status.html`, `tests/test_web_shell.py`, `tests/test_ui.py`, `tests/test_job_queries.py`
- Modify: `app/web/routes.py` (templates import, `/nav/status`), `app/main.py` (static mount, title), `app/services/jobs.py` (`status_counts`), `app/providers/base.py` (`split_label`), `app/web/templates/base.html`, `app/web/templates/chart.html` (local script), `pyproject.toml` (package-data), `tests/test_chart.py` (script path)

**Interfaces:**
- Produces: `app.web.ui.templates` (Jinja2Templates), `ui.STATIC_DIR`, `ui.STATIC_VERSION: str`, `ui.compact(n) -> str`, `ui.ago(value: datetime | None, now: datetime) -> str`, `ui.sparkline(points: list[float], width=96, height=24) -> Markup`, `ui.icon(name: str) -> Markup`; Jinja filters `dt`, `duration`, `num`, `compact`, `ago`, `short_label`; globals `icon`, `sparkline`, `ACTIVE_STATUSES`; context vars `now`, `static_version` on every template; `jobs.status_counts(sf) -> dict[str, int]`; `providers.base.split_label(label: str) -> tuple[str, str]`; base.html blocks `crumb`, `heading`, `heading_extra`, `actions`, `content`, `scripts` and child-template variables `page_title`, `active_nav` (`overview|assets|jobs|settings`).

- [ ] **Step 1: Vendor third-party files**

```bash
cd /Users/minione/repos/asuras-csv
mkdir -p app/web/static/vendor/inter
curl -fsSL -o app/web/static/vendor/htmx-2.0.4.min.js https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js
curl -fsSL -o app/web/static/vendor/lightweight-charts-4.2.0.standalone.production.js https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js
curl -fsSL -o app/web/static/vendor/inter/inter-latin-wght-normal.woff2 https://cdn.jsdelivr.net/npm/@fontsource-variable/inter@5.2.8/files/inter-latin-wght-normal.woff2
curl -fsSL -o app/web/static/vendor/inter/LICENSE.txt https://cdn.jsdelivr.net/npm/@fontsource-variable/inter@5.2.8/LICENSE
curl -fsSL -o app/web/static/vendor/LICENSE-lucide.txt https://cdn.jsdelivr.net/npm/lucide-static@0.544.0/LICENSE
ls -la app/web/static/vendor app/web/static/vendor/inter
```
Expected: htmx ≈ 50 KB, lightweight-charts ≈ 160 KB, woff2 ≈ 48 KB, two license files.

- [ ] **Step 2: Create the icon sprite builder and run it**

`scripts/build_icon_sprite.py`:
```python
"""Download the Lucide icons the UI uses and write them as one SVG sprite.

Run once after changing ICONS: .venv/bin/python scripts/build_icon_sprite.py (the output is committed).
"""
from __future__ import annotations

import re
import urllib.request
from pathlib import Path

VERSION = "0.544.0"
ICONS = [
    "activity", "arrow-left", "chart-candlestick", "check", "circle-alert", "database", "download", "ellipsis",
    "inbox", "layout-dashboard", "lock", "menu", "pencil", "plus", "refresh-cw", "search", "settings", "sun-moon",
    "trash-2", "x",
]
OUT = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "vendor" / "icons.svg"


def main() -> None:
    symbols = []
    for name in ICONS:
        url = f"https://cdn.jsdelivr.net/npm/lucide-static@{VERSION}/icons/{name}.svg"
        svg = urllib.request.urlopen(url, timeout=30).read().decode()
        inner = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S).group(1)
        symbols.append(f'<symbol id="i-{name}" viewBox="0 0 24 24">{" ".join(inner.split())}</symbol>')
    OUT.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg">\n<!-- Lucide v{VERSION} (ISC), see LICENSE-lucide.txt -->\n'
        + "\n".join(symbols)
        + "\n</svg>\n"
    )
    print(f"wrote {len(symbols)} icons to {OUT}")


if __name__ == "__main__":
    main()
```
Run: `.venv/bin/python scripts/build_icon_sprite.py`
Expected: `wrote 20 icons to .../app/web/static/vendor/icons.svg`

- [ ] **Step 3: Create `app/web/static/favicon.svg`**

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6d7cff"/><stop offset="1" stop-color="#4ade80"/></linearGradient></defs><rect width="32" height="32" rx="8" fill="url(#g)"/></svg>
```

- [ ] **Step 4: Create `app/web/static/app.css`**

```css
/* Asuras CSV design system: tokens, layout shell and components. Hand-written, no build step. */
@font-face {
  font-family: "Inter";
  src: url("vendor/inter/inter-latin-wght-normal.woff2") format("woff2");
  font-weight: 100 900;
  font-style: normal;
  font-display: swap;
}

/* ---------- Tokens (light) ---------- */
:root {
  color-scheme: light;
  --font: "Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --radius: 8px;
  --radius-sm: 6px;
  --bg: #f7f8fa;
  --bg-sidebar: #ffffff;
  --surface: #ffffff;
  --surface-2: #f3f4f7;
  --surface-hover: #f3f4f7;
  --segment-active: #ffffff;
  --border: #e3e5ea;
  --border-subtle: #eef0f3;
  --text: #2a2f3a;
  --text-strong: #0f1218;
  --text-muted: #6b7280;
  --text-faint: #9aa0ab;
  --accent: #4f5bff;
  --accent-hover: #3f4bef;
  --accent-contrast: #ffffff;
  --accent-soft: #eef0ff;
  --accent-border: #c9cffd;
  --success: #15803d; --success-soft: #dcfce7;
  --info: #3949d6; --info-soft: #e0e7ff;
  --warning: #a16207; --warning-soft: #fef3c7;
  --danger: #b91c1c; --danger-soft: #fee2e2;
  --neutral: #4b5563; --neutral-soft: #f1f2f5;
  --up: #16a34a; --down: #dc2626;
  --shadow: 0 1px 2px rgb(16 24 40 / 0.05);
  --shadow-lg: 0 12px 32px rgb(16 24 40 / 0.16);
  --chart-bg: #ffffff; --chart-text: #373c44; --chart-grid: #eef0f3;
}

/* ---------- Tokens (dark): OS preference unless the user picked light, or an explicit dark choice ---------- */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #0b0d12; --bg-sidebar: #0f1218; --surface: #11141b; --surface-2: #0b0d12; --surface-hover: #181c26;
    --segment-active: #1d2230;
    --border: #1d212b; --border-subtle: #161a22;
    --text: #d6d9e0; --text-strong: #ffffff; --text-muted: #7c8294; --text-faint: #565c6b;
    --accent: #5b6cff; --accent-hover: #6e7dff; --accent-contrast: #ffffff; --accent-soft: #161b2e; --accent-border: #2a3560;
    --success: #4ade80; --success-soft: #10291e;
    --info: #7aa2ff; --info-soft: #172446;
    --warning: #f5b942; --warning-soft: #2d2412;
    --danger: #f87171; --danger-soft: #2e1515;
    --neutral: #8a90a0; --neutral-soft: #1a1d25;
    --up: #4ade80; --down: #f87171;
    --shadow: none; --shadow-lg: 0 12px 32px rgb(0 0 0 / 0.5);
    --chart-bg: #11141b; --chart-text: #c2c7d0; --chart-grid: #1b1f28;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #0b0d12; --bg-sidebar: #0f1218; --surface: #11141b; --surface-2: #0b0d12; --surface-hover: #181c26;
  --segment-active: #1d2230;
  --border: #1d212b; --border-subtle: #161a22;
  --text: #d6d9e0; --text-strong: #ffffff; --text-muted: #7c8294; --text-faint: #565c6b;
  --accent: #5b6cff; --accent-hover: #6e7dff; --accent-contrast: #ffffff; --accent-soft: #161b2e; --accent-border: #2a3560;
  --success: #4ade80; --success-soft: #10291e;
  --info: #7aa2ff; --info-soft: #172446;
  --warning: #f5b942; --warning-soft: #2d2412;
  --danger: #f87171; --danger-soft: #2e1515;
  --neutral: #8a90a0; --neutral-soft: #1a1d25;
  --up: #4ade80; --down: #f87171;
  --shadow: none; --shadow-lg: 0 12px 32px rgb(0 0 0 / 0.5);
  --chart-bg: #11141b; --chart-text: #c2c7d0; --chart-grid: #1b1f28;
}

/* ---------- Base ---------- */
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--text); font: 400 13px/1.5 var(--font); -webkit-font-smoothing: antialiased; }
a { color: inherit; text-decoration: none; }
a:hover { color: var(--text-strong); }
.link, p a { color: var(--accent); }
h1, h2, h3 { margin: 0; color: var(--text-strong); font-weight: 600; letter-spacing: -0.01em; }
h1 { font-size: 17px; }
h2 { font-size: 13.5px; }
h3 { font-size: 13px; }
p { margin: 0; }
code { font-family: var(--mono); font-size: 12px; padding: 0 4px; border: 1px solid var(--border-subtle); border-radius: 4px; background: var(--surface-2); }
[hidden] { display: none !important; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.icon { width: 16px; height: 16px; flex: none; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; vertical-align: -3px; }
.muted { color: var(--text-muted); }
.small { font-size: 11.5px; }
.tabular { font-variant-numeric: tabular-nums; }
.spacer { flex: 1; }
.cap { text-transform: capitalize; }
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

/* ---------- Shell ---------- */
.shell { display: grid; grid-template-columns: 232px minmax(0, 1fr); min-height: 100vh; }
.sidebar { position: sticky; top: 0; height: 100vh; display: flex; flex-direction: column; gap: 2px; padding: 14px 10px; overflow-y: auto; background: var(--bg-sidebar); border-right: 1px solid var(--border); }
.brand { display: flex; align-items: center; gap: 9px; padding: 4px 8px 16px; font-size: 14px; font-weight: 600; color: var(--text-strong); }
.brand-mark { width: 22px; height: 22px; flex: none; border-radius: 6px; background: linear-gradient(135deg, #6d7cff, #4ade80); }
.nav { display: flex; flex-direction: column; gap: 2px; }
.nav-item { display: flex; align-items: center; gap: 10px; padding: 7px 9px; border-radius: var(--radius-sm); color: var(--text-muted); font-weight: 500; }
.nav-item:hover, .nav-item.active { background: var(--surface-hover); color: var(--text-strong); }
.nav-count { margin-left: auto; min-width: 18px; padding: 0 6px; border-radius: 99px; background: var(--info-soft); color: var(--info); font-size: 11px; text-align: center; font-variant-numeric: tabular-nums; }
.sidebar-foot { margin-top: auto; padding: 12px 9px 0; display: flex; flex-direction: column; align-items: flex-start; gap: 8px; border-top: 1px solid var(--border); color: var(--text-muted); font-size: 11.5px; }
.sidebar-foot .btn { margin-left: -9px; }
.sidebar-status { display: flex; flex-direction: column; gap: 2px; }
.dot { display: inline-block; width: 7px; height: 7px; margin-right: 6px; border-radius: 50%; background: var(--text-faint); }
.dot-on { background: var(--success); }
.main { min-width: 0; display: flex; flex-direction: column; }
.topbar { display: none; }
.sidebar-backdrop { display: none; }
.page-header { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; min-height: 58px; padding: 12px 24px; border-bottom: 1px solid var(--border); }
.page-title { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; min-width: 0; flex: 1; }
.crumb { color: var(--text-muted); white-space: nowrap; }
.page-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.page-body { width: 100%; max-width: 1400px; padding: 20px 24px 40px; display: flex; flex-direction: column; gap: 16px; }
.page-narrow { max-width: 760px; width: 100%; }

/* ---------- Layout helpers ---------- */
.stack { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.stack-sm { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.grid-2 { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); gap: 16px; align-items: start; }
.detail-grid { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 16px; align-items: start; }

/* ---------- Card ---------- */
.card { min-width: 0; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); }
.card-header { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; padding: 11px 16px; border-bottom: 1px solid var(--border); }
.card-header p { margin-top: 2px; color: var(--text-muted); font-size: 11.5px; }
.card-link { margin-left: auto; color: var(--text-muted); font-size: 12px; white-space: nowrap; }
.card-body { padding: 14px 16px; }
.card-scroll { overflow-x: auto; }
.step-num { display: inline-flex; align-items: center; justify-content: center; flex: none; width: 22px; height: 22px; border-radius: 50%; background: var(--accent-soft); color: var(--accent); font-size: 11.5px; font-weight: 600; }

/* ---------- KPI ---------- */
.kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.kpi { padding: 13px 16px; }
.kpi-label { color: var(--text-muted); font-size: 12px; }
.kpi-value { margin: 4px 0 2px; color: var(--text-strong); font-size: 24px; font-weight: 600; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.kpi-sub { color: var(--text-faint); font-size: 11.5px; }

/* ---------- Table ---------- */
.table { width: 100%; border-collapse: collapse; }
.table th { padding: 8px 12px; border-bottom: 1px solid var(--border); color: var(--text-muted); font-size: 11.5px; font-weight: 500; text-align: left; white-space: nowrap; }
.table td { padding: 9px 12px; border-bottom: 1px solid var(--border-subtle); vertical-align: middle; white-space: nowrap; font-variant-numeric: tabular-nums; }
.table th:first-child, .table td:first-child { padding-left: 16px; }
.table th:last-child, .table td:last-child { padding-right: 16px; }
.table tbody tr:last-child td { border-bottom: 0; }
.table tbody tr:hover td { background: var(--surface-hover); }
.table tr[data-href] { cursor: pointer; }
.table tr.selected td { background: var(--accent-soft); }
.table .num { text-align: right; }
.table .wrap { white-space: normal; min-width: 180px; }
.table-empty td { padding: 28px 16px; color: var(--text-muted); text-align: center; }
.sym { color: var(--text-strong); font-weight: 500; }
.sub { display: block; color: var(--text-muted); font-size: 11.5px; }

/* ---------- Badge, progress, status ---------- */
.badge { display: inline-flex; align-items: center; gap: 6px; padding: 2px 8px; border-radius: 99px; background: var(--neutral-soft); color: var(--neutral); font-size: 11.5px; font-weight: 500; line-height: 18px; white-space: nowrap; }
.badge-success { background: var(--success-soft); color: var(--success); }
.badge-info { background: var(--info-soft); color: var(--info); }
.badge-warning { background: var(--warning-soft); color: var(--warning); }
.badge-danger { background: var(--danger-soft); color: var(--danger); }
.badge-dot::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.progress { display: inline-block; width: 64px; height: 4px; overflow: hidden; border-radius: 99px; background: color-mix(in srgb, currentColor 22%, transparent); }
.progress > span { display: block; height: 100%; border-radius: inherit; background: currentColor; }
.status-region { display: inline-flex; flex-direction: column; align-items: flex-start; }
.status-detail { display: block; margin-top: 3px; color: var(--text-muted); font-size: 11.5px; white-space: normal; }
.status-error { display: block; max-width: 360px; margin-top: 3px; color: var(--danger); font-size: 11.5px; white-space: normal; }

/* ---------- Buttons ---------- */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 32px; padding: 0 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--text-strong); box-shadow: var(--shadow); font: 500 12.5px/1 var(--font); white-space: nowrap; cursor: pointer; }
.btn:hover { background: var(--surface-hover); color: var(--text-strong); }
.btn-primary { border-color: var(--accent); background: var(--accent); color: var(--accent-contrast); }
.btn-primary:hover { border-color: var(--accent-hover); background: var(--accent-hover); color: var(--accent-contrast); }
.btn-ghost { border-color: transparent; background: transparent; box-shadow: none; color: var(--text-muted); }
.btn-danger { color: var(--danger); }
.btn-danger-solid { border-color: var(--danger); background: var(--danger); color: #ffffff; }
.btn-danger-solid:hover { border-color: var(--danger); background: var(--danger); color: #ffffff; filter: brightness(1.08); }
.btn-sm { height: 26px; padding: 0 9px; font-size: 12px; }
.btn-icon { width: 32px; padding: 0; }
.btn-sm.btn-icon { width: 26px; }
form.inline { display: inline; margin: 0; }

/* ---------- Forms ---------- */
.field { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.field > label, .label { color: var(--text); font-size: 12px; font-weight: 500; }
.input { width: 100%; height: 34px; padding: 0 10px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-2); color: var(--text-strong); font: 13px var(--font); }
.input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
select.input { appearance: none; padding-right: 28px; background-image: linear-gradient(45deg, transparent 50%, var(--text-muted) 50%), linear-gradient(135deg, var(--text-muted) 50%, transparent 50%); background-position: calc(100% - 15px) 50%, calc(100% - 10px) 50%; background-size: 5px 5px; background-repeat: no-repeat; }
.input-sm { height: 30px; font-size: 12.5px; }
.hint { color: var(--text-muted); font-size: 11.5px; }
.field-error { color: var(--danger); font-size: 11.5px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px 16px; }
.form-grid .full { grid-column: 1 / -1; }
.form-actions { display: flex; align-items: center; gap: 8px; }
.check { display: inline-flex; align-items: center; gap: 8px; font-size: 12.5px; cursor: pointer; }
input[type="checkbox"] { width: 15px; height: 15px; margin: 0; accent-color: var(--accent); cursor: pointer; }
input.switch { appearance: none; position: relative; flex: none; width: 32px; height: 18px; border-radius: 99px; background: var(--border); transition: background 0.15s; }
input.switch::after { content: ""; position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; border-radius: 50%; background: #ffffff; transition: transform 0.15s; }
input.switch:checked { background: var(--accent); }
input.switch:checked::after { transform: translateX(14px); }

/* ---------- Segmented control / tabs ---------- */
.segmented { display: inline-flex; gap: 2px; padding: 2px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); }
.segmented > a, .segmented > button { display: inline-flex; align-items: center; gap: 6px; height: 26px; padding: 0 11px; border: 0; border-radius: 6px; background: transparent; color: var(--text-muted); font: 500 12px var(--font); cursor: pointer; }
.segmented > .active { background: var(--segment-active); color: var(--text-strong); box-shadow: var(--shadow); }
.segmented .count { color: var(--text-faint); font-variant-numeric: tabular-nums; }

/* ---------- Add-asset source cards and search results ---------- */
.source-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; }
.source-option { position: relative; display: flex; flex-direction: column; gap: 3px; padding: 11px 13px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface-2); cursor: pointer; }
.source-option input { position: absolute; opacity: 0; pointer-events: none; }
.source-option:has(input:checked) { border-color: var(--accent); background: var(--accent-soft); box-shadow: 0 0 0 1px var(--accent); }
.source-option:has(input:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
.source-name { color: var(--text-strong); font-weight: 600; }
.source-meta { color: var(--text-muted); font-size: 11.5px; }
.result-list { margin: 10px 0 0; padding: 0; list-style: none; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius-sm); }
.result-list li + li { border-top: 1px solid var(--border-subtle); }
.result-list a { display: flex; align-items: baseline; gap: 10px; padding: 8px 12px; }
.result-list a:hover { background: var(--accent-soft); }

/* ---------- Row menu (positioned by app.js because table cards scroll) ---------- */
.menu { display: inline-block; }
.menu > summary { list-style: none; }
.menu > summary::-webkit-details-marker { display: none; }
.menu-list { position: fixed; z-index: 30; display: flex; flex-direction: column; min-width: 172px; padding: 4px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow-lg); }
.menu-list form { margin: 0; }
.menu-list a, .menu-list button { display: flex; align-items: center; gap: 8px; width: 100%; padding: 7px 9px; border: 0; border-radius: 5px; background: transparent; color: var(--text); font: 12.5px var(--font); text-align: left; cursor: pointer; }
.menu-list a:hover, .menu-list button:hover { background: var(--surface-hover); color: var(--text-strong); }
.menu-list .danger { color: var(--danger); }
.menu-list hr { margin: 4px 0; border: 0; border-top: 1px solid var(--border); }

/* ---------- Assets list toolbars ---------- */
.filter-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.filter-bar .input { width: auto; }
.search-input { min-width: 220px; }
.bulk-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 8px 12px; border: 1px solid var(--accent-border); border-radius: var(--radius); background: var(--accent-soft); color: var(--accent); }
.bulk-bar label { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
.bulk-bar .input { width: auto; }

/* ---------- Alerts, callouts, empty states ---------- */
.alert { display: flex; align-items: flex-start; gap: 8px; padding: 9px 12px; border: 1px solid; border-radius: var(--radius-sm); font-size: 12.5px; }
.alert-danger { border-color: color-mix(in srgb, var(--danger) 30%, transparent); background: var(--danger-soft); color: var(--danger); }
.callout { display: flex; align-items: center; gap: 8px; padding: 9px 12px; border: 1px solid var(--accent-border); border-radius: var(--radius-sm); background: var(--accent-soft); color: var(--accent); font-size: 12.5px; }
.callout-muted { border-color: var(--border); background: var(--surface-2); color: var(--text-muted); }
.empty { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 48px 24px; text-align: center; }
.empty .icon { width: 28px; height: 28px; color: var(--text-faint); }
.empty p { max-width: 440px; margin-bottom: 6px; color: var(--text-muted); }
.confirm { width: 100%; max-width: 520px; margin: 24px auto; }
.danger-title { color: var(--danger); }

/* ---------- Key/value, list rows, chart, sparkline ---------- */
.kv { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px 16px; margin: 0; padding: 14px 16px; font-size: 12.5px; }
.kv dt { color: var(--text-muted); }
.kv dd { margin: 0; color: var(--text-strong); overflow-wrap: anywhere; }
.list-row { display: flex; align-items: center; gap: 8px; padding: 10px 16px; border-bottom: 1px solid var(--border-subtle); }
.list-row:last-child { border-bottom: 0; }
.list-row > .badge { margin-left: auto; }
.chart { width: 100%; height: 420px; }
.chart-status { color: var(--text-muted); font-size: 11.5px; }
.spark { display: block; }
.spark polyline { fill: none; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; }
.spark-up polyline { stroke: var(--up); }
.spark-down polyline { stroke: var(--down); }

/* ---------- Toasts ---------- */
.toast-region { position: fixed; right: 20px; bottom: 20px; z-index: 50; display: flex; flex-direction: column; gap: 8px; }
.toast { display: flex; align-items: center; gap: 10px; min-width: 260px; max-width: 420px; padding: 10px 12px; border: 1px solid var(--border); border-left: 3px solid var(--success); border-radius: var(--radius); background: var(--surface); color: var(--text-strong); box-shadow: var(--shadow-lg); animation: toast-in 0.18s ease-out; }
.toast > .icon { color: var(--success); }
.toast-close { margin-left: auto; padding: 2px; border: 0; background: none; color: var(--text-muted); cursor: pointer; }
@keyframes toast-in { from { opacity: 0; transform: translateY(8px); } }

/* ---------- Responsive ---------- */
@media (max-width: 1100px) {
  .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .grid-2, .detail-grid { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 900px) {
  .shell { grid-template-columns: minmax(0, 1fr); }
  .sidebar { position: fixed; z-index: 40; top: 0; left: 0; width: 248px; transform: translateX(-100%); transition: transform 0.2s ease; }
  body.sidebar-open .sidebar { transform: none; box-shadow: var(--shadow-lg); }
  body.sidebar-open .sidebar-backdrop { display: block; position: fixed; inset: 0; z-index: 35; background: rgb(0 0 0 / 0.4); }
  .topbar { position: sticky; top: 0; z-index: 20; display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-bottom: 1px solid var(--border); background: var(--bg-sidebar); }
  .topbar .brand { padding: 0; }
  .page-header { padding: 12px 16px; }
  .page-body { padding: 16px 16px 32px; }
  .form-grid { grid-template-columns: minmax(0, 1fr); }
  .search-input { flex: 1; min-width: 0; }
  .chart { height: 320px; }
}
@media (max-width: 520px) {
  .kpi-value { font-size: 20px; }
  .toast-region { left: 16px; right: 16px; }
  .toast { min-width: 0; }
}
```

- [ ] **Step 5: Create `app/web/static/app.js`**

```js
// Asuras CSV front-end behaviour: theme, row menus, bulk selection, table filters, toasts, mobile sidebar and
// the poll-failure badge. Plain JS, no build step. Event delegation means htmx swaps need no re-binding.
(function () {
  "use strict";
  var root = document.documentElement;
  var darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

  function effectiveTheme() {
    return root.dataset.theme || (darkQuery.matches ? "dark" : "light");
  }
  function announceTheme() {
    document.dispatchEvent(new CustomEvent("themechange", { detail: effectiveTheme() }));
  }
  function toggleTheme() {
    var next = effectiveTheme() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (e) { /* storage blocked: the choice lasts for this page */ }
    announceTheme();
  }
  darkQuery.addEventListener("change", function () { if (!root.dataset.theme) announceTheme(); });

  function closeMenus(except) {
    document.querySelectorAll("details.menu[open]").forEach(function (menu) {
      if (!except || !menu.contains(except)) menu.open = false;
    });
  }
  // Menus sit inside horizontally scrolling table cards, so the list is position: fixed and placed here.
  document.addEventListener("toggle", function (ev) {
    var menu = ev.target;
    if (!(menu instanceof HTMLDetailsElement) || !menu.classList.contains("menu") || !menu.open) return;
    var list = menu.querySelector(".menu-list");
    var r = menu.querySelector("summary").getBoundingClientRect();
    list.style.top = Math.max(8, Math.min(r.bottom + 4, window.innerHeight - list.offsetHeight - 8)) + "px";
    list.style.left = Math.max(8, r.right - list.offsetWidth) + "px";
  }, true);
  window.addEventListener("scroll", function (ev) {
    if (!(ev.target instanceof Element && ev.target.closest(".menu-list"))) closeMenus(null);
  }, true);

  function boxes() { return document.querySelectorAll('input[name="ids"]'); }
  function syncSelection() {
    var bar = document.getElementById("zip-form");
    if (!bar) return;
    var all = boxes(), checked = 0;
    all.forEach(function (box) {
      var row = box.closest("tr");
      if (row) row.classList.toggle("selected", box.checked);
      if (box.checked) checked++;
    });
    bar.hidden = checked === 0;
    document.getElementById("bulk-count").textContent = checked + " selected";
    var head = document.getElementById("select-all");
    if (head) {
      head.checked = all.length > 0 && checked === all.length;
      head.indeterminate = checked > 0 && checked < all.length;
    }
  }

  function applyFilters() {
    var bar = document.querySelector("[data-filters]");
    if (!bar) return;
    var text = bar.querySelector('[data-filter="text"]').value.trim().toLowerCase();
    var provider = bar.querySelector('[data-filter="provider"]').value;
    var status = bar.querySelector('[data-filter="status"]').value;
    var rows = document.querySelectorAll("tr[data-symbol]"), shown = 0;
    rows.forEach(function (row) {
      var match = (!text || row.dataset.symbol.indexOf(text) !== -1) &&
        (!provider || row.dataset.provider === provider) &&
        (!status || row.dataset.status === status);
      row.hidden = !match;
      if (match) shown++;
    });
    var empty = document.getElementById("filter-empty");
    if (empty) empty.hidden = shown > 0 || rows.length === 0;
  }

  function setReconnecting(on) {
    var badge = document.getElementById("reconnecting");
    if (badge) badge.hidden = !on;
  }

  document.addEventListener("click", function (ev) {
    var t = ev.target;
    if (!(t instanceof Element)) return;
    if (t.closest("[data-theme-toggle]")) { toggleTheme(); return; }
    if (t.closest("[data-sidebar-toggle]")) { document.body.classList.toggle("sidebar-open"); return; }
    var toastClose = t.closest(".toast-close");
    if (toastClose) { toastClose.closest(".toast").remove(); return; }
    if (t.closest("[data-clear-selection]")) {
      boxes().forEach(function (box) { box.checked = false; });
      syncSelection();
      return;
    }
    closeMenus(t);
    var row = t.closest("tr[data-href]");
    if (row && !t.closest("a, button, input, label, select, summary, details, form")) window.location.href = row.dataset.href;
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") { closeMenus(null); document.body.classList.remove("sidebar-open"); }
  });
  document.addEventListener("change", function (ev) {
    var t = ev.target;
    if (t.id === "select-all") {
      boxes().forEach(function (box) { if (!box.closest("tr").hidden) box.checked = t.checked; });
    }
    if (t.name === "ids" || t.id === "select-all") syncSelection();
    if (t.closest && t.closest("[data-filters]")) applyFilters();
  });
  document.addEventListener("input", function (ev) {
    if (ev.target.closest && ev.target.closest("[data-filters]")) applyFilters();
  });
  document.addEventListener("submit", function (ev) {
    if (ev.target.id === "zip-form" && !document.querySelector('input[name="ids"]:checked')) ev.preventDefault();
  });
  document.addEventListener("htmx:afterSwap", function () { applyFilters(); syncSelection(); });
  document.addEventListener("htmx:sendError", function () { setReconnecting(true); });
  document.addEventListener("htmx:responseError", function () { setReconnecting(true); });
  document.addEventListener("htmx:afterRequest", function (ev) { if (ev.detail.successful) setReconnecting(false); });

  document.querySelectorAll(".toast").forEach(function (toast) { setTimeout(function () { toast.remove(); }, 4000); });
  applyFilters();
  syncSelection();
})();
```

- [ ] **Step 6: Write the failing tests**

`tests/test_ui.py`:
```python
from datetime import UTC, datetime, timedelta

import pytest

from app.providers.base import split_label
from app.web import ui

NOW = datetime(2024, 1, 10, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "n, text",
    [(0, "0"), (999, "999"), (1000, "1K"), (12_480, "12.5K"), (48_200_000, "48.2M"), (999_960, "1M"), (2_500_000_000, "2.5B")],
)
def test_compact(n, text):
    assert ui.compact(n) == text


@pytest.mark.parametrize(
    "delta, text",
    [
        (timedelta(seconds=-30), "just now"),
        (timedelta(seconds=30), "just now"),
        (timedelta(minutes=5), "5 min ago"),
        (timedelta(hours=3), "3 h ago"),
        (timedelta(days=2), "2 d ago"),
        (timedelta(days=90), "2023-10-12"),
    ],
)
def test_ago(delta, text):
    assert ui.ago(NOW - delta, NOW) == text


def test_ago_none():
    assert ui.ago(None, NOW) == "—"


def test_sparkline_needs_two_points():
    assert "—" in ui.sparkline([]) and "—" in ui.sparkline([1.0])


def test_sparkline_direction_and_points():
    up = ui.sparkline([1.0, 2.0, 3.0])
    assert 'class="spark spark-up"' in up and 'points="0.0,22.0 48.0,12.0 96.0,2.0"' in up
    assert "spark-down" in ui.sparkline([3.0, 1.0])


def test_flat_sparkline_is_centred():
    assert 'points="0.0,10.0 10.0,10.0"' in ui.sparkline([5.0, 5.0], width=10, height=20)


def test_icon_uses_the_versioned_sprite():
    assert f'href="/static/vendor/icons.svg?v={ui.STATIC_VERSION}#i-plus"' in ui.icon("plus")


@pytest.mark.parametrize(
    "label, parts",
    [("Binance (crypto)", ("Binance", "crypto")), ("Twelve Data (US stocks, forex, metals)", ("Twelve Data", "US stocks, forex, metals")), ("Fake", ("Fake", ""))],
)
def test_split_label(label, parts):
    assert split_label(label) == parts
```

`tests/test_job_queries.py`:
```python
from app.providers.base import ProviderRegistry
from app.services import jobs
from tests.fakes import FakeProvider, make_asset


async def test_status_counts(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock)])
    a = await make_asset(sf)
    b = await make_asset(sf, provider_symbol="B", jesse_symbol="B-USD")
    await jobs.enqueue(sf, registry, clock, a.id, "backfill")
    job = await jobs.enqueue(sf, registry, clock, b.id, "backfill")
    await jobs.cancel(sf, clock, job.id)
    assert await jobs.status_counts(sf) == {"queued": 1, "cancelled": 1}
```

`tests/test_web_shell.py`:
```python
from app.providers.base import ProviderRegistry
from app.services import jobs
from app.web import ui
from tests.fakes import FakeProvider, make_asset

STATIC = [
    "/static/app.css",
    "/static/app.js",
    "/static/favicon.svg",
    "/static/vendor/icons.svg",
    "/static/vendor/htmx-2.0.4.min.js",
    "/static/vendor/lightweight-charts-4.2.0.standalone.production.js",
    "/static/vendor/inter/inter-latin-wght-normal.woff2",
]


async def test_static_files_are_served(client):
    for path in STATIC:
        assert (await client.get(path)).status_code == 200, path


async def test_shell_has_sidebar_and_only_local_assets(client):
    page = (await client.get("/settings")).text
    assert "Asuras CSV" in page and 'class="sidebar"' in page
    assert f"/static/app.css?v={ui.STATIC_VERSION}" in page and "/static/vendor/htmx-2.0.4.min.js" in page
    for href in ('href="/"', 'href="/assets"', 'href="/jobs"', 'href="/settings"'):
        assert href in page
    assert "unpkg.com" not in page and "cdn.jsdelivr.net" not in page and "pico" not in page


async def test_nav_status_shows_active_job_count_and_schedule(client, sf, clock):
    idle = (await client.get("/nav/status")).text
    assert 'id="nav-status"' in idle and "Scheduler off" in idle
    assert '<span id="nav-jobs-count" class="nav-count" hx-swap-oob="true" hidden></span>' in idle
    asset = await make_asset(sf)
    await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, asset.id, "backfill")
    busy = (await client.get("/nav/status")).text
    assert '<span id="nav-jobs-count" class="nav-count" hx-swap-oob="true">1</span>' in busy
```

- [ ] **Step 7: Run the new tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ui.py tests/test_job_queries.py tests/test_web_shell.py -q`
Expected: collection errors / FAIL (`app.web.ui` and `split_label` do not exist).

- [ ] **Step 8: Add `split_label` to `app/providers/base.py`** (below `ProviderRegistry`)

```python
def split_label(label: str) -> tuple[str, str]:
    """'Binance (crypto)' -> ('Binance', 'crypto'); a label without parentheses has no coverage part."""
    name, _, coverage = label.partition(" (")
    return name, coverage.rstrip(")")
```

- [ ] **Step 9: Add `status_counts` to `app/services/jobs.py`** (after `latest_jobs_by_asset`)

```python
async def status_counts(sf: SessionFactory) -> dict[str, int]:
    async with sf() as s:
        rows = await s.execute(select(Job.status, func.count()).group_by(Job.status))
        return {status: count for status, count in rows}
```
(`func` and `select` are already imported in this module.)

- [ ] **Step 10: Create `app/web/ui.py`**

```python
"""Jinja environment and template helpers shared by the HTML routes."""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.models import ACTIVE_STATUSES
from app.providers.base import split_label
from app.services.progress import format_duration

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"


def _static_version() -> str:
    """Short content hash of the files that change with the UI, used as a cache-busting query string."""
    digest = hashlib.sha256()
    for name in ("app.css", "app.js", "vendor/icons.svg"):
        digest.update((STATIC_DIR / name).read_bytes())
    return digest.hexdigest()[:10]


STATIC_VERSION = _static_version()
_UNITS = (("B", 1e9), ("M", 1e6), ("K", 1e3))


def fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "—"


def compact(n: float) -> str:
    """48_200_000 -> '48.2M'; rounds up into the next unit instead of showing '1000K'."""
    for i, (suffix, size) in enumerate(_UNITS):
        if abs(n) >= size:
            value = round(n / size, 1)
            if value >= 1000 and i > 0:
                suffix, value = _UNITS[i - 1][0], round(n / _UNITS[i - 1][1], 1)
            return f"{value:g}{suffix}"
    return f"{n:,.0f}"


def ago(value: datetime | None, now: datetime) -> str:
    if value is None:
        return "—"
    seconds = (now - value).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    days = int(seconds // 86400)
    return f"{days} d ago" if days < 60 else value.strftime("%Y-%m-%d")


def sparkline(points: list[float], width: int = 96, height: int = 24) -> Markup:
    if len(points) < 2:
        return Markup('<span class="muted">—</span>')
    lo, hi = min(points), max(points)
    step = width / (len(points) - 1)

    def y(p: float) -> float:
        return height / 2 if hi == lo else height - 2 - (p - lo) / (hi - lo) * (height - 4)

    coords = " ".join(f"{i * step:.1f},{y(p):.1f}" for i, p in enumerate(points))
    tone = "up" if points[-1] >= points[0] else "down"
    return Markup(
        f'<svg class="spark spark-{tone}" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'aria-hidden="true"><polyline points="{coords}"/></svg>'
    )


def icon(name: str) -> Markup:
    return Markup(
        f'<svg class="icon" aria-hidden="true"><use href="/static/vendor/icons.svg?v={STATIC_VERSION}#i-{name}"/></svg>'
    )


def _page_context(request: Request) -> dict:
    return {"now": request.app.state.services.clock.now(), "static_version": STATIC_VERSION}


templates = Jinja2Templates(directory=str(WEB_DIR / "templates"), context_processors=[_page_context])
templates.env.filters |= {
    "dt": fmt_dt,
    "duration": format_duration,
    "num": lambda n: f"{n:,}",
    "compact": compact,
    "ago": ago,
    "short_label": lambda label: split_label(label)[0],
}
templates.env.globals |= {"icon": icon, "sparkline": sparkline, "ACTIVE_STATUSES": ACTIVE_STATUSES}
```

- [ ] **Step 11: Point `app/web/routes.py` at `ui.templates` and add `/nav/status`**

Replace these lines near the top:
```python
from fastapi.templating import Jinja2Templates
...
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["dt"] = lambda value: value.strftime("%Y-%m-%d %H:%M") if value else "—"
templates.env.filters["duration"] = format_duration
```
with:
```python
from app.web.ui import templates
```
Remove the now-unused `Path` and `Jinja2Templates` imports (keep `format_duration` only if still referenced; it is not after this change, so drop it from the `progress` import line).

Add this route after `cancel_job`:
```python
@router.get("/nav/status", response_class=HTMLResponse)
async def nav_status(request: Request):
    svc = services(request)
    counts = await jobs.status_counts(svc.sf)
    current = await svc.settings.load()
    context = {
        "active_jobs": sum(counts.get(status, 0) for status in ACTIVE_STATUSES),
        "schedule_enabled": current.schedule_enabled,
        "next_run": svc.scheduler.next_run() if svc.scheduler else None,
    }
    return templates.TemplateResponse(request, "_nav_status.html", context)
```

- [ ] **Step 12: Create `app/web/templates/_nav_status.html`**

```jinja
{# Sidebar footer + Jobs badge (out-of-band). Polled every 10 s by itself. #}
<div id="nav-status" class="sidebar-status" hx-get="/nav/status" hx-trigger="every 10s" hx-swap="outerHTML">
  <span><span class="dot{{ ' dot-on' if schedule_enabled }}"></span>Scheduler {{ "on" if schedule_enabled else "off" }}</span>
  {% if schedule_enabled and next_run %}<span>Next run {{ next_run|dt }} UTC</span>{% endif %}
</div>
<span id="nav-jobs-count" class="nav-count" hx-swap-oob="true"{% if not active_jobs %} hidden{% endif %}>{{ active_jobs or "" }}</span>
```

- [ ] **Step 13: Replace `app/web/templates/base.html`**

```jinja
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% if page_title %}{{ page_title }} · {% endif %}Asuras CSV</title>
  <script>try { var t = localStorage.getItem("theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch (e) {}</script>
  <link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/static/app.css?v={{ static_version }}">
  <script src="/static/vendor/htmx-2.0.4.min.js"></script>
  <script src="/static/app.js?v={{ static_version }}" defer></script>
</head>
<body>
<div class="shell">
  <aside class="sidebar" id="sidebar">
    <a class="brand" href="/"><span class="brand-mark"></span>Asuras CSV</a>
    <nav class="nav" aria-label="Main">
      {% for key, href, icon_name, label in [("overview", "/", "layout-dashboard", "Overview"), ("assets", "/assets", "database", "Assets"), ("jobs", "/jobs", "activity", "Jobs"), ("settings", "/settings", "settings", "Settings")] %}
      <a class="nav-item{% if active_nav == key %} active{% endif %}" href="{{ href }}"{% if active_nav == key %} aria-current="page"{% endif %}>{{ icon(icon_name) }}<span>{{ label }}</span>{% if key == "jobs" %}<span id="nav-jobs-count" class="nav-count" hidden></span>{% endif %}</a>
      {% endfor %}
    </nav>
    <div class="sidebar-foot">
      <div id="nav-status" class="sidebar-status" hx-get="/nav/status" hx-trigger="load" hx-swap="outerHTML"></div>
      <button type="button" class="btn btn-ghost btn-sm" data-theme-toggle>{{ icon("sun-moon") }}Toggle theme</button>
    </div>
  </aside>
  <div class="main">
    <header class="topbar">
      <button type="button" class="btn btn-ghost btn-icon" data-sidebar-toggle aria-label="Open menu">{{ icon("menu") }}</button>
      <a class="brand" href="/"><span class="brand-mark"></span>Asuras CSV</a>
    </header>
    <div class="page-header">
      <div class="page-title">{% block crumb %}{% endblock %}<h1>{% block heading %}{{ page_title }}{% endblock %}</h1>{% block heading_extra %}{% endblock %}</div>
      <span id="reconnecting" class="badge badge-warning" hidden>Reconnecting…</span>
      <div class="page-actions">{% block actions %}{% endblock %}</div>
    </div>
    <main class="page-body">{% block content %}{% endblock %}</main>
  </div>
</div>
<div class="sidebar-backdrop" data-sidebar-toggle></div>
<div class="toast-region" aria-live="polite">{% if flash %}<div class="toast" role="status">{{ icon("check") }}<span>{{ flash }}</span><button type="button" class="toast-close" aria-label="Dismiss">{{ icon("x") }}</button></div>{% endif %}</div>
{% block scripts %}{% endblock %}
</body>
</html>
```

- [ ] **Step 14: Load the chart library locally in `app/web/templates/chart.html`**

Replace `<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>` with:
```html
<script src="/static/vendor/lightweight-charts-4.2.0.standalone.production.js"></script>
```
In `tests/test_chart.py::test_chart_page`, replace `"lightweight-charts@4.2.0" in page.text` with `"/static/vendor/lightweight-charts-4.2.0.standalone.production.js" in page.text`.

- [ ] **Step 15: Mount static files in `app/main.py`**

Add imports:
```python
from fastapi.staticfiles import StaticFiles

from app.web.ui import STATIC_DIR
```
Replace `app = FastAPI(title="OHLCV Downloader", lifespan=lifespan)` with `app = FastAPI(title="Asuras CSV", lifespan=lifespan)` and add after `app.include_router(router)`:
```python
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
```

- [ ] **Step 16: Ship the static files in the package (`pyproject.toml`)**

```toml
[tool.setuptools.package-data]
app = [
  "web/templates/*.html",
  "web/static/*.css",
  "web/static/*.js",
  "web/static/*.svg",
  "web/static/vendor/*",
  "web/static/vendor/inter/*",
]
```

- [ ] **Step 17: Run the tests**

Run: `.venv/bin/pytest -q`
Expected: all pass (290 existing + the new ones).

- [ ] **Step 18: Verify the wheel contains the static files**

```bash
.venv/bin/pip wheel --no-deps -q -w "$TMPDIR/asuras-wheel" . && unzip -l "$TMPDIR"/asuras-wheel/*.whl | grep -E "static/(app\.css|app\.js|favicon|vendor/(icons|htmx|lightweight|inter/))"
```
Expected: 8 matching lines (app.css, app.js, favicon.svg, icons.svg, htmx, lightweight-charts, woff2, Inter LICENSE.txt).

- [ ] **Step 19: Commit**

```bash
git add app/web/static scripts/build_icon_sprite.py app/web/ui.py app/web/routes.py app/main.py app/services/jobs.py app/providers/base.py app/web/templates/base.html app/web/templates/_nav_status.html app/web/templates/chart.html pyproject.toml tests/test_ui.py tests/test_job_queries.py tests/test_web_shell.py tests/test_chart.py
git commit -m "feat(ui): design system, vendored assets and sidebar app shell"
```

---

### Task 2: Components, flash toasts and safe redirects

**Files:**
- Create: `app/web/templates/message.html`, `tests/test_feedback.py`
- Modify: `app/web/ui.py` (flash, `redirect`, `safe_next`, `FlashCleaner`), `app/main.py` (middleware), `app/web/routes.py` (POST handlers, `message_page`), `app/web/templates/_macros.html` (rewrite), `app/web/templates/_asset_rows.html`, `app/web/templates/jobs.html` (macro rename)

**Interfaces:**
- Consumes: `ui.templates`, `icon` global, `ACTIVE_STATUSES` global (Task 1).
- Produces: `ui.redirect(url: str, notice: str | None = None) -> RedirectResponse`, `ui.safe_next(value: str | None, default: str) -> str`, `ui.FlashCleaner` (ASGI middleware), `ui.FLASH_COOKIE = "flash"`, context var `flash`; `routes.message_page(request, message, status_code) -> HTMLResponse`; macros in `_macros.html`: `status_pill(job, progress)`, `kpi(label, value, sub="")`, `empty_state(icon_name, title, text, href=None, cta=None)`, `alert(message)`. POST handlers accept a `next` form field: `update_one` (default `/assets`), `update_all` (default `/`).

- [ ] **Step 1: Write the failing tests** — `tests/test_feedback.py`

```python
from urllib.parse import quote

import pytest

from app.providers.base import ProviderRegistry
from app.services import jobs
from app.web import ui
from tests.fakes import FakeProvider, make_asset


@pytest.mark.parametrize(
    "value, expected",
    [
        ("/assets", "/assets"),
        ("/assets/3#chart", "/assets/3#chart"),
        ("/jobs?status=active", "/jobs?status=active"),
        ("//evil.com", "/d"),
        ("/\\evil.com", "/d"),
        ("https://evil.com", "/d"),
        ("", "/d"),
        (None, "/d"),
    ],
)
def test_safe_next(value, expected):
    assert ui.safe_next(value, "/d") == expected


async def test_post_sets_flash_and_next_page_shows_and_clears_it(client, sf):
    asset = await make_asset(sf)
    r = await client.post(f"/assets/{asset.id}/update", data={"next": "/jobs"})
    assert r.status_code == 303 and r.headers["location"] == "/jobs"
    assert "flash=" in r.headers["set-cookie"] and "Max-Age=30" in r.headers["set-cookie"]
    assert client.cookies.get("flash")
    poll = await client.get("/nav/status", headers={"HX-Request": "true"})
    assert "set-cookie" not in poll.headers  # htmx fragments never consume the toast
    page = await client.get("/jobs")
    assert 'class="toast"' in page.text and "Update queued for FAKE-USD" in page.text
    assert client.cookies.get("flash") is None
    assert 'class="toast"' not in (await client.get("/jobs")).text


async def test_flash_is_escaped(client):
    client.cookies.set("flash", quote("<b>hi</b>", safe=""))
    page = (await client.get("/jobs")).text
    assert "&lt;b&gt;hi&lt;/b&gt;" in page and "<b>hi</b>" not in page


@pytest.mark.parametrize("target", ["//evil.com", "https://evil.com"])
async def test_update_rejects_offsite_next(client, sf, target):
    asset = await make_asset(sf)
    r = await client.post(f"/assets/{asset.id}/update", data={"next": target})
    assert r.headers["location"] == "/assets"
    r = await client.post("/assets/update-all", data={"next": target})
    assert r.headers["location"] == "/"


async def test_update_all_reports_how_many_assets_were_queued(client, sf):
    await make_asset(sf)
    r = await client.post("/assets/update-all")
    assert "Update%20queued%20for%201%20asset" in r.headers["set-cookie"]


async def test_errors_render_as_a_styled_page(client, sf):
    stale = await make_asset(sf, provider="gone", provider_symbol="OLD", jesse_symbol="OLD-USD")
    r = await client.post(f"/assets/{stale.id}/update")
    assert r.status_code == 400 and "alert-danger" in r.text and "gone" in r.text
    assert 'href="/assets"' in r.text and 'class="sidebar"' in r.text


async def test_status_pill_shows_progress_and_errors(client, sf, clock):
    registry = ProviderRegistry([FakeProvider(clock)])
    asset = await make_asset(sf)
    await jobs.enqueue(sf, registry, clock, asset.id, "backfill")
    rows = (await client.get("/assets/rows")).text
    assert "badge-info" in rows and ">queued" in rows and 'class="progress"' in rows
    job_id = await jobs.claim_next(sf, clock)
    await jobs.fail(sf, clock, job_id, "HTTP 451 from upstream", 0.0)
    rows = (await client.get("/assets/rows")).text
    assert "badge-danger" in rows and 'class="status-error">HTTP 451 from upstream' in rows
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_feedback.py -q`
Expected: FAIL (`ui.safe_next` missing, no toast, no `next` handling).

- [ ] **Step 3: Add flash and redirect helpers to `app/web/ui.py`**

Add imports:
```python
from urllib.parse import quote, unquote

from fastapi.responses import RedirectResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send
```
Add below `icon`:
```python
FLASH_COOKIE = "flash"
_CLEAR_FLASH = f"{FLASH_COOKIE}=; Max-Age=0; Path=/; SameSite=lax; HttpOnly"


def safe_next(value: str | None, default: str) -> str:
    """Only same-site paths: '/x' is fine, '//host' and '/\\host' are protocol-relative URLs browsers follow off-site."""
    if value and value.startswith("/") and not value.startswith(("//", "/\\")):
        return value
    return default


def redirect(url: str, notice: str | None = None) -> RedirectResponse:
    """303 after a POST; `notice` becomes a one-shot toast on the next full page."""
    response = RedirectResponse(url, status_code=303)
    if notice:
        response.set_cookie(FLASH_COOKIE, quote(notice, safe=""), max_age=30, path="/", samesite="lax", httponly=True)
    return response


class FlashCleaner:
    """Deletes the flash cookie when a full HTML page (which shows it as a toast) is sent.

    htmx requests (polls, fragments) are left alone so a poll cannot swallow a toast before the page renders.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if f"{FLASH_COOKIE}=" not in headers.get("cookie", "") or "hx-request" in headers:
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                if response_headers.get("content-type", "").startswith("text/html"):
                    response_headers.append("set-cookie", _CLEAR_FLASH)
            await send(message)

        await self.app(scope, receive, send_wrapper)
```
Replace `_page_context` with:
```python
def _page_context(request: Request) -> dict:
    raw = request.cookies.get(FLASH_COOKIE)
    return {
        "now": request.app.state.services.clock.now(),
        "static_version": STATIC_VERSION,
        "flash": unquote(raw)[:200] if raw else None,
    }
```

- [ ] **Step 4: Register the middleware in `app/main.py`**

Change the import to `from app.web.ui import STATIC_DIR, FlashCleaner` and add after `app.add_middleware(CrossSiteGuard)`:
```python
    app.add_middleware(FlashCleaner)
```

- [ ] **Step 5: Rewrite `app/web/templates/_macros.html`**

```jinja
{# Shared UI components. Import what you need: {% from "_macros.html" import status_pill, kpi %} #}

{% macro status_pill(job, progress) -%}
{%- if job is none -%}
<span class="badge cap">idle</span>
{%- else -%}
{%- set tone = {"queued": "info", "running": "info", "waiting": "warning", "paused": "warning", "done": "success", "failed": "danger"}.get(job.status, "") -%}
<span class="badge badge-dot cap{{ ' badge-' ~ tone if tone }}">{{ job.status }}
{%- if progress and job.status in ACTIVE_STATUSES %} <span class="progress" aria-hidden="true"><span style="width: {{ progress.percent }}%"></span></span> {{ progress.percent }}%{% endif -%}
</span>
{%- if progress and progress.eta_seconds is not none and job.status in ACTIVE_STATUSES %}<span class="status-detail">~{{ progress.eta_seconds|duration }} left</span>{% endif -%}
{%- if job.status_detail %}<span class="status-detail">{{ job.status_detail }}</span>{% endif -%}
{%- if job.status == "failed" and job.error %}<span class="status-error">{{ job.error }}</span>{% endif -%}
{%- endif -%}
{%- endmacro %}

{% macro kpi(label, value, sub="") -%}
<div class="card kpi"><div class="kpi-label">{{ label }}</div><div class="kpi-value">{{ value }}</div>{% if sub %}<div class="kpi-sub">{{ sub }}</div>{% endif %}</div>
{%- endmacro %}

{% macro empty_state(icon_name, title, text, href=None, cta=None) -%}
<div class="empty">{{ icon(icon_name) }}<h3>{{ title }}</h3><p>{{ text }}</p>{% if href %}<a class="btn btn-primary" href="{{ href }}">{{ icon("plus") }}{{ cta }}</a>{% endif %}</div>
{%- endmacro %}

{% macro alert(message) -%}
{% if message %}<div class="alert alert-danger" role="alert">{{ icon("circle-alert") }}<span>{{ message }}</span></div>{% endif %}
{%- endmacro %}
```

- [ ] **Step 6: Use `status_pill` in the existing row templates**

In `app/web/templates/_asset_rows.html` and `app/web/templates/jobs.html`, replace `{% from "_macros.html" import job_status %}` with `{% from "_macros.html" import status_pill %}` and `job_status(` with `status_pill(`.

- [ ] **Step 7: Create `app/web/templates/message.html`**

```jinja
{% extends "base.html" %}
{% from "_macros.html" import alert %}
{% set page_title = "Something went wrong" %}
{% block content %}
<div class="card confirm">
  <div class="card-body stack-sm">
    {{ alert(message) }}
    <div class="form-actions"><a class="btn" href="/assets">{{ icon("arrow-left") }}Back to assets</a></div>
  </div>
</div>
{% endblock %}
```

- [ ] **Step 8: Update `app/web/routes.py`**

Imports: change `from app.web.ui import templates` to `from app.web.ui import redirect, safe_next, templates`; remove `from html import escape` only if unused after Task 8 (it is still used by `estimate` now — keep it). Delete the local `redirect` function.

Replace `message_page`:
```python
def message_page(request: Request, message: str, status_code: int) -> HTMLResponse:
    return templates.TemplateResponse(request, "message.html", {"message": message}, status_code=status_code)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
```
Update its two callers: `return message_page(request, f"Cannot update this asset: {exc}", 400)` in `update_one`, and in `export_zip` `return message_page(request, "Select at least one asset.", 400)` / `return message_page(request, "No candles in this range for the selected assets.", 404)`.

Replace the POST handlers' redirects:
```python
    # create (end of function)
    return redirect("/assets", notice=f"{asset.jesse_symbol} added, download queued")
```
```python
@router.post("/assets/update-all")
async def update_all(request: Request, next: Annotated[str, Form()] = "/"):
    svc = services(request)
    try:
        created = await jobs.enqueue_all(svc.sf, svc.registry, svc.clock)
    except ProviderError as exc:
        log.warning("update-all failed", exc_info=True)
        return redirect(safe_next(next, "/"), notice=f"Could not queue updates: {exc}")
    return redirect(safe_next(next, "/"), notice=f"Update queued for {_plural(len(created), 'asset')}")


@router.post("/assets/{asset_id}/update")
async def update_one(request: Request, asset_id: int, next: Annotated[str, Form()] = "/assets"):
    svc = services(request)
    asset = await asset_service.get_asset(svc.sf, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    try:
        await jobs.enqueue(svc.sf, svc.registry, svc.clock, asset_id, "update")
    except ProviderError as exc:
        return message_page(request, f"Cannot update this asset: {exc}", 400)
    return redirect(safe_next(next, "/assets"), notice=f"Update queued for {asset.jesse_symbol}")
```
```python
    # edit (success)
    return redirect("/assets", notice=f"Saved {jesse_symbol.strip()}")
```
```python
    # delete
    asset = await _asset_or_404(svc, asset_id)
    await asset_service.delete_asset(svc.sf, asset_id)
    svc.stats.invalidate()
    return redirect("/assets", notice=f"Deleted {asset.jesse_symbol}")
```
```python
@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: int, next: Annotated[str, Form()] = "/jobs"):
    svc = services(request)
    await jobs.cancel(svc.sf, svc.clock, job_id)
    return redirect(safe_next(next, "/jobs"), notice=f"Cancelled job #{job_id}")
```
```python
    # save_settings (end)
    return redirect("/settings", notice="Settings saved")
```
In `_settings_page`, delete the `"saved": request.query_params.get("saved") == "1",` line; in `settings.html` delete `{% if saved %}<p><ins>Saved.</ins></p>{% endif %}`.

Note: until Task 3 adds `/assets`, `"/"` still renders the assets page; Task 3 adds the `/assets` route. Redirects to `/assets` 404 in between — run Task 3 before deploying.

- [ ] **Step 9: Run the tests**

Run: `.venv/bin/pytest -q`
Expected: all pass. If `tests/test_web_assets.py::test_update_one_with_unknown_provider_shows_a_message_not_a_500` fails, it is only checking status/text and should still pass; fix any assertion that checked `class="error"` in the message page by asserting `"alert-danger"` instead.

- [ ] **Step 10: Commit**

```bash
git add app/web/ui.py app/main.py app/web/routes.py app/web/templates/_macros.html app/web/templates/_asset_rows.html app/web/templates/jobs.html app/web/templates/message.html app/web/templates/settings.html tests/test_feedback.py
git commit -m "feat(ui): status pills, toast flashes, safe next redirects and styled message page"
```

---

### Task 3: Assets list page at `/assets`

**Files:**
- Create: `app/web/rows.py`
- Modify: `app/web/routes.py`, `app/web/templates/assets.html`, `app/web/templates/_asset_rows.html`, `tests/test_web_assets.py`, `tests/test_zip_export.py`, `tests/test_chart.py`

**Interfaces:**
- Consumes: `status_pill`, `empty_state` macros; filters `short_label`, `num`, `dt` (Tasks 1–2).
- Produces: `app.web.rows`: `AssetRow(asset, provider_label, stats, job, progress)`, `JobRow(job, asset, progress)`, `make_asset_row(svc, asset, stats, latest) -> AssetRow`, `load_asset_rows(svc) -> list[AssetRow]`, `load_asset_row(svc, asset) -> AssetRow`, `job_rows(svc, pairs: list[tuple[Job, Asset]]) -> list[JobRow]`. Route `GET /assets` (and `GET /` temporarily, removed in Task 5). Row markup contract used by `app.js`: `tr[data-href][data-symbol][data-provider][data-status]`, `#zip-form`, `#bulk-count`, `#select-all`, `[data-filters]`, `#filter-empty`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_web_assets.py`:
- `test_empty_assets_page`: change `client.get("/")` to `client.get("/assets")` and also assert `"Add your first asset" in r.text`.
- `test_assets_page_tbody_polls_with_the_same_rule`: change both `client.get("/")` to `client.get("/assets")`.
- Add:
```python
async def test_assets_page_has_filters_bulk_bar_and_row_menu(client, sf):
    asset = await make_asset(sf)
    page = (await client.get("/assets")).text
    assert "data-filters" in page and 'id="zip-form"' in page and 'id="select-all"' in page and 'id="bulk-count"' in page
    assert f'data-href="/assets/{asset.id}"' in page and 'data-symbol="fake-usd fakeusd"' in page
    assert 'data-provider="fake"' in page and 'data-status="idle"' in page
    assert f'<details class="menu" id="menu-{asset.id}" hx-preserve>' in page
    assert f'action="/assets/{asset.id}/update"' in page and f'href="/assets/{asset.id}/delete"' in page
    assert 'href="/assets" aria-current="page"' in page
```
In `tests/test_zip_export.py`:
- `test_assets_page_has_selection_checkboxes_that_survive_polling`: replace `("/", "/assets/rows")` with `("/assets", "/assets/rows")` and `client.get("/")` with `client.get("/assets")`.
- `test_zip_form_has_optional_date_inputs_and_empty_selection_guard`: replace the body after `await make_asset(sf)` with:
```python
    page = (await client.get("/assets")).text
    assert 'name="start"' in page and 'name="end"' in page
    script = (await client.get("/static/app.js")).text
    assert 'ev.target.id === "zip-form"' in script and "preventDefault" in script  # empty-selection guard
```
In `tests/test_chart.py::test_chart_page`, replace the last line with:
```python
    assert f'href="/assets/{asset.id}"' in (await client.get("/assets")).text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_web_assets.py tests/test_zip_export.py tests/test_chart.py -q`
Expected: FAIL (`/assets` is 404/405).

- [ ] **Step 3: Create `app/web/rows.py`**

```python
"""View models shared by the HTML pages: an asset with its stats and latest job, or a job with its asset."""
from __future__ import annotations

from dataclasses import dataclass

from app.models import Asset, Job
from app.services import assets as asset_service
from app.services import jobs
from app.services.assets import EMPTY_STATS, AssetStats
from app.services.progress import JobProgress, job_progress


@dataclass(frozen=True)
class AssetRow:
    asset: Asset
    provider_label: str
    stats: AssetStats
    job: Job | None
    progress: JobProgress | None


@dataclass(frozen=True)
class JobRow:
    job: Job
    asset: Asset
    progress: JobProgress | None


def make_asset_row(svc, asset: Asset, stats: dict[int, AssetStats], latest: dict[int, Job]) -> AssetRow:
    provider = svc.registry.find(asset.provider)
    job = latest.get(asset.id)
    progress = job_progress(job, asset.fetched_until, provider) if job and provider else None
    label = provider.label if provider else asset.provider
    return AssetRow(asset, label, stats.get(asset.id, EMPTY_STATS), job, progress)


async def load_asset_rows(svc) -> list[AssetRow]:
    all_assets = await asset_service.list_assets(svc.sf)
    stats = await svc.stats.get(svc.sf)
    latest = await jobs.latest_jobs_by_asset(svc.sf)
    return [make_asset_row(svc, asset, stats, latest) for asset in all_assets]


async def load_asset_row(svc, asset: Asset) -> AssetRow:
    return make_asset_row(svc, asset, await svc.stats.get(svc.sf), await jobs.latest_jobs_by_asset(svc.sf))


def job_rows(svc, pairs: list[tuple[Job, Asset]]) -> list[JobRow]:
    rows = []
    for job, asset in pairs:
        provider = svc.registry.find(asset.provider)
        rows.append(JobRow(job, asset, job_progress(job, asset.fetched_until, provider) if provider else None))
    return rows
```

- [ ] **Step 4: Use `rows.py` in `app/web/routes.py` and add `/assets`**

Delete the `AssetRow` and `JobRow` dataclasses and `_asset_rows` from `routes.py`; add `from app.web.rows import job_rows, load_asset_rows`. Remove now-unused imports (`dataclass`, `EMPTY_STATS`, `AssetStats`, `JobProgress`, `job_progress` — keep `EMPTY_STATS` because `delete_page` uses it).

```python
async def _rows_context(svc) -> dict:
    rows = await load_asset_rows(svc)
    active = any(row.job is not None and row.job.status in ACTIVE_STATUSES for row in rows)
    return {"rows": rows, "active": active}


@router.get("/", response_class=HTMLResponse)  # moves to the Overview in Task 5
@router.get("/assets", response_class=HTMLResponse)
async def assets_page(request: Request):
    svc = services(request)
    context = await _rows_context(svc) | {"providers": svc.registry.all()}
    return templates.TemplateResponse(request, "assets.html", context)
```
In `jobs_page`, replace the manual loop with `rows = job_rows(svc, await jobs.list_recent(svc.sf))`.

- [ ] **Step 5: Replace `app/web/templates/assets.html`**

```jinja
{% extends "base.html" %}
{% from "_macros.html" import empty_state %}
{% set page_title = "Assets" %}{% set active_nav = "assets" %}
{% block heading_extra %}<span class="muted tabular">{{ rows|length }}</span>{% endblock %}
{% block actions %}
<form class="inline" method="post" action="/assets/update-all"><input type="hidden" name="next" value="/assets"><button class="btn">{{ icon("refresh-cw") }}Update all</button></form>
<a class="btn btn-primary" href="/assets/new">{{ icon("plus") }}Add asset</a>
{% endblock %}
{% block content %}
{% if rows %}
<div class="filter-bar" data-filters>
  <input class="input input-sm search-input" type="search" placeholder="Filter symbols…" data-filter="text" aria-label="Filter symbols">
  <select class="input input-sm" data-filter="provider" aria-label="Provider">
    <option value="">All providers</option>
    {% for p in providers %}<option value="{{ p.name }}">{{ p.label|short_label }}</option>{% endfor %}
  </select>
  <select class="input input-sm" data-filter="status" aria-label="Status">
    <option value="">All statuses</option><option value="active">Active</option><option value="failed">Failed</option><option value="idle">Idle</option>
  </select>
</div>
{# Row checkboxes attach to this form via form="zip-form"; app.js shows it while at least one is ticked. #}
<form id="zip-form" class="bulk-bar" method="get" action="/export.zip" hidden>
  <strong id="bulk-count">0 selected</strong><span class="spacer"></span>
  <label>From <input class="input input-sm" type="date" name="start"></label>
  <label>To <input class="input input-sm" type="date" name="end"></label>
  <button class="btn btn-primary btn-sm">{{ icon("download") }}Export ZIP</button>
  <button type="button" class="btn btn-sm" data-clear-selection>Clear</button>
</form>
<div class="card card-scroll">
  <table class="table">
    <thead>
      <tr>
        <th><input type="checkbox" id="select-all" aria-label="Select all"></th><th>Symbol</th><th>Provider</th><th>Class</th>
        <th>First candle</th><th>Last candle</th><th class="num">Candles</th><th>Status</th><th><span class="sr-only">Actions</span></th>
      </tr>
    </thead>
    {% include "_asset_tbody.html" %}
  </table>
</div>
<p id="filter-empty" class="muted" hidden>No assets match the filters.</p>
<p class="muted small">Times are UTC. Click a row to open the asset.</p>
{% else %}
<div class="card">{{ empty_state("database", "No assets yet", "Add a crypto pair, stock, ETF, forex pair or metal and Asuras CSV downloads its full 1-minute history.", "/assets/new", "Add your first asset") }}</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 6: Replace `app/web/templates/_asset_rows.html`**

```jinja
{% from "_macros.html" import status_pill %}
{% for row in rows %}
{% set a = row.asset %}
{% set group = "active" if row.job and row.job.status in ACTIVE_STATUSES else ("failed" if row.job and row.job.status == "failed" else "idle") %}
<tr data-href="/assets/{{ a.id }}" data-symbol="{{ (a.jesse_symbol ~ ' ' ~ a.provider_symbol)|lower }}" data-provider="{{ a.provider }}" data-status="{{ group }}">
  <td><input type="checkbox" id="sel-{{ a.id }}" name="ids" value="{{ a.id }}" form="zip-form" hx-preserve aria-label="Select {{ a.jesse_symbol }}"></td>
  <td><a class="sym" href="/assets/{{ a.id }}">{{ a.jesse_symbol }}</a>{% if not a.enabled %} <span class="badge">disabled</span>{% endif %}<span class="sub">{{ a.provider_symbol }}</span></td>
  <td>{{ row.provider_label|short_label }}</td>
  <td>{{ a.asset_class }}</td>
  <td>{{ row.stats.first|dt }}</td>
  <td>{{ row.stats.last|dt }}</td>
  <td class="num">{{ row.stats.count|num }}</td>
  <td class="wrap">{{ status_pill(row.job, row.progress) }}</td>
  <td class="num">
    <details class="menu" id="menu-{{ a.id }}" hx-preserve>
      <summary class="btn btn-sm btn-icon btn-ghost" aria-label="Actions for {{ a.jesse_symbol }}">{{ icon("ellipsis") }}</summary>
      <div class="menu-list">
        <form method="post" action="/assets/{{ a.id }}/update"><input type="hidden" name="next" value="/assets"><button>{{ icon("refresh-cw") }}Update now</button></form>
        <a href="/assets/{{ a.id }}">{{ icon("chart-candlestick") }}Open detail</a>
        <a href="/assets/{{ a.id }}#export">{{ icon("download") }}Export CSV</a>
        <a href="/assets/{{ a.id }}#settings">{{ icon("pencil") }}Edit</a>
        <hr>
        <a class="danger" href="/assets/{{ a.id }}/delete">{{ icon("trash-2") }}Delete…</a>
      </div>
    </details>
  </td>
</tr>
{% else %}
<tr class="table-empty"><td colspan="9">No assets yet.</td></tr>
{% endfor %}
```

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add app/web/rows.py app/web/routes.py app/web/templates/assets.html app/web/templates/_asset_rows.html tests/test_web_assets.py tests/test_zip_export.py tests/test_chart.py
git commit -m "feat(ui): assets list with filters, bulk ZIP bar and row menus"
```

---

### Task 4: Overview data services

**Files:**
- Create: `app/services/sparklines.py`, `app/services/sources.py`, `tests/test_sparklines.py`, `tests/test_sources.py`
- Modify: `app/services/jobs.py` (`list_recent` filters, `last_done_by_asset`), `app/state.py`, `app/main.py`, `tests/test_job_queries.py`

**Interfaces:**
- Consumes: `StatsCache` (`app/services/assets.py`), `valid_candle()` (`app/services/export.py`), `split_label` (Task 1).
- Produces: `sparklines.daily_closes(sf, now: datetime, days: int = 30) -> dict[int, list[float]]`; `sparklines.SparklineCache(clock, ttl_seconds=600.0, days=30)` with `await .get(sf) -> dict[int, list[float]]`; `Services.sparklines: SparklineCache`; `sources.SourceStatus(name, label, coverage, needs_key, ready)`; `sources.source_statuses(registry, current: AppSettings) -> list[SourceStatus]`; `jobs.list_recent(sf, limit=200, *, status: str | None = None, asset_id: int | None = None)` with `status in {"active", "failed"}`; `jobs.JOB_FILTERS`; `jobs.last_done_by_asset(sf) -> dict[int, datetime]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_sparklines.py`:
```python
from datetime import UTC, datetime, timedelta

from app.domain import Candle
from app.services.sparklines import SparklineCache, daily_closes
from app.services.sync import insert_candles
from tests.fakes import make_asset

NOW = datetime(2024, 1, 31, 12, 0, tzinfo=UTC)


def candle(ts: datetime, close: float) -> Candle:
    return Candle(ts, 1.0, 10.0, 0.5, close, 1.0)


async def test_daily_closes_take_the_last_close_of_each_day_in_the_window(sf):
    a = await make_asset(sf)
    await make_asset(sf, provider_symbol="B", jesse_symbol="B-USD")  # no candles: absent from the result
    day = datetime(2024, 1, 20, tzinfo=UTC)
    async with sf.begin() as s:
        await insert_candles(
            s,
            a.id,
            [
                candle(NOW - timedelta(days=40), 1.9),  # outside the 30-day window
                candle(day + timedelta(hours=1), 1.1),
                candle(day + timedelta(hours=5), 1.2),  # last of Jan 20
                candle(day + timedelta(days=1, hours=3), 1.4),
            ],
        )
    assert await daily_closes(sf, NOW) == {a.id: [1.2, 1.4]}


async def test_sparkline_cache_reuses_its_result_within_the_ttl(sf, clock):
    a = await make_asset(sf)
    async with sf.begin() as s:
        await insert_candles(s, a.id, [candle(clock.now() - timedelta(hours=30), 1.0), candle(clock.now() - timedelta(hours=2), 1.5)])
    cache = SparklineCache(clock, ttl_seconds=600)
    assert await cache.get(sf) == {a.id: [1.0, 1.5]}
    async with sf.begin() as s:
        await insert_candles(s, a.id, [candle(clock.now() - timedelta(hours=1), 9.0)])
    assert await cache.get(sf) == {a.id: [1.0, 1.5]}
```

`tests/test_sources.py`:
```python
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
```

Append to `tests/test_job_queries.py`:
```python
async def test_list_recent_filters_by_status_and_asset(sf, clock):
    registry = ProviderRegistry([FakeProvider(clock)])
    a = await make_asset(sf)
    b = await make_asset(sf, provider_symbol="B", jesse_symbol="B-USD")
    ja = await jobs.enqueue(sf, registry, clock, a.id, "backfill")
    jb = await jobs.enqueue(sf, registry, clock, b.id, "backfill")
    failed = await jobs.claim_next(sf, clock)
    await jobs.fail(sf, clock, failed, "boom", 0.0)
    other = jb.id if failed == ja.id else ja.id
    assert [job.id for job, _ in await jobs.list_recent(sf, status="failed")] == [failed]
    assert [job.id for job, _ in await jobs.list_recent(sf, status="active")] == [other]
    assert [job.id for job, _ in await jobs.list_recent(sf, asset_id=a.id)] == [ja.id]


async def test_last_done_by_asset(sf, clock):
    a = await make_asset(sf)
    job = await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, a.id, "backfill")
    assert await jobs.claim_next(sf, clock) == job.id
    await jobs.finish(sf, clock, job.id, 1.0)
    assert await jobs.last_done_by_asset(sf) == {a.id: clock.now()}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_sparklines.py tests/test_sources.py tests/test_job_queries.py -q`
Expected: FAIL (modules / functions / keyword arguments missing).

- [ ] **Step 3: Create `app/services/sparklines.py`**

```python
"""Daily closing prices for the Overview sparklines: one query for all assets, cached like the candle stats."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import Float, func, select

from app.clock import Clock
from app.db import SessionFactory
from app.models import CandleRow
from app.services.assets import StatsCache
from app.services.export import valid_candle

DAY = timedelta(days=1)


async def daily_closes(sf: SessionFactory, now: datetime, days: int = 30) -> dict[int, list[float]]:
    """{asset_id: [last close of each UTC day, oldest first]} for candles in the last `days` days."""
    day = func.time_bucket(DAY, CandleRow.ts).label("day")
    stmt = (
        select(CandleRow.asset_id, day, func.last(CandleRow.close, CandleRow.ts, type_=Float))
        .where(CandleRow.ts >= now - timedelta(days=days), valid_candle())
        .group_by(CandleRow.asset_id, day)
        .order_by(CandleRow.asset_id, day)
    )
    closes: dict[int, list[float]] = {}
    async with sf() as s:
        for asset_id, _day, close in await s.execute(stmt):
            closes.setdefault(asset_id, []).append(close)
    return closes


class SparklineCache(StatsCache):
    """StatsCache's single-flight, serve-stale caching, holding daily_closes() instead of candle stats."""

    def __init__(self, clock: Clock, ttl_seconds: float = 600.0, days: int = 30):
        super().__init__(clock, ttl_seconds)
        self._days = days

    async def _load_uncached(self, sf: SessionFactory) -> dict[int, list[float]]:  # type: ignore[override]
        return await daily_closes(sf, self._clock.now(), self._days)
```

- [ ] **Step 4: Create `app/services/sources.py`**

```python
"""Whether each data source can be used right now: no key needed, or its key is configured."""
from __future__ import annotations

from dataclasses import dataclass

from app.providers.base import ProviderRegistry, split_label
from app.services.settings import AppSettings


@dataclass(frozen=True)
class SourceStatus:
    name: str
    label: str
    coverage: str
    needs_key: bool
    ready: bool


def source_statuses(registry: ProviderRegistry, current: AppSettings) -> list[SourceStatus]:
    keys = {
        "alpaca": bool(current.alpaca_key_id and current.alpaca_secret_key),
        "twelvedata": bool(current.twelvedata_api_key),
    }
    statuses = []
    for provider in registry.all():
        label, coverage = split_label(provider.label)
        statuses.append(SourceStatus(provider.name, label, coverage, provider.name in keys, keys.get(provider.name, True)))
    return statuses
```

- [ ] **Step 5: Extend `app/services/jobs.py`**

Replace `list_recent` and add `JOB_FILTERS` and `last_done_by_asset`:
```python
JOB_FILTERS = {"active": ACTIVE_STATUSES, "failed": ("failed",)}


async def list_recent(
    sf: SessionFactory, limit: int = 200, *, status: str | None = None, asset_id: int | None = None
) -> list[tuple[Job, Asset]]:
    stmt = select(Job, Asset).join(Asset, Asset.id == Job.asset_id).order_by(Job.id.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(Job.status.in_(JOB_FILTERS[status]))
    if asset_id is not None:
        stmt = stmt.where(Job.asset_id == asset_id)
    async with sf() as s:
        return [(job, asset) for job, asset in await s.execute(stmt)]


async def last_done_by_asset(sf: SessionFactory) -> dict[int, datetime]:
    """When each asset's most recent successful job finished."""
    async with sf() as s:
        rows = await s.execute(select(Job.asset_id, func.max(Job.finished_at)).where(Job.status == "done").group_by(Job.asset_id))
        return {asset_id: finished for asset_id, finished in rows}
```
If `ACTIVE_STATUSES` or `datetime` are not yet imported in `jobs.py`, add `from app.models import ACTIVE_STATUSES` (to the existing models import) and `from datetime import datetime` (to the existing datetime import).

- [ ] **Step 6: Wire the cache into `Services`**

`app/state.py`: add `from app.services.sparklines import SparklineCache` and the field after `stats: StatsCache`:
```python
    sparklines: SparklineCache
```
`app/main.py`: import `from app.services.sparklines import SparklineCache` and build it:
```python
    services = Services(
        env=env, sf=sf, clock=clock, registry=registry, settings=settings, stats=StatsCache(clock), sparklines=SparklineCache(clock)
    )
```

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add app/services/sparklines.py app/services/sources.py app/services/jobs.py app/state.py app/main.py tests/test_sparklines.py tests/test_sources.py tests/test_job_queries.py
git commit -m "feat: sparkline closes, data-source status and job filters for the overview"
```

---

### Task 5: Overview page at `/`

**Files:**
- Create: `app/web/overview.py`, `app/web/templates/overview.html`, `app/web/templates/_overview_live.html`, `app/web/templates/_sources_card.html`, `tests/test_overview.py`
- Modify: `app/web/routes.py` (drop `/` from `assets_page`), `app/main.py` (include router)

**Interfaces:**
- Consumes: `load_asset_rows`, `job_rows` (Task 3); `jobs.status_counts`, `jobs.last_done_by_asset`, `jobs.list_recent`, `svc.sparklines`, `source_statuses` (Tasks 1, 4); macros `kpi`, `status_pill`, `empty_state`.
- Produces: `GET /` → `overview.html`; `GET /overview/live` → `_overview_live.html` (root `<div id="overview-live">`, polls itself); `_sources_card.html` (expects `sources`, `settings`).

- [ ] **Step 1: Write the failing tests** — `tests/test_overview.py`

```python
from datetime import UTC, datetime, timedelta

from app.domain import Candle
from app.providers.base import ProviderRegistry
from app.services import jobs
from app.services.sync import insert_candles
from tests.fakes import FakeProvider, make_asset

T = datetime(2023, 12, 30, 12, 0, tzinfo=UTC)  # FakeClock.now() is 2024-01-01 02:00 UTC


async def test_empty_overview_welcomes_and_lists_sources(client):
    r = await client.get("/")
    assert r.status_code == 200 and "Welcome to Asuras CSV" in r.text and 'href="/assets/new"' in r.text
    assert "Data sources" in r.text and "Fake" in r.text and "No key needed" not in r.text
    assert 'href="/" aria-current="page"' in r.text and "<title>Overview · Asuras CSV</title>" in r.text


async def test_overview_kpis_sparklines_and_recent_jobs(client, sf, clock):
    on = await make_asset(sf)
    await make_asset(sf, provider_symbol="OFF", jesse_symbol="OFF-USD", enabled=False)
    async with sf.begin() as s:
        await insert_candles(s, on.id, [Candle(T, 1, 2, 0.5, 1.0, 10), Candle(T + timedelta(days=1), 1, 2, 0.5, 1.5, 10)])
    job = await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, on.id, "backfill")
    assert await jobs.claim_next(sf, clock) == job.id
    await jobs.finish(sf, clock, job.id, 1.0)
    page = (await client.get("/")).text
    assert "1 enabled · 1 disabled" in page
    assert '<div class="kpi-value">1 / 1</div>' in page and "Nothing running" in page
    assert 'class="spark spark-up"' in page
    assert "FAKE-USD" in page and "OFF-USD" in page and "backfill" in page and "badge-success" in page
    assert 'hx-trigger="every 30s"' in page


async def test_overview_live_polls_fast_while_a_job_is_active(client, sf, clock):
    asset = await make_asset(sf)
    await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, asset.id, "backfill")
    live = (await client.get("/overview/live")).text
    assert live.lstrip().startswith('<div id="overview-live"') and 'hx-trigger="every 2s"' in live
    assert "1 queued" in live and "<html" not in live


async def test_overview_lists_at_most_eight_assets(client, sf):
    for i in range(9):
        await make_asset(sf, provider_symbol=f"S{i}", jesse_symbol=f"S{i}-USD")
    page = (await client.get("/")).text
    assert page.count('<tr data-href="/assets/') == 8 and "View all 9" in page


async def test_overview_survives_an_asset_with_an_unknown_provider_and_no_candles(client, sf):
    await make_asset(sf, provider="gone", provider_symbol="OLD", jesse_symbol="OLD-USD")
    r = await client.get("/")
    assert r.status_code == 200 and "OLD-USD" in r.text and ">gone<" in r.text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_overview.py -q`
Expected: FAIL ("/" still renders the assets page; `/overview/live` 404).

- [ ] **Step 3: Create `app/web/overview.py`**

```python
"""Overview (home) page: KPIs, assets with sparklines, recent jobs and data-source status."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.models import ACTIVE_STATUSES
from app.services import jobs
from app.services.sources import source_statuses
from app.web.rows import job_rows, load_asset_rows
from app.web.ui import templates

router = APIRouter()
OVERVIEW_ASSETS = 8
FRESH_WITHIN = timedelta(hours=24)


async def _context(request: Request) -> dict:
    svc = request.app.state.services
    rows = await load_asset_rows(svc)
    enabled = [row for row in rows if row.asset.enabled]
    counts = await jobs.status_counts(svc.sf)
    active = {status: counts[status] for status in ACTIVE_STATUSES if counts.get(status)}
    last_done = await jobs.last_done_by_asset(svc.sf)
    now = svc.clock.now()
    fresh = sum(1 for row in enabled if row.asset.id in last_done and now - last_done[row.asset.id] <= FRESH_WITHIN)
    current = await svc.settings.load()
    return {
        "rows": rows[:OVERVIEW_ASSETS],
        "asset_total": len(rows),
        "enabled_total": len(enabled),
        "candle_total": sum(row.stats.count for row in rows),
        "active_total": sum(active.values()),
        "active_detail": " · ".join(f"{n} {status}" for status, n in active.items()) or "Nothing running",
        "fresh": fresh,
        "sparks": await svc.sparklines.get(svc.sf),
        "recent": job_rows(svc, await jobs.list_recent(svc.sf, limit=5)),
        "sources": source_statuses(svc.registry, current),
        "settings": current,
        "active": bool(active),
    }


@router.get("/", response_class=HTMLResponse)
async def overview_page(request: Request):
    return templates.TemplateResponse(request, "overview.html", await _context(request))


@router.get("/overview/live", response_class=HTMLResponse)
async def overview_live(request: Request):
    return templates.TemplateResponse(request, "_overview_live.html", await _context(request))
```

- [ ] **Step 4: Register the router and free `/`**

`app/main.py`: `from app.web import overview` and `app.include_router(overview.router)` before `app.include_router(router)`.
`app/web/routes.py`: delete the `@router.get("/", response_class=HTMLResponse)  # moves to the Overview in Task 5` line above `assets_page`.

- [ ] **Step 5: Create `app/web/templates/overview.html`**

```jinja
{% extends "base.html" %}
{% set page_title = "Overview" %}{% set active_nav = "overview" %}
{% block actions %}
<form class="inline" method="post" action="/assets/update-all"><input type="hidden" name="next" value="/"><button class="btn">{{ icon("refresh-cw") }}Update all</button></form>
<a class="btn btn-primary" href="/assets/new">{{ icon("plus") }}Add asset</a>
{% endblock %}
{% block content %}{% include "_overview_live.html" %}{% endblock %}
```

- [ ] **Step 6: Create `app/web/templates/_sources_card.html`**

```jinja
<div class="card">
  <div class="card-header"><h2>Data sources</h2><a class="card-link" href="/settings">Settings →</a></div>
  {% for s in sources %}
  <div class="list-row">
    <span class="sym">{{ s.label }}</span><span class="muted small">{{ s.coverage }}</span>
    {% if s.ready %}<span class="badge badge-success badge-dot">Ready</span>{% else %}<a class="badge badge-warning badge-dot" href="/settings">Key missing</a>{% endif %}
  </div>
  {% endfor %}
  <div class="list-row">
    <span class="sym">Scheduled updates</span><code>{{ settings.schedule_cron }}</code>
    {% if settings.schedule_enabled %}<span class="badge badge-success badge-dot">On</span>{% else %}<span class="badge badge-dot">Off</span>{% endif %}
  </div>
</div>
```

- [ ] **Step 7: Create `app/web/templates/_overview_live.html`**

```jinja
{% from "_macros.html" import kpi, status_pill, empty_state %}
<div id="overview-live" class="stack" hx-get="/overview/live" hx-trigger="every {{ '2s' if active else '30s' }}" hx-swap="outerHTML">
{% if asset_total == 0 %}
  <div class="card">{{ empty_state("database", "Welcome to Asuras CSV", "Add your first asset and Asuras CSV downloads its full 1-minute history, keeps it up to date and exports it for Jesse.", "/assets/new", "Add your first asset") }}</div>
  {% include "_sources_card.html" %}
{% else %}
  <div class="kpi-grid">
    {{ kpi("Assets", asset_total, enabled_total ~ " enabled · " ~ (asset_total - enabled_total) ~ " disabled") }}
    {{ kpi("Candles stored", candle_total|compact, "1-minute bars") }}
    {{ kpi("Active jobs", active_total, active_detail) }}
    {{ kpi("Freshness", fresh ~ " / " ~ enabled_total, "updated in the last 24 h") }}
  </div>
  <div class="card">
    <div class="card-header"><h2>Assets</h2><a class="card-link" href="/assets">View all {{ asset_total }} →</a></div>
    <div class="card-scroll">
      <table class="table">
        <thead><tr><th>Symbol</th><th>Provider</th><th>History</th><th>Last 30 days</th><th>Last candle</th><th>Status</th></tr></thead>
        <tbody>
        {% for row in rows %}
          <tr data-href="/assets/{{ row.asset.id }}">
            <td><a class="sym" href="/assets/{{ row.asset.id }}">{{ row.asset.jesse_symbol }}</a></td>
            <td>{{ row.provider_label|short_label }}</td>
            <td class="muted">{% if row.stats.first %}{{ row.stats.first.date() }} → {{ row.stats.last.date() }}{% else %}—{% endif %}</td>
            <td>{{ sparkline(sparks.get(row.asset.id, [])) }}</td>
            <td>{{ row.stats.last|ago(now) }}</td>
            <td class="wrap">{{ status_pill(row.job, row.progress) }}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  <div class="grid-2">
    <div class="card">
      <div class="card-header"><h2>Recent jobs</h2><a class="card-link" href="/jobs">All jobs →</a></div>
      <div class="card-scroll">
        <table class="table">
          <thead><tr><th>Asset</th><th>Kind</th><th>Status</th><th class="num">Added</th><th>Finished</th></tr></thead>
          <tbody>
          {% for r in recent %}
            <tr data-href="/assets/{{ r.asset.id }}">
              <td><a class="sym" href="/assets/{{ r.asset.id }}">{{ r.asset.jesse_symbol }}</a></td>
              <td>{{ r.job.kind }}</td>
              <td class="wrap">{{ status_pill(r.job, r.progress) }}</td>
              <td class="num">{{ r.job.candles_added|num }}</td>
              <td>{{ r.job.finished_at|ago(now) }}</td>
            </tr>
          {% else %}
            <tr class="table-empty"><td colspan="5">No jobs yet.</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
    </div>
    {% include "_sources_card.html" %}
  </div>
{% endif %}
</div>
```

- [ ] **Step 8: Run the tests**

Run: `.venv/bin/pytest -q`
Expected: all pass (`tests/test_security.py` GET `/` is the Overview now and still 200).

- [ ] **Step 9: Commit**

```bash
git add app/web/overview.py app/web/routes.py app/main.py app/web/templates/overview.html app/web/templates/_overview_live.html app/web/templates/_sources_card.html tests/test_overview.py
git commit -m "feat(ui): overview dashboard with KPIs, sparklines, recent jobs and data sources"
```

---

### Task 6: Asset detail page

**Files:**
- Create: `app/web/templates/asset_detail.html`, `app/web/templates/_asset_parts.html`, `app/web/templates/_asset_live.html`, `tests/test_asset_detail.py`
- Delete: `app/web/templates/chart.html`, `app/web/templates/export.html`, `app/web/templates/asset_edit.html`
- Modify: `app/web/routes.py`, `tests/test_web_pages.py` (`test_edit_asset`, `test_export_downloads_jesse_csv`), `tests/test_chart.py` (remove the two page tests), `tests/test_web_assets.py` (`test_adding_an_asset_queues_a_backfill`)

**Interfaces:**
- Consumes: `load_asset_row`, `job_rows` (Task 3), `jobs.list_recent(..., asset_id=)` (Task 4), `export_range`, `RANGES`, `DEFAULT_RANGE`, macros `status_pill`, `alert`.
- Produces: `GET /assets/{asset_id:int}` → `asset_detail.html` (anchors `#chart`, `#export`, `#settings`); `GET /assets/{asset_id:int}/live` → `_asset_live.html` (`#asset-status` primary + `#asset-details`, `#asset-jobs` out-of-band); old `GET /assets/{id}/chart|export|edit` → 303 to `/assets/{id}#chart|#export|#settings`; `POST /assets` and `POST /assets/{id}/edit` redirect to `/assets/{id}`. `_asset_parts.html` macros: `status_region(asset, row, active)`, `details_card(asset, row, oob=False)`, `jobs_card(history, now, oob=False)`.

- [ ] **Step 1: Write the failing tests** — `tests/test_asset_detail.py`

```python
from datetime import UTC, datetime, timedelta

import pytest

from app.domain import Candle
from app.providers.base import ProviderRegistry
from app.services import jobs
from app.services.charts import RANGES
from app.services.sync import insert_candles
from tests.fakes import FakeProvider, make_asset

T0 = datetime(2024, 1, 1, tzinfo=UTC)


async def test_detail_page_without_candles(client, sf):
    asset = await make_asset(sf)
    r = await client.get(f"/assets/{asset.id}")
    assert r.status_code == 200
    t = r.text
    assert "Custom Data · FAKE-USD" in t and "No candles stored yet." in t
    assert 'id="chart"' in t and 'id="export"' in t and 'id="settings"' in t
    assert "/static/vendor/lightweight-charts-4.2.0.standalone.production.js" in t and f"/assets/{asset.id}/candles.json" in t
    for label in RANGES:
        assert f'data-range="{label}"' in t
    assert "Chart library failed to load" in t and t.count('addEventListener("resize"') == 1
    assert 'href="/assets" aria-current="page"' in t and "<title>FAKE-USD · Asuras CSV</title>" in t


async def test_detail_page_export_defaults_to_the_stored_range(client, sf):
    asset = await make_asset(sf)
    async with sf.begin() as s:
        await insert_candles(s, asset.id, [Candle(T0 + timedelta(minutes=m), 1, 2, 0.5, 1.5, 10) for m in (0, 1)])
    t = (await client.get(f"/assets/{asset.id}")).text
    assert 'value="2024-01-01"' in t and f'action="/assets/{asset.id}/export.csv"' in t


async def test_unknown_and_non_numeric_assets(client):
    assert (await client.get("/assets/999")).status_code == 404
    assert (await client.get("/assets/abc")).status_code == 404
    assert (await client.get("/assets/new")).status_code == 200
    assert (await client.get("/assets/rows")).status_code == 200


@pytest.mark.parametrize("old, anchor", [("chart", "chart"), ("export", "export"), ("edit", "settings")])
async def test_old_pages_redirect_to_the_detail_page(client, sf, old, anchor):
    asset = await make_asset(sf)
    r = await client.get(f"/assets/{asset.id}/{old}")
    assert r.status_code == 303 and r.headers["location"] == f"/assets/{asset.id}#{anchor}"
    assert (await client.get(f"/assets/999/{old}")).status_code == 404


async def test_live_fragment_updates_status_details_and_jobs(client, sf, clock):
    asset = await make_asset(sf)
    idle = (await client.get(f"/assets/{asset.id}/live")).text
    assert idle.lstrip().startswith('<span id="asset-status"') and 'hx-trigger="every 30s"' in idle
    assert idle.count('hx-swap-oob="true"') == 2 and "No jobs yet." in idle
    await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, asset.id, "backfill")
    busy = (await client.get(f"/assets/{asset.id}/live")).text
    assert 'hx-trigger="every 2s"' in busy and "backfill" in busy


async def test_edit_saves_and_errors_render_on_the_detail_page(client, sf):
    asset = await make_asset(sf)
    r = await client.post(f"/assets/{asset.id}/edit", data={"jesse_symbol": "NEW-USD"})
    assert r.status_code == 303 and r.headers["location"] == f"/assets/{asset.id}"
    r = await client.post(f"/assets/{asset.id}/edit", data={"jesse_symbol": "bad", "enabled": "on"})
    assert r.status_code == 400 and "alert-danger" in r.text and 'id="chart"' in r.text and 'value="bad"' in r.text


async def test_detail_page_for_an_asset_with_an_unknown_provider(client, sf):
    stale = await make_asset(sf, provider="gone", provider_symbol="OLD", jesse_symbol="OLD-USD")
    r = await client.get(f"/assets/{stale.id}")
    assert r.status_code == 200 and "gone · OLD" in r.text
```

Update existing tests:
- `tests/test_web_pages.py::test_edit_asset` → replace its body with:
```python
    asset = await make_asset(sf)
    r = await client.post(f"/assets/{asset.id}/edit", data={"jesse_symbol": "NEW-USD"})
    assert r.status_code == 303 and r.headers["location"] == f"/assets/{asset.id}"
    got = await get_asset(sf, asset.id)
    assert (got.jesse_symbol, got.enabled) == ("NEW-USD", False)
    r = await client.post(f"/assets/{asset.id}/edit", data={"jesse_symbol": "bad", "enabled": "on"})
    assert r.status_code == 400 and "Jesse symbol" in r.text
    assert (await client.post("/assets/999/edit", data={"jesse_symbol": "X-USD"})).status_code == 404
```
- `tests/test_web_pages.py::test_export_downloads_jesse_csv` → change `page = await client.get(f"/assets/{asset.id}/export")` to `page = await client.get(f"/assets/{asset.id}")`.
- `tests/test_chart.py` → delete `test_chart_page` and `test_chart_page_guards_missing_library_and_resize_listener` (covered by `test_asset_detail.py`).
- `tests/test_web_assets.py::test_adding_an_asset_queues_a_backfill` → after `assert r.status_code == 303` add `assert r.headers["location"] == "/assets/1"`.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_asset_detail.py tests/test_web_pages.py tests/test_web_assets.py -q`
Expected: FAIL (`/assets/{id}` 404/405, old pages return 200).

- [ ] **Step 3: Add the detail routes to `app/web/routes.py`**

Imports: add `load_asset_row` to `from app.web.rows import ...`.

Delete the old `edit_page`, `export_page` and `chart_page` functions. Add after `_asset_or_404`:
```python
async def _live_context(svc, asset: Asset) -> dict:
    row = await load_asset_row(svc, asset)
    history = job_rows(svc, await jobs.list_recent(svc.sf, limit=10, asset_id=asset.id))
    active = row.job is not None and row.job.status in ACTIVE_STATUSES
    return {"asset": asset, "row": row, "history": history, "active": active}


async def _detail_page(request: Request, asset: Asset, error: str | None = None, form_symbol: str | None = None, status_code: int = 200):
    svc = services(request)
    context = await _live_context(svc, asset) | {
        "rng": await export_range(svc.sf, asset.id, None, None),
        "ranges": list(RANGES),
        "default_range": DEFAULT_RANGE,
        "error": error,
        "form_symbol": form_symbol,
    }
    return templates.TemplateResponse(request, "asset_detail.html", context, status_code=status_code)


@router.get("/assets/{asset_id:int}", response_class=HTMLResponse)
async def asset_detail(request: Request, asset_id: int):
    return await _detail_page(request, await _asset_or_404(services(request), asset_id))


@router.get("/assets/{asset_id:int}/live", response_class=HTMLResponse)
async def asset_live(request: Request, asset_id: int):
    svc = services(request)
    context = await _live_context(svc, await _asset_or_404(svc, asset_id))
    return templates.TemplateResponse(request, "_asset_live.html", context)


async def _to_detail(request: Request, asset_id: int, anchor: str):
    await _asset_or_404(services(request), asset_id)
    return redirect(f"/assets/{asset_id}#{anchor}")


@router.get("/assets/{asset_id}/chart")
async def chart_page(request: Request, asset_id: int):
    return await _to_detail(request, asset_id, "chart")


@router.get("/assets/{asset_id}/export")
async def export_page(request: Request, asset_id: int):
    return await _to_detail(request, asset_id, "export")


@router.get("/assets/{asset_id}/edit")
async def edit_page(request: Request, asset_id: int):
    return await _to_detail(request, asset_id, "settings")
```
Change the `edit` POST handler's error branch and success redirect:
```python
    except ValueError as exc:
        return await _detail_page(request, asset, str(exc), jesse_symbol, 400)
    return redirect(f"/assets/{asset_id}", notice=f"Saved {jesse_symbol.strip()}")
```
Change `create`'s final line to:
```python
    return redirect(f"/assets/{asset.id}", notice=f"{asset.jesse_symbol} added, download queued")
```

- [ ] **Step 4: Create `app/web/templates/_asset_parts.html`**

```jinja
{# Pieces of the asset detail page that the /assets/{id}/live poll refreshes. #}
{% from "_macros.html" import status_pill %}

{% macro status_region(asset, row, active) -%}
<span id="asset-status" class="status-region" hx-get="/assets/{{ asset.id }}/live" hx-trigger="every {{ '2s' if active else '30s' }}" hx-swap="outerHTML">{{ status_pill(row.job, row.progress) }}</span>
{%- endmacro %}

{% macro details_card(asset, row, oob=False) -%}
<div class="card" id="asset-details"{% if oob %} hx-swap-oob="true"{% endif %}>
  <div class="card-header"><h2>Details</h2></div>
  <dl class="kv">
    <dt>Provider</dt><dd>{{ row.provider_label|short_label }} · {{ asset.provider_symbol }}</dd>
    <dt>Class</dt><dd>{{ asset.asset_class }}</dd>
    <dt>Stored</dt><dd>{% if row.stats.first %}{{ row.stats.first|dt }} → {{ row.stats.last|dt }}{% else %}—{% endif %}</dd>
    <dt>Candles</dt><dd class="tabular">{{ row.stats.count|num }}</dd>
    <dt>Fetched until</dt><dd>{{ asset.fetched_until|dt }}</dd>
    <dt>Jesse</dt><dd>Custom Data · {{ asset.jesse_symbol }}</dd>
  </dl>
</div>
{%- endmacro %}

{% macro jobs_card(history, now, oob=False) -%}
<div class="card" id="asset-jobs"{% if oob %} hx-swap-oob="true"{% endif %}>
  <div class="card-header"><h2>Job history</h2><a class="card-link" href="/jobs">All jobs →</a></div>
  <div class="card-scroll">
    <table class="table">
      <thead><tr><th>#</th><th>Kind</th><th>Status</th><th>Range (UTC)</th><th class="num">Added</th><th>Run time</th><th>Finished</th></tr></thead>
      <tbody>
      {% for r in history %}
        <tr>
          <td class="muted">{{ r.job.id }}</td>
          <td>{{ r.job.kind }}</td>
          <td class="wrap">{{ status_pill(r.job, r.progress) }}</td>
          <td>{{ r.job.range_start|dt }} → {{ r.job.range_end|dt }}</td>
          <td class="num">{{ r.job.candles_added|num }}</td>
          <td>{{ r.job.run_seconds|duration }}</td>
          <td>{{ r.job.finished_at|ago(now) }}</td>
        </tr>
      {% else %}
        <tr class="table-empty"><td colspan="7">No jobs yet.</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{%- endmacro %}
```

- [ ] **Step 5: Create `app/web/templates/_asset_live.html`**

```jinja
{% from "_asset_parts.html" import status_region, details_card, jobs_card %}
{{ status_region(asset, row, active) }}
{{ details_card(asset, row, oob=True) }}
{{ jobs_card(history, now, oob=True) }}
```

- [ ] **Step 6: Create `app/web/templates/asset_detail.html`**

```jinja
{% extends "base.html" %}
{% from "_macros.html" import alert %}
{% from "_asset_parts.html" import status_region, details_card, jobs_card %}
{% set page_title = asset.jesse_symbol %}{% set active_nav = "assets" %}
{% block crumb %}<a class="crumb" href="/assets">Assets /</a>{% endblock %}
{% block heading_extra %}{{ status_region(asset, row, active) }}{% endblock %}
{% block actions %}
<form class="inline" method="post" action="/assets/{{ asset.id }}/update"><input type="hidden" name="next" value="/assets/{{ asset.id }}"><button class="btn">{{ icon("refresh-cw") }}Update now</button></form>
<a class="btn btn-danger" href="/assets/{{ asset.id }}/delete">{{ icon("trash-2") }}Delete</a>
{% endblock %}
{% block content %}
<div class="detail-grid">
  <div class="card" id="chart">
    <div class="card-header">
      <h2>Price</h2><span id="chart-status" class="chart-status"></span><span class="spacer"></span>
      <div class="segmented" id="ranges" role="group" aria-label="Range">{% for r in ranges %}<button type="button" data-range="{{ r }}"{% if r == default_range %} class="active"{% endif %}>{{ r }}</button>{% endfor %}</div>
    </div>
    <div id="chart-canvas" class="chart"></div>
  </div>
  <div class="stack">
    {{ details_card(asset, row) }}
    <div class="card" id="export">
      <div class="card-header"><h2>Export CSV</h2></div>
      {% if rng is none %}
      <div class="card-body muted">No candles stored yet.</div>
      {% else %}
      <form class="card-body stack-sm" method="get" action="/assets/{{ asset.id }}/export.csv">
        <div class="form-grid">
          <div class="field"><label for="exp-start">From</label><input class="input" id="exp-start" type="date" name="start" value="{{ rng.first.date().isoformat() }}"></div>
          <div class="field"><label for="exp-end">To (inclusive)</label><input class="input" id="exp-end" type="date" name="end" value="{{ rng.last.date().isoformat() }}"></div>
        </div>
        <div class="form-actions"><button class="btn btn-primary btn-sm">{{ icon("download") }}Download CSV</button></div>
      </form>
      {% endif %}
    </div>
  </div>
</div>
<div class="detail-grid">
  {{ jobs_card(history, now) }}
  <div class="card" id="settings">
    <div class="card-header"><h2>Settings</h2></div>
    <form class="card-body stack-sm" method="post" action="/assets/{{ asset.id }}/edit">
      {{ alert(error) }}
      <div class="field">
        <label for="jesse_symbol">Jesse symbol</label>
        <input class="input" id="jesse_symbol" name="jesse_symbol" value="{{ form_symbol or asset.jesse_symbol }}" required>
        <span class="hint">Use with exchange "Custom Data" when importing into Jesse.</span>
      </div>
      <label class="check"><input type="checkbox" name="enabled" {% if asset.enabled %}checked{% endif %}> Include in "Update all" and scheduled updates</label>
      <p class="hint">Start date {{ asset.start_date|dt }} UTC (can't be changed).</p>
      <div class="form-actions"><button class="btn btn-sm">Save</button></div>
    </form>
  </div>
</div>
{% endblock %}
{% block scripts %}
<script src="/static/vendor/lightweight-charts-4.2.0.standalone.production.js"></script>
<script>
(function () {
  var url = "/assets/{{ asset.id }}/candles.json";
  var el = document.getElementById("chart-canvas"), status = document.getElementById("chart-status");
  if (!window.LightweightCharts) {
    status.textContent = "Chart library failed to load";
    return;
  }
  function theme() {
    var css = getComputedStyle(document.documentElement);
    function v(name) { return css.getPropertyValue(name).trim(); }
    return {
      layout: { background: { type: "solid", color: v("--chart-bg") }, textColor: v("--chart-text"), fontFamily: "Inter, system-ui, sans-serif" },
      grid: { vertLines: { color: v("--chart-grid") }, horzLines: { color: v("--chart-grid") } },
      rightPriceScale: { borderColor: v("--chart-grid") },
      timeScale: { borderColor: v("--chart-grid") }
    };
  }
  var chart = LightweightCharts.createChart(el, {
    width: el.clientWidth, height: el.clientHeight, timeScale: { timeVisible: true, secondsVisible: false }
  });
  chart.applyOptions(theme());
  var candles = chart.addCandlestickSeries({
    upColor: "#26a69a", downColor: "#ef5350", wickUpColor: "#26a69a", wickDownColor: "#ef5350", borderVisible: false
  });
  var volume = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol" });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
  document.addEventListener("themechange", function () { chart.applyOptions(theme()); });
  function fit() { chart.applyOptions({ width: el.clientWidth, height: el.clientHeight }); }
  if (window.ResizeObserver) { new ResizeObserver(fit).observe(el); } else { window.addEventListener("resize", fit); }

  var token = 0;
  function load(range) {
    var mine = ++token;
    status.textContent = "Loading…";
    fetch(url + "?range=" + encodeURIComponent(range))
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (data) {
        if (mine !== token) return;
        candles.setData(data.candles);
        volume.setData(data.candles.map(function (c) {
          return { time: c.time, value: c.volume, color: c.close >= c.open ? "#26a69a66" : "#ef535066" };
        }));
        chart.timeScale().fitContent();
        status.textContent = data.candles.length ? data.interval + " bars · " + data.candles.length.toLocaleString() : "No candles stored yet.";
      })
      .catch(function (e) { if (mine === token) status.textContent = "Failed to load: " + e.message; });
  }
  document.getElementById("ranges").addEventListener("click", function (ev) {
    var b = ev.target.closest("button[data-range]");
    if (!b) return;
    document.querySelectorAll("#ranges button").forEach(function (x) { x.classList.toggle("active", x === b); });
    load(b.dataset.range);
  });
  load("{{ default_range }}");
})();
</script>
{% endblock %}
```

- [ ] **Step 7: Delete the replaced templates**

```bash
git rm app/web/templates/chart.html app/web/templates/export.html app/web/templates/asset_edit.html
```

- [ ] **Step 8: Run the tests**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add app/web/routes.py app/web/templates/asset_detail.html app/web/templates/_asset_parts.html app/web/templates/_asset_live.html tests/test_asset_detail.py tests/test_web_pages.py tests/test_chart.py tests/test_web_assets.py
git commit -m "feat(ui): asset detail page merging chart, export, settings and job history"
```

---

### Task 7: Jobs page with status tabs and polling

**Files:**
- Create: `app/web/templates/_jobs_tbody.html`, `tests/test_jobs_page.py`
- Modify: `app/web/routes.py` (`jobs_page`, `/jobs/rows`), `app/web/templates/jobs.html`

**Interfaces:**
- Consumes: `jobs.list_recent(status=)`, `jobs.status_counts`, `job_rows`, `status_pill`, `safe_next`.
- Produces: `GET /jobs?status=active|failed` (other values → 422); `GET /jobs/rows?status=…` → `<tbody id="job-rows">` that polls itself with the same filter.

- [ ] **Step 1: Write the failing tests** — `tests/test_jobs_page.py`

```python
from app.providers.base import ProviderRegistry
from app.services import jobs
from tests.fakes import FakeProvider, make_asset


async def test_jobs_tabs_filter_and_count(client, sf, clock):
    registry = ProviderRegistry([FakeProvider(clock)])
    a = await make_asset(sf)
    b = await make_asset(sf, provider_symbol="B", jesse_symbol="B-USD")
    await jobs.enqueue(sf, registry, clock, a.id, "backfill")
    await jobs.enqueue(sf, registry, clock, b.id, "backfill")
    failed_id = await jobs.claim_next(sf, clock)
    await jobs.fail(sf, clock, failed_id, "HTTP 451 from upstream", 0.0)
    active_symbol = "B-USD" if (await jobs.get_job(sf, failed_id)).asset_id == a.id else "FAKE-USD"

    page = (await client.get("/jobs")).text
    assert 'Active <span class="count">1</span>' in page and 'Failed <span class="count">1</span>' in page
    assert 'href="/jobs" aria-current="page"' in page

    failed = (await client.get("/jobs?status=failed")).text
    assert "HTTP 451 from upstream" in failed and active_symbol not in failed

    active = (await client.get("/jobs?status=active")).text
    assert active_symbol in active and "HTTP 451" not in active
    assert 'name="next" value="/jobs?status=active"' in active


async def test_jobs_rows_poll_and_keep_the_filter(client, sf, clock):
    idle = (await client.get("/jobs/rows", params={"status": "failed"})).text
    assert idle.lstrip().startswith('<tbody id="job-rows"') and 'hx-get="/jobs/rows?status=failed"' in idle
    assert 'hx-trigger="every 30s"' in idle and "No failed jobs." in idle
    asset = await make_asset(sf)
    await jobs.enqueue(sf, ProviderRegistry([FakeProvider(clock)]), clock, asset.id, "backfill")
    assert 'hx-trigger="every 2s"' in (await client.get("/jobs/rows")).text


async def test_jobs_rejects_an_unknown_filter(client):
    assert (await client.get("/jobs", params={"status": "bogus"})).status_code == 422
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_jobs_page.py -q`
Expected: FAIL.

- [ ] **Step 3: Replace `jobs_page` in `app/web/routes.py`**

```python
JobFilter = Annotated[str | None, Query(pattern="^(active|failed)$")]


async def _jobs_context(svc, status: str | None) -> dict:
    counts = await jobs.status_counts(svc.sf)
    active_count = sum(counts.get(s, 0) for s in ACTIVE_STATUSES)
    return {
        "rows": job_rows(svc, await jobs.list_recent(svc.sf, status=status)),
        "status": status,
        "active_count": active_count,
        "failed_count": counts.get("failed", 0),
        "active": active_count > 0,
    }


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request, status: JobFilter = None):
    return templates.TemplateResponse(request, "jobs.html", await _jobs_context(services(request), status))


@router.get("/jobs/rows", response_class=HTMLResponse)
async def jobs_rows(request: Request, status: JobFilter = None):
    return templates.TemplateResponse(request, "_jobs_tbody.html", await _jobs_context(services(request), status))
```

- [ ] **Step 4: Replace `app/web/templates/jobs.html`**

```jinja
{% extends "base.html" %}
{% set page_title = "Jobs" %}{% set active_nav = "jobs" %}
{% block actions %}
<nav class="segmented" aria-label="Filter jobs">
  <a href="/jobs"{% if not status %} class="active"{% endif %}>All</a>
  <a href="/jobs?status=active"{% if status == "active" %} class="active"{% endif %}>Active <span class="count">{{ active_count }}</span></a>
  <a href="/jobs?status=failed"{% if status == "failed" %} class="active"{% endif %}>Failed <span class="count">{{ failed_count }}</span></a>
</nav>
{% endblock %}
{% block content %}
<div class="card card-scroll">
  <table class="table">
    <thead>
      <tr>
        <th>#</th><th>Asset</th><th>Kind</th><th>Status</th><th>Range (UTC)</th><th class="num">Requests</th>
        <th class="num">Candles added</th><th>Run time</th><th>Created</th><th>Finished</th><th><span class="sr-only">Actions</span></th>
      </tr>
    </thead>
    {% include "_jobs_tbody.html" %}
  </table>
</div>
<p class="muted small">Times are UTC. Finished jobs are kept for 30 days.</p>
{% endblock %}
```

- [ ] **Step 5: Create `app/web/templates/_jobs_tbody.html`**

```jinja
{% from "_macros.html" import status_pill %}
{% set query = "?status=" ~ status if status else "" %}
<tbody id="job-rows" hx-get="/jobs/rows{{ query }}" hx-trigger="every {{ '2s' if active else '30s' }}" hx-swap="outerHTML">
{% for row in rows %}
  <tr>
    <td class="muted">{{ row.job.id }}</td>
    <td><a class="sym" href="/assets/{{ row.asset.id }}">{{ row.asset.jesse_symbol }}</a></td>
    <td>{{ row.job.kind }}</td>
    <td class="wrap">{{ status_pill(row.job, row.progress) }}</td>
    <td>{{ row.job.range_start|dt }} → {{ row.job.range_end|dt }}</td>
    <td class="num">{{ row.job.requests_made|num }}</td>
    <td class="num">{{ row.job.candles_added|num }}</td>
    <td>{{ row.job.run_seconds|duration }}</td>
    <td>{{ row.job.created_at|dt }}</td>
    <td>{{ row.job.finished_at|dt }}</td>
    <td class="num">
      {% if row.job.status in ACTIVE_STATUSES %}
      <form class="inline" method="post" action="/jobs/{{ row.job.id }}/cancel">
        <input type="hidden" name="next" value="/jobs{{ query }}">
        <button class="btn btn-sm">Cancel</button>
      </form>
      {% endif %}
    </td>
  </tr>
{% else %}
  <tr class="table-empty"><td colspan="11">{{ "No active jobs." if status == "active" else ("No failed jobs." if status == "failed" else "No jobs yet.") }}</td></tr>
{% endfor %}
</tbody>
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest -q`
Expected: all pass (existing `test_jobs_page_and_cancel` still finds "FAKE-USD", "queued", "Cancel").

- [ ] **Step 7: Commit**

```bash
git add app/web/routes.py app/web/templates/jobs.html app/web/templates/_jobs_tbody.html tests/test_jobs_page.py
git commit -m "feat(ui): jobs page with active/failed tabs and live polling"
```

---

### Task 8: Add-asset page

**Files:**
- Create: `tests/test_add_asset_page.py`
- Modify: `app/web/routes.py` (`_new_page`, `estimate`), `app/web/templates/asset_new.html`, `app/web/templates/_search_results.html`, `app/web/templates/_asset_details.html`

**Interfaces:**
- Consumes: `source_statuses` (Task 4), `alert` macro.
- Produces: `_new_page(request, error=None, status_code=200)` is now `async`; source radios `name="search_provider"`; search `hx-include="[name=search_provider]:checked"`.

- [ ] **Step 1: Write the failing tests** — `tests/test_add_asset_page.py`

```python
import httpx

from app.config import EnvConfig
from app.main import create_app
from app.providers.base import ProviderRegistry
from tests.fakes import FakeProvider


async def test_add_page_lists_sources_as_radio_cards(client):
    page = (await client.get("/assets/new")).text
    assert 'type="radio" name="search_provider" value="fake" checked' in page and "No key needed" in page
    assert 'hx-include="[name=search_provider]:checked"' in page and "event.preventDefault()" in page
    assert 'id="details"' in page and "Pick a symbol above" in page
    assert '<a class="crumb" href="/assets">' in page


async def test_search_results_are_a_list(client):
    r = await client.get("/assets/search", params={"search_provider": "fake", "q": "fake"})
    assert 'class="result-list"' in r.text and "Fake/USD" in r.text


async def test_add_page_flags_missing_keys(sf, clock):
    keyed = FakeProvider(clock)
    keyed.name, keyed.label = "alpaca", "Alpaca (US stocks & ETFs)"
    env = EnvConfig(_env_file=None, database_url="postgresql+asyncpg://unused@localhost/unused", alpaca_key_id="", alpaca_secret_key="")
    app = create_app(env, sf=sf, clock=clock, registry=ProviderRegistry([keyed]), start_background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        page = (await c.get("/assets/new")).text
    assert "Key missing" in page and "US stocks &amp; ETFs" in page


async def test_rejected_asset_shows_the_error_on_the_page(client):
    form = {"provider": "fake", "provider_symbol": "FAKEUSD", "asset_class": "crypto", "jesse_symbol": "bad symbol", "start_date": "2024-01-01"}
    r = await client.post("/assets", data=form)
    assert r.status_code == 400 and "alert-danger" in r.text and "Jesse symbol" in r.text
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_add_asset_page.py -q`
Expected: FAIL.

- [ ] **Step 3: Update `app/web/routes.py`**

Import `from app.services.sources import source_statuses`. Replace `_new_page`:
```python
async def _new_page(request: Request, error: str | None = None, status_code: int = 200):
    svc = services(request)
    context = {"sources": source_statuses(svc.registry, await svc.settings.load()), "error": error}
    return templates.TemplateResponse(request, "asset_new.html", context, status_code=status_code)
```
and make every caller await it: `return await _new_page(request)` in `new_asset`, and `return await _new_page(request, ..., 400)` at each of the five error returns in `create`.

In `estimate`, change the error fragment to `return HTMLResponse(f'<span class="field-error">{escape(str(exc))}</span>')`.

- [ ] **Step 4: Replace `app/web/templates/asset_new.html`**

```jinja
{% extends "base.html" %}
{% from "_macros.html" import alert %}
{% set page_title = "Add asset" %}{% set active_nav = "assets" %}
{% block crumb %}<a class="crumb" href="/assets">Assets /</a>{% endblock %}
{% block content %}
<form class="stack page-narrow" method="post" action="/assets">
  {{ alert(error) }}
  <section class="card">
    <div class="card-header"><span class="step-num">1</span><div><h2>Choose a data source</h2><p>Where the candles come from.</p></div></div>
    <div class="card-body">
      <div class="source-grid" hx-get="/assets/search" hx-trigger="change" hx-target="#results" hx-include="[name=q], [name=search_provider]:checked">
        {% for s in sources %}
        <label class="source-option">
          <input type="radio" name="search_provider" value="{{ s.name }}"{% if loop.first %} checked{% endif %}>
          <span class="source-name">{{ s.label }}</span>
          {% if s.coverage %}<span class="source-meta">{{ s.coverage }}</span>{% endif %}
          <span class="source-meta">{% if not s.needs_key %}No key needed{% elif s.ready %}{{ icon("check") }} Key configured{% else %}<a class="link" href="/settings">Key missing → Settings</a>{% endif %}</span>
        </label>
        {% endfor %}
      </div>
    </div>
  </section>
  <section class="card">
    <div class="card-header"><span class="step-num">2</span><div><h2>Find the symbol</h2><p>Search the selected source.</p></div></div>
    <div class="card-body">
      <input class="input" type="search" name="q" placeholder="e.g. BTCUSDT, AAPL, EURUSD" autocomplete="off" aria-label="Search symbol"
             hx-on:keydown="if(event.key==='Enter'){event.preventDefault()}"
             hx-get="/assets/search" hx-trigger="input changed delay:300ms, search"
             hx-target="#results" hx-include="[name=search_provider]:checked">
      <div id="results"></div>
    </div>
  </section>
  <section class="card">
    <div class="card-header"><span class="step-num">3</span><div><h2>Configure</h2><p>Name it for Jesse and pick how far back to download.</p></div></div>
    <div class="card-body" id="details"><p class="muted">Pick a symbol above to configure the download.</p></div>
  </section>
</form>
{% endblock %}
```

- [ ] **Step 5: Replace `app/web/templates/_search_results.html`**

```jinja
{% from "_macros.html" import alert %}
{% if error %}{{ alert(error) }}
{% elif symbols %}
<ul class="result-list">
  {% for s in symbols %}
  <li>
    <a href="#" hx-get="/assets/new/details" hx-target="#details"
       hx-vals='{{ {"provider": provider, "symbol": s.provider_symbol, "asset_class": s.asset_class, "jesse_symbol": s.suggested_jesse_symbol}|tojson }}'>
      <span class="sym">{{ s.provider_symbol }}</span><span class="muted">{{ s.name }} · {{ s.asset_class }}</span>
    </a>
  </li>
  {% endfor %}
</ul>
{% elif q %}<p class="muted">No matches.</p>
{% endif %}
```

- [ ] **Step 6: Replace `app/web/templates/_asset_details.html`**

```jinja
{% from "_macros.html" import alert %}
{% if error %}{{ alert(error) }}
{% else %}
<input type="hidden" name="provider" value="{{ provider }}">
<input type="hidden" name="provider_symbol" value="{{ symbol }}">
<div class="form-grid">
  <div class="field full"><span class="label">Symbol</span><span class="sym">{{ symbol }}</span></div>
  <div class="field">
    <label for="jesse_symbol">Jesse symbol</label>
    <input class="input" id="jesse_symbol" name="jesse_symbol" value="{{ jesse_symbol }}" required pattern="[A-Z0-9]+-[A-Z0-9]+">
    <span class="hint">Use this symbol with exchange "Custom Data" when importing into Jesse.</span>
  </div>
  <div class="field">
    <label for="asset_class">Asset class</label>
    <select class="input" id="asset_class" name="asset_class">
      {% for c in asset_classes %}<option value="{{ c }}" {% if c == asset_class %}selected{% endif %}>{{ c }}</option>{% endfor %}
    </select>
  </div>
  <div class="field">
    <label for="start_date">Start date</label>
    <input class="input" type="date" id="start_date" name="start_date" value="{{ earliest }}" min="{{ earliest }}" required
           hx-get="/assets/estimate" hx-trigger="change" hx-target="#estimate" hx-include="[name=provider]">
    <span class="hint">Earliest available: {{ earliest }}</span>
  </div>
  <div class="field">
    <span class="label">Estimate</span>
    <p id="estimate" class="callout">{{ estimate }}</p>
  </div>
  <div class="form-actions full"><button type="submit" class="btn btn-primary">{{ icon("download") }}Add and start download</button></div>
</div>
{% endif %}
```

- [ ] **Step 7: Run the tests**

Run: `.venv/bin/pytest -q`
Expected: all pass (`test_search_details_and_estimate`, `test_search_input_blocks_enter_submit`, `test_unknown_provider_renders_error_in_search_and_details`, `test_estimate_errors_render_fragments` keep passing).

- [ ] **Step 8: Commit**

```bash
git add app/web/routes.py app/web/templates/asset_new.html app/web/templates/_search_results.html app/web/templates/_asset_details.html tests/test_add_asset_page.py
git commit -m "feat(ui): step-by-step add-asset page with source cards and key status"
```

---

### Task 9: Settings page and delete confirmation

**Files:**
- Modify: `app/web/templates/settings.html`, `app/web/templates/asset_delete.html`, `tests/test_web_pages.py`

**Interfaces:**
- Consumes: `alert` macro, `icon`, `num` filter.
- Produces: settings form `id="settings-form"` (header button submits it via `form="settings-form"`); delete page Cancel goes back to `/assets/{id}`.

- [ ] **Step 1: Write the failing tests** (edit `tests/test_web_pages.py`)

- In `test_settings_roundtrip`, replace `assert r.status_code == 400 and 'class="error"' in r.text` with:
```python
    assert r.status_code == 400 and 'class="alert alert-danger"' in r.text
    page = (await client.get("/settings")).text
    assert 'id="settings-form"' in page and 'form="settings-form"' in page and 'class="switch"' in page
    assert 'href="/settings" aria-current="page"' in page
```
- In `test_delete_asset`, after `assert r.status_code == 200 and "FAKE-USD" in r.text` add:
```python
    assert "Delete FAKE-USD?" in r.text and "btn-danger-solid" in r.text and f'href="/assets/{asset.id}"' in r.text
```
  and change `assert (await client.post(f"/assets/{asset.id}/delete")).status_code == 303` to:
```python
    r = await client.post(f"/assets/{asset.id}/delete")
    assert r.status_code == 303 and r.headers["location"] == "/assets"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_web_pages.py -q`
Expected: FAIL on the new assertions.

- [ ] **Step 3: Replace `app/web/templates/settings.html`**

```jinja
{% extends "base.html" %}
{% from "_macros.html" import alert %}
{% set page_title = "Settings" %}{% set active_nav = "settings" %}
{% block actions %}<button class="btn btn-primary" type="submit" form="settings-form">Save changes</button>{% endblock %}
{% block content %}
<form id="settings-form" class="stack page-narrow" method="post" action="/settings">
  {{ alert(error) }}
  <section class="card">
    <div class="card-header"><div><h2>Scheduled updates</h2><p>Update all enabled assets automatically.</p></div></div>
    <div class="card-body form-grid">
      <label class="check full"><input type="checkbox" class="switch" name="schedule_enabled" {% if s.schedule_enabled %}checked{% endif %}> Enabled</label>
      <div class="field">
        <label for="schedule_cron">Schedule (cron, UTC)</label>
        <input class="input" id="schedule_cron" name="schedule_cron" value="{{ s.schedule_cron }}" list="cron-presets" required>
        <datalist id="cron-presets">{% for value, label in presets %}<option value="{{ value }}">{{ label }}</option>{% endfor %}</datalist>
        <span class="hint">Presets: {% for value, label in presets %}<code>{{ value }}</code> {{ label }}{% if not loop.last %} · {% endif %}{% endfor %}.{% if next_run %} Next run {{ next_run|dt }} UTC.{% endif %}</span>
      </div>
      <div class="field">
        <label for="worker_concurrency">Parallel downloads</label>
        <input class="input" type="number" id="worker_concurrency" name="worker_concurrency" min="1" max="10" value="{{ s.worker_concurrency }}" required>
        <span class="hint">1–10 assets download at the same time.</span>
      </div>
    </div>
  </section>
  <section class="card">
    <div class="card-header"><div><h2>Alpaca</h2><p>US stocks &amp; ETFs · free account at alpaca.markets</p></div></div>
    <div class="card-body">
      {% if s.alpaca_from_env %}
      <div class="callout callout-muted">{{ icon("lock") }}<span>Set through environment variables (ALPACA_KEY_ID / ALPACA_SECRET_KEY). Edit your .env file to change it.</span></div>
      {% else %}
      <div class="form-grid">
        <div class="field"><label for="alpaca_key_id">Key ID</label><input class="input" id="alpaca_key_id" name="alpaca_key_id" autocomplete="off"></div>
        <div class="field"><label for="alpaca_secret_key">Secret key</label><input class="input" type="password" id="alpaca_secret_key" name="alpaca_secret_key" autocomplete="off"></div>
        <p class="hint full">Current key: {{ key_hint }}. Leave the fields blank to keep it.</p>
      </div>
      {% endif %}
    </div>
  </section>
  <section class="card">
    <div class="card-header"><div><h2>Twelve Data</h2><p>US stocks, forex &amp; metals · free key at twelvedata.com (email signup) · 800 requests/day</p></div></div>
    <div class="card-body">
      {% if s.twelvedata_from_env %}
      <div class="callout callout-muted">{{ icon("lock") }}<span>Key set through the TWELVEDATA_API_KEY environment variable. Edit your .env file to change it.</span></div>
      {% else %}
      <div class="field">
        <label for="twelvedata_api_key">API key</label>
        <input class="input" type="password" id="twelvedata_api_key" name="twelvedata_api_key" autocomplete="off">
        <span class="hint">Current key: {{ twelvedata_hint }}. Leave the key blank to keep it.</span>
      </div>
      {% endif %}
    </div>
  </section>
  <div class="form-actions"><button class="btn btn-primary" type="submit">Save changes</button></div>
</form>
{% endblock %}
```

- [ ] **Step 4: Replace `app/web/templates/asset_delete.html`**

```jinja
{% extends "base.html" %}
{% set page_title = "Delete " ~ asset.jesse_symbol %}{% set active_nav = "assets" %}
{% block crumb %}<a class="crumb" href="/assets/{{ asset.id }}">{{ asset.jesse_symbol }} /</a>{% endblock %}
{% block heading %}Delete asset{% endblock %}
{% block content %}
<div class="card confirm">
  <div class="card-body stack-sm">
    <h2 class="danger-title">Delete {{ asset.jesse_symbol }}?</h2>
    <p>This removes the asset, its jobs and all {{ count|num }} stored candles. This cannot be undone.</p>
    <form class="form-actions" method="post" action="/assets/{{ asset.id }}/delete">
      <button type="submit" class="btn btn-danger-solid">{{ icon("trash-2") }}Delete</button>
      <a class="btn" href="/assets/{{ asset.id }}">Cancel</a>
    </form>
  </div>
</div>
{% endblock %}
```

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest -q`
Expected: all pass (`test_env_keys_are_read_only` finds "environment"; `test_twelvedata_key_is_masked_and_never_rendered` finds "•••• 4321" and "twelvedata.com").

- [ ] **Step 6: Check no page references a CDN or Pico any more**

Run: `grep -rnE "unpkg|jsdelivr|pico|role=\"button\"|class=\"secondary" app/web/templates`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add app/web/templates/settings.html app/web/templates/asset_delete.html tests/test_web_pages.py
git commit -m "feat(ui): settings cards and delete confirmation in the new design"
```

---

### Task 10: Visual verification in a real browser, then PR

**Files:**
- Modify: only files where the browser pass finds defects (each fix gets a test where pytest can express it).

**Interfaces:**
- Consumes: the whole UI.

- [ ] **Step 1: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 2: Start an isolated stack (never the live one on 9016)**

```bash
cd /Users/minione/repos/asuras-csv
APP_PORT=9116 docker compose -p ohlcv-e2e up -d --build
until curl -sf http://localhost:9116/ >/dev/null; do sleep 2; done; echo up
```

- [ ] **Step 3: Seed one real asset**

Open `http://localhost:9116/assets/new` with Playwright (`mcp__playwright__browser_navigate`), pick **Binance**, search `BTCUSDT`, click the result, set the start date to 3 days ago, click **Add and start download**. Expected: lands on `/assets/1` with a toast "BTC-USDT added, download queued" and a running status pill.

- [ ] **Step 4: Screenshot every page in dark, light and mobile**

For each of `/`, `/assets`, `/assets/1`, `/assets/new`, `/jobs`, `/jobs?status=failed`, `/settings`, `/assets/1/delete`:
1. `mcp__playwright__browser_emulate_media` with `colorScheme: "dark"`, viewport 1440×900 → `browser_take_screenshot`.
2. Same with `colorScheme: "light"`.
3. `browser_resize` to 390×844 (dark) → screenshot; open the sidebar with the menu button → screenshot.
4. `browser_evaluate`: `() => document.documentElement.scrollWidth <= window.innerWidth` must be `true` at 390 px.
5. `browser_console_messages`: no errors.

- [ ] **Step 5: Exercise the interactive pieces**

On `/assets` while the download runs (2 s polling): open a row's ⋯ menu and wait 5 s (menu stays open); tick the checkbox and wait 5 s (still ticked, bulk bar shows "1 selected"); type `eth` in the filter (row hides, "No assets match the filters." shows). On `/assets/1`: switch chart ranges; toggle the theme from the sidebar (chart colors follow). Stop the app container for 5 s (`docker compose -p ohlcv-e2e stop app`, then `start app`) and confirm "Reconnecting…" appears and disappears.

- [ ] **Step 6: Fix anything found, re-run tests, commit**

```bash
.venv/bin/pytest -q
git add -A app tests
git commit -m "fix(ui): polish from the browser pass"
```
(Skip the commit if nothing changed.)

- [ ] **Step 7: Tear down the isolated stack only**

```bash
docker compose -p ohlcv-e2e down -v
```
(`-v` is safe here: it removes only the `ohlcv-e2e_pgdata` volume. Never run it without `-p ohlcv-e2e`.)

- [ ] **Step 8: Push and open the PR**

```bash
git push -u origin feat/saas-ui
gh pr create --title "feat(ui): SaaS redesign — Asuras CSV shell, overview, asset detail" --body "$(cat <<'EOF'
## Summary
- Linear-style sidebar shell with light/dark themes, hand-written CSS, all assets vendored (no CDNs)
- New Overview home page: KPIs, 30-day sparklines, recent jobs, data-source status
- Assets list with filters, bulk ZIP bar and row menus; new asset detail page (chart, export, settings, job history)
- Jobs tabs (All/Active/Failed) with live polling, step-style Add asset page, settings cards, toast feedback

Spec: docs/superpowers/specs/2026-10-02-saas-ui-redesign-design.md
Plan: docs/superpowers/plans/2026-10-02-saas-ui-redesign.md

## Test plan
- [ ] `.venv/bin/pytest -q` green
- [ ] Browser pass (dark/light/390 px) on an isolated compose project

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 9: Deploy only after the PR is merged and the user asks**

`docker compose up -d --build app` (rebuilds the app only; the database and its volume are untouched).

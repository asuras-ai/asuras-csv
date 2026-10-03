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

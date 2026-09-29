"""HTML routes (Jinja2 + HTMX)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.domain import ProviderError
from app.models import Asset, Job
from app.services import assets as asset_service
from app.services import jobs
from app.services.assets import EMPTY_STATS, AssetStats
from app.services.progress import JobProgress, estimate_text, format_duration, job_progress

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["dt"] = lambda value: value.strftime("%Y-%m-%d %H:%M") if value else "—"
templates.env.filters["duration"] = format_duration


def services(request: Request):
    return request.app.state.services


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _start_of(day: date) -> datetime:
    return datetime.combine(day, time(), UTC)


@dataclass(frozen=True)
class AssetRow:
    asset: Asset
    provider_label: str
    stats: AssetStats
    job: Job | None
    progress: JobProgress | None


async def _asset_rows(svc) -> list[AssetRow]:
    all_assets = await asset_service.list_assets(svc.sf)
    stats = await svc.stats.get(svc.sf)
    latest = await jobs.latest_jobs_by_asset(svc.sf)
    rows = []
    for asset in all_assets:
        provider = svc.registry.find(asset.provider)
        job = latest.get(asset.id)
        progress = job_progress(job, asset.fetched_until, provider) if job and provider else None
        label = provider.label if provider else asset.provider
        rows.append(AssetRow(asset, label, stats.get(asset.id, EMPTY_STATS), job, progress))
    return rows


@router.get("/", response_class=HTMLResponse)
async def assets_page(request: Request):
    return templates.TemplateResponse(request, "assets.html", {"rows": await _asset_rows(services(request))})


@router.get("/assets/rows", response_class=HTMLResponse)
async def asset_rows(request: Request):
    return templates.TemplateResponse(request, "_asset_rows.html", {"rows": await _asset_rows(services(request))})


def _new_page(request: Request, error: str | None = None, status_code: int = 200):
    context = {"providers": services(request).registry.all(), "error": error}
    return templates.TemplateResponse(request, "asset_new.html", context, status_code=status_code)


@router.get("/assets/new", response_class=HTMLResponse)
async def new_asset(request: Request):
    return _new_page(request)


@router.get("/assets/search", response_class=HTMLResponse)
async def search(request: Request, search_provider: str, q: str = ""):
    symbols, error = [], None
    if q.strip():
        try:
            symbols = await services(request).registry.get(search_provider).search_symbols(q)
        except ProviderError as exc:
            error = str(exc)
    context = {"symbols": symbols, "error": error, "q": q, "provider": search_provider}
    return templates.TemplateResponse(request, "_search_results.html", context)


@router.get("/assets/new/details", response_class=HTMLResponse)
async def new_asset_details(request: Request, provider: str, symbol: str, asset_class: str, jesse_symbol: str):
    svc = services(request)
    context = {"provider": provider, "symbol": symbol, "asset_class": asset_class, "jesse_symbol": jesse_symbol, "error": None}
    try:
        p = svc.registry.get(provider)
        earliest = (await p.earliest_available(symbol)).date()
        context |= {
            "asset_classes": p.asset_classes,
            "earliest": earliest.isoformat(),
            "estimate": estimate_text(p, _start_of(earliest), p.available_until(svc.clock.now())),
        }
    except ProviderError as exc:
        context["error"] = str(exc)
    return templates.TemplateResponse(request, "_asset_details.html", context)


@router.get("/assets/estimate", response_class=HTMLResponse)
async def estimate(request: Request, provider: str, start_date: date):
    svc = services(request)
    p = svc.registry.get(provider)
    return HTMLResponse(estimate_text(p, _start_of(start_date), p.available_until(svc.clock.now())))


@router.post("/assets")
async def create(
    request: Request,
    provider: Annotated[str, Form()],
    provider_symbol: Annotated[str, Form()],
    asset_class: Annotated[str, Form()],
    jesse_symbol: Annotated[str, Form()],
    start_date: Annotated[date, Form()],
):
    svc = services(request)
    p = svc.registry.find(provider)
    if p is None or asset_class not in p.asset_classes:
        return _new_page(request, "Unknown provider or asset class.", 400)
    try:
        asset = await asset_service.create_asset(
            svc.sf,
            provider=provider,
            provider_symbol=provider_symbol,
            asset_class=asset_class,
            jesse_symbol=jesse_symbol,
            start_date=start_date,
        )
    except ValueError as exc:
        return _new_page(request, str(exc), 400)
    await jobs.enqueue(svc.sf, svc.registry, svc.clock, asset.id, "backfill")
    svc.stats.invalidate()
    return redirect("/")


@router.post("/assets/update-all")
async def update_all(request: Request):
    svc = services(request)
    await jobs.enqueue_all(svc.sf, svc.registry, svc.clock)
    return redirect("/")


@router.post("/assets/{asset_id}/update")
async def update_one(request: Request, asset_id: int):
    svc = services(request)
    await jobs.enqueue(svc.sf, svc.registry, svc.clock, asset_id, "update")
    return redirect("/")

"""HTML routes (Jinja2 + HTMX)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.domain import ProviderError
from app.models import ACTIVE_STATUSES, Asset, Job
from app.services import assets as asset_service
from app.services import jobs
from app.services.assets import EMPTY_STATS, AssetStats
from app.services.export import day_bounds, export_filename, export_range, stream_csv
from app.services.charts import DEFAULT_RANGE, RANGES, candles_for_chart, interval_label
from app.services.zip_export import build_zip, iter_and_close
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
async def estimate(request: Request, provider: str, start_date: str = ""):
    svc = services(request)
    try:
        start = date.fromisoformat(start_date.strip())
    except ValueError:
        return HTMLResponse("")
    try:
        p = svc.registry.get(provider)
        return HTMLResponse(estimate_text(p, _start_of(start), p.available_until(svc.clock.now())))
    except ProviderError as exc:
        return HTMLResponse(f'<span class="error">{escape(str(exc))}</span>')


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
    if start_date > svc.clock.now().date():
        return _new_page(request, "Start date cannot be in the future.", 400)
    try:
        earliest = (await p.earliest_available(provider_symbol)).date()
    except ProviderError as exc:
        return _new_page(request, str(exc), 400)
    if start_date < earliest:
        return _new_page(request, f"Start date is before the earliest available data ({earliest.isoformat()}).", 400)
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
    if await asset_service.get_asset(svc.sf, asset_id) is None:
        raise HTTPException(404, "Asset not found")
    await jobs.enqueue(svc.sf, svc.registry, svc.clock, asset_id, "update")
    return redirect("/")


async def _asset_or_404(svc, asset_id: int) -> Asset:
    asset = await asset_service.get_asset(svc.sf, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    return asset


@router.get("/assets/{asset_id}/edit", response_class=HTMLResponse)
async def edit_page(request: Request, asset_id: int):
    asset = await _asset_or_404(services(request), asset_id)
    return templates.TemplateResponse(request, "asset_edit.html", {"asset": asset, "error": None})


@router.post("/assets/{asset_id}/edit")
async def edit(
    request: Request,
    asset_id: int,
    jesse_symbol: Annotated[str, Form()],
    enabled: Annotated[str | None, Form()] = None,
):
    svc = services(request)
    asset = await _asset_or_404(svc, asset_id)
    try:
        await asset_service.update_asset(svc.sf, asset_id, jesse_symbol=jesse_symbol, enabled=enabled is not None)
    except ValueError as exc:
        return templates.TemplateResponse(request, "asset_edit.html", {"asset": asset, "error": str(exc)}, status_code=400)
    return redirect("/")


@router.get("/assets/{asset_id}/delete", response_class=HTMLResponse)
async def delete_page(request: Request, asset_id: int):
    svc = services(request)
    asset = await _asset_or_404(svc, asset_id)
    count = (await svc.stats.get(svc.sf)).get(asset_id, EMPTY_STATS).count
    return templates.TemplateResponse(request, "asset_delete.html", {"asset": asset, "count": count})


@router.post("/assets/{asset_id}/delete")
async def delete(request: Request, asset_id: int):
    svc = services(request)
    await _asset_or_404(svc, asset_id)
    await asset_service.delete_asset(svc.sf, asset_id)
    svc.stats.invalidate()
    return redirect("/")


@router.get("/assets/{asset_id}/export", response_class=HTMLResponse)
async def export_page(request: Request, asset_id: int):
    svc = services(request)
    asset = await _asset_or_404(svc, asset_id)
    rng = await export_range(svc.sf, asset_id, None, None)
    return templates.TemplateResponse(request, "export.html", {"asset": asset, "rng": rng})


def _parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"Invalid date: {value!r}") from None


@router.get("/assets/{asset_id}/export.csv")
async def export_csv(request: Request, asset_id: int, start: str | None = None, end: str | None = None):
    svc = services(request)
    asset = await _asset_or_404(svc, asset_id)
    start_dt, end_dt = day_bounds(_parse_day(start), _parse_day(end))
    rng = await export_range(svc.sf, asset_id, start_dt, end_dt)
    if rng is None:
        raise HTTPException(404, "No candles in this range")
    filename = export_filename(asset.jesse_symbol, rng)
    return StreamingResponse(
        stream_csv(svc.sf, asset_id, rng.first, rng.last + timedelta(minutes=1)),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/assets/{asset_id}/chart", response_class=HTMLResponse)
async def chart_page(request: Request, asset_id: int):
    asset = await _asset_or_404(services(request), asset_id)
    context = {"asset": asset, "ranges": list(RANGES), "default_range": DEFAULT_RANGE}
    return templates.TemplateResponse(request, "chart.html", context)


@router.get("/assets/{asset_id}/candles.json")
async def candles_json(request: Request, asset_id: int, range: Annotated[str, Query(pattern="^(1D|1W|1M|6M|1Y|All)$")] = DEFAULT_RANGE):
    svc = services(request)
    await _asset_or_404(svc, asset_id)
    bucket, candles = await candles_for_chart(svc.sf, asset_id, range)
    return {"interval": interval_label(bucket), "candles": candles}


@router.get("/export.zip")
async def export_zip(request: Request, ids: Annotated[list[int] | None, Query()] = None, start: str | None = None, end: str | None = None):
    svc = services(request)
    if not ids:
        raise HTTPException(400, "Select at least one asset")
    start_dt, end_dt = day_bounds(_parse_day(start), _parse_day(end))
    assets = [await _asset_or_404(svc, asset_id) for asset_id in dict.fromkeys(ids)]
    tmp = await build_zip(svc.sf, assets, start_dt, end_dt)
    if tmp is None:
        raise HTTPException(404, "No candles in this range for the selected assets")
    filename = f"ohlcv-export-{datetime.now(UTC):%Y%m%d-%H%M%S}.zip"
    return StreamingResponse(
        iter_and_close(tmp),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@dataclass(frozen=True)
class JobRow:
    job: Job
    asset: Asset
    progress: JobProgress | None


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    svc = services(request)
    rows = []
    for job, asset in await jobs.list_recent(svc.sf):
        provider = svc.registry.find(asset.provider)
        progress = job_progress(job, asset.fetched_until, provider) if provider else None
        rows.append(JobRow(job, asset, progress))
    return templates.TemplateResponse(request, "jobs.html", {"rows": rows, "active_statuses": ACTIVE_STATUSES})


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: int, next: Annotated[str, Form()] = "/jobs"):
    svc = services(request)
    await jobs.cancel(svc.sf, svc.clock, job_id)
    same_origin = next.startswith("/") and not next.startswith(("//", "/\\"))
    return redirect(next if same_origin else "/jobs")


CRON_PRESETS = [
    ("0 * * * *", "hourly"),
    ("0 */6 * * *", "every 6 hours"),
    ("0 2 * * *", "daily at 02:00 UTC"),
]


def _key_hint(key_id: str) -> str:
    return f"•••• {key_id[-4:]}" if key_id else "not set"


async def _settings_page(request: Request, error: str | None = None, status_code: int = 200):
    svc = services(request)
    current = await svc.settings.load()
    context = {
        "s": current,
        "error": error,
        "saved": request.query_params.get("saved") == "1",
        "presets": CRON_PRESETS,
        "next_run": svc.scheduler.next_run() if svc.scheduler else None,
        "key_hint": _key_hint(current.alpaca_key_id),
        "twelvedata_hint": _key_hint(current.twelvedata_api_key),
    }
    return templates.TemplateResponse(request, "settings.html", context, status_code=status_code)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return await _settings_page(request)


@router.post("/settings")
async def save_settings(
    request: Request,
    schedule_cron: Annotated[str, Form()],
    worker_concurrency: Annotated[str, Form()],
    schedule_enabled: Annotated[str | None, Form()] = None,
    alpaca_key_id: Annotated[str, Form()] = "",
    alpaca_secret_key: Annotated[str, Form()] = "",
    twelvedata_api_key: Annotated[str, Form()] = "",
):
    svc = services(request)
    current = await svc.settings.load()
    values = {
        "schedule_enabled": "true" if schedule_enabled else "false",
        "schedule_cron": schedule_cron.strip(),
        "worker_concurrency": worker_concurrency.strip(),
    }
    if not current.alpaca_from_env:
        if alpaca_key_id.strip():
            values["alpaca_key_id"] = alpaca_key_id.strip()
        if alpaca_secret_key.strip():
            values["alpaca_secret_key"] = alpaca_secret_key.strip()
    if not current.twelvedata_from_env and twelvedata_api_key.strip():
        values["twelvedata_api_key"] = twelvedata_api_key.strip()
    try:
        await svc.settings.save(values)
    except ValueError as exc:
        return await _settings_page(request, str(exc), 400)
    updated = await svc.settings.load()
    if svc.worker:
        svc.worker.resize(updated.worker_concurrency)
    if svc.scheduler:
        svc.scheduler.apply(updated)
    return redirect("/settings?saved=1")

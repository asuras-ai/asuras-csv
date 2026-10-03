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

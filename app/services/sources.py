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

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

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.progress import estimate_text, format_duration, job_progress
from tests.fakes import FakeProvider

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def job(**overrides):
    values = {
        "status": "running",
        "range_start": T0,
        "range_end": T0 + timedelta(minutes=100),
        "requests_made": 0,
        "run_seconds": 0.0,
    } | overrides
    return SimpleNamespace(**values)


def test_progress_and_eta_use_policy_rate_at_start():
    p = job_progress(job(), T0 + timedelta(minutes=50), FakeProvider())
    assert p.percent == 50.0
    assert p.eta_seconds == pytest.approx(0.5)  # 5 chunks left at 10 requests/s


def test_eta_uses_measured_rate_after_20_requests():
    p = job_progress(job(requests_made=40, run_seconds=80.0), T0 + timedelta(minutes=50), FakeProvider())
    assert p.eta_seconds == pytest.approx(10.0)  # 5 chunks left at 0.5 requests/s


def test_finished_jobs_have_full_progress_and_no_eta():
    p = job_progress(job(status="done"), None, FakeProvider())
    assert (p.percent, p.eta_seconds) == (100.0, None)


def test_cursor_before_range_counts_as_zero():
    assert job_progress(job(), None, FakeProvider()).percent == 0.0


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(30, "< 1 min"), (120, "2 min"), (3600, "1 h"), (7800, "2 h 10 min"), (100_000, "1 d 3 h"), (172_800, "2 d")],
)
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


def test_estimate_text():
    assert estimate_text(FakeProvider(), T0, T0 + timedelta(minutes=100)) == "≈ 10 requests · < 1 min"
    assert estimate_text(FakeProvider(), T0, T0 + timedelta(days=7)) == "≈ 1,008 requests · 2 min"
    assert estimate_text(FakeProvider(), T0, T0) == "Already up to date."

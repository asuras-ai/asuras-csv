import pytest

from tests.fakes import FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()

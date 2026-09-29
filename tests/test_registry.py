import pytest

from app.domain import PermanentError, SymbolInfo
from app.providers.base import ProviderRegistry, rank_matches
from tests.fakes import FakeProvider


def test_registry_lookup():
    provider = FakeProvider()
    registry = ProviderRegistry([provider])
    assert registry.get("fake") is provider
    assert registry.find("nope") is None
    assert registry.all() == [provider]
    with pytest.raises(PermanentError, match="Unknown provider"):
        registry.get("nope")


def test_rank_matches_prefers_exact_then_prefix():
    symbols = [SymbolInfo(s, "crypto", s, s) for s in ["WBTCBTC", "BTCUSDT", "ETHBTC", "BTC"]]
    assert [s.provider_symbol for s in rank_matches(symbols, "btc")] == ["BTC", "BTCUSDT", "ETHBTC", "WBTCBTC"]
    assert rank_matches(symbols, "  ") == []
    assert [s.provider_symbol for s in rank_matches(symbols, "BTC-USDT")] == ["BTCUSDT"]

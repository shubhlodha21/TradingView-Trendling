"""Session state must follow the ASSET CLASS, everywhere it is read.

The engine's gates were already spec-aware. Two readers were not, and both
mislead the operator rather than the machine:

  * dashboard.py read the global US-equity helper, so every FX bot displayed
    "Outside session -- opens in 8.4h" all night while the engine underneath
    was correctly trading. Indistinguishable from the bot being broken.
  * a failed AssetSpec resolution set _asset_spec = None and said nothing.
    That one is not cosmetic: it silently reverts every session check to
    09:30-16:00 ET, so an FX bot refuses entries through its real hours with
    no line in the log to explain it.
"""
from datetime import datetime, timezone

import pytest

from src.assets.resolver import resolve
from src.config.models import session_is_open


# --------------------------------------------------------------------------- #
# the specs themselves
# --------------------------------------------------------------------------- #

class TestPerAssetSessions:

    def test_fx_and_equity_resolve_to_different_policies(self):
        assert type(resolve("USDJPY").session).__name__ == "ForexContinuousSession"
        assert type(resolve("AAPL").session).__name__ == "USEquitySession"

    def test_fx_is_open_when_us_equities_are_shut(self):
        """03:00 UTC on a Wednesday: FX trading, NYSE closed for hours."""
        overnight = datetime(2026, 8, 12, 3, 0, tzinfo=timezone.utc)
        assert resolve("USDJPY").session.is_open_at(overnight) is True
        assert resolve("AAPL").session.is_open_at(overnight) is False

    def test_both_shut_at_the_weekend(self):
        """Saturday: nothing trades, FX included."""
        saturday = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
        assert resolve("USDJPY").session.is_open_at(saturday) is False
        assert resolve("AAPL").session.is_open_at(saturday) is False

    def test_the_global_helper_only_speaks_equity(self):
        """Why the dashboard was wrong: this helper has no asset awareness."""
        overnight = datetime(2026, 8, 12, 3, 0, tzinfo=timezone.utc)
        assert session_is_open(overnight) is False          # correct for AAPL
        assert resolve("USDJPY").session.is_open_at(overnight) is True  # not FX

    def test_next_open_is_asset_specific(self):
        overnight = datetime(2026, 8, 12, 3, 0, tzinfo=timezone.utc)
        fx_wait = resolve("USDJPY").session.next_open(overnight) - overnight
        eq_wait = resolve("AAPL").session.next_open(overnight) - overnight
        # FX is already open, so its "next open" is a whole week away;
        # the equity open is later today. Either way they must differ.
        assert fx_wait != eq_wait


# --------------------------------------------------------------------------- #
# the silent-fallback warning
# --------------------------------------------------------------------------- #

class TestUnresolvableSymbolIsLoud:

    def test_engine_warns_on_stderr_when_no_spec(self, monkeypatch, capsys):
        """A None spec must never be silent -- it changes session hours."""
        import src.assets as assets_pkg
        from src.config.models import Config
        from src.execution.broker import Gateway
        from src.strategy.engine import Engine

        def _boom(symbol, hint=None):
            raise LookupError(f"no spec for {symbol}")

        monkeypatch.setattr(assets_pkg, "resolve", _boom, raising=True)

        engine = Engine(
            config=Config(ticker="WEIRD", trigger_price=1.0, quantity=1),
            gateway=Gateway(port=4002, paper=True, symbol="WEIRD"),
        )
        assert engine._asset_spec is None

        warning = capsys.readouterr().err
        assert "WARNING" in warning
        assert "WEIRD" in warning
        assert "US-equity" in warning
        assert "09:30-16:00 ET" in warning

    def test_a_good_symbol_stays_quiet(self, capsys):
        from src.config.models import Config
        from src.execution.broker import Gateway
        from src.strategy.engine import Engine

        engine = Engine(
            config=Config(ticker="AAPL", trigger_price=1.0, quantity=1),
            gateway=Gateway(port=4002, paper=True, symbol="AAPL"),
        )
        assert engine._asset_spec is not None
        assert "WARNING: no AssetSpec" not in capsys.readouterr().err

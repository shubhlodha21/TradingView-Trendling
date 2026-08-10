"""AssetClass enum — the discriminator for asset-type-specific behavior.

This is the only `if asset.kind == X` we accept in the engine.
Everywhere else should reach into the AssetSpec's policies and let
them dispatch via composition.

Adding a new asset class:
  1. Add an enum value here.
  2. Add a concrete spec factory under `src/assets/<name>.py`.
  3. Register it with SpecRegistry in `src/assets/resolver.py`.

That's the whole onboarding path — no changes to engine.py.
"""

from __future__ import annotations

from enum import Enum


class AssetClass(Enum):
    """Top-level asset taxonomy.

    Sub-types (e.g. US equity vs LSE equity) are differentiated by
    venue inside the spec; we don't split them at the enum level
    because the engine's state machine doesn't care.

    Convention: name == value (string), uppercase. Lets us round-trip
    through JSON state files cleanly.
    """

    US_EQUITY     = "US_EQUITY"      # NYSE/NASDAQ stocks via SMART routing
    FX_CASH       = "FX_CASH"        # Spot Forex on IDEALPRO
    FUTURE        = "FUTURE"         # CME/CBOT/NYMEX futures contracts
    INDEX_CFD     = "INDEX_CFD"      # IBKR contracts-for-difference (index)
    SHARE_CFD     = "SHARE_CFD"      # CFDs on individual shares
    FX_CFD        = "FX_CFD"         # FX-pair CFDs (different routing from spot FX)
    CFD           = "CFD"            # GENERIC CFD — any symbol under --cfd;
                                     # currency/tick resolved from the broker
                                     # at qualify (no per-symbol registry).

    # Deferred to future sprints — kept here so the discriminator is
    # exhaustive and forward-compatible.
    OPTION        = "OPTION"         # Equity options (deferred — own sprint)
    OPTION_ON_FUT = "OPTION_ON_FUT"  # Options on futures (deferred)
    CRYPTO        = "CRYPTO"         # IBKR crypto via Paxos (deferred)
    BOND          = "BOND"           # Fixed income (deferred)

    def __repr__(self) -> str:
        return f"AssetClass.{self.name}"

    @property
    def is_supported_day1(self) -> bool:
        """Which asset classes have working concrete specs in the
        Day-1 deliverable. Used by SpecRegistry to reject symbols
        whose asset class isn't shippable yet."""
        return self in {
            AssetClass.US_EQUITY,
            AssetClass.FX_CASH,
            AssetClass.FUTURE,
            AssetClass.INDEX_CFD,
            AssetClass.SHARE_CFD,
            AssetClass.FX_CFD,
            AssetClass.CFD,
        }

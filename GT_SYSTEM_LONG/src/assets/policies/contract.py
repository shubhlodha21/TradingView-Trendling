"""ContractPolicy — how to build and qualify an IBKR contract for an asset.

The 6 hardcoded `Stock(symbol, "SMART", "USD")` sites across broker.py
and handler.py all route here. Each asset class supplies its own
implementation:

  USEquityContractPolicy   → Stock("PLTR", "SMART", "USD")
  IDEALPROForexPolicy      → Forex("EURUSD")  (auto-routes to IDEALPRO)
  FuturesContractPolicy    → Future("ES", "202503", "CME", multiplier="50")
  IndexCFDPolicy           → CFD("IBUS500", "SMART", "USD")
  ShareCFDPolicy           → CFD("AAPL", "SMART", "USD")
  FXCFDPolicy              → CFD("EUR.USD", "SMART", ...)

`make()` returns the raw contract; `qualify()` round-trips it against
IBKR's ContractDetails to populate `conId`, `localSymbol`, etc.

Two methods because they have different failure modes:
  * `make()`     — pure function, no network. Fails only on bad input.
  * `qualify()` — network call. Fails on no-match, ambiguous-match,
                  or IBKR down.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # ib_async types only used for typing; we don't import the lib
    # at module load time so policies can be unit-tested without IBKR
    # being available.
    from ib_async import Contract, IB


@runtime_checkable
class ContractPolicy(Protocol):
    """Build and qualify a tradeable IBKR contract for one asset class."""

    def make(self, symbol: str) -> "Contract":
        """Construct the raw IBKR Contract from a human symbol.

        Examples:
            make("PLTR")    → Stock("PLTR", "SMART", "USD")
            make("EURUSD")  → Forex("EURUSD")
            make("ES")      → Future("ES", lastTradeDateOrContractMonth="202503", ...)

        Pure function — no network, no I/O. Must be deterministic.
        """
        ...

    async def qualify(self, ib: "IB", contract: "Contract") -> "Contract":
        """Round-trip the contract against IBKR's ContractDetails.

        On success, returns a Contract with `conId`, `localSymbol`,
        `tradingClass`, `primaryExchange`, etc. populated. This is the
        contract object the engine should use for orders.

        On failure:
            * raises ContractNotFound if IBKR returned zero matches
            * raises ContractAmbiguous if IBKR returned >1 match and
              the policy can't disambiguate
            * propagates network errors from ib_async
        """
        ...


# ────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────

class ContractError(Exception):
    """Base for contract-related failures."""


class ContractNotFound(ContractError):
    """IBKR has no match for the contract we asked about. Usually
    means the symbol is wrong, the exchange is wrong, or the asset
    isn't available to this account."""


class ContractAmbiguous(ContractError):
    """IBKR returned multiple matches and the policy didn't pick one.
    Typical with futures (multiple expiry months) — policy should
    narrow via month/year."""

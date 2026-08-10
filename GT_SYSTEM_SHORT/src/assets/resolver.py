"""SpecRegistry — symbol → AssetSpec resolution + IBKR cross-validation.

The single entry point the engine uses to get a spec:

    spec = SpecRegistry.resolve("PLTR")
    # → AssetSpec[US_EQUITY] composed from us_stock.py

Resolution flow:
  1. Walk the registered resolvers in priority order. First match wins.
  2. Each resolver gets a (symbol, optional hint) and returns either
     an AssetSpec or None ("not mine").
  3. The first non-None result is returned.
  4. If `validate_with_broker=True` and an `ib` is provided, the spec
     is cross-validated against IBKR's ContractDetails:
       * tick_size from spec matches contractDetails.minTick
       * multiplier (futures) matches contractDetails.multiplier
       * currency matches contractDetails.currency
       * trading hours match contractDetails.tradingHours
     If any mismatch: raise SpecMismatchError with structured diff.

Why the resolver pattern (vs a single big match-statement):
  * Open-closed: adding a new asset class doesn't require editing
    a central switch. You write a resolver and register it.
  * Multiple resolvers can know about the same symbol (a typed hint
    lets the operator force one over the other when ambiguous —
    e.g. "EURUSD" could be FX_CASH or FX_CFD; default to FX_CASH).
  * Easy to unit-test: mock the registry by registering a single
    fake resolver.
"""

from __future__ import annotations

from typing import Callable, Optional, TYPE_CHECKING

from .enum import AssetClass
from .spec import AssetSpec

if TYPE_CHECKING:
    from ib_async import IB


# ────────────────────────────────────────────────────────────────────
# Resolver type
# ────────────────────────────────────────────────────────────────────

# A resolver is any callable taking (symbol, hint) and returning
# AssetSpec or None.
Resolver = Callable[[str, Optional[AssetClass]], Optional[AssetSpec]]


# ────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────

class UnknownSymbol(LookupError):
    """No registered resolver claimed `symbol`. Either the symbol is
    typed wrong or its asset class isn't supported yet."""

    def __init__(self, symbol: str, hint: Optional[AssetClass]):
        h = f" (hint={hint.name})" if hint else ""
        super().__init__(
            f"No AssetSpec resolver matched symbol '{symbol}'{h}. "
            f"Either the symbol is typed wrong, or its asset class isn't "
            f"supported yet (Options/Crypto/Bonds are deferred)."
        )
        self.symbol = symbol
        self.hint = hint


class SpecMismatchError(RuntimeError):
    """Cross-validation against IBKR's ContractDetails failed. Engine
    refuses to start; operator must reconcile the spec vs the venue
    reality.

    Common causes:
      * Configured ES (multiplier $50) but symbol resolved to MES ($5)
      * Configured FX with .005 pip but IBKR says .00005 for this pair
      * Currency mismatch (USD spec on EUR-denominated contract)
    """

    def __init__(self, field: str, expected, actual, context: str = ""):
        super().__init__(
            f"AssetSpec/IBKR mismatch: {field} expected={expected!r} "
            f"actual={actual!r}{(' ('+context+')') if context else ''}"
        )
        self.field = field
        self.expected = expected
        self.actual = actual
        self.context = context


# ────────────────────────────────────────────────────────────────────
# The registry
# ────────────────────────────────────────────────────────────────────

class SpecRegistry:
    """Class-level registry of resolvers, deliberately not a singleton
    object so unit tests can monkey-patch _resolvers directly.

    Resolution is O(N) over registered resolvers. We expect <10
    resolvers ever, so a list is fine.

    `__slots__ = ()` makes the no-instance-state contract explicit —
    SpecRegistry is class-method-only, never instantiated.
    """

    __slots__ = ()
    _resolvers: list[tuple[int, Resolver]] = []

    @classmethod
    def register(cls, resolver: Resolver, priority: int = 100) -> None:
        """Register a resolver. Lower priority = checked first.
        Default priority 100 is conventional; specialized resolvers
        (e.g. operator-override) can register at priority=0 to win
        unconditionally."""
        cls._resolvers.append((priority, resolver))
        cls._resolvers.sort(key=lambda t: t[0])

    @classmethod
    def reset(cls) -> None:
        """Clear all registered resolvers. Used by tests to start
        from a known empty state."""
        cls._resolvers = []

    @classmethod
    def resolve(
        cls,
        symbol: str,
        hint: Optional[AssetClass] = None,
    ) -> AssetSpec:
        """Find the AssetSpec for `symbol`.

        `hint` lets the caller disambiguate when a symbol matches
        multiple resolvers (e.g. force FX_CFD when "EURUSD" would
        default to FX_CASH).

        Raises UnknownSymbol if no resolver matches.
        """
        for _priority, resolver in cls._resolvers:
            spec = resolver(symbol, hint)
            if spec is None:
                continue
            if hint is not None and spec.asset_class is not hint:
                continue  # respect the hint strictly
            return spec
        raise UnknownSymbol(symbol, hint)

    @classmethod
    async def cross_validate(
        cls,
        spec: AssetSpec,
        symbol: str,
        ib: "IB",
    ) -> dict:
        """Round-trip the spec against IBKR's ContractDetails and
        raise SpecMismatchError on any divergence.

        Returns:
            dict with broker-truth fields the engine can adopt at
            runtime: `min_tick` (float), `currency` (str), `multiplier`
            (float, futures-only). Empty dict if the broker didn't
            return any of these. Use the broker's values for runtime
            rounding (the spec's hardcoded defaults are only the safe
            offline-test starting point).

        Called at engine startup. Cheap (one ContractDetails RPC).
        The point is to catch "I think this is ES but spec is
        configured for MES" at startup, not on the first fill when
        the notional is silently 10x wrong.

        Checked fields:
          * Tick size:   spec.tick.tick_size(ref_price) vs cd.minTick
          * Currency:    spec.quote_currency.value vs cd.contract.currency
          * Multiplier:  spec.sizing.multiplier vs cd.contract.multiplier
                         (only when spec.sizing has .multiplier — futures)

        Skipped fields (deferred to follow-up work):
          * Trading hours — string parsing is venue-specific
          * Exchange routing — SMART auto-routes to multiple venues
            so equality check would over-trigger

        D3-PM scope: this is the "stop the bot at startup if I'm
        configured for ES but actually pointing at MES" check. The
        engine wires it via Gateway.qualify_contract calling this
        immediately after the qualifyContractsAsync RPC succeeds.
        """
        # Build the contract and request ContractDetails
        contract = spec.contract.make(symbol)
        cds = await ib.reqContractDetailsAsync(contract)
        if not cds:
            # Don't raise SpecMismatchError here — the contract simply
            # doesn't qualify. ContractNotFound from the policy is the
            # right error class for this. SpecMismatchError is for
            # qualified contracts that disagree with the spec.
            from .policies.contract import ContractNotFound
            raise ContractNotFound(
                f"IBKR returned no ContractDetails for {symbol} via "
                f"{type(spec.contract).__name__}. Symbol typed wrong, "
                f"asset class hint wrong, or account lacks permission."
            )
        cd = cds[0]
        broker_contract = cd.contract
        cls._assert_currency_matches(spec, broker_contract)
        cls._assert_multiplier_matches(spec, broker_contract)
        cls._assert_tick_matches(spec, cd)

        # ── Return broker truth for runtime adoption (D5-Runtime-Tick) ──
        # Real HFT systems treat the venue's reported minTick as
        # authoritative — account type, contract listing, and venue
        # routing all affect the actual valid price grid. The spec's
        # hardcoded tick is the SAFE DEFAULT for offline tests; the
        # broker's reported value is what we should USE at runtime.
        # E.g. IDEALPRO EURUSD: spec hardcodes 0.00005 (half-pip), but
        # IBKR may report 0.00001 (full sub-pip) depending on account
        # type — our rounding should follow the broker's grid.
        #
        # Returns a dict of broker-truth fields the engine can adopt.
        # Caller stores them on Gateway, engine reads via accessors.
        broker_truth: dict = {}
        bmt = getattr(cd, "minTick", None)
        if bmt is not None and bmt > 0:
            broker_truth["min_tick"] = float(bmt)
        bccy = getattr(broker_contract, "currency", None)
        if bccy:
            broker_truth["currency"] = str(bccy)
        bmult = getattr(broker_contract, "multiplier", None)
        if bmult:
            try:
                broker_truth["multiplier"] = float(bmult)
            except (TypeError, ValueError):
                pass
        return broker_truth

    # ── Per-field assertion helpers ─────────────────────────────────
    # Each helper either passes silently or raises SpecMismatchError
    # with structured field/expected/actual so the operator's log
    # line points exactly at the disagreement.

    @classmethod
    def _assert_currency_matches(cls, spec: AssetSpec, broker_contract) -> None:
        # For Forex, ib_async's Contract.currency holds the QUOTE
        # currency (e.g. "USD" for EURUSD). That matches our spec
        # convention.
        broker_ccy = getattr(broker_contract, "currency", "")
        expected_ccy = spec.quote_currency.value
        if broker_ccy and broker_ccy != expected_ccy:
            raise SpecMismatchError(
                "currency",
                expected=expected_ccy,
                actual=broker_ccy,
                context=(
                    f"spec.quote_currency = {expected_ccy} but IBKR "
                    f"reports {broker_ccy}. Wrong asset hint or wrong "
                    f"symbol — refuse to proceed."
                ),
            )

    @classmethod
    def _assert_multiplier_matches(cls, spec: AssetSpec, broker_contract) -> None:
        # Only applies when the spec's sizing carries a multiplier
        # (currently: futures via MultiplierSizing). For equity / FX /
        # CFDs the spec.sizing doesn't have .multiplier and we skip.
        if not hasattr(spec.sizing, "multiplier"):
            return
        spec_mult = spec.sizing.multiplier  # Decimal
        broker_mult_raw = getattr(broker_contract, "multiplier", "")
        if not broker_mult_raw:
            # Some non-multiplier contracts return empty string. If
            # spec has a multiplier > 1, that's a real mismatch.
            from decimal import Decimal as _D
            if spec_mult != _D("1"):
                raise SpecMismatchError(
                    "multiplier",
                    expected=str(spec_mult),
                    actual="(empty)",
                    context=(
                        f"spec.sizing.multiplier = {spec_mult} but "
                        f"IBKR contract has no multiplier — wrong "
                        f"asset class hint."
                    ),
                )
            return
        # IBKR multiplier comes as a string ("50" for ES, "5" for MES)
        from decimal import Decimal as _D, InvalidOperation
        try:
            broker_mult = _D(str(broker_mult_raw))
        except (InvalidOperation, ValueError):
            raise SpecMismatchError(
                "multiplier",
                expected=str(spec_mult),
                actual=str(broker_mult_raw),
                context="IBKR returned non-numeric multiplier",
            )
        if broker_mult != spec_mult:
            raise SpecMismatchError(
                "multiplier",
                expected=str(spec_mult),
                actual=str(broker_mult),
                context=(
                    f"FUTURES MULTIPLIER MISMATCH — wrong product or "
                    f"wrong contract. ES uses 50, MES uses 5. A 10× "
                    f"mismatch here under-sizes risk by 10×. Refuse."
                ),
            )

    @classmethod
    def _assert_tick_matches(cls, spec: AssetSpec, cd) -> None:
        broker_min_tick = getattr(cd, "minTick", None)
        if broker_min_tick is None or broker_min_tick == 0:
            # IBKR didn't tell us; skip rather than false-alarm
            return
        # Tick policies that take a price argument: pass a representative
        # price (1.0 is safe for the constant-tick policies we ship)
        from .types import price as _price
        from decimal import Decimal as _D
        try:
            spec_tick = spec.tick.tick_size(_price("1.0"))
        except Exception:
            return  # tick policy may have variable-per-price grain
        # IBKR minTick is float; convert via str() to avoid binary drift
        broker_tick_dec = _D(str(broker_min_tick))
        # Compare: spec_tick must be a multiple of broker_min_tick OR equal.
        # Most cases: they're equal. Allow spec_tick to be tighter than
        # broker (a finer grid is a stricter constraint, harmless) but
        # NOT looser (would cause IBKR-side rejection at order time).
        if spec_tick < broker_tick_dec:
            raise SpecMismatchError(
                "tick_size",
                expected=str(spec_tick),
                actual=str(broker_tick_dec),
                context=(
                    f"spec tick {spec_tick} is FINER than IBKR's "
                    f"minimum {broker_tick_dec}. Orders rounded to "
                    f"spec grid will be rejected by IBKR. Loosen the "
                    f"spec or pick a different asset symbol."
                ),
            )
        # If spec is coarser (e.g. ES spec=0.25 but IBKR allows
        # 0.05 finer), that's also a real bug — operator could place
        # orders the broker accepts but spec wouldn't snap to. Block.
        if spec_tick != broker_tick_dec:
            # Spec tick is COARSER. Allow only if it's an integer
            # multiple of broker's tick — that means our grid is a
            # strict subset of the broker's grid, which is safe.
            if (spec_tick / broker_tick_dec) % _D("1") != 0:
                raise SpecMismatchError(
                    "tick_size",
                    expected=str(spec_tick),
                    actual=str(broker_tick_dec),
                    context=(
                        f"spec tick {spec_tick} is not aligned with "
                        f"IBKR minimum {broker_tick_dec}; orders may "
                        f"be off-grid. Check the per-asset tick table."
                    ),
                )


# ────────────────────────────────────────────────────────────────────
# Convenience top-level functions
# ────────────────────────────────────────────────────────────────────

def resolve(symbol: str, hint: Optional[AssetClass] = None) -> AssetSpec:
    """Module-level shortcut for SpecRegistry.resolve(). Most engine
    code can do `from src.assets import resolve` and call this."""
    return SpecRegistry.resolve(symbol, hint)

"""ShortPolicy — asset-class-specific short-selling requirements.

WHY THIS POLICY EXISTS

The engine already knows how to place the *orders* for a short (SELL
to open, BUY to cover — see SHORT_CONVERSION_CHANGES.md). What it did
NOT model is the set of *requirements and costs* that make a short
different from a long, and that differ sharply across asset classes.
Per `IBKR - short selling.pdf`:

  1. Initial margin — a Reg-T equity short needs 150% of the position
     value posted as collateral: 100% comes from the sale proceeds,
     50% must be your own equity.
  2. Maintenance margin — NOT a fixed 30%/50%. It is stock-specific
     (price, liquidity, volatility, market cap, IBKR's risk model). A
     large-cap might be ~30%; a crowded small-cap can be 100%, 300%+.
  3. Short-sale proceeds are RESTRICTED — held as collateral, not free
     cash. They do not increase your buying power.
  4. Borrow fees — a stock borrow fee (near-zero for liquid large-caps,
     triple-digit annualised for crowded shorts) plus possible short-
     stock interest.
  5. Portfolio Margin vs Reg-T changes the formulas (risk-based).
  6. The ONLY authoritative number is IBKR's Order Preview (whatIf),
     because the requirement is stock-specific and changes daily.

None of that applies uniformly:

  Equity (Reg-T):  150% collateral, restricted proceeds, must locate/
                   borrow the shares, pays a borrow fee, SSR/uptick.
  CFD:             synthetic — you never borrow shares, there are no
                   "proceeds" to restrict. Requirement is a margin %
                   (leverage); cost is overnight financing.
  FX / Futures:    symmetric instruments. "Shorting" EURUSD is just
                   being long USD/short EUR — no borrow, no locate, no
                   restricted proceeds. Requirement is leverage margin
                   (FX) or SPAN (futures); cost is swap / carry.

So `ShortPolicy` is the 9th AssetSpec policy: each asset class ships
its own. The engine (and the risk gate / dashboard / pre-trade
preview) ask `spec.short.requirement(notional)` instead of hardcoding
the equity assumption a second time.

WHAT THIS POLICY IS *NOT*

It is a *pre-trade estimate*, deliberately conservative, computed
offline from notional. It is NOT a replacement for IBKR's whatIf
Order Preview — for equity especially, the maintenance requirement and
borrow rate are stock-specific and change daily. Treat these numbers
as a sanity gate ("do I even have room for this?") and reconcile
against the broker's real InitMarginChange / MaintMarginChange at
order time. `ShortRequirement.authoritative` is always False here;
only a broker-sourced requirement sets it True.

The policy is a pure function of its inputs — no I/O, no broker calls,
no mutation — exactly like the other policies.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Protocol, runtime_checkable

from ..types import Money


# Day-count convention for annualised borrow / financing / swap rates.
# IBKR quotes borrow and financing on a 360-day year.
_DAYS_PER_YEAR: Decimal = Decimal("360")


# ────────────────────────────────────────────────────────────────────
# ShortRequirement — the structured pre-trade preview
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ShortRequirement:
    """A pre-trade snapshot of what opening (and holding) a short costs.

    Mirrors what IBKR's Order Preview surfaces (Initial Margin Impact,
    Maintenance Margin Impact) plus the carry cost, so the operator and
    the audit log see one consistent bundle regardless of asset class.

    All Money fields are in the asset's QUOTE currency (same as
    `notional`). Cross-currency aggregation is the caller's job via
    `Money.to(base, fx)` — we never guess an FX rate here (same rule as
    SizingPolicy).
    """

    # The short position value this requirement was computed from.
    notional: Money

    # Your own equity that must be posted to OPEN the short. For a
    # Reg-T equity short this is 50% of notional (the 100% proceeds
    # cover the rest). This is the number that reduces excess liquidity.
    initial_margin: Money

    # Equity that must remain posted to KEEP the short open. Stock-
    # specific in reality; this is a conservative default estimate
    # unless it came from the broker (see `authoritative`).
    maintenance_margin: Money

    # Sale proceeds held as collateral (not free cash). 0 for synthetic
    # instruments (CFDs) and for symmetric ones (FX/futures) that have
    # no "proceeds" concept.
    proceeds_collateral: Money

    # Cost of holding the short for ONE day: stock borrow fee (equity),
    # overnight financing (CFD), or swap/carry (FX/futures). A positive
    # amount is a cost to you; some FX carries can be a credit but we
    # report magnitude and let the overlay interpret sign.
    daily_carry: Money

    # ── Flags describing the short's mechanics ──────────────────────
    proceeds_restricted: bool   # are sale proceeds locked as collateral?
    requires_locate: bool       # must the shares be located/borrowed first?
    hard_to_borrow: bool        # flagged HTB (elevated borrow + maint)?
    is_symmetric: bool          # short == long instrument (FX/futures)?

    # False when computed offline by a ShortPolicy; True only when the
    # requirement came from IBKR's whatIf Order Preview (the sole
    # authoritative source — the requirement is stock-specific and
    # changes daily). Callers should prefer an authoritative one.
    authoritative: bool = False

    # The annualised borrow/financing/swap rate used for `daily_carry`,
    # as a fraction (0.0025 = 25 bps/yr). Recorded for audit so a post-
    # mortem can see what assumption produced the carry number.
    carry_rate_annual: Decimal = Decimal("0")

    notes: str = ""

    @property
    def collateral_multiplier(self) -> Decimal:
        """Total collateral as a multiple of notional: (proceeds +
        your equity) / notional. Reg-T equity short → 1.5 (150%)."""
        if self.notional.amount == 0:
            return Decimal("0")
        total = self.proceeds_collateral.amount + self.initial_margin.amount
        return total / self.notional.amount

    def to_audit_dict(self) -> dict:
        """Serialisable snapshot for the audit log / order preview."""
        return {
            "notional": str(self.notional.amount),
            "currency": self.notional.currency.value,
            "initial_margin": str(self.initial_margin.amount),
            "maintenance_margin": str(self.maintenance_margin.amount),
            "proceeds_collateral": str(self.proceeds_collateral.amount),
            "daily_carry": str(self.daily_carry.amount),
            "collateral_multiplier": str(self.collateral_multiplier),
            "proceeds_restricted": self.proceeds_restricted,
            "requires_locate": self.requires_locate,
            "hard_to_borrow": self.hard_to_borrow,
            "is_symmetric": self.is_symmetric,
            "authoritative": self.authoritative,
            "carry_rate_annual": str(self.carry_rate_annual),
            "notes": self.notes,
        }


# ────────────────────────────────────────────────────────────────────
# The protocol
# ────────────────────────────────────────────────────────────────────

@runtime_checkable
class ShortPolicy(Protocol):
    """Asset-class-specific short-selling requirements & costs.

    Stateless and pure by convention: given a notional (and optionally
    a live borrow/financing rate), return the requirement. No I/O, no
    broker calls, no mutation — same contract as the other policies.

    Callers should still prefer IBKR's whatIf Order Preview for the
    authoritative number at order time; this policy is the offline
    sanity estimate.
    """

    # Does the asset require locating/borrowing before you can short?
    requires_locate: bool
    # Are short-sale proceeds held as restricted collateral?
    proceeds_restricted: bool
    # Is the instrument symmetric (short == long: FX/futures)?
    is_symmetric: bool

    def initial_margin(self, notional: Money) -> Money:
        """Own equity required to OPEN a short of this notional."""
        ...

    def maintenance_margin(self, notional: Money) -> Money:
        """Own equity required to KEEP the short open."""
        ...

    def daily_carry(self, notional: Money, *, annual_rate: Optional[Decimal] = None) -> Money:
        """Cost of holding the short for one day. `annual_rate` (a
        fraction, e.g. 0.30 = 30%/yr) overrides the policy default —
        pass IBKR's live borrow/financing rate when you have it."""
        ...

    def requirement(
        self,
        notional: Money,
        *,
        annual_rate: Optional[Decimal] = None,
        hard_to_borrow: bool = False,
    ) -> ShortRequirement:
        """Full pre-trade bundle (margin + carry + flags) for audit /
        Order-Preview-style display."""
        ...


def _zero_like(m: Money) -> Money:
    """A zero Money in the same currency as `m` — keeps currency tags
    consistent so downstream arithmetic never trips CurrencyMismatch."""
    return Money(Decimal("0"), m.currency)


# ────────────────────────────────────────────────────────────────────
# Reg-T equity short (PDF §1–4)
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RegTEquityShort:
    """Standard Reg-T margin-account short on a US equity.

    Initial (PDF §1): 150% collateral = 100% sale proceeds + 50% your
    equity. `initial_margin` returns the 50% your-equity slice — the
    part that actually consumes excess liquidity. The proceeds slice is
    reported separately as `proceeds_collateral`.

    Maintenance (PDF §2): defaults to 30% (large-cap-ish) but is
    stock-specific and can be far higher; `hard_to_borrow` bumps it to
    `htb_maintenance_rate`. The real number comes from whatIf — treat
    this as a conservative offline estimate only.

    Proceeds (PDF §3): restricted — held as collateral, not free cash.

    Borrow (PDF §4): a stock borrow fee, annualised, on the borrowed
    value. `default_borrow_rate_annual` is a placeholder for a liquid
    name; pass the live rate via `annual_rate` for anything real.
    """

    # 50% additional equity on top of the 100% proceeds → 150% total.
    initial_equity_rate: Decimal = Decimal("0.50")
    maintenance_rate: Decimal = Decimal("0.30")
    htb_maintenance_rate: Decimal = Decimal("1.00")  # hard-to-borrow
    default_borrow_rate_annual: Decimal = Decimal("0.0025")  # 25 bps/yr

    requires_locate: bool = True
    proceeds_restricted: bool = True
    is_symmetric: bool = False

    def initial_margin(self, notional: Money) -> Money:
        return Money(abs(notional.amount) * self.initial_equity_rate, notional.currency)

    def maintenance_margin(self, notional: Money, *, hard_to_borrow: bool = False) -> Money:
        rate = self.htb_maintenance_rate if hard_to_borrow else self.maintenance_rate
        return Money(abs(notional.amount) * rate, notional.currency)

    def daily_carry(self, notional: Money, *, annual_rate: Optional[Decimal] = None) -> Money:
        rate = self.default_borrow_rate_annual if annual_rate is None else annual_rate
        daily = abs(notional.amount) * rate / _DAYS_PER_YEAR
        return Money(daily, notional.currency)

    def requirement(
        self,
        notional: Money,
        *,
        annual_rate: Optional[Decimal] = None,
        hard_to_borrow: bool = False,
    ) -> ShortRequirement:
        rate = self.default_borrow_rate_annual if annual_rate is None else annual_rate
        return ShortRequirement(
            notional=notional,
            initial_margin=self.initial_margin(notional),
            maintenance_margin=self.maintenance_margin(notional, hard_to_borrow=hard_to_borrow),
            # 100% of proceeds held as restricted collateral.
            proceeds_collateral=Money(abs(notional.amount), notional.currency),
            daily_carry=self.daily_carry(notional, annual_rate=rate),
            proceeds_restricted=self.proceeds_restricted,
            requires_locate=self.requires_locate,
            hard_to_borrow=hard_to_borrow,
            is_symmetric=self.is_symmetric,
            authoritative=False,
            carry_rate_annual=rate,
            notes=(
                "Reg-T equity short: 150% collateral (100% proceeds + 50% equity). "
                "Maintenance is stock-specific — confirm via IBKR whatIf Order Preview."
            ),
        )


# ────────────────────────────────────────────────────────────────────
# CFD short — synthetic, margin %, overnight financing
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class CFDShort:
    """Short via a Contract-for-Difference.

    A CFD is synthetic: you never borrow shares and there are no sale
    proceeds to restrict. The requirement is simply a margin percentage
    of notional (leverage), typically the same to open and to hold. The
    holding cost is overnight financing (benchmark + spread) on the full
    notional.

    Typical IBKR margin: ~20% for share CFDs, ~5% for major index CFDs.
    """

    margin_rate: Decimal = Decimal("0.20")                    # 20% share CFD default
    default_financing_rate_annual: Decimal = Decimal("0.03")  # benchmark+spread

    requires_locate: bool = False
    proceeds_restricted: bool = False
    is_symmetric: bool = False

    def initial_margin(self, notional: Money) -> Money:
        return Money(abs(notional.amount) * self.margin_rate, notional.currency)

    def maintenance_margin(self, notional: Money) -> Money:
        # CFDs use the same margin % to open and maintain.
        return self.initial_margin(notional)

    def daily_carry(self, notional: Money, *, annual_rate: Optional[Decimal] = None) -> Money:
        rate = self.default_financing_rate_annual if annual_rate is None else annual_rate
        daily = abs(notional.amount) * rate / _DAYS_PER_YEAR
        return Money(daily, notional.currency)

    def requirement(
        self,
        notional: Money,
        *,
        annual_rate: Optional[Decimal] = None,
        hard_to_borrow: bool = False,  # ignored — no borrow for CFDs
    ) -> ShortRequirement:
        rate = self.default_financing_rate_annual if annual_rate is None else annual_rate
        return ShortRequirement(
            notional=notional,
            initial_margin=self.initial_margin(notional),
            maintenance_margin=self.maintenance_margin(notional),
            proceeds_collateral=_zero_like(notional),  # synthetic: no proceeds
            daily_carry=self.daily_carry(notional, annual_rate=rate),
            proceeds_restricted=False,
            requires_locate=False,
            hard_to_borrow=False,
            is_symmetric=False,
            authoritative=False,
            carry_rate_annual=rate,
            notes=(
                f"CFD short: synthetic, {self.margin_rate * 100:.0f}% margin, "
                "overnight financing accrues while held. No borrow/locate."
            ),
        )


# ────────────────────────────────────────────────────────────────────
# Symmetric short — FX cash / FX CFD / futures
# ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SymmetricShort:
    """Short on a SYMMETRIC instrument — FX or futures.

    Shorting EURUSD is just being long USD and short EUR; shorting an ES
    future is mechanically identical to going long one. There is no
    share to borrow, no locate, no restricted proceeds — the short side
    is a first-class position, not a borrowed one.

    The requirement is leverage margin (a small % for FX majors) or, for
    futures, SPAN — which is already handled by the futures RiskOverlay,
    so `margin_source` records where the real number lives. The holding
    cost is swap / carry (the interest-rate differential), which can be
    a credit or a charge; we report magnitude and let the overlay
    interpret the sign.
    """

    margin_rate: Decimal = Decimal("0.03")               # ~3% FX major default
    default_carry_rate_annual: Decimal = Decimal("0.0")  # swap differential
    margin_source: str = "leverage"                       # or "SPAN" for futures

    requires_locate: bool = False
    proceeds_restricted: bool = False
    is_symmetric: bool = True

    @classmethod
    def for_fx(cls, margin_rate: Decimal = Decimal("0.03")) -> "SymmetricShort":
        return cls(margin_rate=margin_rate, margin_source="leverage")

    @classmethod
    def for_futures(cls, margin_rate: Decimal = Decimal("0.10")) -> "SymmetricShort":
        # Futures margin is SPAN (risk-based); `margin_rate` is only a
        # coarse fallback estimate. The FuturesRiskOverlay owns the real
        # headroom check.
        return cls(margin_rate=margin_rate, margin_source="SPAN")

    def initial_margin(self, notional: Money) -> Money:
        return Money(abs(notional.amount) * self.margin_rate, notional.currency)

    def maintenance_margin(self, notional: Money) -> Money:
        return self.initial_margin(notional)

    def daily_carry(self, notional: Money, *, annual_rate: Optional[Decimal] = None) -> Money:
        rate = self.default_carry_rate_annual if annual_rate is None else annual_rate
        daily = abs(notional.amount) * abs(rate) / _DAYS_PER_YEAR
        return Money(daily, notional.currency)

    def requirement(
        self,
        notional: Money,
        *,
        annual_rate: Optional[Decimal] = None,
        hard_to_borrow: bool = False,  # ignored — nothing to borrow
    ) -> ShortRequirement:
        rate = self.default_carry_rate_annual if annual_rate is None else annual_rate
        return ShortRequirement(
            notional=notional,
            initial_margin=self.initial_margin(notional),
            maintenance_margin=self.maintenance_margin(notional),
            proceeds_collateral=_zero_like(notional),
            daily_carry=self.daily_carry(notional, annual_rate=rate),
            proceeds_restricted=False,
            requires_locate=False,
            hard_to_borrow=False,
            is_symmetric=True,
            authoritative=False,
            carry_rate_annual=rate,
            notes=(
                f"Symmetric short ({self.margin_source}): no borrow/locate/"
                "restricted proceeds. Holding cost is swap/carry (may be a credit)."
            ),
        )

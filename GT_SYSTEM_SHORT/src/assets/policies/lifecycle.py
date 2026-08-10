"""LifecyclePolicy — events that affect a position outside the engine's
ordinary BUY/SELL state machine.

Asset classes have very different lifecycle obligations:

  US equity:    Settlement T+1 (was T+2 pre-2024). Dividends. Corporate
                actions (splits, mergers, spinoffs). Position-keeping
                is straightforward post-fill.

  Futures:      Settlement is mark-to-market daily. CONTRACT ROLL is
                the big one: every contract has an expiry date; before
                expiry the engine must close the front month and re-
                open the new front. Failure to roll → forced settlement
                or physical delivery (catastrophic for retail).

  Forex (cash): Continuous settlement, no roll. Holding overnight
                accrues swap (interest rate differential), but the
                engine doesn't actively manage it.

  CFDs:         No expiry (most), but daily overnight FINANCING is
                charged based on the position's notional and the
                LIBOR-replacement rate of the quote currency. Engine
                should accrue this so P&L is real.

  Options:      EXPIRY is the big one. Plus exercise/assignment.
                Deferred until the Options sprint.

This policy answers:
  * "Does this contract need to roll soon?" (futures only)
  * "When does this contract expire?" (futures, options)
  * "What's the settlement-day count for sizing risk decisions?" (T+0 vs T+1 vs T+2)

The engine doesn't ACT on roll/expiry automatically in Day-1 scope
— it just SURFACES the warning via an alert. The actual roll logic
is a separate piece of work parked for after Futures lands.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional, Protocol, runtime_checkable

# Forward reference for typing; ContractPolicy may pass us a Contract.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ib_async import Contract


@runtime_checkable
class LifecyclePolicy(Protocol):
    """Asset-class-specific lifecycle metadata + warnings."""

    def needs_roll(self, contract: "Contract", ts: datetime) -> bool:
        """Does this contract need to be rolled to the next month
        within the policy's roll window?

        For non-rolling assets (equity, FX cash, most CFDs): always
        False. For futures: True when `ts` is within N days of
        contract expiry, where N is the policy's roll_window_days.

        The engine should fire a `ROLL_NEEDED` alert when this
        returns True but the actual roll trade is operator-initiated
        in Day-1. (Auto-roll lands in week 2.)
        """
        ...

    def expiry(self, contract: "Contract") -> Optional[date]:
        """The contract's expiry date, or None if non-expiring (equity,
        FX cash, most CFDs).

        Used by SpecRegistry validation to refuse to start an engine
        on an already-expired contract.
        """
        ...

    def settlement_days(self) -> int:
        """T+N settlement convention for this asset, used by the risk
        gate's available-capital calculations.

        US equity: 1 (T+1 since May 2024)
        Forex cash: 2 (T+2)
        Futures:   0 (intraday mark-to-market)
        CFDs:      0 (continuous mark-to-market)
        """
        ...

    def has_overnight_financing(self) -> bool:
        """True if holding this position overnight accrues a daily
        financing charge (CFDs always; some leveraged products).
        Engine uses this to schedule daily financing accrual jobs
        — out of scope for Day-1 but the flag is here for the future.
        """
        ...

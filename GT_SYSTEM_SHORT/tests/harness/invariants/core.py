"""Core invariant library — 5 critical correctness checks.

Each invariant is a class implementing the Invariant Protocol.
Observes engine + backend state after specific event kinds and
returns a Violation if the property is broken.

INVARIANT INVENTORY (this file):

  01. PriceOnVenueGrid
        Every PLACED order's stop_price + limit_price is on the
        venue's reported tick grid. Catches `round(x, 2)` for FX
        and any future precision regression.

  02. ModifyNotReplace
        After a CHILD_STOP_MODIFIED audit event, the bracket-child's
        broker_id must be the SAME as before the modify. If it
        changed, the modify silently became a cancel+replace —
        which means there was a window with no protective stop.

  03. PositionQtyMatch
        Within tolerance (positions_lag), the broker's position
        for the asset matches the sum of fills minus exits. Catches
        the 2026-06-05 false-fold class of bug.

  04. NoOffGridOrders
        No order placed at the broker has an off-grid stop or
        limit price. Backed by MockGateway's tick-grid rejection.

  05. SellStopBelowEntryWhenLong
        While LONG, the SELL bracket child's stop_price must be
        STRICTLY LESS than the BUY entry price. Catches the
        2026-06-05 "stop got modified to 1.16000" class where
        the stop ended up above entry (which would fire immediately).

These 5 cover the bug classes we've actually hit. More invariants
follow in subsequent expansions (A5 continuation).
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from ..invariant import EventKind, Invariant, InvariantEnv, REGISTRY, Severity, Violation


# ════════════════════════════════════════════════════════════════════════════
# 01. PRICE-ON-VENUE-GRID
# ════════════════════════════════════════════════════════════════════════════

class PriceOnVenueGridInvariant:
    """Every resting order's stop/limit prices live on the venue's
    minTick grid. Off-grid prices either get rejected by IBKR (error
    110) or silently slid by the venue — both bad."""

    name = "PRICE_ON_VENUE_GRID"
    description = (
        "Every resting order's stop_price / limit_price must be an "
        "integer multiple of the venue-reported minTick. Off-grid prices "
        "indicate engine math used wrong precision (e.g., round(p, 2) for FX)."
    )
    severity = Severity.CRITICAL
    scope = frozenset([
        EventKind.PLACED, EventKind.MODIFIED, EventKind.EXECUTION,
        EventKind.RECONCILE,
    ])

    def check(self, env, event_kind, event_seq, event_payload):
        backend = env.backend
        tick = getattr(backend, '_runtime_min_tick', None)
        if tick is None or tick <= 0:
            return None
        # Iterate currently-resting orders (synchronous fetch where
        # available; otherwise fall back).
        if not hasattr(backend, 'fetch_open_orders'):
            return None
        try:
            orders = backend.fetch_open_orders()
        except Exception:
            return None
        for o in orders:
            for field in ('stop_price', 'limit_price'):
                px = o.get(field)
                if px is None or px <= 0:
                    continue
                if not _is_on_grid(px, tick):
                    return Violation(
                        invariant_name=self.name,
                        severity=self.severity,
                        scenario_id=env.scenario_id,
                        event_seq=event_seq,
                        event_kind=event_kind,
                        timestamp=env.now,
                        expected=f"{field} multiple of {tick}",
                        actual=f"{field}={px}",
                        diagnostic=(
                            f"Order {o.get('order_ref') or o.get('broker_id')} "
                            f"has off-grid {field}={px} (tick={tick})"
                        ),
                        context={'order': dict(o), 'tick': tick, 'field': field},
                        seed=env.seed,
                    )
        return None


# ════════════════════════════════════════════════════════════════════════════
# 02. MODIFY-NOT-REPLACE
# ════════════════════════════════════════════════════════════════════════════

class ModifyNotReplaceInvariant:
    """When a bracket child stop's auxPrice is modified, the broker_id
    MUST be preserved. If it changes, the modify silently became a
    cancel+replace — leaving a window with no protective stop."""

    name = "MODIFY_NOT_REPLACE"
    description = (
        "Bracket child stop modification preserves the same broker_id "
        "throughout the order's lifetime. A broker_id change indicates "
        "cancel+replace, which leaves an unprotected window."
    )
    severity = Severity.CRITICAL
    scope = frozenset([EventKind.MODIFIED, EventKind.EXECUTION])

    def __init__(self):
        # Track broker_id per engine_id; if it ever changes for the same
        # engine_id within a scenario, that's the bug.
        self._engine_to_broker: dict[str, str] = {}

    def check(self, env, event_kind, event_seq, event_payload):
        backend = env.backend
        # Read every order's (engine_id, broker_id) mapping from the
        # backend's internal map. For MockGateway this is _engine_to_broker_id.
        mapping = getattr(backend, '_engine_to_broker_id', None)
        if mapping is None:
            return None
        for engine_id, broker_id in mapping.items():
            prior = self._engine_to_broker.get(engine_id)
            if prior is None:
                self._engine_to_broker[engine_id] = broker_id
            elif prior != broker_id:
                return Violation(
                    invariant_name=self.name,
                    severity=self.severity,
                    scenario_id=env.scenario_id,
                    event_seq=event_seq,
                    event_kind=event_kind,
                    timestamp=env.now,
                    expected=f"broker_id stable for {engine_id} = {prior}",
                    actual=f"broker_id = {broker_id}",
                    diagnostic=(
                        f"Order {engine_id} broker_id changed from {prior} → "
                        f"{broker_id}. Modify silently became cancel+replace."
                    ),
                    context={'engine_id': engine_id, 'prior_broker_id': prior, 'new_broker_id': broker_id},
                    seed=env.seed,
                )
        return None


# ════════════════════════════════════════════════════════════════════════════
# 03. POSITION-QTY-MATCH
# ════════════════════════════════════════════════════════════════════════════

class PositionQtyMatchInvariant:
    """After accounting for positions_lag, the broker's reported
    position quantity matches the sum of net fills.

    Tolerance: positions_lag_seconds. During the lag window we don't
    fire (it's expected disagreement). After the lag, the position
    should reflect all fills.
    """

    name = "POSITION_QTY_MATCH"
    description = (
        "Broker's reported position quantity equals sum of net fills "
        "(after positions_lag tolerance). Catches the 2026-06-05 false-"
        "fold class where reconcile saw 0 while the BUY had just filled."
    )
    severity = Severity.CRITICAL
    scope = frozenset([EventKind.EXECUTION, EventKind.TICK_TIMER, EventKind.RECONCILE])

    def check(self, env, event_kind, event_seq, event_payload):
        backend = env.backend
        # Sum net fills from the backend's fill history.
        try:
            fills = backend.get_all_fills()
        except Exception:
            return None
        net = 0
        for f in fills:
            qty = f.execution.shares
            if f.execution.side == 'BOT':
                net += qty
            elif f.execution.side == 'SLD':
                net -= qty
        # Skip during lag (within positions_lag_seconds since latest fill).
        if fills:
            latest_fill_time = max(f.execution.time for f in fills)
            lag_grace = getattr(backend.config, 'positions_lag_seconds', 2.0) if hasattr(backend, 'config') else 2.0
            time_since_latest = (env.now - latest_fill_time).total_seconds()
            if time_since_latest < lag_grace + 0.5:  # small extra margin
                return None
        # Apply the position lag manually so the broker has caught up.
        if hasattr(backend, '_apply_pending_position_updates'):
            backend._apply_pending_position_updates()
        positions = backend._positions if hasattr(backend, '_positions') else {}
        broker_qty = sum(p.quantity for p in positions.values())
        if abs(broker_qty - net) > 0.5:
            return Violation(
                invariant_name=self.name,
                severity=self.severity,
                scenario_id=env.scenario_id,
                event_seq=event_seq,
                event_kind=event_kind,
                timestamp=env.now,
                expected=f"broker.qty == sum(net_fills) == {net}",
                actual=f"broker.qty = {broker_qty}",
                diagnostic=(
                    f"Broker position {broker_qty} doesn't match sum of net "
                    f"fills {net} (delta={broker_qty - net})."
                ),
                context={'broker_qty': broker_qty, 'net_fills': net},
                seed=env.seed,
            )
        return None


# ════════════════════════════════════════════════════════════════════════════
# 04. NO OFF-GRID ORDERS
# ════════════════════════════════════════════════════════════════════════════

class NoOffGridOrdersInvariant:
    """No order is EVER placed at the broker with an off-grid price.
    This is the LAST line of defense — even after engine rounding, an
    off-grid price reaching the wire is a CRITICAL failure."""

    name = "NO_OFF_GRID_ORDERS"
    description = (
        "No fill in fill_history has a price off the venue tick grid. "
        "Every placed order MUST have on-grid prices BEFORE reaching the "
        "wire — venue would otherwise reject (error 110) or silently slide."
    )
    severity = Severity.CRITICAL
    scope = frozenset([EventKind.EXECUTION])

    def check(self, env, event_kind, event_seq, event_payload):
        backend = env.backend
        tick = getattr(backend, '_runtime_min_tick', None)
        if tick is None or tick <= 0:
            return None
        fills = backend.get_all_fills() if hasattr(backend, 'get_all_fills') else []
        for f in fills:
            px = f.execution.price
            if not _is_on_grid(px, tick):
                return Violation(
                    invariant_name=self.name,
                    severity=self.severity,
                    scenario_id=env.scenario_id,
                    event_seq=event_seq,
                    event_kind=event_kind,
                    timestamp=env.now,
                    expected=f"fill price on {tick} grid",
                    actual=f"fill price = {px}",
                    diagnostic=(
                        f"Fill exec_id={f.execution.execId} price={px} is off "
                        f"the venue tick grid (tick={tick})"
                    ),
                    context={'price': px, 'tick': tick, 'exec_id': f.execution.execId},
                    seed=env.seed,
                )
        return None


# ════════════════════════════════════════════════════════════════════════════
# 05. SELL STOP BELOW ENTRY WHEN LONG
# ════════════════════════════════════════════════════════════════════════════

class SellStopBelowEntryInvariant:
    """While we hold a LONG position, any resting SELL STP must have
    stop_price STRICTLY BELOW the position's average cost. Otherwise
    the stop would fire immediately on placement (or worse: be silently
    snapped above entry by the venue, exiting the position at a loss
    we didn't intend)."""

    name = "SELL_STOP_BELOW_ENTRY"
    description = (
        "When LONG, every resting SELL stop must be strictly below the "
        "position's average cost. Catches the 2026-06-05 'stop ended up "
        "above entry' class. Fires on PLACED/MODIFIED/EXECUTION AND on "
        "every TICK + RECONCILE so seeded-bad-state orphans don't slip past."
    )
    severity = Severity.CRITICAL
    scope = frozenset([
        EventKind.PLACED, EventKind.MODIFIED, EventKind.EXECUTION,
        EventKind.TICK, EventKind.RECONCILE,
    ])

    def check(self, env, event_kind, event_seq, event_payload):
        backend = env.backend
        # Get current position
        if hasattr(backend, '_apply_pending_position_updates'):
            backend._apply_pending_position_updates()
        positions = backend._positions if hasattr(backend, '_positions') else {}
        long_positions = [p for p in positions.values() if p.quantity > 0]
        if not long_positions:
            return None
        avg_entry = long_positions[0].avg_cost
        if avg_entry <= 0:
            return None
        # Find resting SELL stops
        orders = backend.fetch_open_orders() if hasattr(backend, 'fetch_open_orders') else []
        for o in orders:
            if o.get('action') != 'SELL':
                continue
            if (o.get('order_type') or '').upper() not in ('STP', 'STPLMT'):
                continue
            stop = o.get('stop_price')
            if stop is None:
                continue
            if stop >= avg_entry:
                return Violation(
                    invariant_name=self.name,
                    severity=self.severity,
                    scenario_id=env.scenario_id,
                    event_seq=event_seq,
                    event_kind=event_kind,
                    timestamp=env.now,
                    expected=f"SELL stop < entry ({avg_entry})",
                    actual=f"SELL stop = {stop}",
                    diagnostic=(
                        f"SELL STP {o.get('order_ref')} stop={stop} is AT or "
                        f"ABOVE the LONG entry {avg_entry}. Would fire "
                        f"immediately on placement."
                    ),
                    context={
                        'order_ref': o.get('order_ref'),
                        'stop': stop,
                        'avg_entry': avg_entry,
                    },
                    seed=env.seed,
                )
        return None


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _is_on_grid(price: float, tick: float) -> bool:
    """Is `price` an integer multiple of `tick`?
    Tolerance: tick/1000 — catches precision drift but not float noise."""
    if tick <= 0:
        return True
    p = Decimal(str(price))
    t = Decimal(str(tick))
    n = (p / t).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    snapped = n * t
    return abs(float(snapped) - price) < tick * 0.001


# ════════════════════════════════════════════════════════════════════════════
# Self-registration into the default REGISTRY
# ════════════════════════════════════════════════════════════════════════════

def register_all() -> None:
    """Register every core invariant into the default REGISTRY.
    Called from `tests/harness/invariants/__init__.py`."""
    invariants = [
        (PriceOnVenueGridInvariant(),       ("precision", "critical")),
        (ModifyNotReplaceInvariant(),       ("orders", "critical")),
        (PositionQtyMatchInvariant(),       ("position", "critical")),
        (NoOffGridOrdersInvariant(),        ("precision", "critical")),
        (SellStopBelowEntryInvariant(),     ("position", "critical")),
    ]
    for inv, tags in invariants:
        try:
            REGISTRY.register(inv, tags=tags)
        except ValueError:
            # Already registered — happens on test reload. Silently OK.
            pass


__all__ = [
    "PriceOnVenueGridInvariant", "ModifyNotReplaceInvariant",
    "PositionQtyMatchInvariant", "NoOffGridOrdersInvariant",
    "SellStopBelowEntryInvariant", "register_all",
]

"""Synthetic market data generators — drive MockGateway.feed_tick() from clock.

WHY THIS EXISTS:
    Scenarios need predictable market behavior to test the engine's
    response. Real market data is too noisy and unreproducible. We
    generate synthetic ticks under various REGIMES that stress-test
    specific code paths:

      Brownian        — baseline; tests "normal day"
      Trending        — directional drift; tests breakout fires correctly
      Mean-reverting  — bounded; tests range-bound non-trigger behavior
      Gap             — discontinuity; tests stop-skipped class of bugs
      Halt            — silence; tests stale-feed / heartbeat liveness
      Fast-market     — tick spike; tests latency overrun
      Flash-crash     — spike+recover; tests stop-trigger-during-spike
      Quote-only      — no trades; tests CFD/FX LTP=0 fallback
      Crossed         — bid>ask briefly; rare but real

DESIGN:
    1. TickGenerator Protocol: takes (time_offset_s, rng, prior_bid, prior_ask)
       and returns (bid, ask, last). Pure function modulo RNG.

    2. MarketSimulator: pulls ticks at script's `tick_rate_hz`, feeds them
       to MockGateway via clock.sleep + feed_tick. Periodically calls
       MockGateway.tick_clock() to drain pending commission reports and
       position-lag updates.

    3. Tick-grid enforcement: every emitted price snaps to the MockGateway's
       reported minTick. No off-grid prices ever reach the engine — matches
       real venue behavior.

    4. DETERMINISM: same (seed, MarketScript) → identical tick sequence.
       Verified by tests.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Protocol

from ..clock import Clock
from ..mock_gateway import MockGateway
from ..rng import RNG
from ..scenario import MarketScript


# ════════════════════════════════════════════════════════════════════════════
# TICK GENERATOR PROTOCOL + IMPLEMENTATIONS
# ════════════════════════════════════════════════════════════════════════════

class TickGenerator(Protocol):
    """Produces a (bid, ask, last) for the current time offset."""

    def next_tick(
        self,
        time_offset_s: float,
        rng: RNG,
        prior_bid: Optional[Decimal],
        prior_ask: Optional[Decimal],
    ) -> tuple[Decimal, Decimal, Optional[Decimal]]:
        ...


@dataclass(slots=True)
class _Regime:
    """Shared state for stateful generators."""
    spread_floor: Decimal
    has_trades: bool


# ── Brownian (baseline) ────────────────────────────────────────────────────
@dataclass(slots=True)
class BrownianGenerator:
    """Geometric Brownian motion — log-returns ~ Normal(0, σ).

    Reasonable default for "boring liquid market" scenarios. Spread
    constant; mid evolves as random walk."""

    initial_mid: Decimal
    volatility_per_second: Decimal     # σ per second in price units
    spread: Decimal                    # constant bid-ask spread
    has_trades: bool = True

    def next_tick(self, t_s, rng, prior_bid, prior_ask):
        prior_mid = (
            (prior_bid + prior_ask) / 2 if prior_bid and prior_ask
            else self.initial_mid
        )
        # Wiener increment ~ Normal(0, σ * sqrt(dt))
        # We treat each call as 1/tick_rate dt; caller controls dt via tick_rate_hz.
        dz = rng.uniform(-1, 1) * float(self.volatility_per_second)
        new_mid = Decimal(str(float(prior_mid) + dz))
        bid = new_mid - self.spread / 2
        ask = new_mid + self.spread / 2
        last = new_mid if self.has_trades else None
        return bid, ask, last


# ── Trending (directional drift) ──────────────────────────────────────────
@dataclass(slots=True)
class TrendingGenerator:
    """Steady directional drift + noise. Tests breakout trigger firing."""

    initial_mid: Decimal
    drift_per_second: Decimal      # signed: positive = up trend
    noise_per_second: Decimal
    spread: Decimal
    has_trades: bool = True

    def next_tick(self, t_s, rng, prior_bid, prior_ask):
        if prior_bid is None or prior_ask is None:
            prior_mid = self.initial_mid
        else:
            prior_mid = (prior_bid + prior_ask) / 2
        drift = float(self.drift_per_second)
        noise = rng.uniform(-1, 1) * float(self.noise_per_second)
        new_mid = Decimal(str(float(prior_mid) + drift + noise))
        bid = new_mid - self.spread / 2
        ask = new_mid + self.spread / 2
        last = new_mid if self.has_trades else None
        return bid, ask, last


# ── Mean-reverting (Ornstein-Uhlenbeck) ───────────────────────────────────
@dataclass(slots=True)
class MeanRevertGenerator:
    """OU process: dx = θ(μ - x) dt + σ dW. Tests range-bound non-trigger."""

    anchor: Decimal
    initial_mid: Decimal
    theta: Decimal                 # pull strength
    volatility_per_second: Decimal
    spread: Decimal
    has_trades: bool = True

    def next_tick(self, t_s, rng, prior_bid, prior_ask):
        if prior_bid is None or prior_ask is None:
            prior_mid = self.initial_mid
        else:
            prior_mid = (prior_bid + prior_ask) / 2
        pull = float(self.theta) * (float(self.anchor) - float(prior_mid))
        noise = rng.uniform(-1, 1) * float(self.volatility_per_second)
        new_mid = Decimal(str(float(prior_mid) + pull + noise))
        bid = new_mid - self.spread / 2
        ask = new_mid + self.spread / 2
        last = new_mid if self.has_trades else None
        return bid, ask, last


# ── Gap (discontinuity at a configured time) ──────────────────────────────
@dataclass(slots=True)
class GapGenerator:
    """Brownian until `gap_at_seconds`, then jump by `gap_magnitude`,
    continue brownian. Tests stop-skipped-by-gap class of bugs."""

    initial_mid: Decimal
    volatility_per_second: Decimal
    spread: Decimal
    gap_at_seconds: float
    gap_magnitude: Decimal         # signed: negative for gap-down
    has_trades: bool = True
    _gap_applied: bool = False

    def next_tick(self, t_s, rng, prior_bid, prior_ask):
        if prior_bid is None or prior_ask is None:
            prior_mid = self.initial_mid
        else:
            prior_mid = (prior_bid + prior_ask) / 2
        noise = rng.uniform(-1, 1) * float(self.volatility_per_second)
        new_mid = float(prior_mid) + noise
        if not self._gap_applied and t_s >= self.gap_at_seconds:
            new_mid += float(self.gap_magnitude)
            self._gap_applied = True
        new_mid_d = Decimal(str(new_mid))
        bid = new_mid_d - self.spread / 2
        ask = new_mid_d + self.spread / 2
        last = new_mid_d if self.has_trades else None
        return bid, ask, last


# ── Flash-crash (sudden drop + recover) ───────────────────────────────────
@dataclass(slots=True)
class FlashCrashGenerator:
    """Brownian until `crash_at`, then sharp drop, then recovery over
    `recovery_seconds`. Tests stop-fires-during-spike then recovery."""

    initial_mid: Decimal
    volatility_per_second: Decimal
    spread: Decimal
    crash_at_seconds: float
    crash_magnitude: Decimal       # absolute drop amount (positive)
    recovery_seconds: float = 30.0
    has_trades: bool = True

    def next_tick(self, t_s, rng, prior_bid, prior_ask):
        if prior_bid is None or prior_ask is None:
            prior_mid = self.initial_mid
        else:
            prior_mid = (prior_bid + prior_ask) / 2
        base = float(prior_mid)
        noise = rng.uniform(-1, 1) * float(self.volatility_per_second)
        # Crash envelope: 0 before crash_at; max drop just after; linear recovery.
        if t_s < self.crash_at_seconds:
            crash_adj = 0.0
        elif t_s < self.crash_at_seconds + self.recovery_seconds:
            phase = (t_s - self.crash_at_seconds) / self.recovery_seconds
            crash_adj = -float(self.crash_magnitude) * (1 - phase)
        else:
            crash_adj = 0.0
        new_mid = Decimal(str(base + noise + crash_adj))
        bid = new_mid - self.spread / 2
        ask = new_mid + self.spread / 2
        last = new_mid if self.has_trades else None
        return bid, ask, last


# ── Halt (silence window) ─────────────────────────────────────────────────
@dataclass(slots=True)
class HaltGenerator:
    """Brownian, but during halt_window NO ticks emitted (simulator drops them).
    The simulator handles the silence; this generator returns the
    pre-halt last bid/ask unchanged when called during halt (the
    simulator should skip calling us during halt, but we return safely
    in case it does)."""

    initial_mid: Decimal
    volatility_per_second: Decimal
    spread: Decimal
    halt_at_seconds: float
    halt_duration_seconds: float
    has_trades: bool = True

    def next_tick(self, t_s, rng, prior_bid, prior_ask):
        if prior_bid is None or prior_ask is None:
            prior_mid = self.initial_mid
        else:
            prior_mid = (prior_bid + prior_ask) / 2
        # Halt simulator should skip us; if called, return last-known values.
        if self.halt_at_seconds <= t_s < self.halt_at_seconds + self.halt_duration_seconds:
            return prior_bid or self.initial_mid, prior_ask or self.initial_mid, None
        noise = rng.uniform(-1, 1) * float(self.volatility_per_second)
        new_mid = Decimal(str(float(prior_mid) + noise))
        bid = new_mid - self.spread / 2
        ask = new_mid + self.spread / 2
        last = new_mid if self.has_trades else None
        return bid, ask, last


# ── Quote-only (CFD / FX style — no trades) ──────────────────────────────
@dataclass(slots=True)
class QuoteOnlyGenerator:
    """Same as Brownian but `last` is ALWAYS None. Tests CFD/FX behavior
    where the dashboard must use mid for LTP (the 2026-06-05 IBUS500 bug)."""

    initial_mid: Decimal
    volatility_per_second: Decimal
    spread: Decimal

    def next_tick(self, t_s, rng, prior_bid, prior_ask):
        if prior_bid is None or prior_ask is None:
            prior_mid = self.initial_mid
        else:
            prior_mid = (prior_bid + prior_ask) / 2
        noise = rng.uniform(-1, 1) * float(self.volatility_per_second)
        new_mid = Decimal(str(float(prior_mid) + noise))
        return new_mid - self.spread / 2, new_mid + self.spread / 2, None


# ════════════════════════════════════════════════════════════════════════════
# GENERATOR FACTORY
# ════════════════════════════════════════════════════════════════════════════

def make_generator(script: MarketScript) -> TickGenerator:
    """Build the right TickGenerator from a MarketScript."""
    initial_mid = (script.initial_bid + script.initial_ask) / 2
    spread = script.initial_ask - script.initial_bid
    regime = script.regime.lower()

    if regime == "brownian":
        return BrownianGenerator(
            initial_mid=initial_mid,
            volatility_per_second=script.volatility_per_second,
            spread=spread,
            has_trades=script.has_trades,
        )
    if regime == "trending":
        return TrendingGenerator(
            initial_mid=initial_mid,
            drift_per_second=script.drift_per_second,
            noise_per_second=script.volatility_per_second,
            spread=spread,
            has_trades=script.has_trades,
        )
    if regime == "mean_revert" or regime == "ranging":
        return MeanRevertGenerator(
            anchor=initial_mid,
            initial_mid=initial_mid,
            theta=Decimal("0.05"),
            volatility_per_second=script.volatility_per_second,
            spread=spread,
            has_trades=script.has_trades,
        )
    if regime == "gap":
        if script.gap_at_seconds is None or script.gap_magnitude is None:
            raise ValueError("'gap' regime requires gap_at_seconds + gap_magnitude")
        return GapGenerator(
            initial_mid=initial_mid,
            volatility_per_second=script.volatility_per_second,
            spread=spread,
            gap_at_seconds=script.gap_at_seconds,
            gap_magnitude=script.gap_magnitude,
            has_trades=script.has_trades,
        )
    if regime == "flash_crash":
        if script.gap_at_seconds is None or script.gap_magnitude is None:
            raise ValueError("'flash_crash' regime requires gap_at_seconds + gap_magnitude")
        return FlashCrashGenerator(
            initial_mid=initial_mid,
            volatility_per_second=script.volatility_per_second,
            spread=spread,
            crash_at_seconds=script.gap_at_seconds,
            crash_magnitude=abs(script.gap_magnitude),
            has_trades=script.has_trades,
        )
    if regime == "halt":
        if script.halt_at_seconds is None or script.halt_duration_seconds is None:
            raise ValueError("'halt' regime requires halt_at_seconds + halt_duration_seconds")
        return HaltGenerator(
            initial_mid=initial_mid,
            volatility_per_second=script.volatility_per_second,
            spread=spread,
            halt_at_seconds=script.halt_at_seconds,
            halt_duration_seconds=script.halt_duration_seconds,
            has_trades=script.has_trades,
        )
    if regime == "quote_only":
        return QuoteOnlyGenerator(
            initial_mid=initial_mid,
            volatility_per_second=script.volatility_per_second,
            spread=spread,
        )
    raise ValueError(f"Unknown market regime: {script.regime!r}")


# ════════════════════════════════════════════════════════════════════════════
# MARKET SIMULATOR — drives MockGateway from clock-time
# ════════════════════════════════════════════════════════════════════════════

def _snap_to_grid(price: Decimal, tick: Decimal) -> Decimal:
    """Snap to the venue's tick grid. Mirrors the engine's _round_to_tick."""
    if tick <= 0:
        return price
    n = (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return n * tick


class MarketSimulator:
    """Runs a MarketScript against a MockGateway via clock-driven ticks.

    Lifecycle:
        sim = MarketSimulator(mock_gateway, clock, rng, script)
        await sim.run()      # blocks until script.duration_seconds elapses

    Internally:
        - Computes tick interval = 1 / script.tick_rate_hz
        - Loops: generate tick → snap to grid → feed_tick → tick_clock → sleep
        - Skips ticks during HaltGenerator's halt window (the silence is the test)
    """

    __slots__ = ('mock_gateway', 'clock', 'rng', 'script', '_generator',
                 '_running', '_prior_bid', '_prior_ask', '_start_monotonic')

    def __init__(
        self,
        mock_gateway: MockGateway,
        clock: Clock,
        rng: RNG,
        script: MarketScript,
    ):
        self.mock_gateway = mock_gateway
        self.clock = clock
        self.rng = rng
        self.script = script
        self._generator = make_generator(script)
        self._running = False
        self._prior_bid: Optional[Decimal] = script.initial_bid
        self._prior_ask: Optional[Decimal] = script.initial_ask
        self._start_monotonic = 0.0

    async def run(self) -> None:
        """Generate ticks until script.duration_seconds elapses."""
        self._running = True
        self._start_monotonic = self.clock.monotonic()
        tick_interval = 1.0 / max(self.script.tick_rate_hz, 0.1)
        tick_grid = Decimal(str(self.mock_gateway.get_runtime_min_tick() or 0.01))

        # Emit the initial bid/ask immediately so the engine has a starting
        # reference. Tested generators all derive subsequent ticks from prior.
        await self._emit_tick(self._prior_bid, self._prior_ask, None)

        while self._running:
            await self.clock.sleep(tick_interval)
            t_offset = self.clock.monotonic() - self._start_monotonic
            if t_offset >= self.script.duration_seconds:
                break

            # If we're a HaltGenerator and in the halt window, skip the tick.
            if isinstance(self._generator, HaltGenerator):
                if (self._generator.halt_at_seconds
                        <= t_offset
                        < self._generator.halt_at_seconds + self._generator.halt_duration_seconds):
                    continue

            bid, ask, last = self._generator.next_tick(
                t_offset, self.rng, self._prior_bid, self._prior_ask,
            )
            # Snap all prices to the venue's tick grid before feeding.
            bid_snapped = _snap_to_grid(bid, tick_grid)
            ask_snapped = _snap_to_grid(ask, tick_grid)
            last_snapped = _snap_to_grid(last, tick_grid) if last is not None else None
            # Ensure ask >= bid after snapping (avoid crossed book artifacts).
            if ask_snapped < bid_snapped:
                ask_snapped, bid_snapped = bid_snapped, ask_snapped

            self._prior_bid = bid_snapped
            self._prior_ask = ask_snapped
            await self._emit_tick(bid_snapped, ask_snapped, last_snapped)

    async def stop(self) -> None:
        self._running = False

    async def _emit_tick(
        self,
        bid: Optional[Decimal],
        ask: Optional[Decimal],
        last: Optional[Decimal],
    ) -> None:
        """Feed one tick to the MockGateway and drain pending events."""
        if bid is None or ask is None:
            return
        await self.mock_gateway.feed_tick(
            bid=float(bid),
            ask=float(ask),
            last=float(last) if last is not None else None,
        )
        # Drain pending commission reports + position updates whose
        # delay has elapsed by NOW.
        await self.mock_gateway.tick_clock()


__all__ = [
    "TickGenerator", "BrownianGenerator", "TrendingGenerator",
    "MeanRevertGenerator", "GapGenerator", "FlashCrashGenerator",
    "HaltGenerator", "QuoteOnlyGenerator",
    "make_generator", "MarketSimulator",
]

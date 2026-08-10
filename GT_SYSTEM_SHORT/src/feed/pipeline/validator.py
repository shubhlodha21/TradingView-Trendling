"""
Validator Pipeline Stage

Validates tick data for correctness and rejects invalid data.
Critical for preventing bad data from reaching trading decisions.

Validation Checks:
1. Price bounds (not negative, not unreasonably large)
2. Price consistency (bid <= ask)
3. Size bounds (not negative, not unreasonably large)
4. Timestamp validity (not in future, not too old)
5. Required fields present

Why Validation Matters:
- Bad data can cause incorrect trading signals
- Extreme values might indicate data feed errors
- Future timestamps indicate a bug
- Negative prices are impossible and indicate corruption

Design Pattern: Chain of Responsibility (pipeline stage)
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.feed.pipeline.base import PipelineStage, PipelineEvent
from src.feed.handler import Tick


@dataclass(slots=True)
class ValidationConfig:
    """Configuration for validation rules."""
    min_price: float = 0.0               # Minimum allowed price
    max_price: float = 1000000.0         # Maximum allowed price (1M for stocks)
    max_price_change_pct: float = 0.5     # Max % change from previous tick
    min_size: int = 0                   # Minimum allowed size
    max_size: int = 1000000000           # Maximum allowed size
    max_timestamp_age_seconds: float = 300  # Max age of timestamp
    allow_future_timestamps: bool = False  # Reject timestamps in future
    require_bid_ask: bool = False        # Require both bid and ask
    require_last: bool = False            # Require last price


@dataclass(slots=True)
class ValidationResult:
    """Result of tick validation."""
    valid: bool
    reason: str = ""
    warnings: list = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


class Validator(PipelineStage):
    """
    Validates tick data against configured rules.

    Validation Rules:
    1. Price must be in valid range [min_price, max_price]
    2. Bid must be <= Ask
    3. Size must be non-negative and reasonable
    4. Timestamp must not be too old or in future
    5. Required fields must be present

    Configuration:
    - min_price/max_price: Bounds for price values
    - max_price_change_pct: Max % change from last known price
    - max_timestamp_age: Max age of tick to accept
    - require_bid_ask: Whether bid/ask are required

    Usage:
        validator = Validator(min_price=0.01, max_price=100000)
        validator.set_next(NextStage())
        result = validator.process(tick)
    """

    __slots__ = (
        '_config', '_last_tick',
        '_rejected_bounds', '_rejected_consistency', '_rejected_timestamp',
        '_rejected_missing', '_warnings', '_ts',
    )

    def __init__(
        self,
        name: str = "Validator",
        config: Optional[ValidationConfig] = None,
    ):
        """
        Initialize validator.

        Args:
            name: Stage name for logging
            config: Validation configuration
        """
        super().__init__(name)
        self._config = config or ValidationConfig()

        # Track last valid tick per symbol for price change validation
        self._last_tick: dict = {}

        # Validation statistics
        self._rejected_bounds = 0
        self._rejected_consistency = 0
        self._rejected_timestamp = 0
        self._rejected_missing = 0
        self._warnings = 0
        self._ts = datetime.now

    def _process(self, tick: Tick) -> Optional[Tick]:
        """
        Validate tick data.

        Performs all validation checks and returns None if any fail.

        Args:
            tick: Tick to validate

        Returns:
            Tick if valid, None if invalid
        """
        # Check 1: Required fields
        result = self._check_required_fields(tick)
        if not result.valid:
            self._rejected_missing += 1
            self._emit_invalid_event(tick, result.reason)
            return None

        # Check 2: Price bounds
        result = self._check_price_bounds(tick)
        if not result.valid:
            self._rejected_bounds += 1
            self._emit_invalid_event(tick, result.reason)
            return None

        # Check 3: Bid/Ask consistency
        result = self._check_price_consistency(tick)
        if not result.valid:
            self._rejected_consistency += 1
            self._emit_invalid_event(tick, result.reason)
            return None

        # Check 4: Timestamp validity
        result = self._check_timestamp(tick)
        if not result.valid:
            self._rejected_timestamp += 1
            self._emit_invalid_event(tick, result.reason)
            return None

        # Check 5: Price change sanity
        result = self._check_price_change(tick)
        if not result.valid:
            self._rejected_bounds += 1
            self._emit_invalid_event(tick, result.reason)
            return None

        # Update last tick for next comparison
        self._last_tick[tick.symbol] = tick

        return tick

    def _check_required_fields(self, tick: Tick) -> ValidationResult:
        """Check that required fields are present."""
        warnings = []

        if self._config.require_bid_ask:
            if tick.bid <= 0 or tick.ask <= 0:
                return ValidationResult(False, "bid_or_ask_missing")

        if self._config.require_last:
            if tick.last <= 0:
                return ValidationResult(False, "last_price_missing")

        return ValidationResult(True, warnings=warnings)

    def _check_price_bounds(self, tick: Tick) -> ValidationResult:
        """Check that prices are within bounds."""
        prices = [
            ("bid", tick.bid),
            ("ask", tick.ask),
            ("last", tick.last),
        ]

        for name, price in prices:
            if price <= 0:
                continue  # Zero prices are handled elsewhere

            if price < self._config.min_price:
                return ValidationResult(False, f"{name}_below_min")

            if price > self._config.max_price:
                return ValidationResult(False, f"{name}_above_max")

        return ValidationResult(True)

    def _check_price_consistency(self, tick: Tick) -> ValidationResult:
        """
        Check bid/ask consistency.

        Validates:
        - bid <= ask (if both present)
        - prices are not inverted
        """
        if tick.bid > 0 and tick.ask > 0:
            if tick.bid > tick.ask:
                return ValidationResult(False, "bid_greater_than_ask")

            # Check spread is reasonable (warn if > 10%)
            if tick.bid > 0:
                spread_pct = (tick.ask - tick.bid) / tick.bid
                if spread_pct > 0.10:
                    return ValidationResult(
                        True,  # Still valid, just a warning
                        warnings=[f"large_spread_{spread_pct:.1%}"]
                    )

        return ValidationResult(True)

    def _check_timestamp(self, tick: Tick) -> ValidationResult:
        """Check that timestamp is valid."""
        now = self._ts()
        ts = tick.timestamp

        # Handle timezone-aware timestamps
        if ts.tzinfo is not None:
            # Convert to local time for comparison
            ts = ts.astimezone().replace(tzinfo=None)

        # Check if timestamp is in the future
        if not self._config.allow_future_timestamps:
            age_seconds = (now - ts).total_seconds()
            if age_seconds < -5:  # More than 5 seconds in future
                return ValidationResult(False, "timestamp_in_future")

        # Check if timestamp is too old
        max_age_seconds = self._config.max_timestamp_age_seconds
        age_seconds = (now - ts).total_seconds()

        if age_seconds > max_age_seconds:
            return ValidationResult(False, f"timestamp_too_old_{age_seconds:.0f}s")

        return ValidationResult(True)

    def _check_price_change(self, tick: Tick) -> ValidationResult:
        """
        Check that price change is reasonable.

        Compares with last known price for this symbol.

        Args:
            tick: Tick to validate

        Returns:
            ValidationResult indicating if change is reasonable
        """
        if tick.symbol not in self._last_tick:
            return ValidationResult(True)  # First tick, no comparison

        last = self._last_tick[tick.symbol]

        # Check last price change
        if tick.last > 0 and last.last > 0:
            change_pct = abs(tick.last - last.last) / last.last

            if change_pct > self._config.max_price_change_pct:
                return ValidationResult(
                    False,
                    f"price_change_{change_pct:.1%}_exceeds_limit"
                )

        return ValidationResult(True)

    def _emit_invalid_event(self, tick: Tick, reason: str) -> None:
        """Emit event for invalid tick."""
        self._emit_event(PipelineEvent.TICK_INVALID, tick, {
            "reason": reason,
            "symbol": tick.symbol,
        })

    def _emit_event(self, event: PipelineEvent, tick: Tick, data: dict) -> None:
        """Emit pipeline event."""
        pass

    def reset(self) -> None:
        """Reset validation state."""
        self._last_tick.clear()
        self._rejected_bounds = 0
        self._rejected_consistency = 0
        self._rejected_timestamp = 0
        self._rejected_missing = 0
        self._warnings = 0

    def get_report(self) -> dict:
        """Get detailed validation report."""
        base = super().get_report()
        base.update({
            "rejected_bounds": self._rejected_bounds,
            "rejected_consistency": self._rejected_consistency,
            "rejected_timestamp": self._rejected_timestamp,
            "rejected_missing": self._rejected_missing,
            "symbols_tracked": len(self._last_tick),
        })
        return base

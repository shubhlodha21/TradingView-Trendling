"""
Normalizer Pipeline Stage

Standardizes tick data format across different symbols and exchanges.
Ensures consistent data structure for downstream processing.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.feed.pipeline.base import PipelineStage
from src.feed.handler import Tick


@dataclass(slots=True)
class NormalizerConfig:
    """Configuration for normalization.

    `fill_missing_prices` defaults to False as of this patch. The previous
    True default fabricated bid/ask from last (and vice versa) — strategy
    code then saw "liquidity" at a price that nobody was actually quoting.
    Set explicitly when the consumer can tolerate the simplification.
    """
    price_precision: int = 2
    size_precision: int = 0
    timestamp_tz: timezone = timezone.utc
    fill_missing_prices: bool = False
    normalize_symbols: bool = True


class Normalizer(PipelineStage):
    """Normalizes tick data to standard format."""

    def __init__(
        self,
        name: str = "Normalizer",
        config: Optional[NormalizerConfig] = None,
    ):
        super().__init__(name)
        self._config = config or NormalizerConfig()
        self._symbol_map: dict = {}

    def _process(self, tick: Tick) -> Optional[Tick]:
        """Normalize tick data."""
        normalized = Tick(
            timestamp=self._normalize_timestamp(tick.timestamp),
            symbol=self._normalize_symbol(tick.symbol),
            bid=self._round_price(tick.bid),
            ask=self._round_price(tick.ask),
            last=self._round_price(tick.last),
            bid_size=self._normalize_size(tick.bid_size),
            ask_size=self._normalize_size(tick.ask_size),
            volume=self._normalize_size(tick.volume),
            tick_type=tick.tick_type,
            req_id=tick.req_id,
        )

        if self._config.fill_missing_prices:
            self._fill_missing_prices(normalized)

        return normalized

    def _round_price(self, price: float) -> float:
        """Round price to configured precision."""
        if price <= 0:
            return 0.0
        return round(price, self._config.price_precision)

    def _normalize_size(self, size: int) -> int:
        """Normalize size to integer."""
        if size <= 0:
            return 0
        return int(size)

    def _normalize_timestamp(self, ts: datetime) -> datetime:
        """Normalize timestamp to UTC."""
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)

    def _normalize_symbol(self, symbol: str) -> str:
        """Normalize symbol format."""
        if not self._config.normalize_symbols:
            return symbol
        if symbol in self._symbol_map:
            return self._symbol_map[symbol]
        return symbol.upper().strip()

    def _fill_missing_prices(self, tick: Tick) -> None:
        """Fill missing prices using available data.

        DANGER: this fabricates market data. Only the (bid, ask) → last
        midpoint case is reasonable, because midpoint is a defensible
        synthetic. Filling bid=last and ask=last creates phantom liquidity
        at a price nobody actually quoted — strategy code that crosses
        this bid/ask will see fills it can't actually get.

        Default is now `fill_missing_prices=False`. Override only when the
        downstream consumer truly accepts the simplification.
        """
        # Safe: derive last as the midpoint of a tight (bid, ask) book.
        if tick.bid > 0 and tick.ask > 0 and tick.last <= 0:
            tick.last = (tick.bid + tick.ask) / 2
        # The previous branches (bid=last, ask=last, last=bid) are intentionally
        # removed — they fabricated quotes. Leave the original NaN/0 sentinel
        # in place so downstream stages can reject the tick if they care.

    def add_symbol_mapping(self, from_symbol: str, to_symbol: str) -> None:
        """Add custom symbol mapping."""
        self._symbol_map[from_symbol.upper()] = to_symbol.upper()

    def get_report(self) -> dict:
        base = super().get_report()
        base.update({
            "price_precision": self._config.price_precision,
            "symbol_mappings": len(self._symbol_map),
        })
        return base

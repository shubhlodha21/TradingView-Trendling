#!/usr/bin/env python3
"""
GT System - Ultra-Fast LTP Feed for Strategy

Minimal processing for maximum speed. Target: < 0.004ms per tick.

What we capture:
- LTP (last trade price) - the ONLY thing strategy needs
- Timestamp for sequencing

What we SKIP (not needed for entry/exit):
- CSV logging (use debug script for that)
- Display (use debug script for that)
- Full Tick object creation
- Handler chain processing

Usage:
    python fast_ltp_feed.py AAPL                  # Default
    python fast_ltp_feed.py AAPL --trigger 220.00  # With trigger for demo
"""
import asyncio
import signal
import sys
from datetime import datetime
from typing import Callable, Optional

sys.path.insert(0, 'src')

from src.execution.broker import Gateway
from src.feed.connection import ConnectionManager, ConnectionConfig

# ═══════════════════════════════════════════════════════════════════════════
# Ultra-Fast LTP Callback
# ═══════════════════════════════════════════════════════════════════════════

class FastLTPFeed:
    """
    Minimal LTP feed - designed for speed.

    Only captures what strategy needs: price + timestamp.
    Skips everything else.
    """

    __slots__ = (
        '_ib', '_tbto_ticker', '_callback',
        '_prev_ltp', '_running', '_tick_count',
        '_min_latency', '_max_latency', '_avg_latency',
        '_last_update_time',
    )

    def __init__(self, ib, callback: Callable[[float, datetime], None]):
        """
        Args:
            ib: ib_async IB instance
            callback: Called with (ltp_price, timestamp) on each trade
        """
        self._ib = ib
        self._tbto_ticker = None
        self._callback = callback
        self._prev_ltp = 0.0
        self._running = False
        self._tick_count = 0

        # Latency tracking
        self._min_latency = float('inf')
        self._max_latency = 0.0
        self._avg_latency = 0.0
        self._last_update_time = datetime.now

    def subscribe(self, symbol: str, exchange: str = "SMART", currency: str = "USD"):
        """Subscribe to tick-by-tick trades only."""
        from ib_async import Stock

        contract = Stock(symbol, exchange, currency)
        qualified = self._ib.qualifyContracts(contract)
        if not qualified:
            raise ValueError(f"Cannot qualify {symbol}")
        contract = qualified[0]

        # Only subscribe to tick-by-tick trades (not BBO)
        self._tbto_ticker = self._ib.reqTickByTickData(contract, 'AllLast', 0, True)

        # Ultra-fast callback - no object creation, just call the function
        def on_trade(ticker):
            if ticker.tickByTicks:
                # Process each trade
                for tbto in ticker.tickByTicks:
                    if tbto.price > 0:
                        # Direct call - no Tick object, no handler chain
                        self._callback(tbto.price, tbto.time)
                        self._tick_count += 1

                        # Track latency (IB timestamp -> now)
                        now = datetime.now()
                        latency = (now - tbto.time).total_seconds() * 1000  # ms
                        self._update_latency_stats(latency)

        self._tbto_ticker.updateEvent += on_trade

    def _update_latency_stats(self, latency_ms: float):
        """Track latency statistics."""
        if latency_ms < self._min_latency:
            self._min_latency = latency_ms
        if latency_ms > self._max_latency:
            self._max_latency = latency_ms
        # Running average
        n = self._tick_count
        self._avg_latency = (self._avg_latency * (n - 1) + latency_ms) / n

    def unsubscribe(self):
        """Cancel subscription."""
        if self._tbto_ticker:
            try:
                self._ib.cancelTickByTickData(self._tbto_ticker)
            except:
                pass

    @property
    def stats(self) -> dict:
        return {
            'ticks': self._tick_count,
            'latency_ms': {
                'min': self._min_latency if self._min_latency != float('inf') else 0,
                'max': self._max_latency,
                'avg': self._avg_latency,
            }
        }


# ═══════════════════════════════════════════════════════════════════════════
# Strategy Handler - Ultra-Minimal
# ═══════════════════════════════════════════════════════════════════════════

class FastStrategy:
    """
    Minimal strategy handler for LTP feed.

    Only tracks:
    - Current LTP
    - Previous LTP (for cross detection)
    - Trigger price
    - Position state
    - Stop loss
    """

    __slots__ = (
        '_trigger', '_stop_pct', '_qty',
        '_position_open', '_entry_price', '_stop_price',
        '_highest', '_prev_ltp', '_prev_ltp_time',
        '_on_entry', '_on_exit',
        '_trade_count', '_wins', '_losses', '_pnl',
    )

    def __init__(
        self,
        trigger: float,
        stop_pct: float = 0.01,
        qty: int = 100,
        on_entry: Optional[Callable] = None,
        on_exit: Optional[Callable] = None,
    ):
        self._trigger = trigger
        self._stop_pct = stop_pct
        self._qty = qty
        self._on_entry = on_entry
        self._on_exit = on_exit

        # Position state
        self._position_open = False
        self._entry_price = 0.0
        self._stop_price = 0.0
        self._highest = 0.0
        self._prev_ltp = 0.0

        # Stats
        self._trade_count = 0
        self._wins = 0
        self._losses = 0
        self._pnl = 0.0

    def on_ltp(self, ltp: float, timestamp: datetime):
        """Process LTP update. Called directly from feed."""
        self._prev_ltp = ltp

        if not self._position_open:
            # Check entry: prev < trigger <= ltp
            if self._prev_ltp > 0 and ltp >= self._trigger and self._prev_ltp < self._trigger:
                self._entry(ltp)
        else:
            # Track highest
            if ltp > self._highest:
                self._highest = ltp

            # Check stop loss
            if ltp <= self._stop_price:
                self._exit(ltp, "SL")

    def _entry(self, ltp: float):
        """Execute entry."""
        self._position_open = True
        self._entry_price = ltp
        self._stop_price = round(ltp * (1 - self._stop_pct), 2)
        self._highest = ltp
        self._trade_count += 1

        print(f"[{datetime.now():%H:%M:%S}] ENTRY @ ${ltp:.2f} | SL: ${self._stop_price:.2f}")

        if self._on_entry:
            self._on_entry(ltp)

    def _exit(self, ltp: float, reason: str):
        """Execute exit."""
        pnl = (ltp - self._entry_price) * self._qty - 2.0  # ~$2 commission
        self._pnl += pnl

        if pnl > 0:
            self._wins += 1
        else:
            self._losses += 1

        print(f"[{datetime.now():%H:%M:%S}] EXIT {reason} @ ${ltp:.2f} | P&L: ${pnl:+.2f} | Total: ${self._pnl:+.2f}")

        self._position_open = False
        self._entry_price = 0.0
        self._stop_price = 0.0

        if self._on_exit:
            self._on_exit(ltp, pnl)

    @property
    def status(self) -> dict:
        return {
            'trigger': self._trigger,
            'position': self._position_open,
            'entry': self._entry_price,
            'stop': self._stop_price,
            'highest': self._highest,
            'trades': self._trade_count,
            'wins': self._wins,
            'losses': self._losses,
            'pnl': self._pnl,
        }


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('symbol', nargs='?', default='AAPL')
    parser.add_argument('--trigger', type=float, default=220.0)
    parser.add_argument('--stop', type=float, default=0.01)
    parser.add_argument('--qty', type=int, default=100)
    parser.add_argument('--port', type=int, default=4001)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--client-id', type=int, default=98)
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"FAST LTP FEED | {args.symbol} | Trigger: ${args.trigger}")
    print(f"{'='*60}\n")

    # Connect
    gw = Gateway(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        symbol=args.symbol,
        paper=True,
    )

    print("Connecting to IBKR...", end='', flush=True)
    if not await gw.connect():
        print(" FAILED")
        return 1
    print(" Connected")

    # Create strategy
    strategy = FastStrategy(
        trigger=args.trigger,
        stop_pct=args.stop,
        qty=args.qty,
    )

    # Create fast feed
    feed = FastLTPFeed(gw._ib, strategy.on_ltp)

    print(f"Subscribing to {args.symbol}...", end='', flush=True)
    feed.subscribe(args.symbol)
    print(" Done")

    print(f"\n{'='*60}")
    print("Streaming... (Ctrl+C to stop)")
    print(f"{'='*60}\n")

    running = True

    def shutdown(sig, frame):
        nonlocal running
        print("\nShutting down...")

    signal.signal(signal.SIGINT, shutdown)

    try:
        while running:
            await asyncio.sleep(1)

            # Print stats every 5 seconds
            if strategy._trade_count > 0:
                stats = strategy.status
                lat = feed.stats
                print(f"[{datetime.now():%H:%M:%S}] "
                      f"Trades: {stats['trades']} | "
                      f"P&L: ${stats['pnl']:+.2f} | "
                      f"Latency: {lat['latency_ms']['avg']:.3f}ms avg")

    finally:
        feed.unsubscribe()
        await gw.disconnect()
        print("\nDone")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
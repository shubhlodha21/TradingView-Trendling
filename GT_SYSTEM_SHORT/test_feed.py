#!/usr/bin/env python3
"""
GT System - Feed Test Script
Verifies upgraded feed system with tick-by-tick data + non-blocking CSV logging.

Features:
- Live tick-by-tick trades with exchange/size/conditions
- BBO spread display
- Non-blocking CSV logger (background thread, batched writes)
- Real-time statistics

Usage:
    python test_feed.py AAPL                    # Default port 4001
    python test_feed.py AAPL --port 4002       # Live port
    python test_feed.py AAPL --log             # Enable CSV logging
"""
import asyncio
import argparse
import csv
import os
import queue
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, 'src')

from src.feed.handler import FeedHandler, Tick, MessageType, TickHandler
from src.feed.connection import ConnectionManager, ConnectionConfig
from src.execution.broker import Gateway

# ═══════════════════════════════════════════════════════════════════════════
# ANSI Colors
# ═══════════════════════════════════════════════════════════════════════════

R = '\033[0m'
B = '\033[1m'
G = '\033[32m'
Y = '\033[33m'
R_ = '\033[31m'
C = '\033[36m'
W = '\033[97m'
D = '\033[2m'

# ═══════════════════════════════════════════════════════════════════════════
# AsyncTickLogger - Non-Blocking CSV Writer
# ═══════════════════════════════════════════════════════════════════════════

class AsyncTickLogger:
    """
    Non-blocking CSV logger using background thread + bounded queue.

    Design:
    - log() is non-blocking - never stalls the main thread
    - Drops ticks if queue full (data integrity > completeness)
    - Background thread batches writes (200 ticks per flush)
    - One file per day

    CSV format:
    timestamp,symbol,ltp,ltp_size,ltp_exchange,conditions,bid,ask,bid_size,ask_size,volume
    """
    __slots__ = (
        '_queue', '_symbol', '_path', '_file', '_writer', '_running',
        '_thread', '_dropped', '_written', '_batch_size',
    )

    def __init__(
        self,
        symbol: str,
        directory: str = "data/ticks",
        batch_size: int = 200,
        queue_size: int = 10000
    ):
        self._symbol = symbol
        self._queue = queue.Queue(maxsize=queue_size)
        self._path = None
        self._file = None
        self._writer = None
        self._running = False
        self._dropped = 0
        self._written = 0
        self._batch_size = batch_size
        self._thread = None

    def start(self, directory: str = "data/ticks") -> bool:
        """Start background writer thread. Returns True if started."""
        self._running = True

        # Create daily file
        ts = datetime.now().strftime("%Y%m%d")
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        self._path = path / f"{self._symbol}_{ts}.csv"

        # Write header if new file
        file_exists = self._path.exists()
        self._file = open(self._path, 'a', buffering=8192)
        self._writer = csv.writer(self._file)

        if not file_exists:
            self._writer.writerow([
                'timestamp', 'symbol', 'ltp', 'ltp_size', 'ltp_exchange',
                'conditions', 'bid', 'ask', 'bid_size', 'ask_size', 'volume',
                'open', 'high', 'low', 'tick_type'
            ])
            self._file.flush()

        # Start background thread
        self._thread = threading.Thread(target=self._write_loop, daemon=True, name="CSV-Writer")
        self._thread.start()
        return True

    def log(self, tick: Tick) -> bool:
        """
        Queue tick for CSV write. Non-blocking.

        Returns:
            True if queued, False if dropped (queue full)
        """
        try:
            self._queue.put_nowait(tick)
            return True
        except queue.Full:
            self._dropped += 1
            return False

    def _write_loop(self):
        """Background thread: batch writes to CSV."""
        batch = []

        while self._running:
            try:
                # Get first item (blocking, 100ms timeout)
                tick = self._queue.get(timeout=0.1)
                batch.append(tick)

                # Drain queue up to batch size
                while len(batch) < self._batch_size and not self._queue.empty():
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break

                # Write batch
                for tick in batch:
                    self._writer.writerow([
                        tick.timestamp.isoformat(timespec='microseconds'),
                        tick.symbol,
                        f"{tick.last:.4f}",
                        f"{tick.last_size:.2f}",
                        tick.last_exchange,
                        tick.last_conditions,
                        f"{tick.bid:.4f}",
                        f"{tick.ask:.4f}",
                        tick.bid_size,
                        tick.ask_size,
                        tick.volume,
                        f"{tick.open:.4f}",
                        f"{tick.high:.4f}",
                        f"{tick.low:.4f}",
                        tick.tick_type.value,
                    ])
                    self._written += 1

                self._file.flush()
                batch.clear()

            except queue.Empty:
                # Timed out - continue loop
                continue
            except Exception as e:
                print(f"[CSV] Write error: {e}", file=sys.stderr)
                batch.clear()

        # Drain remaining on shutdown
        while not self._queue.empty():
            try:
                tick = self._queue.get_nowait()
                self._writer.writerow([
                    tick.timestamp.isoformat(timespec='microseconds'),
                    tick.symbol,
                    f"{tick.last:.4f}",
                    f"{tick.last_size:.2f}",
                    tick.last_exchange,
                    tick.last_conditions,
                    f"{tick.bid:.4f}",
                    f"{tick.ask:.4f}",
                    tick.bid_size,
                    tick.ask_size,
                    tick.volume,
                    f"{tick.open:.4f}",
                    f"{tick.high:.4f}",
                    f"{tick.low:.4f}",
                    tick.tick_type.value,
                ])
                self._written += 1
            except queue.Empty:
                break
            except Exception as e:
                print(f"[CSV] Final write error: {e}", file=sys.stderr)

        # Final flush
        if self._file:
            self._file.flush()

    def stop(self):
        """Stop writer, flush remaining, close file."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._file:
            try:
                self._file.flush()
                self._file.close()
            except Exception:
                pass
        return {'written': self._written, 'dropped': self._dropped, 'path': str(self._path) if self._path else None}

    @property
    def stats(self) -> dict:
        return {
            'written': self._written,
            'dropped': self._dropped,
            'queue_size': self._queue.qsize()
        }


# ═══════════════════════════════════════════════════════════════════════════
# FeedStats - Real-time Statistics
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class FeedStats:
    """Lightweight rolling statistics."""
    total: int = 0
    trade_count: int = 0
    bbo_count: int = 0
    rate: float = 0.0
    last_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_size: int = 0
    ask_size: int = 0
    volume: int = 0
    open_price: float = 0.0
    high: float = 0.0
    low: float = 0.0
    last_exchange: str = ""
    last_size: float = 0.0
    last_conditions: str = ""
    _times: deque = None

    def __post_init__(self):
        self._times = deque(maxlen=100)

    def update(self, tick: Tick):
        """Update stats from tick."""
        self.total += 1
        now = datetime.now()
        self._times.append(now)

        if tick.tick_type == MessageType.TRADE:
            self.trade_count += 1
            self.last_price = tick.last
            self.last_exchange = tick.last_exchange
            self.last_size = tick.last_size
            self.last_conditions = tick.last_conditions
        elif tick.tick_type == MessageType.TICK:
            self.bbo_count += 1

        # Update BBO
        if tick.bid > 0:
            self.bid = tick.bid
        if tick.ask > 0:
            self.ask = tick.ask
        if tick.bid_size > 0:
            self.bid_size = tick.bid_size
        if tick.ask_size > 0:
            self.ask_size = tick.ask_size

        # Update daily ref
        if tick.volume > 0:
            self.volume = tick.volume
        if tick.open > 0 and self.open_price == 0:
            self.open_price = tick.open
        if tick.high > 0:
            self.high = tick.high
        if tick.low > 0:
            self.low = tick.low

        # Rate calculation
        if len(self._times) >= 2:
            window = (self._times[-1] - self._times[0]).total_seconds()
            if window > 0:
                self.rate = len(self._times) / window


# ═══════════════════════════════════════════════════════════════════════════
# TickDisplay - Terminal Display
# ═══════════════════════════════════════════════════════════════════════════

class TickDisplay:
    """Minimal terminal display with cursor positioning."""

    def __init__(self, symbol: str, csv_logger: AsyncTickLogger = None):
        self.symbol = symbol
        self.stats = FeedStats()
        self.csv = csv_logger
        self._trade_history = deque(maxlen=20)

    def on_tick(self, tick: Tick):
        """Process incoming tick."""
        self.stats.update(tick)

        if tick.tick_type == MessageType.TRADE:
            self._trade_history.append({
                'time': tick.timestamp.strftime("%H:%M:%S.%f")[:-3],
                'price': tick.last,
                'size': tick.last_size,
                'exchange': tick.last_exchange,
                'conditions': tick.last_conditions,
            })

    def render(self) -> str:
        """Render display frame."""
        s = self.stats

        # Calculate values
        spread = s.ask - s.bid if s.bid > 0 and s.ask > 0 else 0
        change = s.last_price - s.open_price if s.open_price > 0 else 0
        change_pct = (change / s.open_price * 100) if s.open_price > 0 else 0

        # Price color
        if change > 0:
            price_color = G
            change_color = G
        elif change < 0:
            price_color = R_
            change_color = R_
        else:
            price_color = W
            change_color = D

        lines = []

        # Header
        lines.append(f"{B}{C}╔{'═' * 78}╗{R}")
        header = f"{C}GT FEED{R}  {B}{self.symbol}{R}  {price_color}${s.last_price:.4f}{R}"
        if s.last_size > 0:
            header += f"  {D}{s.last_size:.0f}@{s.last_exchange}{R}"
        lines.append(f"{C}║{R} {header:<76} {C}║{R}")

        # Stats row
        change_str = f"{change_color}{change:+.4f} ({change_pct:+.2f}%){R}"
        lines.append(f"{C}╠{'═' * 78}╣{R}")
        stats_line = f"{C}║{R} "
        stats_line += f"BBO: {G}${s.bid:.4f}{R} x {R_}${s.ask:.4f}{R} ("
        stats_line += f"{D}spread: ${spread:.4f}{R}) | "
        stats_line += f"Change: {change_str} | "
        stats_line += f"Vol: {D}{s.volume:,}{R}"
        stats_line += f" " * max(0, 78 - len(stats_line))
        lines.append(stats_line[:79] + f" {C}║{R}")

        # Trade history
        lines.append(f"{C}╠{'═' * 36}╦{'═' * 41}╣{R}")
        lines.append(f"{C}║{R} {B}RECENT TRADES{R} {' ' * 20} {C}║{R} {B}STATS{R}")

        for i, trade in enumerate(list(self._trade_history)[-8:]):
            t = trade['time']
            p = trade['price']
            sz = trade['size']
            ex = trade['exchange']
            cond = trade['conditions']

            # Color by conditions
            cond_color = D
            if 'I' in cond:
                cond_color = Y  # Odd lot
            if 'F' in cond:
                cond_color = G  # Regular

            trade_line = f"{C}║{R} {D}{t}{R}  {W}${p:.4f}{R}  {cond_color}{sz:6.0f}{R}  {D}{ex:<5}{R} {cond_color}{cond:<4}{R}"
            trade_line += " " * (37 - len(trade_line))
            trade_line += f" {C}║{R}"

            if i == 0:
                stats_line = f"   Total: {s.total:,} | Trades: {s.trade_count:,} | BBO: {s.bbo_count:,}"
                stats_line += " " * (42 - len(stats_line))
                trade_line += stats_line + f" {C}║{R}"
            elif i == 1:
                trade_line += f"   Rate: {s.rate:.1f}/s" + " " * 30 + f" {C}║{R}"

            lines.append(trade_line)

        # Footer
        lines.append(f"{C}╚{'═' * 36}╩{'═' * 41}╝{R}")

        # CSV status
        if self.csv:
            cs = self.csv.stats
            csv_status = f"CSV: {cs['written']:,} written"
            if cs['dropped'] > 0:
                csv_status += f" | {R_}{cs['dropped']:,} dropped{R}"
            lines.append(f"  {D}{csv_status}{R} | Queue: {cs['queue_size']}/10000")

        return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# FeedHandler Wrapper for test script
# ═══════════════════════════════════════════════════════════════════════════

class TestFeedHandler(TickHandler):
    """TickHandler that captures ticks for test feed."""

    def __init__(self, callback):
        super().__init__(name="TestFeed")
        self._callback = callback

    def on_tick(self, tick: Tick):
        self._callback(tick)

    def on_error(self, error: Exception):
        print(f"[Feed] Error: {error}", file=sys.stderr)


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description='GT Feed Test')
    parser.add_argument('symbol', nargs='?', default='AAPL')
    parser.add_argument('--port', type=int, default=4001)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--log', action='store_true', help='Enable CSV logging')
    parser.add_argument('--client-id', type=int, default=99)
    parser.add_argument('--no-display', action='store_true', help='Disable terminal display')
    args = parser.parse_args()

    print(f"{C}GT Feed Test | {args.symbol} | Port {args.port}{R}\n")

    # CSV logger
    csv_logger = None
    if args.log:
        csv_logger = AsyncTickLogger(args.symbol)
        csv_logger.start()
        print(f"{D}CSV logging enabled: {csv_logger._path}{R}\n")

    # Display
    display = TickDisplay(args.symbol, csv_logger) if not args.no_display else None

    # Gateway for connection
    gw = Gateway(
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        symbol=args.symbol,
        paper=True,
    )

    # Connect
    print(f"{D}Connecting to IBKR...{R}", end='', flush=True)
    if not await gw.connect():
        print(f"\n{R_}Failed to connect!{R}")
        return 1
    print(f" {G}Connected{R}")

    # Connection manager
    conn_mgr = ConnectionManager(ConnectionConfig(host=args.host, port=args.port, client_id=args.client_id))
    conn_mgr._state = type('State', (), {'value': 'CONNECTED'})()
    conn_mgr._ib = gw._ib

    # Feed handler
    feed = FeedHandler(gw._ib, conn_mgr)

    # Subscribe symbol
    print(f"{D}Subscribing to {args.symbol}...{R}", end='', flush=True)
    feed.subscribe_symbol(args.symbol)
    print(f" {G}Done{R}")

    # Tick handler
    def on_tick(tick: Tick):
        if csv_logger:
            csv_logger.log(tick)
        if display:
            display.on_tick(tick)

    feed.subscribe(TestFeedHandler(on_tick))

    # Start feed (event-driven)
    print(f"{D}Starting feed handler...{R}")
    feed_task = asyncio.create_task(feed.start())

    # Display loop
    print(f"\n{G}Streaming live ticks... (Ctrl+C to stop){R}\n")

    running = True
    last_render = 0.0

    def shutdown(sig, frame):
        nonlocal running
        print(f"\n{D}Shutting down...{R}")
        running = False

    signal.signal(signal.SIGINT, shutdown)

    try:
        while running:
            if display:
                # Render at 10Hz
                now = time.monotonic()
                if now - last_render > 0.1:
                    # Clear and redraw
                    sys.stdout.write('\033[H')
                    sys.stdout.write(display.render())
                    sys.stdout.write('\n')
                    sys.stdout.flush()
                    last_render = now

            await asyncio.sleep(0.05)

    finally:
        # Stop everything
        await feed.stop()
        feed_task.cancel()

        if csv_logger:
            csv_stats = csv_logger.stop()
            print(f"\n{D}CSV: {csv_stats['written']:,} written, {csv_stats['dropped']:,} dropped{R}")

        await gw.disconnect()
        print(f"{D}Done.{R}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

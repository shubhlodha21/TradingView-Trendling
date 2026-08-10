#!/usr/bin/env python3
"""
Debug script to capture ALL raw fields from IBKR Ticker object.

Usage:
    python debug_raw_ticker.py [SYMBOL] [PORT] [COUNT]

Examples:
    python debug_raw_ticker.py              # AAPL, paper port 4001, 50 ticks
    python debug_raw_ticker.py AAPL         # AAPL on paper port 4001
    python debug_raw_ticker.py AAPL 4002   # AAPL on live port 4002
    python debug_raw_ticker.py AAPL 4001 0  # Infinite ticks (Ctrl+C to stop)

DUMPS THE ENTIRE TICKER OBJECT AS-IS - NO PROCESSING.
"""
import asyncio
import json
import sys
import os
from datetime import datetime
from pprint import pprint

# Apply async patches for Python 3.14+ compatibility
try:
    import asyncio
    def _safe(s):
        class N:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
        return N()
    asyncio.timeouts.timeout = _safe
except:
    pass

try:
    import nest_asyncio
    nest_asyncio.apply()
except:
    pass


def is_forex(symbol: str) -> bool:
    """Recognise Forex pairs by shape: 6 alpha chars, base+quote (EURUSD, GBPJPY)."""
    s = symbol.upper()
    return len(s) == 6 and s.isalpha()


def get_contract(symbol: str):
    """Create appropriate contract for symbol.

    Recognises three shapes:
      * Forex 6-char pair → ib_async.Forex (secType=CASH, exchange=IDEALPRO)
      * Known EU equity   → Stock(SMART, EUR)
      * Default           → Stock(SMART, USD)
    """
    from ib_async import Stock, Forex

    sym = symbol.upper()

    # Forex pair (EURUSD, GBPUSD, USDJPY, …)
    if is_forex(sym):
        # ib_async.Forex("EURUSD") expands to Contract(symbol="EUR",
        # currency="USD", secType="CASH", exchange="IDEALPRO").
        return Forex(sym)

    eu_stocks = {"ASML", "SAP", "NVD", "SHELL", "ULVR", "LVMH", "AIRBUS"}
    if sym in eu_stocks:
        return Stock(sym, "SMART", "EUR")

    return Stock(sym, "SMART", "USD")


def extract_ticker_raw(ticker, contract) -> dict:
    """
    Dump the ENTIRE Ticker object as-is.
    No method calls, no processing - raw attributes only.
    """
    result = {
        "timestamp": datetime.now().isoformat(),
        "symbol": contract.symbol if contract else None,
    }

    # Iterate over ALL attributes
    for attr in dir(ticker):
        if attr.startswith('_'):
            continue
        try:
            val = getattr(ticker, attr)
            # Skip methods and functions
            if callable(val) and not isinstance(val, type):
                continue
            # Convert to JSON-safe format
            result[f'ticker.{attr}'] = repr(val)
        except Exception as e:
            result[f'ticker.{attr}_error'] = str(e)

    return result


def print_raw_ticks(symbol: str, port: int, count: int):
    """Print raw tick data for a symbol - dump EVERYTHING."""
    from ib_async import IB

    print(f"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    IBKR RAW TICKER DUMP                                      ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Symbol: {symbol:<20}  Port: {port:<10}  Ticks: {'∞' if count == 0 else count}                          ║
╚══════════════════════════════════════════════════════════════════════════════╝
""")

    print("Connecting to IBKR Gateway...")

    ib = IB()
    try:
        ib.connect('127.0.0.1', port, clientId=99)
        print(f"✓ Connected! Client ID: 99\n")
        ib.reqMarketDataType(1)  # Live data
        print("✓ Market data type: 1 (LIVE)\n")
    except Exception as e:
        print(f"✗ Failed to connect: {e}")
        print("\n⚠ Make sure IBKR Gateway/TWS is running!")
        return

    contract = get_contract(symbol)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        print(f"✗ ERROR: Could not qualify {symbol}")
        ib.disconnect()
        return

    contract = qualified[0]
    print(f"✓ Contract: {contract}")
    print(f"   ConId: {contract.conId}")
    print(f"   Exchange: {contract.exchange}")
    print(f"   Currency: {contract.currency}\n")

    # Subscribe with genericTicks='' for ALL ticks
    ticker = ib.reqMktData(contract, '', False, False)
    print(f"✓ Subscribed to market data - BBO, last trade\n")

    # Tick-by-tick subscription type depends on asset class:
    #   * Stocks  → 'AllLast' (every trade prints)
    #   * Forex   → 'BidAsk'  (IDEALPRO has no last-trade feed, quote-driven)
    # 'Last' or 'AllLast' on a Forex contract returns nothing at all, which
    # is the silent failure mode we want to avoid here.
    is_fx = is_forex(symbol)
    tbt_type = 'BidAsk' if is_fx else 'AllLast'
    label = 'BidAsk quotes' if is_fx else 'AllLast trades'
    print(f"Requesting tick-by-tick {tbt_type} data...")
    tbto_ticker = ib.reqTickByTickData(contract, tbt_type, 0, True)
    print(f"✓ Subscribed to tick-by-tick '{tbt_type}' ({label})\n")

    # Shared counter for tick-by-tick trades
    tick_by_tick_count = [0]

    # Tick-by-tick data comes through updateEvent, stored in tickByTicks list
    def on_tbto_update(tbto_ticker):
        # Check if tickByTicks has new data
        if tbto_ticker.tickByTicks:
            for tbto in tbto_ticker.tickByTicks:
                tick_by_tick_count[0] += 1
                print(f"\n{'='*80}")
                print(f"### TICK-BY-TICK TRADE #{tick_by_tick_count[0]} ###")
                print(f"{'='*80}")
                print(f"  repr: {repr(tbto)}")
                print(f"  type: {type(tbto)}")
                # Dump all attributes
                for attr in dir(tbto):
                    if not attr.startswith('_'):
                        try:
                            val = getattr(tbto, attr)
                            if not callable(val):
                                print(f"    {attr}: {val}")
                        except:
                            pass

    tbto_ticker.updateEvent += on_tbto_update
    print(f"✓ Tick-by-tick event handler registered\n")

    print(f"Waiting for ticks (Ctrl+C to stop)...\n")
    print("=" * 80)

    tick_count = 0

    def on_update(ticker):
        nonlocal tick_count
        tick_count += 1

        # Skip if tickByTicks is populated (handled separately)
        if ticker.tickByTicks:
            return

        # Just dump the raw Ticker repr and all attributes
        print(f"\n{'='*80}")
        print(f"TICK #{tick_count} - TIMESTAMP: {datetime.now().isoformat()}")
        print(f"{'='*80}")

        # Print the raw Ticker repr (this is what IBKR sends)
        print("\n>>> RAW TICKER repr():")
        print(repr(ticker))

        # Also print all attributes
        print("\n>>> ALL ATTRIBUTES:")
        data = extract_ticker_raw(ticker, contract)

        # Print key fields first
        print("\n[PRICE FIELDS]")
        for key in ['ticker.bid', 'ticker.ask', 'ticker.last', 'ticker.open',
                    'ticker.high', 'ticker.low', 'ticker.close', 'ticker.vwap']:
            if key in data:
                print(f"  {key}: {data[key]}")

        print("\n[SIZE FIELDS]")
        for key in ['ticker.bidSize', 'ticker.askSize', 'ticker.lastSize',
                    'ticker.volume', 'ticker.openInterest']:
            if key in data:
                print(f"  {key}: {data[key]}")

        print("\n[CHANGE DATA]")
        for key in ['ticker.change', 'ticker.changePercent', 'ticker.prevBid',
                    'ticker.prevAsk', 'ticker.prevLast', 'ticker.prevBidSize',
                    'ticker.prevAskSize', 'ticker.prevLastSize']:
            if key in data:
                print(f"  {key}: {data[key]}")

        print("\n[EXCHANGE FLAGS]")
        for key in ['ticker.bidExchange', 'ticker.askExchange', 'ticker.lastExchange',
                    'ticker.bboExchange', 'ticker.snapshotPermissions']:
            if key in data:
                print(f"  {key}: {data[key]}")

        print("\n[TICKS LIST]")
        if 'ticker.ticks' in data:
            print(f"  ticker.ticks: {data['ticker.ticks']}")

        print("\n[TIMING]")
        for key in ['ticker.time', 'ticker.lastTradeTime']:
            if key in data:
                print(f"  {key}: {data[key]}")

        print("\n[AUCTION]")
        for key in ['ticker.auctionVolume', 'ticker.auctionPrice', 'ticker.auctionImbalance']:
            if key in data:
                print(f"  {key}: {data[key]}")

        print("\n[ALL REMAINING ATTRIBUTES]")
        skip_keys = set()
        for section in ['PRICE', 'SIZE', 'CHANGE', 'EXCHANGE', 'TICKS', 'TIMING', 'AUCTION']:
            skip_keys.add(f'--- {section} ---')
        for k, v in sorted(data.items()):
            if k not in ['timestamp', 'symbol'] and not any(k.startswith(p) for p in
                ['ticker.bid', 'ticker.ask', 'ticker.last', 'ticker.open', 'ticker.high',
                 'ticker.low', 'ticker.close', 'ticker.vwap', 'ticker.volume',
                 'ticker.change', 'ticker.changePercent', 'ticker.prev', 'ticker.exchange',
                 'ticker.ticks', 'ticker.time', 'ticker.lastTradeTime', 'ticker.auction',
                 'ticker.openInterest']):
                print(f"  {k}: {v}")

        print(f"\n{'='*80}\n")

        if count > 0 and tick_count >= count:
            print(f"\n✓ Captured {count} ticks, disconnecting...")
            ib.disconnect()
            sys.exit(0)

    ticker.updateEvent += on_update

    try:
        while ib.isConnected():
            ib.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n\nInterrupted. Captured {tick_count} ticks.")
    finally:
        if ib.isConnected():
            ib.disconnect()
        print("Disconnected.")


def main():
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "AAPL"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 4001
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 10

    print_raw_ticks(symbol, port, count)


if __name__ == "__main__":
    main()

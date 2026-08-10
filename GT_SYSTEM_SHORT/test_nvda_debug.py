#!/usr/bin/env python3
"""Debug test for NVDA feed."""
import asyncio
import sys
sys.path.insert(0, 'src')

from src.execution.broker import Gateway
from src.feed.handler import FeedHandler, TickHandler

class DebugHandler(TickHandler):
    def __init__(self):
        super().__init__("Debug")
        self.count = 0
    
    def on_tick(self, tick):
        self.count += 1
        if self.count <= 5:
            print(f"  Tick {self.count}: last={tick.last}, bid={tick.bid}, ask={tick.ask}, type={tick.tick_type}")
    
    def on_error(self, e):
        print(f"  Error: {e}")

async def main():
    print("Connecting to IBKR...")
    gw = Gateway(port=4001, client_id=58, symbol="NVDA")
    
    if not await gw.connect():
        print("FAILED to connect")
        return
    
    print("Connected! Setting up feed...")
    
    # Simple mock conn_mgr
    class MockConn:
        pass
    
    feed = FeedHandler(gw._ib, MockConn())
    handler = DebugHandler()
    feed.subscribe(handler)
    
    print("Subscribing to NVDA...")
    feed.subscribe_symbol("NVDA")
    
    print("Starting feed (5 seconds)...")
    # Run for 5 seconds
    try:
        await asyncio.wait_for(feed.start(), timeout=5.0)
    except asyncio.TimeoutError:
        pass
    
    print(f"\nTotal ticks received: {handler.count}")
    await feed.stop()
    await gw.disconnect()

asyncio.run(main())

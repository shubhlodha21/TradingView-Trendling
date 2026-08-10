#!/usr/bin/env python3
"""Simple test to debug connection."""
import asyncio
import sys
import os

os.environ.setdefault('GT_TICKER', 'NVDA')
os.environ.setdefault('GT_PAPER', 'true')

sys.path.insert(0, 'src')
sys.path.insert(0, '.')

print("1. Importing...")
from src.execution.broker import Gateway
from src.config.models import Config

print("2. Loading config...")
config = Config.from_env()
print(f"   Ticker: {config.ticker}")
print(f"   Port: {config.ibkr_port}")
print(f"   Paper: {config.paper_trading}")

print("3. Creating Gateway...")
gw = Gateway(
    host=config.ibkr_host,
    port=config.ibkr_port,
    client_id=69,
    symbol=config.ticker,
    paper=config.paper_trading,
)

print("4. Connecting...")
result = asyncio.run(gw.connect())
print(f"   Result: {result}")
print(f"   Connected: {gw.connected}")
print(f"   Status: {gw.status}")

if gw.connected:
    print("5. Disconnecting...")
    asyncio.run(gw.disconnect())

print("Done!")

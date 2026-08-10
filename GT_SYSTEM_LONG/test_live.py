#!/usr/bin/env python3
"""
GT System - Live Test Script with INFY NSE
"""
import asyncio
import sys
from datetime import datetime

from src.config import load
from src.execution.broker import Gateway
from src.strategy.engine import Engine
from src.strategy.risk import RiskCheck
from src.strategy.logging import QuantLogger
from src.feed.handler import Tick, MessageType
from src.config.persistence import StateStore, AuditLog


async def main():
    # Load config from env
    config = load()
    print(f"Config: {config.ticker} @ ₹{config.trigger_price}")
    print(f"Paper: {config.paper_trading}, Stop: {config.stop_loss_pct*100}%")
    print(f"IBKR: {config.ibkr_host}:{config.ibkr_port}")

    # Create structured logger
    logger = QuantLogger(output=sys.stdout, trade_cycle_id="")

    # Create gateway
    gw = Gateway(
        host=config.ibkr_host,
        port=config.ibkr_port,
        client_id=config.ibkr_client_id,
        symbol=config.ticker,
        paper=config.paper_trading,
    )

    # Connect
    print(f"\n--- Connecting to IB Gateway ---")
    if not await gw.connect():
        print("FAILED: Could not connect to IB Gateway")
        return 1

    print(f"Connected! Status: {gw.status}")
    logger.connected(config.ibkr_host, config.ibkr_port)

    # Test get_price (uses reqHistoricalData - no subscription needed)
    print(f"\n--- Fetching Price for {config.ticker} ---")
    price = await gw.get_price()
    if price:
        print(f"Current price: ${price:.2f}")
    else:
        print("Could not get price - check connection")

    # Create engine with all components
    state_store = StateStore()
    audit_log = AuditLog()

    engine = Engine(
        config=config,
        gateway=gw,
        state_store=state_store,
        audit_log=audit_log,
        logger=logger,
    )

    # Create risk checker
    risk = RiskCheck(
        config=config,
        gateway=gw,
        registry=engine.registry,
    )
    engine.risk = risk

    # Start engine
    print("\n--- Starting Engine ---")
    await engine.start()
    print(f"State: {engine.state.value}")

    # Test pre-trade risk check
    if price:
        result = risk.check(price, config.quantity)
        print(f"Risk check: {'✅ PASSED' if result.allowed else '❌ REJECTED'}")
        if not result.allowed:
            print(f"  Reason: {result.reason}")

        # Test entry simulation
        print(f"\n--- Testing Entry @ ₹{price:.2f} ---")
        tick = Tick(timestamp=datetime.now(), symbol=config.ticker, last=price, tick_type=MessageType.TRADE)
        await engine.on_tick(tick)
        print(f"Position: {'✅ OPEN' if engine._position_open else '❌ CLOSED'}")

        if engine._position_open:
            print(f"Entry: ₹{engine._entry_price:.2f}")
            print(f"Stop Loss: ₹{engine._stop_loss:.1f}")

            # Simulate price rise
            print(f"\n--- Simulating Price Rise ---")
            for new_price in [price + 10, price + 20, price + 30]:
                tick = Tick(timestamp=datetime.now(), symbol=config.ticker, last=new_price, tick_type=MessageType.TRADE)
                await engine.on_tick(tick)
                print(f"  @ ₹{new_price:.2f} - High: ₹{engine._highest_price:.2f}, State: {engine.state.value}")

    # Get final status
    print("\n--- Final Status ---")
    status = engine.get_status()
    for k, v in status.items():
        print(f"  {k}: {v}")

    # Cleanup
    print("\n--- Cleanup ---")
    await engine.stop()
    await gw.disconnect()
    logger.disconnected()
    print("Done!")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

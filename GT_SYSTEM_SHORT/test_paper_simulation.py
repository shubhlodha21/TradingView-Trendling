#!/usr/bin/env python3
"""
GT System - Paper Trading Simulator
Tests the full trading system without IB Gateway connection.
Uses simulated prices for paper trading validation.
"""
import asyncio
import sys

from src.config.models import Config, OrderSide, OrderType
from src.execution.broker import Gateway
from src.strategy.engine import Engine
from src.strategy.risk import RiskCheck
from src.strategy.logging import QuantLogger
from src.config.persistence import StateStore, AuditLog


class SimulatedPriceGateway(Gateway):
    """Gateway with simulated price stream for paper trading."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._connected = True
        self._last_price = 1800.0  # INR - INFY current range
        self._last_heartbeat = None
        self._prices = [
            1790.0,  # Below trigger - no entry
            1800.0,  # At trigger - should enter
            1810.0,  # Rising - track high
            1820.0,  # New high
            1830.0,  # New high
            1782.0,  # At stop loss (1800 * 0.99) - should exit
        ]
        self._price_idx = 0

    async def connect(self):
        """Simulate connection."""
        self._connected = True
        from datetime import datetime
        self._last_heartbeat = datetime.now()
        return True

    async def disconnect(self):
        self._connected = False

    @property
    def status(self):
        return "CONNECTED" if self._connected else "DISCONNECTED"

    async def get_price(self):
        """Return simulated price."""
        from datetime import datetime
        self._last_heartbeat = datetime.now()
        return self._prices[self._price_idx] if self._price_idx < len(self._prices) else self._last_price

    async def get_positions(self):
        """Return simulated positions."""
        return []


async def main():
    print("=" * 60)
    print("GT SYSTEM - PAPER TRADING SIMULATION")
    print("=" * 60)
    print()

    # Config for INFY with 1% stop loss
    config = Config(
        ticker="INFY",
        trigger_price=1800.0,  # INR
        stop_loss_pct=0.01,    # 1% stop loss
        quantity=1,            # 1 share
        paper_trading=True,
    )
    print(f"Config: {config.ticker} @ ₹{config.trigger_price}")
    print(f"Stop Loss: {config.stop_loss_pct*100}% = ₹{config.trigger_price * (1-config.stop_loss_pct):.2f}")
    print(f"Quantity: {config.quantity}")
    print()

    # Create logger
    logger = QuantLogger(output=sys.stdout)

    # Create simulated gateway
    gw = SimulatedPriceGateway(symbol="INFY")

    # Connect
    print("--- Connecting to simulated feed ---")
    if await gw.connect():
        print(f"Connected: {gw.status}")
        logger.connected("SIMULATED", 0)
    print()

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
    print("--- Starting Engine ---")
    await engine.start()
    print(f"State: {engine.state.value}")
    print()

    # Test pre-trade risk check
    result = risk.check(1800.0, 1)
    print(f"Risk Check @ ₹1800: {'✅ PASSED' if result.allowed else '❌ REJECTED'}")
    if not result.allowed:
        print(f"  Reason: {result.reason}")
    print()

    # Simulate price ticks
    prices = [
        1790.0,  # Below trigger - no entry
        1800.0,  # At trigger - ENTER
        1810.0,  # Rising - track high
        1820.0,  # New high
        1830.0,  # New high
        1782.0,  # At stop loss (1% below 1800) - EXIT
    ]

    print("=" * 60)
    print("PRICE SIMULATION")
    print("=" * 60)

    for i, price in enumerate(prices):
        print(f"\n--- Tick {i+1}: ₹{price} ---")
        await engine.on_tick(price)

        status = engine.get_status()
        print(f"  State: {status['state']}")
        print(f"  Position: {'✅ OPEN' if status['position_open'] else '❌ CLOSED'}")
        if status['entry_price']:
            print(f"  Entry: ₹{status['entry_price']:.2f}")
            print(f"  Stop Loss: ₹{status['stop_loss']:.2f}")
            print(f"  Highest: ₹{status['highest_price']:.2f}")
        if status['pnl'] != 0:
            print(f"  P&L: ₹{status['pnl']:+.2f}")

        # Update gateway price for next tick
        gw._price_idx = i + 1

    # Final status
    print()
    print("=" * 60)
    print("FINAL STATUS")
    print("=" * 60)
    status = engine.get_status()
    print(f"State: {status['state']}")
    print(f"Position Open: {status['position_open']}")
    print(f"Trades Today: {status['trades_today']}")
    print(f"Wins: {status['wins']}")
    print(f"Losses: {status['losses']}")
    print(f"Total P&L: ₹{status['pnl']:+.2f}")
    print(f"Commission Paid: ₹{status['total_commission']:.2f}")
    print(f"Cycle ID: {status['cycle_id']}")
    print(f"Orders in Registry: {status['trades_in_registry']}")

    # Show audit log
    print()
    print("--- Audit Log ---")
    if hasattr(audit_log, 'read'):
        entries = audit_log.read()
        for entry in entries[-5:]:  # Last 5 entries
            print(f"  {entry}")

    # Cleanup
    print()
    await engine.stop()
    await gw.disconnect()
    logger.disconnected()
    print()
    print("Simulation complete!")


if __name__ == "__main__":
    asyncio.run(main())

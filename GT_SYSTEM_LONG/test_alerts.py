#!/usr/bin/env python3
"""
Alert System Demo

Demonstrates Jane Street-style production engineering:
1. Event-based monitoring (explicit edge cases)
2. Symptom-based alerting (not root causes)
3. Defense in depth (multiple checks)
4. Anomaly detection ("Trade Too Good")
"""
import sys
sys.path.insert(0, 'src')

from src.infra.alerts import (
    AlertManager,
    AlertSeverity,
    PrintChannel,
    AnomalyDetector,
)


def demo_alert_types():
    """Show different alert categories."""
    print("=" * 60)
    print("DEMO: Alert Types")
    print("=" * 60)

    alerts = AlertManager()
    alerts.add_channel(PrintChannel())

    # --- Every Order Is Critical ---
    print("\n[1] Every Order Is Critical:")
    alerts.raise_alert(
        code="ORDER_REJECTED",
        severity=AlertSeverity.CRITICAL,
        message="BUY AAPL rejected: insufficient margin",
        context={
            "order_id": "ORD-001",
            "symbol": "AAPL",
            "side": "BUY",
            "qty": 100,
            "price": 185.50,
            "rejection_reason": "margin shortfall",
        },
        correlation_id="TRADE-001",
    )

    alerts.raise_alert(
        code="ORDER_FILL_TIMEOUT",
        severity=AlertSeverity.HIGH,
        message="Limit order not filled within 30 seconds",
        context={
            "order_id": "ORD-002",
            "symbol": "INFY",
            "timeout_seconds": 30,
        },
    )

    # --- Symptom-Based Alerting ---
    print("\n[2] Symptom-Based Alerting (not causes):")
    # BAD: Alert on database being down (cause)
    # GOOD: Alert on orders failing (symptom)

    alerts.raise_alert(
        code="CONNECTION_LOST",
        severity=AlertSeverity.CRITICAL,
        message="All order attempts failing",
        context={
            "failure_count": 5,
            "last_error": "Connection refused",
            "affected_orders": ["ORD-003", "ORD-004", "ORD-005"],
        },
    )
    # Note: We alert on the symptom (orders failing), not the cause (connection)
    # But we include connection info in context for debugging

    # --- Defense in Depth ---
    print("\n[3] Defense in Depth (multiple checks):")
    # Risk check 1: Strategy engine
    alerts.raise_alert(
        code="RISK_POSITION_LIMIT",
        severity=AlertSeverity.HIGH,
        message="Position limit reached - strategy engine",
        context={
            "layer": "strategy_engine",
            "symbol": "AAPL",
            "current_position": 500,
            "limit": 500,
        },
    )

    # Risk check 2: Order entry port
    alerts.raise_alert(
        code="RISK_POSITION_LIMIT",
        severity=AlertSeverity.HIGH,
        message="Position limit reached - order entry",
        context={
            "layer": "order_entry",
            "symbol": "AAPL",
            "current_position": 500,
            "limit": 500,
        },
    )

    # Risk check 3: External risk enforcer
    alerts.raise_alert(
        code="RISK_POSITION_LIMIT",
        severity=AlertSeverity.CRITICAL,
        message="Position limit exceeded - external risk enforcer",
        context={
            "layer": "external_risk",
            "symbol": "AAPL",
            "attempted_position": 525,
            "limit": 500,
            "action": "rejected",
        },
    )

    print("  (3 independent systems all checking the same thing)")


def demo_anomaly_detection():
    """Show "Trade Too Good" anomaly detection."""
    print("\n" + "=" * 60)
    print("DEMO: Anomaly Detection (Jane Street's favorite)")
    print("=" * 60)

    alerts = AlertManager()
    alerts.add_channel(PrintChannel())

    detector = AnomalyDetector(alerts, correlation_id="SESSION-001")

    # Set baseline from historical performance
    detector.set_baseline(
        pnl_per_hour=100.0,  # We expect to make $100/hour
        volatility=0.02,       # 2% normal volatility
        fill_rate=0.95,      # 95% fill rate normal
    )

    # Simulate trades
    print("\n[1] Normal trades (within baseline):")
    for i in range(10):
        detector.record_trade(pnl=10.0, volume=100)

    detector.check_all()

    print("\n[2] Suspicious: P&L too good (possible bug):")
    # Add a bug that makes us profitable incorrectly
    for i in range(5):
        detector.record_trade(pnl=80.0, volume=100)  # Way too profitable

    detector.check_all()

    print("\n[3] Alert History:")
    history = alerts.get_history()
    for alert in history:
        print(f"  [{alert.severity.value}] {alert.code}: {alert.message}")

    print("\n[4] Alert Counts:")
    counts = alerts.get_counts()
    for code, count in counts.items():
        print(f"  {code}: {count}")


def demo_edge_case_catalog():
    """Show explicit edge case enumeration."""
    print("\n" + "=" * 60)
    print("DEMO: Explicit Edge Case Catalog")
    print("=" * 60)

    alerts = AlertManager()
    alerts.add_channel(PrintChannel())

    print("\nAll cataloged alert codes:")
    for code, description in alerts.ALERT_CODES.items():
        print(f"  {code}: {description}")


if __name__ == "__main__":
    print("Jane Street-Style Production Engineering Demo")
    print("=" * 60)

    demo_alert_types()
    demo_anomaly_detection()
    demo_edge_case_catalog()

    print("\n" + "=" * 60)
    print("Key Takeaways:")
    print("=" * 60)
    print("""
1. Every order is critical - NO silently failing operations

2. Event-based monitoring:
   - Explicitly enumerate every edge case
   - Decide consciously whether to alert
   - Don't ignore "unlikely" scenarios

3. Symptom-based alerting:
   - Alert on symptoms (orders failing), not causes (database down)
   - Include root cause context in the alert metadata
   - Avoid duplicate alerts for same incident

4. Defense in depth:
   - Multiple independent risk checks
   - Different systems written by different teams
   - Don't share underlying logic

5. "Trade Too Good" anomaly detection:
   - If P&L is suspiciously good, something is wrong
   - Catches bugs in many parts of the stack
   - Jane Street's favorite alert

6. Signal to noise ratio:
   - Noisy alerts are worse than useless
   - Requires cultural buy-in from traders AND engineers
   - Spend time tuning alert thresholds
""")

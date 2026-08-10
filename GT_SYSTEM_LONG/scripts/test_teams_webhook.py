#!/usr/bin/env python3
"""Smoke-test the Teams webhook by firing one of each alert + trade event.

Usage:
    export GT_TEAMS_WEBHOOK_URL='...'
    python3 scripts/test_teams_webhook.py

If the webhook is correctly configured you should see 5 messages land in
your Teams channel within ~3 seconds:
    1. CRITICAL alert (red banner)
    2. HIGH alert (orange)
    3. MEDIUM alert (blue)
    4. LOW alert (green)
    5. FILLED trade event (green, with fact set)

If nothing arrives, check the script's exit summary — it shows sent /
dropped / http_errors counters so you can tell where the failure is.
"""

import os
import sys
import time
from pathlib import Path

# Allow running from anywhere — add repo root to path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.infra.alerts import (
    AlertManager, TeamsChannel, AlertSeverity, build_default_alert_manager,
)


def main():
    webhook = os.environ.get("GT_TEAMS_WEBHOOK_URL", "").strip()
    if not webhook:
        print("ERROR: GT_TEAMS_WEBHOOK_URL not set in environment.", file=sys.stderr)
        print("       Set it and re-run:  export GT_TEAMS_WEBHOOK_URL='https://...'",
              file=sys.stderr)
        sys.exit(2)

    print(f"[TEST] Webhook host: {webhook.split('/')[2]}")
    print(f"[TEST] Building TeamsChannel + firing 5 test messages …")

    teams = TeamsChannel(webhook_url=webhook)

    # Fire one alert per severity level
    severities = [
        AlertSeverity.CRITICAL,
        AlertSeverity.HIGH,
        AlertSeverity.MEDIUM,
        AlertSeverity.LOW,
    ]
    # We need to construct an Alert object for `send`; easier path is to
    # use the AlertManager and let it route through us.
    mgr = AlertManager()
    mgr.add_channel(teams)
    for sev in severities:
        mgr.raise_alert(
            code=f"TEAMS_WEBHOOK_TEST_{sev.value}",
            severity=sev,
            message=f"This is a test {sev.value} alert from GT System. "
                    f"If you can see this in Teams, the webhook is wired correctly.",
            context={
                "ticker": "TEST",
                "severity_demo": sev.value,
                "channel": "trading-alerts",
            },
            correlation_id="smoke-test-001",
        )

    # Also fire a FILLED trade event so the trade-formatter is exercised
    teams.send_trade(
        "FILLED",
        symbol="TSLA",
        side="BUY",
        qty=100,
        fill_price=618.55,
        signal_price=618.50,
        slippage=0.05,
        pnl=0.0,
        state="IN_POSITION",
        cycle_id="test-c1",
        reason="Smoke test — not a real trade",
    )

    # Give the worker a moment to drain
    print(f"[TEST] Waiting up to 5s for queue to drain …")
    stats = teams.close(timeout=5.0)
    print(f"[TEST] Done. Stats: {stats}")

    if stats["sent"] >= 5:
        print(f"[TEST] ✓ SUCCESS — 5/5 messages sent, 0 errors.")
        sys.exit(0)
    elif stats["sent"] > 0:
        print(f"[TEST] ⚠ PARTIAL — {stats['sent']}/5 sent, "
              f"{stats['http_errors']} HTTP errors, {stats['dropped']} dropped.")
        sys.exit(1)
    else:
        print(f"[TEST] ✗ FAILURE — 0 messages sent. "
              f"HTTP errors: {stats['http_errors']}, dropped: {stats['dropped']}.")
        print(f"[TEST] Check: webhook URL valid? network reachable? workflow active?")
        sys.exit(1)


if __name__ == "__main__":
    main()

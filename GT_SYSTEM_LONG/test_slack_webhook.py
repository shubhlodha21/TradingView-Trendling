#!/usr/bin/env python3
"""
Verify the Slack webhook is wired up correctly.

Fires one alert at each severity level (LOW, MEDIUM, HIGH, CRITICAL) into
your configured channel via the same code path the live bot will use.

Usage:
    export GT_SLACK_WEBHOOK="https://hooks.slack.com/services/T.../B.../..."
    python test_slack_webhook.py

If GT_SLACK_WEBHOOK is unset, the script will tell you so. Otherwise you
should see 4 messages appear in your Slack channel within a few seconds.
"""
import os
import sys
import time

sys.path.insert(0, 'src')
from src.infra.alerts import build_default_alert_manager, AlertSeverity


def main() -> int:
    webhook = os.environ.get("GT_SLACK_WEBHOOK", "").strip()

    if not webhook:
        print("\033[31mGT_SLACK_WEBHOOK is not set.\033[0m")
        print("Run:  export GT_SLACK_WEBHOOK='https://hooks.slack.com/services/T.../B.../...'")
        return 1

    # Soft sanity check on URL shape (Slack hooks always start with this prefix)
    if not webhook.startswith("https://hooks.slack.com/services/"):
        print("\033[33mWARNING:\033[0m GT_SLACK_WEBHOOK doesn't look like a Slack webhook URL")
        print(f"  got: {webhook[:60]}...")
        print("  expected: https://hooks.slack.com/services/T.../B.../...")
        print("Continuing anyway — if you have a custom webhook proxy, ignore this.")
        print()

    print(f"\033[36mFiring test alerts via GT_SLACK_WEBHOOK...\033[0m")
    print(f"  webhook: {webhook[:50]}...{webhook[-8:]}")
    print()

    # Build the same AlertManager run_live.py uses. file + stdout + Slack.
    mgr = build_default_alert_manager(
        directory="data/alerts",
        enable_stdout=True,
    )

    # Fire one alert per severity level. Each goes to all channels — Slack,
    # stdout, file. Watch your Slack channel: should see 4 messages.
    test_alerts = [
        (AlertSeverity.LOW,      "BOT_TEST_LOW",      "Slack webhook test — LOW severity. Cyan dot in dashboard."),
        (AlertSeverity.MEDIUM,   "BOT_TEST_MEDIUM",   "Slack webhook test — MEDIUM severity. Yellow dot."),
        (AlertSeverity.HIGH,     "BOT_TEST_HIGH",     "Slack webhook test — HIGH severity. Orange dot. Would page you for things like ORDER_REJECTED."),
        (AlertSeverity.CRITICAL, "BOT_TEST_CRITICAL", "Slack webhook test — CRITICAL severity. Purple dot. Reserved for naked positions / tripwires."),
    ]

    for sev, code, msg in test_alerts:
        mgr.raise_alert(
            code=code,
            severity=sev,
            message=msg,
            context={"test": True, "timestamp": time.time()},
        )
        time.sleep(0.3)  # Space out the Slack messages so order is clear

    print()
    print("\033[32mDone.\033[0m If you see 4 colored messages in your Slack channel — webhook works.")
    print("If nothing appears in Slack:")
    print("  1. Check your firewall isn't blocking outbound HTTPS to hooks.slack.com")
    print("  2. Verify the webhook URL is correct (no extra spaces, no quotes)")
    print("  3. Look at this script's stderr — SlackChannel.send prints errors there")
    print()
    print("File log: data/alerts/alerts_YYYYMMDD.jsonl  (already written regardless of Slack)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

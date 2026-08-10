"""Paper-session audit-log verifier.

Works on its own without the orchestrator. Given a session's audit
directory, validates the behavior against a scenario's expectations.

Usable two ways:
  1. Orchestrated scenario: runner.py calls verify(scenario, log_dir).
  2. Ad-hoc manual run:
       python -m tests.paper.verifier --session data/audit/20260608/EURUSD \\
              --expect-scenario P05_disconnect_while_in_position
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .catalog import AuditExpect, Scenario, by_id


@dataclass(slots=True)
class VerificationResult:
    scenario_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    def __str__(self) -> str:
        lines = [f"Scenario: {self.scenario_id}   {'✓ PASS' if self.passed else '✗ FAIL'}"]
        if self.failures:
            lines.append("  Failures:")
            for f in self.failures:
                lines.append(f"    - {f}")
        if self.warnings:
            lines.append("  Warnings:")
            for w in self.warnings:
                lines.append(f"    - {w}")
        if self.summary:
            lines.append("  Summary:")
            for k, v in self.summary.items():
                lines.append(f"    {k}: {v}")
        return "\n".join(lines)


class AuditVerifier:
    """Validates a session's audit logs against a scenario's expectations."""

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        if not self.log_dir.exists():
            raise FileNotFoundError(f"Audit log directory not found: {log_dir}")

    def verify(self, scenario: Scenario, stdout_log: Optional[Path] = None) -> VerificationResult:
        result = VerificationResult(scenario_id=scenario.id, passed=True)
        expect = scenario.expect

        order_events = self._load_csv("order.csv")
        state_events = self._load_csv("state.csv")
        all_event_names = (
            [r.get("event", "") for r in order_events] +
            [r.get("event", "") for r in state_events]
        )

        result.summary["total_order_rows"] = len(order_events)
        result.summary["total_state_rows"] = len(state_events)
        result.summary["distinct_events"] = sorted(set(all_event_names))

        if expect.must_have_events:
            missing = self._check_ordered_subsequence(all_event_names, expect.must_have_events)
            if missing:
                result.passed = False
                result.failures.append(f"Missing required events (in order): {missing}")

        for forbidden in expect.must_not_have_events:
            if forbidden in all_event_names:
                count = all_event_names.count(forbidden)
                result.passed = False
                result.failures.append(f"Forbidden event appeared {count}× : {forbidden}")

        log_text = ""
        if stdout_log and stdout_log.exists():
            log_text = stdout_log.read_text(encoding='utf-8', errors='replace')
        for needed in expect.must_have_logs:
            if needed not in log_text:
                result.passed = False
                result.failures.append(f"Missing log line: {needed!r}")
        for forbidden in expect.must_not_have_logs:
            if forbidden in log_text:
                result.passed = False
                result.failures.append(f"Forbidden log line found: {forbidden!r}")

        if state_events:
            latest = state_events[-1]
            if expect.final_state is not None:
                if latest.get("state") != expect.final_state:
                    result.passed = False
                    result.failures.append(
                        f"Final state mismatch: expected {expect.final_state!r}, "
                        f"got {latest.get('state')!r}"
                    )
            if expect.final_position_open is not None:
                actual_open = latest.get("position_open", "").lower() == "true"
                if actual_open != expect.final_position_open:
                    result.passed = False
                    result.failures.append(
                        f"Final position_open mismatch: expected "
                        f"{expect.final_position_open}, got {actual_open}"
                    )

        for inv_name in expect.invariants_clean:
            violations = self._check_invariant_on_audit(inv_name, order_events)
            if violations:
                result.passed = False
                for v in violations[:3]:
                    result.failures.append(f"Invariant {inv_name}: {v}")

        return result

    def _load_csv(self, name: str) -> list[dict]:
        path = self.log_dir / name
        if not path.exists():
            return []
        try:
            with open(path, newline="") as f:
                return list(csv.DictReader(f))
        except Exception as e:
            print(f"[verifier] failed to read {path}: {e}", file=sys.stderr)
            return []

    def _check_ordered_subsequence(self, sequence: list[str], required: tuple[str, ...]) -> list[str]:
        """Walk sequence linearly; return any required events not matched in order.
        Each required event consumes one position — repeated names need distinct matches."""
        missing: list[str] = []
        i = 0
        for r in required:
            found = False
            while i < len(sequence):
                if r == '*' or sequence[i] == r:
                    found = True
                    i += 1
                    break
                i += 1
            if not found:
                missing.append(r)
        return missing

    def _check_invariant_on_audit(self, invariant_name: str, order_events: list[dict]) -> list[str]:
        """Replay specific invariants against the audit log. Best-effort —
        catches what's evidence-able from the log alone."""
        violations: list[str] = []

        if invariant_name == "PRICE_ON_VENUE_GRID":
            for row in order_events:
                for fld in ("stop_price", "limit_price", "fill_price"):
                    v = row.get(fld, "")
                    if not v:
                        continue
                    if "." in v and len(v.split(".")[-1].rstrip("0")) > 5:
                        violations.append(
                            f"row event={row.get('event')} {fld}={v} "
                            f"has more than 5 decimals — off any FX grid"
                        )

        elif invariant_name == "MODIFY_NOT_REPLACE":
            by_eid: dict[str, list[str]] = {}
            for row in order_events:
                eid = row.get("order_id", "")
                if not eid:
                    continue
                by_eid.setdefault(eid, []).append(row.get("event", ""))
            for eid, events in by_eid.items():
                for i in range(len(events) - 2):
                    if (events[i] == "SUBMITTED" and
                            events[i+1] == "CANCELLED" and
                            events[i+2] == "SUBMITTED"):
                        violations.append(
                            f"engine_id {eid}: SUBMITTED→CANCELLED→SUBMITTED "
                            f"= cancel+replace pattern"
                        )

        elif invariant_name == "SELL_STOP_BELOW_ENTRY":
            buy_fills: list[tuple[str, float]] = []
            for row in order_events:
                if row.get("event") == "FILLED" and row.get("side") == "BUY":
                    try:
                        px = float(row.get("fill_price", "") or row.get("signal_price", "") or 0)
                        if px > 0:
                            buy_fills.append((row.get("order_id", ""), px))
                    except ValueError:
                        pass
            for row in order_events:
                if row.get("side") != "SELL":
                    continue
                otype = (row.get("order_type") or "").upper().replace(" ", "")
                if otype not in ("STP", "STPLMT"):
                    continue
                stop_v = row.get("stop_price", "")
                if not stop_v:
                    continue
                try:
                    stop_px = float(stop_v)
                except ValueError:
                    continue
                for _, bp in buy_fills:
                    if stop_px >= bp:
                        violations.append(
                            f"row event={row.get('event')} SELL "
                            f"{row.get('order_id')} stop={stop_px} >= "
                            f"BUY fill {bp} (would fire immediately)"
                        )
                        break

        elif invariant_name == "POSITION_QTY_MATCH":
            net = 0
            for row in order_events:
                if row.get("event") != "FILLED":
                    continue
                try:
                    qty = int(float(row.get("qty", "") or 0))
                except ValueError:
                    continue
                if row.get("side") == "BUY":
                    net += qty
                elif row.get("side") == "SELL":
                    net -= qty
            if net < 0:
                violations.append(
                    f"Net fill qty = {net} < 0 ⟹ engine sold more than it bought (shorted)"
                )

        return violations


def _main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Verify a paper-trading session's audit logs against a scenario.",
    )
    parser.add_argument("--session", required=True,
                        help="Path to data/audit/<DATE>/<SYM>/ directory")
    parser.add_argument("--expect-scenario", required=True,
                        help="Scenario id (e.g., P05_disconnect_while_in_position)")
    parser.add_argument("--stdout-log", default=None,
                        help="Path to captured bot stdout/stderr log file")
    args = parser.parse_args()

    try:
        scenario = by_id(args.expect_scenario)
    except KeyError:
        print(f"Unknown scenario id: {args.expect_scenario}", file=sys.stderr)
        return 2

    verifier = AuditVerifier(Path(args.session))
    stdout_log = Path(args.stdout_log) if args.stdout_log else None
    result = verifier.verify(scenario, stdout_log=stdout_log)
    print(result)
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(_main())


__all__ = ["VerificationResult", "AuditVerifier"]

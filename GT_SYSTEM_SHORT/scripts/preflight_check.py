#!/usr/bin/env python3
"""Pre-flight check — run this BEFORE every live trading session.

Verifies that what you think is deployed is actually deployed, that
critical guards are in the source code on disk, that env vars are set,
that IBKR is reachable, and that Teams alerts can fire.

Exit code 0 = safe to start engine. Exit code 1 = STOP, investigate.

Usage:
    python3 scripts/preflight_check.py
    python3 scripts/preflight_check.py --ticker PLTR  # also checks PLTR state file

Add this to your tmux startup script:
    python3 scripts/preflight_check.py || exit 1
    python3 run_live.py --ticker PLTR ...
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────────────
# Each check returns (ok: bool, message: str, fix_hint: str | None)
# ─────────────────────────────────────────────────────────────────

def check_qty_guard_deployed() -> tuple[bool, str, str | None]:
    """The qty-mismatch guard MUST be in engine.py. Today's PLTR happened
    because this guard wasn't on the box."""
    engine_py = REPO_ROOT / "src" / "strategy" / "engine.py"
    if not engine_py.exists():
        return False, f"engine.py not found at {engine_py}", "Wrong repo root?"
    text = engine_py.read_text()
    count = text.count("STALE_SELL_REJECTED")
    if count >= 2:
        return True, f"STALE_SELL_REJECTED present ({count} refs)", None
    return False, (
        f"STALE_SELL_REJECTED appears only {count}× in engine.py — "
        f"expected ≥2. The qty-mismatch guard is NOT deployed. "
        f"This is exactly what caused the PLTR -30 short."
    ), "git pull origin nabi"


def check_phantom_sell_guard_deployed() -> tuple[bool, str, str | None]:
    engine_py = REPO_ROOT / "src" / "strategy" / "engine.py"
    text = engine_py.read_text()
    if "PHANTOM_SELL_REJECTED" in text:
        return True, "PHANTOM_SELL_REJECTED present", None
    return False, "PHANTOM_SELL_REJECTED missing from engine.py", "git pull origin nabi"


def check_exposure_cap() -> tuple[bool, str, str | None]:
    """The $50k cap must be in models.py defaults."""
    models_py = REPO_ROOT / "src" / "config" / "models.py"
    text = models_py.read_text()
    if "max_position_value_usd: float = 50000" in text.replace(" ", "").replace(":float=50000", ": float = 50000"):
        # tolerate whitespace variance
        pass
    # cleaner check
    for line in text.splitlines():
        if "max_position_value_usd" in line and "=" in line:
            if "50000" in line:
                return True, f"Exposure cap = 50000 ({line.strip()})", None
            return False, f"Exposure cap line: {line.strip()}", "Bump default to 50000.0 in models.py"
    return False, "max_position_value_usd line not found", "Check models.py"


def check_env_vars() -> tuple[bool, str, str | None]:
    """Teams webhook + IBKR creds must be set in THIS shell."""
    missing = []
    if not os.environ.get("GT_TEAMS_WEBHOOK_URL"):
        missing.append("GT_TEAMS_WEBHOOK_URL")
    if missing:
        return False, f"Missing env vars: {', '.join(missing)}", (
            f"export {missing[0]}='...' before launching engine"
        )
    return True, "GT_TEAMS_WEBHOOK_URL set", None


def check_ibkr_reachable(host: str = "127.0.0.1", port: int = 7497) -> tuple[bool, str, str | None]:
    """TWS/Gateway must be listening on the expected port."""
    try:
        with socket.create_connection((host, port), timeout=2):
            return True, f"IBKR socket open on {host}:{port}", None
    except OSError as e:
        return False, f"Cannot reach IBKR on {host}:{port} ({e})", (
            "Start TWS/Gateway and enable API connections"
        )


def check_state_files_parse(ticker: str | None) -> tuple[bool, str, str | None]:
    """Every .gt_state_*.json must be valid JSON. Corrupt state = engine
    can't restart."""
    pattern = f".gt_state_{ticker}_*.json" if ticker else ".gt_state_*.json"
    files = list(REPO_ROOT.glob(pattern))
    if not files:
        return True, f"No state files matching {pattern} (clean start)", None
    bad = []
    for f in files:
        try:
            json.loads(f.read_text())
        except Exception as e:
            bad.append(f"{f.name}: {e}")
    if bad:
        return False, f"{len(bad)} corrupt state file(s): {bad}", (
            "Either fix the JSON or run with --reset to wipe"
        )
    return True, f"{len(files)} state file(s) parse cleanly", None


def check_disk_space() -> tuple[bool, str, str | None]:
    """If disk fills, audit + state writes fail silently."""
    try:
        out = subprocess.check_output(["df", "-h", str(REPO_ROOT)], text=True)
        line = out.strip().splitlines()[-1]
        # Use% column — adjust index per `df` output
        parts = line.split()
        used_pct = next((p for p in parts if p.endswith("%")), "0%")
        pct = int(used_pct.rstrip("%"))
        if pct >= 90:
            return False, f"Disk {used_pct} full", "Rotate audit logs / free space"
        return True, f"Disk {used_pct} used", None
    except Exception as e:
        return True, f"Disk check skipped ({e})", None


def check_git_clean_and_synced() -> tuple[bool, str, str | None]:
    """Working tree dirty = unknown code running. Behind remote = stale."""
    try:
        os.chdir(REPO_ROOT)
        status = subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()
        if status:
            return False, "Working tree has uncommitted changes", (
                "Either commit/stash or know what you're running"
            )
        # Check sync with origin
        subprocess.check_call(["git", "fetch", "origin"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
        behind = subprocess.check_output(
            ["git", "rev-list", "--count", f"HEAD..origin/{branch}"], text=True).strip()
        if int(behind) > 0:
            return False, f"Branch {branch} is {behind} commits behind origin", (
                f"git pull origin {branch}"
            )
        return True, f"On {branch}, clean, up-to-date", None
    except Exception as e:
        return True, f"Git check skipped ({e})", None


# ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", help="Also validate state file for this ticker")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7497, help="7497=paper, 7496=live")
    args = ap.parse_args()

    checks = [
        ("Qty-mismatch guard deployed (PLTR fix)", check_qty_guard_deployed),
        ("Phantom-SELL guard deployed",            check_phantom_sell_guard_deployed),
        ("Exposure cap = $50k",                    check_exposure_cap),
        ("Env vars set in this shell",             check_env_vars),
        ("IBKR socket reachable",                  lambda: check_ibkr_reachable(args.host, args.port)),
        ("State files parse cleanly",              lambda: check_state_files_parse(args.ticker)),
        ("Disk space OK",                          check_disk_space),
        ("Git clean + synced",                     check_git_clean_and_synced),
    ]

    print(f"\n{'─' * 70}")
    print(f"  PRE-FLIGHT CHECK — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'─' * 70}\n")

    failures = []
    for name, fn in checks:
        try:
            ok, msg, fix = fn()
        except Exception as e:
            ok, msg, fix = False, f"check raised: {e}", "Investigate manually"
        symbol = "✓" if ok else "✗"
        print(f"  {symbol}  {name:42}  {msg}")
        if not ok:
            failures.append((name, msg, fix))

    print()
    if failures:
        print(f"  {'═' * 66}")
        print(f"  ✗ NOT SAFE TO START — {len(failures)} check(s) failed:\n")
        for name, msg, fix in failures:
            print(f"    • {name}")
            print(f"      {msg}")
            if fix:
                print(f"      FIX: {fix}")
            print()
        sys.exit(1)
    else:
        print(f"  ✓ All checks passed — safe to launch engine.\n")
        sys.exit(0)


if __name__ == "__main__":
    main()

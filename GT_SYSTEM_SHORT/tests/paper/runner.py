"""Paper-trading test runner — CLI entry point.

Usage:
    # List all 37 scenarios
    python -m tests.paper.runner --list

    # Run one scenario
    python -m tests.paper.runner --scenario P05_disconnect_while_in_position

    # Run a category (smoke / connection / lifecycle / state / market / risk / orphan)
    python -m tests.paper.runner --category connection

    # Run all scenarios with a specific tag
    python -m tests.paper.runner --tag critical

    # Verify a manual session's logs (without running)
    python -m tests.paper.runner --verify-only --session data/audit/20260608/EURUSD \\
                                 --expect P05_disconnect_while_in_position

    # Run scenarios sequentially against IBKR paper account
    python -m tests.paper.runner --category smoke --report-out reports/smoke.txt
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .catalog import by_category, by_id, by_tag, list_all
from .orchestrator import TmuxOrchestrator
from .verifier import AuditVerifier


def _nuke_all_paper_state(project_root: Path) -> None:
    """Full-purge: kill any leftover gt_paper_* tmux sessions and
    remove every .gt_state_*.json / .gt_live_*.json file with a client_id
    in the test range (80-99). Safe — never touches your live client_ids."""
    # 1. kill paper tmux sessions
    try:
        out = subprocess.run(
            ["tmux", "ls"], capture_output=True, text=True, check=False,
        ).stdout
        for line in out.splitlines():
            name = line.split(":", 1)[0]
            if name.startswith("gt_paper_"):
                subprocess.run(["tmux", "kill-session", "-t", name],
                               capture_output=True, check=False)
                print(f"[clean] killed tmux session {name}")
    except FileNotFoundError:
        pass
    # 2. remove test-range state files (client_id 80..99)
    removed = 0
    for fp in project_root.glob(".gt_state_*.json"):
        # parse client_id from filename: .gt_state_<SYM>_<CID>.json
        try:
            cid = int(fp.stem.rsplit("_", 1)[-1])
        except ValueError:
            continue
        if 80 <= cid <= 99:
            fp.unlink()
            removed += 1
    for fp in project_root.glob(".gt_live_*.json"):
        try:
            cid = int(fp.stem.rsplit("_", 1)[-1])
        except ValueError:
            continue
        if 80 <= cid <= 99:
            fp.unlink()
            removed += 1
    print(f"[clean] removed {removed} test-range state files (client_id 80-99)")


def _apply_client_id_base(scenarios: list, base: int) -> list:
    """Return scenarios with client_ids remapped: 80→base, 81→base+1, etc."""
    out = []
    for s in scenarios:
        bot_args = dict(s.bot_args)
        if "multi" in bot_args:
            bot_args["multi"] = [
                {**b, "client_id": base + (b["client_id"] - 80)}
                for b in bot_args["multi"]
            ]
        else:
            bot_args["client_id"] = base + (bot_args["client_id"] - 80)
        out.append(dataclasses.replace(s, bot_args=bot_args))
    return out


def _apply_ltp_offset_bps(scenarios: list, bps: float) -> list:
    """Override every bot's ltp_offset_pct with bps/10000."""
    pct = bps / 10_000.0
    out = []
    for s in scenarios:
        bot_args = dict(s.bot_args)
        if "multi" in bot_args:
            bot_args["multi"] = [
                {**b, "ltp_offset_pct": pct} for b in bot_args["multi"]
            ]
        else:
            bot_args["ltp_offset_pct"] = pct
        out.append(dataclasses.replace(s, bot_args=bot_args))
    return out


def _apply_max_duration(scenarios: list, max_s: float) -> list:
    """Truncate any scenario to at most max_s seconds. Replaces the
    last StopCampaign with at_seconds=max_s, and clips any later
    actions away. Lets you say `--max-duration 60` to make a 900s
    scenario finish in 60s."""
    from .catalog import StopCampaign
    out = []
    for s in scenarios:
        new_actions = []
        for a in s.actions:
            if hasattr(a, "at_seconds") and getattr(a, "at_seconds", 0) > max_s:
                continue  # drop actions scheduled past the cap
            new_actions.append(a)
        # Ensure there's a StopCampaign at max_s
        has_stop = any(isinstance(a, StopCampaign) for a in new_actions)
        if not has_stop:
            new_actions.append(StopCampaign(at_seconds=max_s))
        else:
            # rewrite the StopCampaign to fire at max_s
            new_actions = [
                StopCampaign(at_seconds=max_s) if isinstance(a, StopCampaign) else a
                for a in new_actions
            ]
        out.append(dataclasses.replace(s, actions=tuple(new_actions)))
    return out


def _list_scenarios() -> int:
    """Print all 37 scenarios grouped by category."""
    by_cat: dict[str, list] = {}
    for s in list_all():
        by_cat.setdefault(s.category, []).append(s)
    print()
    print(f"{'─' * 80}")
    print(f" PAPER-TRADING SCENARIO CATALOG  ({len(list_all())} scenarios)")
    print(f"{'─' * 80}")
    for cat in ("happy", "connection", "lifecycle", "state",
                "market", "multi_instrument", "risk", "orphan", "stress"):
        if cat not in by_cat:
            continue
        print(f"\n  {cat.upper()}")
        for s in by_cat[cat]:
            tags = ", ".join(s.tags) if s.tags else ""
            print(f"    {s.id:<42} {s.name}")
            if tags:
                print(f"    {'':<42} [tags: {tags}]")
    print()
    return 0


async def _run_one(scenario_id: str, project_root: Path, log_root: Path) -> bool:
    scenario = by_id(scenario_id)
    return await _run_one_scenario(scenario, project_root, log_root)


async def _run_one_scenario(scenario, project_root: Path, log_root: Path) -> bool:
    print(f"\n{'═' * 80}")
    print(f"  RUNNING {scenario.id}")
    print(f"  {scenario.name}")
    print(f"  {scenario.description}")
    cid_str = (str(scenario.bot_args["client_id"])
               if "client_id" in scenario.bot_args
               else "multi: " + ",".join(str(b["client_id"]) for b in scenario.bot_args.get("multi", [])))
    print(f"  client_id(s): {cid_str}")
    print(f"{'═' * 80}\n")

    orchestrator = TmuxOrchestrator(project_root, log_root)
    orch_result = await orchestrator.run(scenario)
    print(f"\n[orch] actions executed:")
    for a in orch_result.actions_executed:
        print(f"  - {a}")
    if orch_result.errors:
        print(f"\n[orch] errors:")
        for e in orch_result.errors:
            print(f"  ! {e}")

    # Verify against audit logs — handle multi-bot by verifying each symbol
    today = datetime.now().strftime("%Y%m%d")
    symbols: list[str] = []
    if "multi" in scenario.bot_args and isinstance(scenario.bot_args["multi"], list):
        symbols = [b["symbol"] for b in scenario.bot_args["multi"]]
    else:
        symbols = [scenario.bot_args.get("symbol", "EURUSD")]

    overall_pass = True
    for sym in symbols:
        audit_dir = project_root / "data" / "audit" / today / sym
        if not audit_dir.exists():
            print(f"\n[verifier] audit dir not found for {sym}: {audit_dir}")
            overall_pass = False
            continue
        verifier = AuditVerifier(audit_dir)
        # find matching per-bot stdout log if multi
        per_bot_log = orch_result.stdout_log_path
        if orch_result.bot_sessions:
            for b in orch_result.bot_sessions:
                if b["symbol"] == sym:
                    per_bot_log = Path(b["log"])
                    break
        verification = verifier.verify(scenario, stdout_log=per_bot_log)
        print(f"\n[{sym}] {verification}\n")
        if not verification.passed:
            overall_pass = False
    return overall_pass


async def _run_many(scenarios: list, project_root: Path, log_root: Path) -> dict:
    return await _run_many_scenarios(scenarios, project_root, log_root)


async def _run_many_scenarios(scenarios: list, project_root: Path, log_root: Path) -> dict:
    results: dict[str, bool] = {}
    for s in scenarios:
        passed = await _run_one_scenario(s, project_root, log_root)
        results[s.id] = passed
        if s != scenarios[-1]:
            print(f"\n[runner] cooling 10s before next scenario...\n")
            await asyncio.sleep(10.0)
    return results


def _verify_only(args) -> int:
    """Standalone verification — no orchestration. Run after a manual session."""
    scenario = by_id(args.expect)
    verifier = AuditVerifier(Path(args.session))
    stdout_log = Path(args.stdout_log) if args.stdout_log else None
    result = verifier.verify(scenario, stdout_log=stdout_log)
    print(result)
    return 0 if result.passed else 1


def _main() -> int:
    p = argparse.ArgumentParser(
        description="Paper-trading test runner for the GT engine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--list", action="store_true", help="List all scenarios")
    p.add_argument("--scenario", help="Run a specific scenario by id")
    p.add_argument("--category",
                   choices=("happy", "connection", "lifecycle", "state",
                            "market", "multi_instrument", "risk", "orphan",
                            "stress"))
    p.add_argument("--tag", help="Run all scenarios with this tag (e.g., 'critical')")
    p.add_argument("--verify-only", action="store_true",
                   help="Verify a manually-run session's audit logs against a scenario")
    p.add_argument("--session", help="(verify-only) Path to data/audit/<DATE>/<SYM>/")
    p.add_argument("--expect", help="(verify-only) Scenario id to verify against")
    p.add_argument("--stdout-log", help="(verify-only) Path to captured bot stdout log")
    p.add_argument("--project-root", default=".",
                   help="Project root (where run_live.py + data/audit/ live)")
    p.add_argument("--log-dir", default="tests/paper/logs",
                   help="Where to write captured stdout logs")
    p.add_argument("--clean", action="store_true",
                   help="BEFORE running: kill stale paper tmux sessions + "
                        "remove all test-range state files (client_id 80-99). "
                        "Use this to start fresh for a new campaign.")
    p.add_argument("--client-id-base", type=int, default=80,
                   help="Base client_id for paper tests (default 80). "
                        "Each scenario remaps 80→base, 81→base+1, etc. "
                        "MUST be outside your live client_id range.")
    p.add_argument("--ltp-offset-bps", type=float, default=None,
                   help="Override every bot's ltp_offset_pct with this many "
                        "basis points (1 bp = 0.0001). Use a SMALL value "
                        "(e.g., 2 bps = 0.02 pct) to make triggers fire "
                        "almost immediately. Larger values give the bot more "
                        "monitoring time. If not given, scenario's own "
                        "default offset is used (FX/Eq=5bps, CFD=10bps).")
    p.add_argument("--max-duration", type=float, default=None,
                   help="Truncate any scenario to at most N seconds. "
                        "Useful for quick smoke tests on long-running "
                        "scenarios (e.g., --max-duration 90 forces a "
                        "900s scenario to finish in 90s).")
    args = p.parse_args()

    if args.list:
        return _list_scenarios()

    if args.verify_only:
        if not args.session or not args.expect:
            print("--verify-only requires --session and --expect", file=sys.stderr)
            return 2
        return _verify_only(args)

    project_root = Path(args.project_root).resolve()
    log_root = (project_root / args.log_dir).resolve()
    log_root.mkdir(parents=True, exist_ok=True)

    if args.client_id_base < 80 or args.client_id_base > 99:
        print(f"REFUSED: --client-id-base {args.client_id_base} is outside "
              f"the safe paper-test range (80-99). Pick a base that doesn't "
              f"clash with your live trading client_ids.", file=sys.stderr)
        return 2

    if args.clean:
        print(f"\n{'═' * 80}\n  PRE-FLIGHT CLEANUP\n{'═' * 80}")
        _nuke_all_paper_state(project_root)
        print()

    if args.scenario:
        scn = by_id(args.scenario)
        if args.client_id_base != 80:
            scn = _apply_client_id_base([scn], args.client_id_base)[0]
        if args.ltp_offset_bps is not None:
            scn = _apply_ltp_offset_bps([scn], args.ltp_offset_bps)[0]
        if args.max_duration is not None:
            scn = _apply_max_duration([scn], args.max_duration)[0]
        passed = asyncio.run(_run_one_scenario(scn, project_root, log_root))
        return 0 if passed else 1

    if args.category:
        scenarios = list(by_category(args.category))
        if args.client_id_base != 80:
            scenarios = _apply_client_id_base(scenarios, args.client_id_base)
        if args.ltp_offset_bps is not None:
            scenarios = _apply_ltp_offset_bps(scenarios, args.ltp_offset_bps)
        if args.max_duration is not None:
            scenarios = _apply_max_duration(scenarios, args.max_duration)
        results = asyncio.run(_run_many_scenarios(scenarios, project_root, log_root))
        print(f"\n{'═' * 80}\n  CAMPAIGN RESULTS: {args.category}\n{'═' * 80}")
        for sid, ok in results.items():
            print(f"  {sid:<42} {'✓ PASS' if ok else '✗ FAIL'}")
        passed_count = sum(1 for ok in results.values() if ok)
        print(f"\n  {passed_count}/{len(results)} passed\n")
        return 0 if passed_count == len(results) else 1

    if args.tag:
        scenarios = list(by_tag(args.tag))
        if not scenarios:
            print(f"No scenarios with tag {args.tag!r}", file=sys.stderr)
            return 2
        if args.client_id_base != 80:
            scenarios = _apply_client_id_base(scenarios, args.client_id_base)
        if args.ltp_offset_bps is not None:
            scenarios = _apply_ltp_offset_bps(scenarios, args.ltp_offset_bps)
        if args.max_duration is not None:
            scenarios = _apply_max_duration(scenarios, args.max_duration)
        results = asyncio.run(_run_many_scenarios(scenarios, project_root, log_root))
        print(f"\n{'═' * 80}\n  CAMPAIGN RESULTS: tag={args.tag}\n{'═' * 80}")
        for sid, ok in results.items():
            print(f"  {sid:<42} {'✓ PASS' if ok else '✗ FAIL'}")
        passed_count = sum(1 for ok in results.values() if ok)
        return 0 if passed_count == len(results) else 1

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_main())

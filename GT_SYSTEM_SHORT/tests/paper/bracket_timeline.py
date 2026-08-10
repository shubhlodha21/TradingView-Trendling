"""
A52 — Bracket lifecycle timeline reconstructor.

Reads bot logs (tmux pane captures) PLUS the chaos-test stdout transcript,
greps every line tagged [BRACKET_LIFECYCLE], and prints a chronological
per-bracket timeline.

Usage:
    python -m tests.paper.bracket_timeline LOG_DIR [LOG_DIR ...]
    python -m tests.paper.bracket_timeline ~/.gt_logs ./chaos_stdout.log

Goal: when an orphan SELL fires (PHANTOM_SELL_REJECTED → broker short),
the bracket's timeline immediately answers:
  1. Did the parent BUY fill or not? (FILL role=PARENT entry)
  2. Did the engine see it? (ENGINE_FILL_IN entry)
  3. Did anyone cancel either leg? (CANCEL_SENT entry)
  4. What was the child's last STATUS before it fired?
  5. Was reqExecutionsAsync run, and did it backfill the relevant exec?

Tagged events emitted by the engine + broker:
  PLACE_PARENT        — bracket parent placed
  PLACE_CHILD         — bracket child placed (transmit chain flushed)
  STATUS              — orderStatus transition on a bracket leg
  FILL                — fillEvent on a bracket leg
  CANCEL_SENT         — cancelOrder() call hit a bracket leg
  CONNECT             — gateway connect succeeded
  DISCONNECT          — explicit gateway.disconnect()
  RECONNECT_ATTEMPT   — engine kicked off active reconnect
  BACKFILL_NEW        — reqExecutionsAsync surfaced a NEW execution
  BACKFILL_KNOWN      — reqExecutionsAsync returned a known execution
  RECONCILE_SCAN      — _reconcile_missed_fills scan summary
  SCAN_FILL           — per-fill verdict during scan
  ENGINE_FILL_IN      — fill arrived at engine's _on_gateway_fill
  PHANTOM_SELL        — phantom-reject path taken (smoking-gun event)
  CHAOS_TWS_DOWN/UP   — chaos-test wall-clock markers
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Pattern matches any line containing the [BRACKET_LIFECYCLE] tag, plus an
# optional leading timestamp that the bot's tmux log prefixes lines with.
# We capture the raw remainder so we can parse the key=value fields.
TAG = "[BRACKET_LIFECYCLE]"


@dataclass
class Event:
    src_file: str
    line_no: int
    raw: str           # full line as captured
    ts: Optional[str]  # log line's own timestamp (if any) — best-effort
    kind: str          # e.g. PLACE_PARENT, FILL, ...
    fields: Dict[str, str] = field(default_factory=dict)


_TS_PATTERNS = [
    # ISO-ish:  2026-06-09T22:13:00 or 2026-06-09 22:13:00.123
    re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)"),
    # tmux-style HH:MM:SS at line start
    re.compile(r"^\s*(\d{2}:\d{2}:\d{2})"),
]


def _extract_ts(line: str) -> Optional[str]:
    for p in _TS_PATTERNS:
        m = p.search(line)
        if m:
            return m.group(1)
    return None


_KV_RE = re.compile(r"(\w+)=([^\s]+)")


def parse_event(src: str, lineno: int, line: str) -> Optional[Event]:
    idx = line.find(TAG)
    if idx < 0:
        return None
    body = line[idx + len(TAG):].strip()
    # First token is the kind.
    parts = body.split(None, 1)
    if not parts:
        return None
    kind = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    fields = {k: v for k, v in _KV_RE.findall(rest)}
    return Event(
        src_file=src,
        line_no=lineno,
        raw=line.rstrip(),
        ts=_extract_ts(line),
        kind=kind,
        fields=fields,
    )


def gather(paths: List[Path]) -> List[Event]:
    events: List[Event] = []
    for p in paths:
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    events.extend(_scan_file(f))
        elif p.is_file():
            events.extend(_scan_file(p))
    # Sort by ts string (lexical sort works for ISO format).
    events.sort(key=lambda e: (e.ts or "", e.src_file, e.line_no))
    return events


def _scan_file(f: Path) -> List[Event]:
    out: List[Event] = []
    try:
        with f.open("r", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                if TAG not in line:
                    continue
                ev = parse_event(str(f), i, line)
                if ev:
                    out.append(ev)
    except Exception as e:
        print(f"[warn] failed to read {f}: {e}", file=sys.stderr)
    return out


def group_by_bracket(events: List[Event]) -> Dict[str, List[Event]]:
    """Group events by bracket identity. Keying strategy:
      - PLACE_PARENT / PLACE_CHILD: use engine_id (BR_BUY_/BR_SELL_)
      - STATUS / FILL / CANCEL_SENT: use engine_id when present, else broker_id
      - ENGINE_FILL_IN: use order_id (engine_id)
      - PHANTOM_SELL: use order_id
      - Other (CONNECT/DISCONNECT/BACKFILL/...): grouped under a synthetic
        "_session_<cid>" bucket so they appear in the per-cid timeline too.
    """
    by_bracket: Dict[str, List[Event]] = defaultdict(list)
    # Map broker_id → engine_id learned from PLACE_PARENT/PLACE_CHILD so
    # later STATUS lines (which only have broker_id sometimes) can be
    # rejoined to the right bracket.
    broker_to_engine: Dict[str, str] = {}

    for ev in events:
        eid = ev.fields.get("engine_id")
        bid = ev.fields.get("broker_id")
        order_id = ev.fields.get("order_id")
        if ev.kind in ("PLACE_PARENT", "PLACE_CHILD"):
            if eid and bid:
                broker_to_engine[bid] = eid
            if eid:
                by_bracket[eid].append(ev)
            continue
        # Resolve via broker_id → engine_id if needed.
        key = eid or order_id
        if not key and bid:
            key = broker_to_engine.get(bid, bid)
        if not key:
            key = f"_session_cid={ev.fields.get('cid', '?')}"
        by_bracket[key].append(ev)
    return by_bracket


def print_timeline(by_bracket: Dict[str, List[Event]], session_events: List[Event]) -> None:
    # Print the session events (chaos markers + connects + backfills) first
    # so the operator can read them as the "outer context".
    print("=" * 78)
    print("SESSION TIMELINE  (chaos markers, connects, backfills)")
    print("=" * 78)
    for ev in session_events:
        print(f"  {ev.ts or '?':<26}  cid={ev.fields.get('cid','?'):>3}  "
              f"{ev.kind:<22}  {_summary(ev)}")
    print()

    # Now per-bracket. Sort brackets by their first event time so the most
    # relevant (earliest) ones print at the top.
    keys = sorted(
        by_bracket.keys(),
        key=lambda k: (by_bracket[k][0].ts or "") if by_bracket[k] else "",
    )
    print("=" * 78)
    print(f"PER-BRACKET TIMELINES ({len(keys)} brackets)")
    print("=" * 78)
    for k in keys:
        if k.startswith("_session_"):
            continue
        evs = by_bracket[k]
        # Skip brackets with only one event — not interesting.
        if len(evs) < 2:
            continue
        # Compute verdict: did this bracket end clean or as orphan/phantom?
        kinds = [e.kind for e in evs]
        verdict = _verdict(kinds)
        print(f"\n── bracket: {k}   verdict: {verdict}")
        for ev in evs:
            marker = " 🚨" if ev.kind in ("PHANTOM_SELL",) else ""
            print(
                f"  {ev.ts or '?':<26}  cid={ev.fields.get('cid','?'):>3}  "
                f"{ev.kind:<22}  {_summary(ev)}{marker}"
            )


def _summary(ev: Event) -> str:
    # Compact field rendering — drop cid (already shown), engine_id (in
    # the bracket header), broker_id (less useful than engine_id).
    drop = {"cid", "engine_id", "broker_id"}
    pairs = [f"{k}={v}" for k, v in ev.fields.items() if k not in drop]
    return "  ".join(pairs)


def _verdict(kinds: List[str]) -> str:
    if "PHANTOM_SELL" in kinds:
        return "🚨 PHANTOM_SELL — orphan child fired without engine seeing parent fill"
    has_parent_fill = any(
        k == "FILL" for k in kinds  # at least one fill
    )
    has_cancel = "CANCEL_SENT" in kinds
    if has_parent_fill and "FILL" in kinds:
        # Distinguish: if there are 2 fills (parent + child), normal cycle.
        n_fills = sum(1 for k in kinds if k == "FILL")
        if n_fills >= 2:
            return "✓ completed cycle (BUY filled, SELL filled)"
        return "⚠ partial cycle (one fill, no closing fill)"
    if has_cancel and not has_parent_fill:
        return "✓ cancelled clean (no fills)"
    if "PLACE_PARENT" in kinds and not has_parent_fill and not has_cancel:
        return "⚠ placed but never resolved (no fill, no cancel)"
    return "?"


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    paths = [Path(p).expanduser() for p in argv[1:]]
    events = gather(paths)
    if not events:
        print("No [BRACKET_LIFECYCLE] events found in the supplied paths.",
              file=sys.stderr)
        return 1
    by_bracket = group_by_bracket(events)
    # Pull out "session-level" events (chaos markers, connects, scans).
    session_events = [
        e for e in events
        if e.kind in (
            "CHAOS_TWS_DOWN", "CHAOS_TWS_UP",
            "CONNECT", "DISCONNECT", "RECONNECT_ATTEMPT",
            "RECONCILE_SCAN", "BACKFILL_NEW",
        )
    ]
    print_timeline(by_bracket, session_events)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

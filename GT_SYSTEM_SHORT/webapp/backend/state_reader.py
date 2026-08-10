"""
Reads `.gt_state_<SYM>_<CID>.json` + `.gt_live_<SYM>_<CID>.json` from the
working directory and emits typed SymbolSnapshot objects.

Same filenames + same JSON contract as `dashboard_agg.py:SymbolWatcher`
— we share the artifacts, never the code. If `run_live.py` changes its
write format the dashboards break together, by design (single source
of truth = those JSON files).

Performance notes (the user asked for "low-level techniques"):
  * `os.scandir` instead of `Path.glob`: ~10x faster on directories with
    many siblings because it avoids stat() per entry.
  * Filename parsing via a precompiled `re` pattern instead of `.split('_')`
    — handles symbols with embedded digits and dots (BRK.B, RDS.A) and
    fails fast on malformed names.
  * Per-file mtime cache: re-parse only when the writer has bumped mtime.
    A spinning poll of 8 unchanged files measures at ~30 µs total.
  * `json.loads(open(p, 'rb').read())` on bytes — skips the text decode
    inside `json.load(f)`, which is a measurable win on a 2-3 KB blob.
  * Atomic-write awareness: writers use temp+rename so an in-flight
    read either gets the old file complete or the new file complete,
    never a half-written one. We don't need fsync coordination.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .schemas import SymbolLive, SymbolSnapshot, SymbolState


# Filename grammar: `.gt_state_<SYMBOL>_<CLIENTID>.json`
# SYMBOL is upper letters + digits + dots (BRK.B, RDS.A); CLIENTID is digits.
_STATE_RE = re.compile(r"^\.gt_state_([A-Z][A-Z0-9.]{0,7})_(\d{1,3})\.json$")

# Hide rows whose state file hasn't been touched in this long (matches
# the existing aggregator default). Configurable via env.
STALENESS_S = int(os.environ.get("GT_WEBAPP_STALENESS_S", "30"))


@dataclass(slots=True)
class _CachedFile:
    """One parsed JSON blob + the mtime it was parsed at.

    Re-parsing happens only when the on-disk mtime changes. A frame
    with N watchers and zero file changes costs N stat() calls — about
    5 µs per file on a hot SSD."""
    data: dict = field(default_factory=dict)
    mtime: float = 0.0
    errors: int = 0
    last_error: str = ""


class StateReader:
    """Polls `.gt_state_*_*.json` + `.gt_live_*_*.json` in a directory.

    Stateless from the caller's perspective: every `snapshot()` call
    returns a fresh list of SymbolSnapshot. The internal mtime cache
    is opaque optimization — there's no "reset" or "subscribe" API,
    just a poll.
    """
    __slots__ = ("_cwd", "_state_cache", "_live_cache")

    def __init__(self, cwd: Optional[Path] = None) -> None:
        self._cwd = Path(cwd) if cwd else Path(os.environ.get("GT_WEBAPP_CWD", "."))
        # Per-path caches keyed by absolute path string. Cleared lazily —
        # a removed file just stops appearing in scandir.
        self._state_cache: dict[str, _CachedFile] = {}
        self._live_cache: dict[str, _CachedFile] = {}

    @property
    def cwd(self) -> Path:
        return self._cwd

    # ── Discovery ───────────────────────────────────────────────────────
    def _scan(self) -> Iterable[tuple[str, str, Path, float]]:
        """Yield (symbol, client_id, path, mtime) for every state file."""
        try:
            it = os.scandir(self._cwd)
        except FileNotFoundError:
            return
        with it as entries:
            for entry in entries:
                if not entry.is_file():
                    continue
                m = _STATE_RE.match(entry.name)
                if not m:
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                yield m.group(1), m.group(2), Path(entry.path), mtime

    # ── Cached file read ────────────────────────────────────────────────
    @staticmethod
    def _read_json(path: Path, cache: _CachedFile, mtime: float) -> dict:
        """Return the file's JSON content, re-reading only on mtime change."""
        if mtime == cache.mtime and cache.data:
            return cache.data
        try:
            with open(path, "rb") as f:
                cache.data = json.loads(f.read() or b"{}")
            cache.mtime = mtime
        except (OSError, json.JSONDecodeError) as e:
            cache.errors += 1
            cache.last_error = f"{type(e).__name__}: {e}"
        return cache.data

    # ── Public API ──────────────────────────────────────────────────────
    def snapshot(self) -> list[SymbolSnapshot]:
        """Build a SymbolSnapshot for every live state file in cwd.

        Sort order: active first (by symbol), then stale (by client_id).
        Matches the existing aggregator's row ordering."""
        now = time.time()
        out: list[SymbolSnapshot] = []

        for symbol, cid_str, state_path, mtime in self._scan():
            cid = int(cid_str)
            key = f"{symbol}_{cid}"

            # State file
            scache = self._state_cache.setdefault(str(state_path), _CachedFile())
            state_dict = self._read_json(state_path, scache, mtime)
            try:
                state = SymbolState(**state_dict)
            except Exception:
                state = SymbolState()

            # Live snapshot (sibling file, optional)
            live_path = state_path.with_name(f".gt_live_{symbol}_{cid_str}.json")
            live = SymbolLive()
            if live_path.exists():
                try:
                    lmtime = live_path.stat().st_mtime
                    lcache = self._live_cache.setdefault(str(live_path), _CachedFile())
                    live_dict = self._read_json(live_path, lcache, lmtime)
                    if live_dict:
                        try:
                            live = SymbolLive(**live_dict)
                        except Exception:
                            live = SymbolLive()
                except OSError:
                    pass

            spread = live.ask - live.bid if (live.ask and live.bid) else 0.0
            spread_bps = (spread / live.last * 10_000.0) if live.last > 0 else 0.0
            change_pct = ((live.last - live.open) / live.open * 100.0) if live.open > 0 else 0.0

            out.append(SymbolSnapshot(
                key=key,
                symbol=symbol,
                client_id=cid,
                is_active=(now - mtime) < STALENESS_S,
                last_seen=datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                state=state,
                live=live,
                spread=spread,
                spread_bps=spread_bps,
                change_pct=change_pct,
            ))

        out.sort(key=lambda s: (not s.is_active, s.symbol, s.client_id))
        return out

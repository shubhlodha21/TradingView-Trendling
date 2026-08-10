"""Persistent, exactly-once fill ledger — the "Layer 2 journaling" that
``broker.get_our_position_via_executions`` (A43) explicitly defers to.

WHY THIS EXISTS
---------------
For spot FX, IBKR cannot tell us a per-pair position: it only stores
per-currency cash balances, and pairs that share a currency land in one
shared bucket. Formally, with pairs = edges and currencies = vertices,
``positions()`` observes only flow-conservation at each vertex (rank V-C)
and is BLIND to the graph's cycle space (dim E-V+C). For the 15-pair fleet
that is 8 invisible dimensions. So the only complete observable is the
stream of pair-tagged executions, and the true position is their integral:

        net(symbol) = Σ BOT(shares) − Σ SLD(shares)     over our fills

That integral is wrong ONLY if a fill is missed or double-counted. This
ledger guarantees the integral is **exactly-once**, **gap-free**, and
**durable** across:
  * process restarts / chaos respawns (bot died mid-fill),
  * IBKR's ~24h execution-cache eviction (``ib.fills()`` forgets),
  * reconnect double-callbacks (same execId replayed).

DESIGN
------
* Append-only JSONL, one record per fill, keyed by the broker-unique
  ``exec_id``. Dedup on read AND write → exactly-once.
* fsync on every append → the ledger survives a hard kill (SIGKILL from
  ``tmux kill-session``) with at most the in-flight line lost.
* ONE file per ``(symbol, port, client_id)`` → no cross-writer contention;
  matches the A79 single-writer model and scales to the 32-client TWS cap
  with zero coordination. 1 bot or 32 bots, identical semantics.
* ZERO dependency on engine/gateway/ib_async. Symbol translation and
  client-id filtering are injected by the caller, so the monitor can read
  these files with **no broker connection** (freeing a client-id slot).

This module is pure and standalone; wiring into the engine/gateway/monitor
happens in later steps and never changes existing behavior — it only adds
a durable write and a more-correct read.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Callable, Iterable, Optional


def _norm_side(side) -> int:
    """Map a broker/engine side token to a sign. +1 buy, -1 sell, 0 unknown.

    Accepts IBKR taxonomy ('BOT'/'SLD'), engine taxonomy ('BUY'/'SELL'),
    and single-letter shorthands. Anything else → 0 (record kept for
    dedup/audit but contributes nothing to net)."""
    s = str(side or "").strip().upper()
    if s in ("BOT", "BUY", "B"):
        return +1
    if s in ("SLD", "SELL", "S"):
        return -1
    return 0


class FillLedger:
    """Durable exactly-once journal of executions for a single bot.

    Thread-safe (a lock guards the seen-set / net / file append) because
    fills can arrive on the ib_async event thread while the strategy loop
    reads ``net()``.
    """

    __slots__ = ("path", "_seen", "_net", "_count", "_lock")

    def __init__(self, path: str):
        self.path = str(path)
        self._seen: set = set()        # execIds already applied (dedup)
        self._net: dict = {}           # symbol -> signed net shares
        self._count: int = 0           # number of counted (valid) fills
        self._lock = threading.Lock()
        self.load()

    # ── path convention ────────────────────────────────────────────────
    @staticmethod
    def path_for(data_dir, symbol, port, client_id) -> str:
        """Canonical ledger path for one bot. Keyed by (symbol, port,
        client_id) so any number of bots (1..32) coexist without contention."""
        fname = f".gt_fills_{symbol}_{port}_{client_id}.jsonl"
        return os.path.join(str(data_dir or "."), fname)

    # ── load / replay ───────────────────────────────────────────────────
    def load(self) -> None:
        """Rebuild in-memory dedup-set + net from the on-disk journal.
        Corruption-tolerant: a malformed line is skipped, not fatal."""
        with self._lock:
            self._seen.clear()
            self._net.clear()
            self._count = 0
            if not os.path.exists(self.path):
                return
            try:
                with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        self._apply(rec, persist=False)
            except Exception:
                # Unreadable file → behave as empty rather than crash the bot.
                pass

    # ── core apply (assumes lock held) ──────────────────────────────────
    def _apply(self, rec: dict, persist: bool) -> bool:
        eid = rec.get("exec_id")
        if not eid or eid in self._seen:
            return False
        # Mark seen FIRST so even a junk record is never reprocessed.
        self._seen.add(eid)
        sign = _norm_side(rec.get("side"))
        try:
            shares = int(rec.get("shares", 0) or 0)
        except (TypeError, ValueError):
            shares = 0
        sym = rec.get("symbol")
        counted = bool(sym) and sign != 0 and shares > 0
        if counted:
            self._net[sym] = self._net.get(sym, 0) + sign * shares
            self._count += 1
        if persist:
            self._append_line(rec)
        return counted

    def _append_line(self, rec: dict) -> None:
        line = json.dumps(rec, separators=(",", ":"), default=str)
        # Append + flush + fsync: durable against a hard kill.
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass

    # ── public write API ────────────────────────────────────────────────
    def record(self, *, exec_id, symbol, side, shares,
               price=None, time=None, order_id=None, source="live") -> bool:
        """Record one fill. Returns True iff newly counted (False on dup or
        non-counting record). Idempotent on exec_id."""
        if not exec_id:
            return False
        with self._lock:
            if exec_id in self._seen:
                return False
            rec = {
                "exec_id": exec_id,
                "symbol": symbol,
                "side": side,
                "shares": shares,
                "price": price,
                "time": time,
                "order_id": order_id,
                "source": source,
            }
            return self._apply(rec, persist=True)

    def merge_broker_fills(self, fills: Iterable,
                           symbol_of: Optional[Callable] = None,
                           our_client_id: Optional[int] = None,
                           since=None) -> int:
        """Absorb a list of ib_async ``Fill`` objects (e.g. from
        ``ib.fills()`` after reqExecutions). Dedups against everything
        already recorded, so calling this repeatedly is safe and never
        double-counts. Returns the number of NEW fills counted.

        ``symbol_of(contract) -> str`` lets the caller inject the logical
        symbol translator (keeps this module free of broker imports). If
        omitted, falls back to localSymbol/symbol on the contract.

        ``our_client_id`` (if given) filters to only our own executions.

        ``since`` (datetime, optional) — skip any execution at or before this
        timestamp. The caller passes its reconcile floor here so a fresh,
        flattened restart does NOT replay STALE pre-restart executions (which
        may have been closed by a different clientId, e.g. an external flatten
        script) — that would leave the ledger holding a phantom position. tz
        differences are normalised before comparing.
        """
        n = 0
        for f in fills or []:
            try:
                ex = getattr(f, "execution", None)
                con = getattr(f, "contract", None)
                if ex is None or con is None:
                    continue
                eid = getattr(ex, "execId", None)
                if not eid:
                    continue
                if our_client_id is not None:
                    try:
                        if int(getattr(ex, "clientId", -1)) != int(our_client_id):
                            continue
                    except (TypeError, ValueError):
                        continue
                if since is not None:
                    ft = getattr(ex, "time", None)
                    if ft is not None:
                        try:
                            # Normalise tz so naive/aware compare cleanly.
                            cmp_since = since
                            if getattr(ft, "tzinfo", None) is not None and \
                               getattr(since, "tzinfo", None) is None:
                                from datetime import timezone as _tz
                                cmp_since = since.replace(tzinfo=_tz.utc)
                            elif getattr(ft, "tzinfo", None) is None and \
                                    getattr(since, "tzinfo", None) is not None:
                                ft = ft.replace(tzinfo=getattr(since, "tzinfo"))
                            if ft <= cmp_since:
                                continue
                        except Exception:
                            # Comparison failed → don't drop the fill.
                            pass
                if symbol_of is not None:
                    sym = symbol_of(con)
                else:
                    sym = getattr(con, "localSymbol", None) or getattr(con, "symbol", None)
                if self.record(
                    exec_id=eid,
                    symbol=sym,
                    side=getattr(ex, "side", ""),
                    shares=getattr(ex, "shares", 0),
                    price=getattr(ex, "price", None),
                    time=str(getattr(ex, "time", "")),
                    order_id=getattr(ex, "orderId", None),
                    source="replay",
                ):
                    n += 1
            except Exception:
                # One malformed fill must never abort the whole merge.
                continue
        return n

    # ── public read API ─────────────────────────────────────────────────
    def net(self, symbol: Optional[str] = None):
        """Signed net position. If ``symbol`` is None, returns a dict of
        all symbols → net (a single-bot ledger normally has exactly one)."""
        with self._lock:
            if symbol is None:
                return dict(self._net)
            return self._net.get(symbol, 0)

    def count(self) -> int:
        """Number of counted fills (excludes dups / non-counting records)."""
        with self._lock:
            return self._count

    def __repr__(self) -> str:
        return (f"FillLedger(path={self.path!r}, fills={self._count}, "
                f"net={self._net})")

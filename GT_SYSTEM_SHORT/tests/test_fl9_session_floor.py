"""FL9 — session-start floor on get_our_position_via_executions().

Proves the same-day-restart phantom fix: when a `since` floor is passed
(a bot that booted FLAT), executions from BEFORE the floor are NOT summed,
so stale pre-restart fills left on a REUSED clientId cannot become a phantom
position. With `since=None` (restored-with-position path) behaviour is the
full-history sum, unchanged.

Run: python3 -m pytest tests/test_fl9_session_floor.py -q
 or: python3 tests/test_fl9_session_floor.py
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.execution.broker import Gateway


def _exec(side, shares, t, cid=80):
    return SimpleNamespace(side=side, shares=shares, time=t, clientId=cid)


def _stk(symbol="AAPL"):
    # Minimal ib_async-Contract shape; STK passes through the logical-symbol
    # translator to `symbol`.
    return SimpleNamespace(secType="STK", symbol=symbol, currency="USD",
                           exchange="SMART", localSymbol=symbol)


def _fill(side, shares, t, cid=80, symbol="AAPL"):
    return SimpleNamespace(execution=_exec(side, shares, t, cid),
                           contract=_stk(symbol))


class _FakeIB:
    def __init__(self, fills):
        self._fills = fills

    def isConnected(self):
        return True

    def fills(self):
        return self._fills


def _fake_gateway(fills, client_id=80):
    """Duck-typed stand-in exposing only what the method reads."""
    return SimpleNamespace(
        paper=False,
        _fill_ledger=None,          # force the ib.fills() live-sum path
        connected=True,
        _ib=_FakeIB(fills),
        client_id=client_id,
    )


# ib_async stamps execution.time as tz-aware UTC; the engine floor
# (_engine_started_at) is a NAIVE datetime.now(). Model both.
NOW_UTC = datetime(2026, 6, 16, 14, 0, 0, tzinfo=timezone.utc)
SESSION_START_NAIVE = datetime(2026, 6, 16, 13, 30, 0)  # engine_started_at (naive)

call = Gateway.get_our_position_via_executions.__get__  # bind helper


def _net(gw, symbol, since):
    return Gateway.get_our_position_via_executions(gw, symbol, since=since)


def test_stale_pre_floor_fill_excluded():
    # A stale BUY 25k an hour BEFORE session start (left on a reused cid),
    # and NO post-start activity → floored sum must be 0 (no phantom).
    stale = _fill("BOT", 25000, NOW_UTC - timedelta(hours=1))
    gw = _fake_gateway([stale])
    assert _net(gw, "AAPL", SESSION_START_NAIVE) == 0
    # Without the floor (restored-with-position path) the stale fill counts.
    assert _net(gw, "AAPL", None) == 25000


def test_post_floor_fill_counted():
    # A genuine post-start entry must still be summed under the floor.
    post = _fill("BOT", 25000, NOW_UTC)
    gw = _fake_gateway([post])
    assert _net(gw, "AAPL", SESSION_START_NAIVE) == 25000


def test_mixed_only_post_floor_survives():
    fills = [
        _fill("BOT", 25000, NOW_UTC - timedelta(hours=2)),   # stale entry
        _fill("SLD", 25000, NOW_UTC - timedelta(hours=1)),   # stale exit
        _fill("BOT", 10000, NOW_UTC + timedelta(minutes=5)), # this-session
    ]
    gw = _fake_gateway(fills)
    # Floored: only the +10k post-start fill.
    assert _net(gw, "AAPL", SESSION_START_NAIVE) == 10000
    # Unfloored: 25k - 25k + 10k == 10k (here the stale pair nets out, but
    # the point is the floor changes which fills are visible, not luck).
    assert _net(gw, "AAPL", None) == 10000


def test_stale_open_long_phantom_killed():
    # The actual phantom: a stale OPEN long on a reused cid (entry only, no
    # exit) with state wiped. Unfloored => +50k phantom; floored => 0.
    stale_open = _fill("BOT", 50000, NOW_UTC - timedelta(hours=3))
    gw = _fake_gateway([stale_open])
    assert _net(gw, "AAPL", None) == 50000          # the bug
    assert _net(gw, "AAPL", SESSION_START_NAIVE) == 0  # the fix


def test_other_clientid_still_ignored_with_floor():
    # Floor must not weaken the clientId filter: another bot's post-start
    # fill is still excluded.
    other = _fill("BOT", 99000, NOW_UTC, cid=81)
    gw = _fake_gateway([other], client_id=80)
    assert _net(gw, "AAPL", SESSION_START_NAIVE) == 0


def test_fill_exactly_at_floor_is_kept():
    # Boundary: a fill stamped exactly at the floor is NOT dropped (strict <).
    at_floor = _fill("BOT", 7000, SESSION_START_NAIVE.replace(tzinfo=timezone.utc))
    gw = _fake_gateway([at_floor])
    assert _net(gw, "AAPL", SESSION_START_NAIVE) == 7000


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\nALL {len(fns)} FL9 TESTS PASSED")

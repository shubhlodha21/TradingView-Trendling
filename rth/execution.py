"""Handing a signal to the execution system.

When a trendline signal fires, this launches the GT trading bot for that
instrument, in the folder that matches the direction:

    UP   / BUY   ->  GT_SYSTEM_LONG
    DOWN / SELL  ->  GT_SYSTEM_SHORT

The command is a template, so the exact flags stay yours::

    GT_PAPER=false python3 run_live.py {ticker} --trigger {trigger}
        --port {port} --client-id {client_id} --offset-entry-pct {offset_entry_pct}
        --stop {stop} --uvloop --qty {qty}

``{trigger}`` is filled with the crossing price and ``{client_id}`` with a fresh
number on every launch -- the counter is persisted, so a restart of the signal
runner never re-issues an id that an already-running bot is holding. TWS accepts
a duplicate client id by silently dropping the older connection, which would
disconnect a bot that is holding a live position.

Three modes, and the default does nothing:

``print``    build the command and show it. Nothing is executed. This is the
             default precisely because the armed modes place real orders.
``tmux``     open a tmux window per trade, named ``TICKER-LONG`` / ``-SHORT``.
``process``  spawn detached, with output to a log file. For a box without tmux.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# Two command shapes. Placeholders are filled per signal.
#
# MARKET is the default: by the time this runner fires, the crossing has already
# happened, so the bot has nothing left to wait for and enters at market.
#
# `--offset-entry-pct` IS passed even though the market entry ignores it, and
# that is deliberate. --market is one-shot: the bot's LATER entries — the
# re-entry after a stop-out, the session-open re-arm — fall back to a resting
# STP-LMT, and those read this value. Omit it and the re-entry silently uses
# GT's built-in 5bps default instead of the offset the operator chose, which is
# a different order at a different price with no sign that anything changed.
MARKET_TEMPLATE = (
    "GT_PAPER=false python3 run_live.py {ticker} "
    "--trigger {trigger} "
    "--market "
    "--port {port} "
    "--client-id {client_id} "
    "--offset-entry-pct {offset_entry_pct} "
    "--stop {stop} "
    "--uvloop "
    "--qty {qty}"
)

# STOP-LIMIT rests a STP-LMT at the trigger and lets IBKR fire it. Because the
# crossing has already happened, that order triggers immediately — the entry
# offset is then the ceiling (BUY) or floor (SELL) you are willing to accept,
# so this trades certainty of fill for control of price.
STOP_LIMIT_TEMPLATE = (
    "GT_PAPER=false python3 run_live.py {ticker} "
    "--trigger {trigger} "
    "--port {port} "
    "--client-id {client_id} "
    "--offset-entry-pct {offset_entry_pct} "
    "--stop {stop} "
    "--uvloop "
    "--qty {qty}"
)

DEFAULT_TEMPLATE = MARKET_TEMPLATE

# GT_PAPER IS ALWAYS false. This is not an oversight -- it is the fix for a real
# incident.
#
# GT_PAPER=true / --paper does NOT mean "trade the IB paper account". It
# switches GT to its own internal simulator: `_paper_order` fabricates fills
# through `_execute_fill` and contains zero placeOrder calls, so NOTHING
# reaches IBKR. The dashboard still shows FILLED, because GT invented the fill.
# A GBPUSD run "filled" at 1.4012 while the market sat at 1.35 -- no venue
# fills 500 pips away, and the gap-down protective cascade that followed was
# the simulator reacting to its own fiction.
#
# Paper versus live is therefore decided by the PORT alone, which is the only
# thing that actually changes where the order goes:
#
#     7497 / 4002   IB PAPER account -- real orders, real venue, fake money
#     7496 / 4001   LIVE account     -- real money
#
# Both run GT_PAPER=false, because both place real orders.


def pick_template(entry: str = "market") -> str:
    """The template for an entry style. Both place real orders."""
    if entry not in ("market", "stop-limit"):
        raise ValueError("entry must be 'market' or 'stop-limit'")
    return MARKET_TEMPLATE if entry == "market" else STOP_LIMIT_TEMPLATE


# TWS/Gateway ports, by account. This is the only switch between fake and real
# money, so the paper flag moves it and nothing else.
LIVE_PORT, PAPER_PORT = 7496, 7497

LONG_DIR_NAME = "GT_SYSTEM_LONG"
SHORT_DIR_NAME = "GT_SYSTEM_SHORT"
ENTRY_SCRIPT = "run_live.py"

PRINT, TMUX, PROCESS = "print", "tmux", "process"
MODES = (PRINT, TMUX, PROCESS)


@dataclass
class Launch:
    """One attempt to hand a signal to the execution system."""

    ticker: str
    direction: str
    side: str
    trigger: float
    client_id: int
    cwd: str
    command: str
    window: str
    mode: str
    time: pd.Timestamp
    status: str = "built"          # built | launched | skipped | failed
    detail: str = ""

    def as_row(self) -> dict:
        return {
            "time": f"{self.time:%Y-%m-%d %H:%M:%S}Z",
            "ticker": self.ticker,
            "direction": self.direction,
            "side": self.side,
            "trigger": self.trigger,
            "client_id": self.client_id,
            "cwd": self.cwd,
            "window": self.window,
            "mode": self.mode,
            "status": self.status,
            "detail": self.detail,
            "command": self.command,
        }


class ClientIdAllocator:
    """Hands out a fresh IB client id per launch, surviving restarts.

    Ids must never repeat: TWS resolves a duplicate client id by dropping the
    *older* connection, so re-issuing one silently disconnects a bot that is
    holding a position -- no error, the feed just goes quiet.

    Allocation is therefore a claim, not a counter. Each id is reserved by
    creating a marker file with O_CREAT|O_EXCL, which is atomic across
    processes on every OS; whoever loses the race gets FileExistsError and
    moves to the next number. A plain read-increment-write counter is NOT
    enough -- several bots launched in the same cycle, or one runner per ticker
    in separate tmux panes, all read the same value and all write the same
    successor, and every one of them ends up with the same id.

    The counter file is still maintained alongside, as a fast-forward hint and
    so ``peek`` can report the next id before anything is claimed.
    """

    def __init__(self, path: str | os.PathLike, base: int = 100,
                 reserved_dir: str | os.PathLike | None = None):
        self.path = Path(path)
        # One tiny marker file per claimed id. Pools that must not collide with
        # each other -- the runner's own feed connection and the bots it
        # launches -- share ONE reserved_dir while keeping separate counters,
        # so each keeps its own number range but neither can ever be handed an
        # id the other is holding.
        self.reserved_dir = (
            Path(reserved_dir) if reserved_dir
            else self.path.parent / f"{self.path.name}.d"
        )
        self.base = int(base)
        self._lock = threading.Lock()

    # -- inspection ---------------------------------------------------------

    def _counter_hint(self) -> int:
        try:
            return int(self.path.read_text().strip())
        except (OSError, ValueError):
            return self.base - 1

    def _highest_claim(self) -> int:
        try:
            claimed = [int(p.name) for p in self.reserved_dir.iterdir()
                       if p.name.isdigit()]
        except OSError:
            return self.base - 1
        return max(claimed) if claimed else self.base - 1

    def peek(self) -> int:
        """The highest id this pool has handed out (base - 1 if none).

        Deliberately does NOT consult the shared reserved dir: a pool based at
        17 must keep reporting 17-ish numbers even when another pool has
        claimed 100+ in the same directory. Skipping past ids someone else
        holds is `next`'s job, via O_EXCL.
        """
        return max(self.base - 1, self._counter_hint())

    # -- allocation ---------------------------------------------------------

    def next(self) -> int:
        """Claim and return an id no other process can also be holding."""
        with self._lock:                       # cheap in-process fast path
            candidate = self.peek() + 1
            try:
                self.reserved_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                # A read-only log dir must not block a trade. Fall back to the
                # counter alone and accept the (now unguarded) race.
                self._write_hint(candidate)
                return candidate

            # Bounded so a corrupted directory cannot spin forever.
            for _ in range(10_000):
                marker = self.reserved_dir / str(candidate)
                try:
                    handle = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    candidate += 1
                    continue
                except OSError:
                    self._write_hint(candidate)
                    return candidate
                os.close(handle)
                self._write_hint(candidate)
                return candidate

            raise RuntimeError(
                f"could not claim a client id in {self.reserved_dir} after "
                f"10,000 attempts starting at {self.peek() + 1}"
            )

    def release(self, value: int) -> None:
        """Return an id to the pool.

        Only safe for a connection this process is definitely finished with --
        its own feed socket on clean shutdown. Launched bots are NEVER released:
        they outlive the runner, and handing their id to a new connection would
        make TWS drop a bot that is holding a position.
        """
        value = int(value)
        try:
            (self.reserved_dir / str(value)).unlink()
        except OSError:
            pass
        # Rewind the hint if we were its most recent claimant, so stopping and
        # restarting one runner settles back on the same id instead of climbing
        # 17, 18, 19... forever across restarts.
        if self._counter_hint() == value:
            self._write_hint(value - 1)

    def _write_hint(self, value: int) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(str(value), encoding="utf-8")
        except OSError:
            pass                # bookkeeping must never stop a trade


class TradeLauncher:
    """Turns a signal into a running execution bot."""

    def __init__(
        self,
        gt_root: str | os.PathLike | None = None,
        long_dir: str | os.PathLike | None = None,
        short_dir: str | os.PathLike | None = None,
        template: str = DEFAULT_TEMPLATE,
        mode: str = PRINT,
        allocator: ClientIdAllocator | None = None,
        session: str = "gt",
        defaults: dict | None = None,
        log_path: str | os.PathLike | None = None,
        once_per_ticker: bool = True,
        price_decimals: int = 2,
    ):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")

        # Resolved to absolute: `--gt-root .` is the natural thing to type, but
        # tmux resolves `new-window -c` against the *server's* working
        # directory, not ours, so a relative path would launch the bot in the
        # wrong place (or fail) once execution is armed.
        root = Path(gt_root).expanduser().resolve() if gt_root else Path.cwd()
        self.long_dir = (
            Path(long_dir).expanduser().resolve() if long_dir else root / LONG_DIR_NAME
        )
        self.short_dir = (
            Path(short_dir).expanduser().resolve() if short_dir else root / SHORT_DIR_NAME
        )
        self.template = template
        self.mode = mode
        self.session = session
        self.allocator = allocator or ClientIdAllocator("logs/.gt_client_id")
        self.defaults = dict(defaults or {})
        self.log_path = Path(log_path) if log_path else None
        self.once_per_ticker = once_per_ticker
        self.price_decimals = int(price_decimals)

        self.launches: list[Launch] = []
        self._fired: set[str] = set()
        self._log_started = False

    # -- routing ------------------------------------------------------------

    def directory_for(self, direction: str) -> Path:
        return self.long_dir if direction.upper() == "UP" else self.short_dir

    def check_directories(self) -> list[str]:
        """Problems worth surfacing at startup rather than at the first signal."""
        problems = []
        for label, path in (("UP/long", self.long_dir), ("DOWN/short", self.short_dir)):
            if not path.is_dir():
                problems.append(f"{label} folder does not exist: {path}")
            elif not (path / ENTRY_SCRIPT).is_file():
                problems.append(f"{label} folder has no {ENTRY_SCRIPT}: {path}")
        return problems

    # -- building -----------------------------------------------------------

    def build(self, signal: dict, client_id: int | None = None) -> Launch:
        """Render the command for one signal. Executes nothing."""
        direction = str(signal.get("direction", "UP")).upper()
        ticker = str(signal["ticker"]).upper()
        side = signal.get("side") or ("BUY" if direction == "UP" else "SELL")
        # A trigger is a real order price, so it is rounded to the instrument's
        # quoting precision -- cents for equities, pips for FX.
        decimals = int(signal.get("price_decimals") or self.price_decimals)
        trigger = round(float(signal["trigger"]), decimals)
        cid = self.allocator.next() if client_id is None else int(client_id)

        fields = {
            **self.defaults,
            **{k: v for k, v in signal.items() if v is not None},
            "ticker": ticker,
            "symbol": ticker,
            "direction": direction,
            "side": side,
            "trigger": trigger,
            "client_id": cid,
        }
        try:
            command = self.template.format(**fields)
        except KeyError as exc:
            raise KeyError(
                f"the launch template references {exc} but nothing supplies it; "
                f"available: {', '.join(sorted(fields))}"
            ) from None

        return Launch(
            ticker=ticker,
            direction=direction,
            side=side,
            trigger=trigger,
            client_id=cid,
            cwd=str(self.directory_for(direction)),
            command=command,
            window=f"{ticker}-{'LONG' if direction == 'UP' else 'SHORT'}",
            mode=self.mode,
            time=pd.Timestamp.now(tz="UTC"),
        )

    # -- firing -------------------------------------------------------------

    def fire(self, signal: dict) -> Launch:
        """Build and, unless the mode is ``print``, run the command."""
        ticker = str(signal["ticker"]).upper()
        if self.once_per_ticker and ticker in self._fired:
            launch = self.build(signal, client_id=-1)
            launch.status = "skipped"
            launch.detail = "already launched once for this ticker this session"
            self._record(launch)
            return launch

        launch = self.build(signal)

        directory = Path(launch.cwd)
        if not (directory / ENTRY_SCRIPT).is_file():
            launch.status = "failed"
            launch.detail = f"no {ENTRY_SCRIPT} in {directory}"
            self._record(launch)
            return launch

        if self.mode == PRINT:
            launch.status = "built"
            launch.detail = "print mode -- nothing executed"
        else:
            try:
                launch.detail = (
                    self._launch_tmux(launch) if self.mode == TMUX
                    else self._launch_process(launch)
                )
                launch.status = "launched"
                self._fired.add(ticker)
            except Exception as exc:            # noqa: BLE001 - reported, not raised
                launch.status = "failed"
                launch.detail = str(exc)

        self._record(launch)
        return launch

    def tmux_argv(self, launch: Launch, inside: bool | None = None,
                  session_exists: bool | None = None) -> tuple[list[str], str]:
        """The tmux invocation for a launch, and a description of where it lands.

        Split out from the call so the command can be asserted without opening
        a window.
        """
        # Hold the window open after the bot exits so its last words survive.
        held = (
            f"{launch.command}; echo; "
            f"echo '[{launch.window} exited -- press enter or Ctrl-D to close]'; "
            f"exec bash"
        )
        if inside is None:
            inside = bool(os.environ.get("TMUX"))

        if inside:
            return (["tmux", "new-window", "-d", "-n", launch.window,
                     "-c", launch.cwd, held], "current session")
        if session_exists is None:
            session_exists = self._session_exists()
        if session_exists:
            return (["tmux", "new-window", "-d", "-t", self.session, "-n", launch.window,
                     "-c", launch.cwd, held], f"session {self.session}")
        return (["tmux", "new-session", "-d", "-s", self.session, "-n", launch.window,
                 "-c", launch.cwd, held], f"new session {self.session}")

    def _launch_tmux(self, launch: Launch) -> str:
        argv, target = self.tmux_argv(launch)
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"tmux refused the window: {result.stderr.strip() or result.returncode}"
            )
        return f"tmux window {launch.window} in {target}"

    def _session_exists(self) -> bool:
        try:
            return subprocess.run(
                ["tmux", "has-session", "-t", self.session],
                capture_output=True,
            ).returncode == 0
        except FileNotFoundError:
            raise RuntimeError("tmux is not installed (apt install tmux)") from None

    def _launch_process(self, launch: Launch) -> str:
        log_dir = (self.log_path.parent if self.log_path else Path("logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = launch.time.strftime("%Y%m%d-%H%M%S")
        out_path = log_dir / f"{launch.window}-{stamp}.log"

        handle = open(out_path, "a", encoding="utf-8")
        handle.write(f"# {launch.time:%Y-%m-%d %H:%M:%S}Z  cwd={launch.cwd}\n")
        handle.write(f"# {launch.command}\n\n")
        handle.flush()

        # start_new_session detaches it from our process group, so Ctrl-C on the
        # signal runner does not also kill a bot that is managing a position.
        process = subprocess.Popen(
            launch.command, shell=True, cwd=launch.cwd,
            stdout=handle, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return f"pid {process.pid}, logging to {out_path}"

    # -- record --------------------------------------------------------------

    def _record(self, launch: Launch) -> None:
        self.launches.append(launch)
        if self.log_path is None:
            return
        import csv

        row = launch.as_row()
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            fresh = not self.log_path.exists() or self.log_path.stat().st_size == 0
            with open(self.log_path, "a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                if fresh:
                    writer.writeheader()
                writer.writerow(row)
        except OSError:
            pass                # never let bookkeeping stop a trade

    # -- description ---------------------------------------------------------

    def describe(self) -> list[str]:
        """Lines for the startup banner."""
        sample = self.template.format(
            **{**self.defaults, "ticker": "TICKER", "symbol": "TICKER",
               "trigger": "<crossing price>", "client_id": self.allocator.peek() + 1,
               "direction": "UP", "side": "BUY"}
        )
        return [
            f"mode        {self.mode}"
            + ("  (nothing will be executed)" if self.mode == PRINT else ""),
            f"account     {self.account_label}",
            f"UP   ->     {self.long_dir}",
            f"DOWN ->     {self.short_dir}",
            f"client ids  from {self.allocator.peek() + 1}, one per launch",
            f"command     {sample}",
        ]

    @property
    def port(self) -> int | None:
        try:
            return int(self.defaults.get("port"))
        except (TypeError, ValueError):
            return None

    @property
    def is_live_money(self) -> bool:
        """Whether these launches can lose real money.

        Decided by the PORT, not by GT_PAPER. GT_PAPER=false is now constant --
        it means "place real orders", which is true of the paper account too.
        The port is the only thing that selects which account they land in.
        """
        return self.port in (LIVE_PORT, 4001)

    @property
    def account_label(self) -> str:
        if self.port in (PAPER_PORT, 4002):
            return f"IB PAPER account (port {self.port}) -- real orders, fake money"
        if self.is_live_money:
            return f"LIVE MONEY (port {self.port})"
        return f"port {self.port} -- unrecognised, check which account this is"

    def parsed_command(self, launch: Launch) -> list[str]:
        """The command split into argv, for display or assertion in tests."""
        return shlex.split(launch.command)
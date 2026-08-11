"""Checks for the signal -> GT_SYSTEM_LONG / GT_SYSTEM_SHORT hand-off.

Nothing here places an order or spawns a process: the launcher is exercised in
``print`` mode, which builds the exact command and executes nothing. The tmux
path is checked by pointing the launcher at a fake ``tmux`` on PATH.

Run with:  python -m tests.test_execution
"""

from __future__ import annotations

import os
import pathlib
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pandas as pd

from rth.execution import (
    DEFAULT_TEMPLATE,
    LIVE_PORT,
    MARKET_TEMPLATE,
    PAPER_PORT,
    STOP_LIMIT_TEMPLATE,
    ClientIdAllocator,
    TradeLauncher,
    pick_template,
)

PASS, FAIL = [], []


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        got={got!r} want={want!r}")


def truthy(name, got):
    (PASS if got else FAIL).append(name)
    print(f"[{'PASS' if got else 'FAIL'}] {name}  -> {got!r}")


WORK = pathlib.Path(tempfile.mkdtemp(prefix="rth-exec-"))
LONG = WORK / "GT_SYSTEM_LONG"
SHORT = WORK / "GT_SYSTEM_SHORT"
for folder in (LONG, SHORT):
    folder.mkdir(parents=True)
    (folder / "run_live.py").write_text("# stand-in for the real bot\n", encoding="utf-8")


def launcher(**overrides) -> TradeLauncher:
    settings = dict(
        gt_root=WORK,
        mode="print",
        allocator=ClientIdAllocator(WORK / "client_id", base=100),
        defaults={"port": 7496, "qty": 512, "stop": 0.0025,
                  "offset_entry_pct": 0.001},
        log_path=WORK / "launches.csv",
    )
    settings.update(overrides)
    return TradeLauncher(**settings)


def signal(ticker="AAPL", direction="UP", trigger=128.863, **extra) -> dict:
    return {"ticker": ticker, "direction": direction, "trigger": trigger,
            "side": "BUY" if direction == "UP" else "SELL", **extra}


# --------------------------------------------------------------------------- #
print("\n=== direction routes to the right folder ===")

gt = launcher()
up = gt.fire(signal("AAPL", "UP"))
down = gt.fire(signal("MSFT", "DOWN"))

check("UP runs in GT_SYSTEM_LONG", up.cwd, str(LONG))
check("DOWN runs in GT_SYSTEM_SHORT", down.cwd, str(SHORT))
check("UP is a BUY", up.side, "BUY")
check("DOWN is a SELL", down.side, "SELL")
check("window names say which side", (up.window, down.window),
      ("AAPL-LONG", "MSFT-SHORT"))
truthy("print mode executes nothing", up.status == "built"
       and "nothing executed" in up.detail)


# --------------------------------------------------------------------------- #
print("\n=== the command matches the production shape ===")

argv = gt.parsed_command(up)
check("environment prefix is kept", argv[0], "GT_PAPER=false")
check("interpreter and script", argv[1:3], ["python3", "run_live.py"])
check("symbol is positional and first", argv[3], "AAPL")


def flag(command_argv, name):
    return command_argv[command_argv.index(name) + 1]


check("--trigger carries the crossing price", flag(argv, "--trigger"), "128.86")
check("--port", flag(argv, "--port"), "7496")
check("--stop", flag(argv, "--stop"), "0.0025")
check("--qty", flag(argv, "--qty"), "512")
truthy("--uvloop is present", "--uvloop" in argv)

# Market entry is the default: the crossing already happened upstream, so the
# bot has nothing left to wait for.
truthy("--market is passed by default", "--market" in argv)

# The entry offset rides along even though the market entry ignores it. --market
# is one-shot inside GT: the re-entry after a stop-out falls back to a resting
# STP-LMT and reads this value. Drop it and the re-entry quietly uses GT's 5bps
# default instead of the operator's.
check("the entry offset is carried for the later re-entry",
      flag(argv, "--offset-entry-pct"), "0.001")
check("qty is carried the same way", flag(argv, "--qty"), "512")
check("the whole command reads as expected", up.command,
      "GT_PAPER=false python3 run_live.py AAPL --trigger 128.86 --market "
      "--port 7496 --client-id 100 --offset-entry-pct 0.001 --stop 0.0025 "
      "--uvloop --qty 512")

stop_limit = launcher(template=STOP_LIMIT_TEMPLATE).fire(signal("AAPL", "UP"))
sl_argv = gt.parsed_command(stop_limit)
truthy("the stop-limit template rests an order instead",
       "--market" not in sl_argv)
check("...and carries the entry offset", flag(sl_argv, "--offset-entry-pct"), "0.001")

# The trigger is an order price, so it is rounded to the instrument's precision.
fx = gt.fire(signal("EURUSD", "UP", trigger=1.0857321, price_decimals=5))
check("FX keeps five decimals", flag(gt.parsed_command(fx), "--trigger"), "1.08573")


# --------------------------------------------------------------------------- #
print("\n=== client ids advance and persist ===")

check("first launch took the base", up.client_id, 100)
check("second launch took the next", down.client_id, 101)
truthy("every launch advances the id", fx.client_id > down.client_id)

# A restart must not hand out an id that a running bot is already holding, so
# the sequence continues from disk rather than from the base.
before_restart = int((WORK / "client_id").read_text().strip())
reborn = launcher()
after_restart = reborn.fire(signal("TSLA", "UP"))
check("a fresh process continues the sequence",
      after_restart.client_id, before_restart + 1)
check("the counter is on disk", (WORK / "client_id").read_text().strip(),
      str(after_restart.client_id))

# The failure this guards against: TWS resolves a duplicate client id by
# dropping the OLDER connection, so two bots on one id means one silently
# loses its feed while holding a position. A read-increment-write counter is
# not enough -- three tickers signalling in the same cycle, or one runner per
# tmux pane, all read the same value and all write the same successor.
import threading as _threading  # noqa: E402

concurrent_dir = WORK / "concurrent"
grabbed: list[int] = []
grab_lock = _threading.Lock()


def _grab():
    value = ClientIdAllocator(concurrent_dir / "cid", base=100).next()
    with grab_lock:
        grabbed.append(value)


workers = [_threading.Thread(target=_grab) for _ in range(20)]
for w in workers:
    w.start()
for w in workers:
    w.join()

check("twenty simultaneous allocations are all distinct",
      len(set(grabbed)), len(grabbed))
check("...and are contiguous from the base", sorted(grabbed),
      list(range(100, 120)))
truthy("each claim leaves a marker so it can never be reissued",
       len(list((concurrent_dir / "cid.d").iterdir())) == 20)

# The runner's own feed connection and the bots it launches draw from separate
# ranges but ONE claim space. Before this, two runners in two terminals both
# defaulted to client id 17: TWS accepts the second and silently drops the
# first, so one runner's quotes simply stop arriving.
pool = WORK / "shared" / ".client_ids.d"
feed_pool = lambda: ClientIdAllocator(  # noqa: E731 - terse on purpose
    WORK / "shared" / ".feed_client_id", base=17, reserved_dir=pool)
bot_pool = lambda: ClientIdAllocator(  # noqa: E731
    WORK / "shared" / ".gt_client_id", base=100, reserved_dir=pool)

feed_ids = [feed_pool().next() for _ in range(3)]
bot_ids = [bot_pool().next() for _ in range(3)]
check("separate runners get separate feed ids", feed_ids, [17, 18, 19])
check("bots keep their own range", bot_ids, [100, 101, 102])
check("the two ranges never overlap", set(feed_ids) & set(bot_ids), set())

# Push the feed range up through the bot base and confirm it steps over
# ids that are already held rather than reissuing one.
climbed = [feed_pool().next() for _ in range(95)]
truthy("the feed range can climb past the bot base", max(climbed) > 100)
next_bot = bot_pool().next()
truthy("...and the next bot skips every id already claimed",
       next_bot not in climbed and next_bot not in feed_ids)

released = feed_pool()
rid = released.next()
released.release(rid)
truthy("a released feed id returns to the pool",
       not (pool / str(rid)).exists())
check("...and is handed out again", feed_pool().next(), rid)

fresh_dir = WORK / "fresh"
first_ever = launcher(
    allocator=ClientIdAllocator(fresh_dir / "counter", base=250)
).fire(signal("NVDA", "UP"))
check("a new counter starts at its base", first_ever.client_id, 250)


# --------------------------------------------------------------------------- #
print("\n=== guards ===")

repeated = launcher()
repeated.fire(signal("AAPL", "UP"))
again = repeated.fire(signal("AAPL", "UP"))
# In print mode nothing was launched, so the dedupe does not engage; it guards
# the armed modes, where a second launch would double the position.
truthy("print mode does not pretend to dedupe", again.status == "built")

armed_dedupe = launcher(mode="tmux")
armed_dedupe._fired.add("AAPL")
blocked = armed_dedupe.fire(signal("AAPL", "UP"))
check("an armed relaunch of the same ticker is skipped", blocked.status, "skipped")
truthy("...and says why", "already launched" in blocked.detail)

missing = launcher(gt_root=WORK / "nowhere")
problems = missing.check_directories()
check("both missing folders are reported", len(problems), 2)
truthy("the message names the folder", "GT_SYSTEM_LONG" in problems[0])

hollow = WORK / "hollow"
(hollow / "GT_SYSTEM_LONG").mkdir(parents=True)
(hollow / "GT_SYSTEM_SHORT").mkdir(parents=True)
empty = launcher(gt_root=hollow)
truthy("a folder without run_live.py is caught",
       "no run_live.py" in empty.check_directories()[0])
check("...and firing into it fails rather than half-running",
      empty.fire(signal("AAPL", "UP")).status, "failed")

check("the default template is the market one", DEFAULT_TEMPLATE, MARKET_TEMPLATE)
truthy("GT_PAPER=false is recognised as live money", gt.is_live_money)


# --------------------------------------------------------------------------- #
print("\n=== paper vs live is the PORT, never GT_PAPER ===")

# GT_PAPER=true switches GT to its OWN simulator: _paper_order fabricates fills
# through _execute_fill and contains zero placeOrder calls, so nothing reaches
# IBKR while the dashboard still shows FILLED. Observed live -- a GBPUSD run
# "filled" at 1.4012 with the market at 1.35. It is therefore never emitted.
# Both accounts place real orders; only the port picks which one.
check("market template", pick_template("market"), MARKET_TEMPLATE)
check("stop-limit template", pick_template("stop-limit"), STOP_LIMIT_TEMPLATE)
try:
    pick_template("yolo")
    truthy("an unknown entry style is rejected", False)
except ValueError:
    truthy("an unknown entry style is rejected", True)

for _name, _template in (("market", MARKET_TEMPLATE),
                         ("stop-limit", STOP_LIMIT_TEMPLATE)):
    truthy(f"the {_name} template never enables GT's simulator",
           "GT_PAPER=false" in _template and "--paper" not in _template)

sizing = {"qty": 512, "stop": 0.0025, "offset_entry_pct": 0.001}

paper = launcher(defaults={"port": PAPER_PORT, **sizing})
paper_argv = paper.parsed_command(paper.fire(signal("AAPL", "UP")))
check("a paper run still places REAL orders", paper_argv[0], "GT_PAPER=false")
truthy("...and never passes --paper", "--paper" not in paper_argv)
check("...only the port differs", flag(paper_argv, "--port"), "7497")
truthy("a paper port is not live money", not paper.is_live_money)
truthy("...and the banner names the account",
       "IB PAPER account" in paper.account_label)

live = launcher(defaults={"port": LIVE_PORT, **sizing})
truthy("a live port is live money", live.is_live_money)
truthy("...and the banner says so", "LIVE MONEY" in live.account_label)

odd = launcher(defaults={"port": 1234, **sizing})
truthy("an unrecognised port is never silently treated as live",
       not odd.is_live_money and "unrecognised" in odd.account_label)
check("the two ports never collide", (LIVE_PORT, PAPER_PORT), (7496, 7497))
# is_live_money reads the PORT, never the template text. A template mentioning
# --paper does not make a live port safe -- that was the old, wrong heuristic.
truthy("a --paper template on a live port is still live money",
       launcher(template="python3 run_live.py {ticker} --paper "
                         "--trigger {trigger} --port {port} "
                         "--client-id {client_id}").is_live_money)

try:
    launcher(template="python3 run_live.py {ticker} --lot {lot_size}").fire(signal())
    truthy("an unknown placeholder is rejected", False)
except KeyError as exc:
    truthy("an unknown placeholder is rejected", "lot_size" in str(exc))


# --------------------------------------------------------------------------- #
print("\n=== per-instrument overrides ===")

sized = gt.fire(signal("MSFT", "DOWN", qty=128, stop=0.005, offset_entry_pct=0.002))
sized_argv = gt.parsed_command(sized)
check("config qty wins over the default", flag(sized_argv, "--qty"), "128")
check("config stop wins", flag(sized_argv, "--stop"), "0.005")
check("config offset wins", flag(sized_argv, "--offset-entry-pct"), "0.002")

sized_sl = launcher(template=STOP_LIMIT_TEMPLATE).fire(
    signal("MSFT", "DOWN", qty=128, stop=0.005, offset_entry_pct=0.002))
check("...on the stop-limit template too",
      flag(gt.parsed_command(sized_sl), "--offset-entry-pct"), "0.002")

custom = launcher(template="cd {ticker} && bot --side {side} --px {trigger} "
                           "--id {client_id}").fire(signal("AAPL", "UP"))
check("a custom template gets side and trigger", custom.command,
      f"cd AAPL && bot --side BUY --px 128.86 --id {custom.client_id}")


# --------------------------------------------------------------------------- #
print("\n=== tmux hand-off ===")

# Two steps, on purpose. The window is created with NO command so tmux starts
# the user's shell in it; the bot is then typed in with send-keys. Handing the
# command to new-window instead makes tmux exec it under a bare `sh -c`, which
# leaves no interactive shell behind the full-screen GT dashboard and makes
# moving between windows awkward.
tm = launcher(mode="tmux", session="gt-test")
pending = tm.build(signal("AAPL", "UP"))

argv_new, where_new = tm.tmux_argv(pending, inside=False, session_exists=False)
check("with no session, one is created", argv_new[:5],
      ["tmux", "new-session", "-d", "-s", "gt-test"])
check("the window is named for the trade", argv_new[argv_new.index("-n") + 1],
      "AAPL-LONG")
check("it starts in the LONG folder", argv_new[argv_new.index("-c") + 1], str(LONG))
truthy("the window is created with a shell, not the bot command",
       not any("run_live.py" in part for part in argv_new))
check("...so the last argument is the working directory", argv_new[-2], "-c")

sent = tm.send_keys_argv(pending, inside=False)
check("the bot is typed in afterwards", sent[:3], ["tmux", "send-keys", "-t"])
check("...into the right window", sent[3], "gt-test:AAPL-LONG")
truthy("...as the real command",
       "GT_PAPER=false python3 run_live.py AAPL --trigger 128.86" in sent[4])
check("...submitted with a newline", sent[-1], "C-m")
truthy("the window is held open after the bot exits",
       "press enter or Ctrl-D to close" in sent[4])
truthy("no exec bash -- the shell is already the window's process",
       "exec bash" not in sent[4])

argv_add, _ = tm.tmux_argv(pending, inside=False, session_exists=True)
check("an existing session gets a new window", argv_add[:5],
      ["tmux", "new-window", "-d", "-t", "gt-test"])

argv_inside, where_inside = tm.tmux_argv(pending, inside=True)
check("inside tmux the window joins the current session", argv_inside[:4],
      ["tmux", "new-window", "-d", "-n"])
check("...and says so", where_inside, "current session")
check("...rather than targeting a named one", "-t" in argv_inside, False)
check("...and send-keys targets the bare window name",
      tm.send_keys_argv(pending, inside=True)[3], "AAPL-LONG")

# End to end, with a fake tmux recording its argv. POSIX only: Windows cannot
# exec a shebang script through subprocess.

fake_bin = WORK / "bin"
fake_bin.mkdir(exist_ok=True)
recorder = WORK / "tmux-calls.txt"
fake = fake_bin / "tmux"
fake.write_text(
    "#!/usr/bin/env bash\n"
    f'printf "%s\\n" "$*" >> {recorder}\n'
    'if [ "$1" = "has-session" ]; then exit 1; fi\n'
    "exit 0\n",
    encoding="utf-8",
)
fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

if sys.platform == "win32" or shutil.which("bash") is None:
    print("[skip] the tmux stand-in needs a POSIX shell; the invocation itself "
          "was checked above")
else:
    original_path = os.environ.get("PATH", "")
    original_tmux = os.environ.pop("TMUX", None)
    os.environ["PATH"] = f"{fake_bin}{os.pathsep}{original_path}"
    try:
        via_tmux = launcher(mode="tmux", session="gt-test").fire(signal("AAPL", "UP"))
        calls = recorder.read_text(encoding="utf-8") if recorder.exists() else ""
        check("the launch reports success", via_tmux.status, "launched")
        truthy("tmux actually received the window",
               "new-session -d -s gt-test" in calls and "-n AAPL-LONG" in calls)
        truthy("the bot command reached it",
               "GT_PAPER=false python3 run_live.py AAPL --trigger 128.86" in calls)
    finally:
        os.environ["PATH"] = original_path
        if original_tmux is not None:
            os.environ["TMUX"] = original_tmux


# --------------------------------------------------------------------------- #
print("\n=== the launch log ===")

rows = pd.read_csv(WORK / "launches.csv")
truthy("every launch is recorded", len(rows) >= 6)
check("it carries the command verbatim",
      rows.loc[rows.ticker == "AAPL", "command"].iloc[0], up.command)
truthy("and the client id, folder and status",
       {"client_id", "cwd", "status", "trigger", "window"} <= set(rows.columns))


shutil.rmtree(WORK, ignore_errors=True)

print("\n" + "=" * 60)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for name in FAIL:
    print(f"  FAILED: {name}")
sys.exit(1 if FAIL else 0)
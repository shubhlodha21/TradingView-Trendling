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
    MARKET_TEMPLATE_PAPER,
    PAPER_PORT,
    STOP_LIMIT_TEMPLATE,
    STOP_LIMIT_TEMPLATE_PAPER,
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
truthy("...and the entry offset is omitted, because GT ignores it in that mode",
       "--offset-entry-pct" not in argv)
check("the whole command reads as expected", up.command,
      "GT_PAPER=false python3 run_live.py AAPL --trigger 128.86 --market "
      "--port 7496 --client-id 100 --stop 0.0025 --uvloop --qty 512")

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
print("\n=== paper vs live ===")

# The setting that decides whether real money moves gets its own tests.
check("live + market", pick_template("market", paper=False), MARKET_TEMPLATE)
check("live + stop-limit", pick_template("stop-limit", paper=False),
      STOP_LIMIT_TEMPLATE)
check("paper + market", pick_template("market", paper=True), MARKET_TEMPLATE_PAPER)
check("paper + stop-limit", pick_template("stop-limit", paper=True),
      STOP_LIMIT_TEMPLATE_PAPER)
try:
    pick_template("yolo")
    truthy("an unknown entry style is rejected", False)
except ValueError:
    truthy("an unknown entry style is rejected", True)

paper = launcher(template=MARKET_TEMPLATE_PAPER,
                 defaults={"port": PAPER_PORT, "qty": 512, "stop": 0.0025,
                           "offset_entry_pct": 0.001})
paper_launch = paper.fire(signal("AAPL", "UP"))
paper_argv = paper.parsed_command(paper_launch)
check("paper sets the env var", paper_argv[0], "GT_PAPER=true")
truthy("...and passes --paper too, since GT honours either",
       "--paper" in paper_argv)
check("...and targets the paper port", flag(paper_argv, "--port"), "7497")
truthy("a paper launcher is not live money", not paper.is_live_money)
truthy("...and says so in the banner",
       any("account     paper" in line for line in paper.describe()))
truthy("a live launcher says LIVE MONEY in the banner",
       any("account     LIVE MONEY" in line for line in gt.describe()))
check("the two ports never collide", (LIVE_PORT, PAPER_PORT), (7496, 7497))
truthy("a paper template is not",
       not launcher(template="python3 run_live.py {ticker} --paper "
                             "--trigger {trigger} --client-id {client_id}").is_live_money)

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

sized_sl = launcher(template=STOP_LIMIT_TEMPLATE).fire(
    signal("MSFT", "DOWN", qty=128, stop=0.005, offset_entry_pct=0.002))
check("config offset wins where the template uses it",
      flag(gt.parsed_command(sized_sl), "--offset-entry-pct"), "0.002")

custom = launcher(template="cd {ticker} && bot --side {side} --px {trigger} "
                           "--id {client_id}").fire(signal("AAPL", "UP"))
check("a custom template gets side and trigger", custom.command,
      f"cd AAPL && bot --side BUY --px 128.86 --id {custom.client_id}")


# --------------------------------------------------------------------------- #
print("\n=== tmux hand-off ===")

# The invocation itself is a pure function, so it can be checked anywhere.
tm = launcher(mode="tmux", session="gt-test")
pending = tm.build(signal("AAPL", "UP"))

argv_new, where_new = tm.tmux_argv(pending, inside=False, session_exists=False)
check("with no session, one is created", argv_new[:5],
      ["tmux", "new-session", "-d", "-s", "gt-test"])
check("the window is named for the trade", argv_new[argv_new.index("-n") + 1],
      "AAPL-LONG")
check("it starts in the LONG folder", argv_new[argv_new.index("-c") + 1], str(LONG))
truthy("the bot command is handed to tmux",
       "GT_PAPER=false python3 run_live.py AAPL --trigger 128.86" in argv_new[-1])
truthy("the window is held open after the bot exits", argv_new[-1].endswith("exec bash"))

argv_add, _ = tm.tmux_argv(pending, inside=False, session_exists=True)
check("an existing session gets a new window", argv_add[:5],
      ["tmux", "new-window", "-d", "-t", "gt-test"])

argv_inside, where_inside = tm.tmux_argv(pending, inside=True)
check("inside tmux the window joins the current session", argv_inside[:4],
      ["tmux", "new-window", "-d", "-n"])
check("...and says so", where_inside, "current session")
check("...rather than targeting a named one", "-t" in argv_inside, False)

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
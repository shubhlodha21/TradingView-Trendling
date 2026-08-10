"""tmux-based orchestrator — spawns the bot in tmux, schedules actions, captures logs.

DESIGN:
  - Bot runs in a NAMED tmux session so the operator can attach and watch.
  - Orchestrator schedules actions using wall-clock + asyncio.sleep.
  - All disruptions (kill TWS, manual cancel) are declarative in the scenario.
  - tmux pane output is `pipe-pane`'d to a log file for the verifier.

PREREQS:
  - tmux installed
  - IBKR TWS / Gateway running on the configured port
  - For ManualCancelInTWS / SeedPreExisting*: orchestrator opens a separate
    IBKR connection (clientId=99) to perform the action

NO source changes to src/ — bot is the same production engine.
"""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .catalog import (
    KillBot, KillTWS, ManualCancelInTWS, ManualModifyInTWS, RestartBot,
    Scenario, SeedPreExistingPosition, SeedPreExistingStop, StartBot,
    StartTWS, StopCampaign, WaitUntilFilled,
)


@dataclass(slots=True)
class OrchestrationResult:
    scenario_id: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    tmux_session_name: str = ""
    stdout_log_path: Optional[Path] = None
    # For multi-bot scenarios: per-bot session/log info
    bot_sessions: list[dict] = field(default_factory=list)
    actions_executed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


class TmuxOrchestrator:
    """Spawns + drives bot processes in named tmux sessions."""

    def __init__(self, project_root: Path, log_dir: Path):
        self.project_root = Path(project_root).resolve()
        self.log_dir = Path(log_dir).resolve()
        self.log_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ LTP
    # Venue tick grid (fallback if ContractDetails not available):
    _TICK_GRID: dict[str, float] = {
        # FX
        "EURUSD": 1e-5, "GBPUSD": 1e-5, "AUDUSD": 1e-5,
        "NZDUSD": 1e-5, "USDCAD": 1e-5, "USDCHF": 1e-5,
        "USDJPY": 1e-3, "EURJPY": 1e-3, "GBPJPY": 1e-3,
        # Equities (US stocks)
        "AAPL": 0.01, "MSFT": 0.01, "TSLA": 0.01, "NVDA": 0.01,
        "GOOGL": 0.01, "META": 0.01, "AMZN": 0.01,
        # Index CFDs
        "IBUS500": 0.25, "IBDE40": 0.5, "IBUK100": 0.5,
        # Futures
        "ES": 0.25, "NQ": 0.25, "CL": 0.01,
    }

    @classmethod
    def _round_to_tick(cls, price: float, symbol: str) -> float:
        tick = cls._TICK_GRID.get(symbol, 0.01)
        return round(round(price / tick) * tick, 8)

    async def _resolve_ltp(
        self, bot: dict, port: int,
    ) -> Optional[float]:
        """Connect briefly to IBKR (clientId=99) and fetch current LTP.
        Returns None if unavailable; orchestrator then falls back to
        the hardcoded `trigger` in bot_args."""
        try:
            from ib_async import IB, Forex, Stock, Index, Future, CFD
        except ImportError:
            return None

        sym = bot["symbol"]
        ib = IB()
        try:
            await asyncio.wait_for(
                ib.connectAsync("127.0.0.1", port, clientId=99),
                timeout=5.0,
            )
        except (asyncio.TimeoutError, Exception) as e:
            print(f"  [ltp] {sym}: connect failed ({e}); using fallback trigger")
            return None

        try:
            # Pick contract type by symbol shape
            if len(sym) == 6 and sym.isupper() and sym.isalpha():
                contract = Forex(sym)
            elif sym.startswith("IB") and len(sym) <= 8:
                # CFD: IBUS500, IBDE40, IBUK100 — usually mapped via CFD()
                # try CFD first, then Index as fallback
                contract = CFD(sym)
            elif sym in ("ES", "NQ", "CL", "GC", "SI", "ZB", "ZN"):
                contract = Future(sym, exchange="CME")  # CL is NYMEX
                if sym == "CL":
                    contract = Future(sym, exchange="NYMEX")
            else:
                contract = Stock(sym, "SMART", "USD")

            qualified = await asyncio.wait_for(
                ib.qualifyContractsAsync(contract), timeout=5.0,
            )
            if not qualified:
                print(f"  [ltp] {sym}: qualifyContracts returned empty")
                return None
            qc = qualified[0]

            ticker = ib.reqMktData(qc, "", False, False)
            # Wait up to 3s for a price tick
            for _ in range(15):
                await asyncio.sleep(0.2)
                # last → close → bid → ask
                px = (ticker.last if ticker.last and ticker.last > 0 else
                      ticker.close if ticker.close and ticker.close > 0 else
                      ticker.bid if ticker.bid and ticker.bid > 0 else
                      ticker.ask if ticker.ask and ticker.ask > 0 else
                      None)
                if px is not None:
                    ib.cancelMktData(qc)
                    return float(px)
            ib.cancelMktData(qc)
            print(f"  [ltp] {sym}: no price tick within 3s")
            return None
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    async def _resolve_triggers(self, scenario: Scenario) -> Scenario:
        """For every bot in the scenario with `ltp_offset_pct`, fetch live
        LTP and overwrite its `trigger`. Returns a new Scenario with
        resolved bot_args. Sequential (not parallel) so we don't spawn 14
        concurrent sidecar connections to TWS."""
        import dataclasses as _dc

        args = scenario.bot_args
        port = args.get("port", 7497) if "multi" not in args else 7497

        async def resolve_one(bot: dict) -> dict:
            if "ltp_offset_pct" not in bot or bot["ltp_offset_pct"] is None:
                return bot  # explicit absolute trigger, leave alone
            ltp = await self._resolve_ltp(bot, bot.get("port", port))
            if ltp is None:
                print(f"  [ltp] {bot['symbol']}: FALLBACK to hardcoded trigger={bot['trigger']}")
                return bot
            new_trigger = self._round_to_tick(
                ltp * (1 + bot["ltp_offset_pct"]), bot["symbol"],
            )
            print(f"  [ltp] {bot['symbol']}: LTP={ltp} → trigger={new_trigger} "
                  f"(+{bot['ltp_offset_pct']*1e4:.1f} bps)")
            return {**bot, "trigger": new_trigger}

        if "multi" in args and isinstance(args["multi"], list):
            print(f"[orchestrator] resolving LTP for {len(args['multi'])} bots...")
            resolved_bots = []
            for bot in args["multi"]:
                resolved_bots.append(await resolve_one(bot))
                await asyncio.sleep(0.3)  # stagger
            new_args = {**args, "multi": resolved_bots}
        else:
            print(f"[orchestrator] resolving LTP for {args['symbol']}...")
            new_args = await resolve_one(args)

        return _dc.replace(scenario, bot_args=new_args)

    async def run(self, scenario: Scenario) -> OrchestrationResult:
        session_name = f"gt_paper_{scenario.id}"
        stdout_log = self.log_dir / f"{scenario.id}_{int(time.time())}.log"
        result = OrchestrationResult(
            scenario_id=scenario.id,
            started_at=datetime.now(),
            tmux_session_name=session_name,
            stdout_log_path=stdout_log,
        )

        # PRE-FLIGHT: nuke any stale tmux session AND state files for this
        # (symbol, client_id). Without this, the engine reads stale state
        # from a previous run and behaves unpredictably.
        self._tmux(["kill-session", "-t", session_name], check=False)
        time.sleep(0.3)
        self._cleanup_state_files(scenario, result)

        # LTP RESOLUTION: convert ltp_offset_pct → absolute trigger using
        # live broker data. Without this, triggers are stale guesses and
        # bots sit in MONITORING forever.
        try:
            scenario = await self._resolve_triggers(scenario)
        except Exception as e:
            print(f"[orchestrator] LTP resolution error ({e}); "
                  f"continuing with hardcoded triggers.")
            result.errors.append(f"LTP resolution: {e}")

        try:
            sorted_actions = sorted(
                scenario.actions, key=lambda a: getattr(a, "at_seconds", 0.0),
            )
            campaign_start = time.monotonic()

            for action in sorted_actions:
                if isinstance(action, WaitUntilFilled):
                    await self._wait_until_filled(scenario, action.timeout_seconds)
                    result.actions_executed.append("WaitUntilFilled")
                    continue

                wait_for = getattr(action, "at_seconds", 0.0)
                elapsed = time.monotonic() - campaign_start
                if wait_for > elapsed:
                    delay = wait_for - elapsed
                    print(f"[orchestrator] sleeping {delay:.0f}s until t={wait_for:.0f}s "
                          f"({type(action).__name__})...")
                    await asyncio.sleep(delay)
                print(f"[orchestrator] t={time.monotonic() - campaign_start:.0f}s → "
                      f"{type(action).__name__}")

                if isinstance(action, StartBot):
                    self._spawn_bot(scenario, session_name, stdout_log, result=result)
                    result.actions_executed.append(f"StartBot @ {wait_for:.0f}s")
                elif isinstance(action, KillBot):
                    for s in self._all_sessions(scenario, session_name, result):
                        self._tmux(["send-keys", "-t", s, "C-c"], check=False)
                    result.actions_executed.append(f"KillBot @ {wait_for:.0f}s")
                elif isinstance(action, RestartBot):
                    for s in self._all_sessions(scenario, session_name, result):
                        self._tmux(["send-keys", "-t", s, "C-c"], check=False)
                    await asyncio.sleep(2.0)
                    self._spawn_bot(scenario, session_name, stdout_log,
                                    restart=True, result=result)
                    result.actions_executed.append(f"RestartBot @ {wait_for:.0f}s")
                elif isinstance(action, KillTWS):
                    await self._prompt_operator(
                        f"KILL TWS/IB Gateway now. Press ENTER when done"
                        f" (auto-continue in 15s)..."
                    )
                    result.actions_executed.append(f"KillTWS @ {wait_for:.0f}s")
                elif isinstance(action, StartTWS):
                    await self._prompt_operator(
                        f"RESTART TWS/IB Gateway now. Press ENTER when ready"
                        f" (auto-continue in 30s)...", timeout=30.0,
                    )
                    result.actions_executed.append(f"StartTWS @ {wait_for:.0f}s")
                elif isinstance(action, ManualCancelInTWS):
                    await self._manual_cancel(scenario, action)
                    result.actions_executed.append(
                        f"ManualCancelInTWS({action.order_type}) @ {wait_for:.0f}s"
                    )
                elif isinstance(action, ManualModifyInTWS):
                    await self._manual_modify(scenario, action)
                    result.actions_executed.append(
                        f"ManualModifyInTWS(new_stop={action.new_stop_price}) @ {wait_for:.0f}s"
                    )
                elif isinstance(action, SeedPreExistingPosition):
                    await self._seed_position(scenario, action)
                    result.actions_executed.append(
                        f"SeedPreExistingPosition({action.qty}) @ {wait_for:.0f}s"
                    )
                elif isinstance(action, SeedPreExistingStop):
                    await self._seed_stop(scenario, action)
                    result.actions_executed.append(
                        f"SeedPreExistingStop({action.qty}@{action.stop_price}) @ {wait_for:.0f}s"
                    )
                elif isinstance(action, StopCampaign):
                    result.actions_executed.append(f"StopCampaign @ {wait_for:.0f}s")
                    break
                else:
                    result.errors.append(f"Unknown action: {type(action).__name__}")
        except Exception as e:
            result.errors.append(f"Orchestration error: {type(e).__name__}: {e}")
        finally:
            for s in self._all_sessions(scenario, session_name, result):
                self._tmux(["send-keys", "-t", s, "C-c"], check=False)
            time.sleep(2.0)
            for s in self._all_sessions(scenario, session_name, result):
                self._tmux(["kill-session", "-t", s], check=False)
            result.ended_at = datetime.now()
            result.duration_seconds = (result.ended_at - result.started_at).total_seconds()

        return result

    def _all_sessions(
        self, scenario: Scenario, primary_session: str,
        result: OrchestrationResult,
    ) -> list[str]:
        """Return list of every tmux session this scenario is using.
        Single-bot: just the primary. Multi-bot: every sub-session spawned."""
        if "multi" in scenario.bot_args and isinstance(scenario.bot_args["multi"], list):
            sessions = [b["session"] for b in result.bot_sessions]
            return sessions if sessions else [primary_session]
        return [primary_session]

    def _spawn_bot(
        self, scenario: Scenario, session_name: str,
        stdout_log: Path, restart: bool = False,
        result: Optional[OrchestrationResult] = None,
    ) -> None:
        args = scenario.bot_args
        # ---- MULTI-BOT branch ------------------------------------------------
        if "multi" in args and isinstance(args["multi"], list):
            bots = args["multi"]
            print(f"[orchestrator] MULTI-BOT spawn: {len(bots)} bots concurrent")
            for i, bot in enumerate(bots):
                sym = bot["symbol"]; cid = bot["client_id"]
                sub_session = f"{session_name}_{sym}_{cid}"
                sub_log = self.log_dir / f"{scenario.id}_{sym}_{cid}_{int(time.time())}.log"
                self._spawn_single_in_session(bot, sub_session, sub_log)
                if result is not None:
                    result.bot_sessions.append({
                        "symbol": sym, "client_id": cid,
                        "session": sub_session, "log": str(sub_log),
                    })
                print(f"  [{i+1}/{len(bots)}] {sym} cid={cid} → tmux={sub_session}")
                time.sleep(0.4)  # stagger so TWS API doesn't get hammered
            return

        # ---- SINGLE-BOT branch ----------------------------------------------
        self._spawn_single_in_session(args, session_name, stdout_log, restart=restart)

    def _spawn_single_in_session(
        self, bot_args: dict, session_name: str,
        stdout_log: Path, restart: bool = False,
    ) -> None:
        """Spawn one bot inside one named tmux session."""
        cli = self._build_cli_for_single(bot_args)
        check = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
        )
        if check.returncode != 0 or restart:
            if restart:
                self._tmux(["kill-session", "-t", session_name], check=False)
                time.sleep(0.5)
            self._tmux([
                "new-session", "-d", "-s", session_name,
                "-c", str(self.project_root),
            ])
            self._tmux([
                "pipe-pane", "-o", "-t", session_name,
                f"cat >> {shlex.quote(str(stdout_log))}",
            ])
        self._tmux(["send-keys", "-t", session_name, cli, "Enter"])

    def _build_cli_for_single(self, args: dict) -> str:
        # Just use `python3` — your shell's PATH resolves it correctly
        # (including venv activation). Trusting PATH is more reliable
        # than trying to guess via sys.executable (symlink-resolves to
        # system python) or $VIRTUAL_ENV (may not be in tmux's env).
        parts = ["GT_PAPER=false", "python3", "run_live.py", args["symbol"],
                 "--trigger", str(args["trigger"]),
                 "--stop", str(args["stop_pct"]),
                 "--offset-fixed", str(args["offset_fixed"]),
                 "--qty", str(args["qty"]),
                 "--port", str(args["port"]),
                 "--client-id", str(args["client_id"]),
                 "--uvloop"]
        return " ".join(parts)

    def _cleanup_state_files(
        self, scenario: Scenario, result: OrchestrationResult,
    ) -> None:
        """Remove .gt_state_<SYM>_<CID>.json and .gt_live_<SYM>_<CID>.json
        for every (symbol, client_id) this scenario will use. Logs what was
        removed so the operator can verify."""
        pairs: list[tuple[str, int]] = []
        args = scenario.bot_args
        if "multi" in args:
            # multi-bot scenarios list their bots under args["multi"]
            for b in args.get("multi", []):
                pairs.append((b["symbol"], b["client_id"]))
        else:
            pairs.append((args["symbol"], args["client_id"]))

        removed: list[str] = []
        for sym, cid in pairs:
            for pat in (f".gt_state_{sym}_{cid}.json",
                        f".gt_live_{sym}_{cid}.json"):
                fp = self.project_root / pat
                if fp.exists():
                    try:
                        fp.unlink()
                        removed.append(pat)
                    except OSError as e:
                        result.errors.append(f"cleanup failed: {pat}: {e}")
        if removed:
            print(f"[orchestrator] pre-flight cleanup removed: {', '.join(removed)}")
        else:
            print(f"[orchestrator] pre-flight cleanup: no state files to remove "
                  f"(client_ids: {[c for _,c in pairs]})")

    def _tmux(self, cmd_args: list[str], check: bool = True) -> None:
        try:
            subprocess.run(
                ["tmux", *cmd_args], check=check, capture_output=True,
            )
        except subprocess.CalledProcessError:
            if check:
                raise

    async def _prompt_operator(self, msg: str, timeout: float = 15.0) -> None:
        print(f"\n{'!' * 70}")
        print(f">>> SCENARIO ACTION: {msg}")
        print(f"!!! (tip: `tmux attach -t <session>` in another terminal to watch the bot)")
        print(f"{'!' * 70}\n")
        t0 = time.monotonic()
        timed_out = False
        try:
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, input), timeout=timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
        except (RuntimeError, EOFError):
            pass
        dt = time.monotonic() - t0
        if timed_out:
            print(f"[orchestrator] auto-continuing after {timeout:.0f}s timeout — "
                  f"orchestrator is now sleeping until next scheduled action.\n")
        else:
            print(f"[orchestrator] operator confirmed after {dt:.1f}s — "
                  f"now sleeping until next scheduled action (this can take a while; "
                  f"check tmux/log for live bot output).\n")

    async def _wait_until_filled(self, scenario: Scenario, timeout_s: float) -> None:
        from datetime import datetime as _dt
        deadline = time.monotonic() + timeout_s
        today_str = _dt.now().strftime("%Y%m%d")
        symbol = scenario.bot_args.get("symbol", "EURUSD")
        order_csv = self.project_root / "data" / "audit" / today_str / symbol / "order.csv"

        while time.monotonic() < deadline:
            if order_csv.exists():
                import csv as _csv
                try:
                    with open(order_csv) as f:
                        for row in _csv.DictReader(f):
                            if row.get("event") == "FILLED" and row.get("side") == "BUY":
                                return
                except Exception:
                    pass
            await asyncio.sleep(2.0)

    async def _manual_cancel(self, scenario: Scenario, action: ManualCancelInTWS) -> None:
        try:
            from ib_async import IB
            ib = IB()
            await ib.connectAsync(
                "127.0.0.1", scenario.bot_args.get("port", 7497), clientId=99,
            )
            await asyncio.sleep(1.0)
            target_action = "SELL" if action.order_type == "SELL_STOP" else "BUY"
            for trade in ib.openTrades():
                if trade.order.action == target_action:
                    ib.cancelOrder(trade.order)
                    print(f"[orchestrator] cancelled {target_action} order id={trade.order.orderId}")
            await asyncio.sleep(0.5)
            ib.disconnect()
        except Exception as e:
            print(f"[orchestrator] manual_cancel failed: {e}")

    async def _manual_modify(self, scenario: Scenario, action: ManualModifyInTWS) -> None:
        try:
            from ib_async import IB
            ib = IB()
            await ib.connectAsync(
                "127.0.0.1", scenario.bot_args.get("port", 7497), clientId=99,
            )
            await asyncio.sleep(1.0)
            for trade in ib.openTrades():
                if trade.order.action == "SELL" and trade.order.orderType in ("STP", "STP LMT"):
                    trade.order.auxPrice = action.new_stop_price
                    ib.placeOrder(trade.contract, trade.order)
                    print(f"[orchestrator] modified SELL stop to {action.new_stop_price}")
                    break
            await asyncio.sleep(0.5)
            ib.disconnect()
        except Exception as e:
            print(f"[orchestrator] manual_modify failed: {e}")

    async def _seed_position(self, scenario: Scenario, action: SeedPreExistingPosition) -> None:
        try:
            from ib_async import IB, Forex, Stock, MarketOrder
            ib = IB()
            await ib.connectAsync(
                "127.0.0.1", scenario.bot_args.get("port", 7497), clientId=99,
            )
            symbol = scenario.bot_args.get("symbol", "EURUSD")
            contract = Forex(symbol) if len(symbol) == 6 else Stock(symbol, "SMART", "USD")
            qualified = await ib.qualifyContractsAsync(contract)
            order = MarketOrder(action.side, action.qty)
            ib.placeOrder(qualified[0], order)
            await asyncio.sleep(3.0)
            ib.disconnect()
        except Exception as e:
            print(f"[orchestrator] seed_position failed: {e}")

    async def _seed_stop(self, scenario: Scenario, action: SeedPreExistingStop) -> None:
        try:
            from ib_async import IB, Forex, Stock, StopOrder
            ib = IB()
            await ib.connectAsync(
                "127.0.0.1", scenario.bot_args.get("port", 7497), clientId=99,
            )
            symbol = scenario.bot_args.get("symbol", "EURUSD")
            contract = Forex(symbol) if len(symbol) == 6 else Stock(symbol, "SMART", "USD")
            qualified = await ib.qualifyContractsAsync(contract)
            order = StopOrder("SELL", action.qty, action.stop_price)
            order.orderRef = "PAPER_TEST_SEEDED_STOP"
            order.tif = "GTC"
            ib.placeOrder(qualified[0], order)
            await asyncio.sleep(2.0)
            ib.disconnect()
        except Exception as e:
            print(f"[orchestrator] seed_stop failed: {e}")


__all__ = ["TmuxOrchestrator", "OrchestrationResult"]

"""Paper-trading scenario catalog — 37 scenarios runnable against IBKR paper.

Each scenario is declarative data — the orchestrator interprets the actions;
the verifier checks the expected audit-log markers.

Organization:
   A. Happy path (1-3)
   B. Connection resilience (4-9)
   C. Order lifecycle (10-15)
   D. State persistence (16-21)
   E. Market conditions (22-25)
   F. Multi-instrument (26-28)
   G. Risk gates (29-32)
   H. Orphan recovery (33-37)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ════════════════════════════════════════════════════════════════════════════
# ACTION TYPES
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True, slots=True)
class StartBot:
    at_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class KillBot:
    """SIGINT the bot (Ctrl+C). State file persists; position stays at broker."""
    at_seconds: float


@dataclass(frozen=True, slots=True)
class RestartBot:
    at_seconds: float
    same_args: bool = True


@dataclass(frozen=True, slots=True)
class KillTWS:
    """Force-quit TWS/IB Gateway. Engine should detect disconnect."""
    at_seconds: float


@dataclass(frozen=True, slots=True)
class StartTWS:
    at_seconds: float


@dataclass(frozen=True, slots=True)
class ManualCancelInTWS:
    """Operator-style cancel via a separate IBKR connection (clientId=99)."""
    at_seconds: float
    order_type: str = "SELL_STOP"   # SELL_STOP / BUY_PARENT / ANY


@dataclass(frozen=True, slots=True)
class ManualModifyInTWS:
    at_seconds: float
    new_stop_price: float


@dataclass(frozen=True, slots=True)
class SeedPreExistingPosition:
    at_seconds: float
    qty: int
    side: str = "BUY"


@dataclass(frozen=True, slots=True)
class SeedPreExistingStop:
    at_seconds: float
    qty: int
    stop_price: float


@dataclass(frozen=True, slots=True)
class WaitUntilFilled:
    timeout_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class StopCampaign:
    at_seconds: float


@dataclass(frozen=True, slots=True)
class AuditExpect:
    must_have_events: tuple[str, ...] = field(default_factory=tuple)
    must_not_have_events: tuple[str, ...] = field(default_factory=tuple)
    must_have_logs: tuple[str, ...] = field(default_factory=tuple)
    must_not_have_logs: tuple[str, ...] = field(default_factory=tuple)
    final_state: Optional[str] = None
    final_position_open: Optional[bool] = None
    final_position_qty: Optional[int] = None
    invariants_clean: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    name: str
    description: str
    category: str
    bot_args: dict
    actions: tuple
    expect: AuditExpect
    duration_seconds: float = 300.0
    tags: tuple[str, ...] = field(default_factory=tuple)


# ════════════════════════════════════════════════════════════════════════════
# CONFIG DEFAULTS
# ════════════════════════════════════════════════════════════════════════════

# Test client IDs live in 80-99 so they NEVER clash with the user's live
# trading client_id (21) or any other manual session. Each scenario can be
# overridden via --client-id-base on the runner.
_DEFAULT_EURUSD = {
    "symbol": "EURUSD", "trigger": 1.17005, "ltp_offset_pct": 0.0005,
    "stop_pct": 0.001, "offset_fixed": 0.0005,
    "qty": 25000, "port": 7497, "client_id": 80,
}

_DEFAULT_IBUS500 = {
    "symbol": "IBUS500", "trigger": 7600.00, "ltp_offset_pct": 0.0010,
    "stop_pct": 0.005, "offset_fixed": 1.00,
    "qty": 1, "port": 7497, "client_id": 81,
}

_DEFAULT_AAPL = {
    "symbol": "AAPL", "trigger": 235.00, "ltp_offset_pct": 0.0005,
    "stop_pct": 0.005, "offset_fixed": 0.05,
    "qty": 10, "port": 7497, "client_id": 82,
}


# ────────────────────────────────────────────────────────────────────────────
# DIVERSE ASSET-CLASS BOT POOL — used by stress scenarios.
# Each entry has a UNIQUE client_id (80-99 range so they never clash
# with live trading). Triggers are wide so bots stay in MONITORING long
# enough to exercise the engine — adjust if your paper account is on
# a market regime where these prices are already past.
# ────────────────────────────────────────────────────────────────────────────

# IMPORTANT: `trigger` is a FALLBACK; if `ltp_offset_pct` is set, the
# orchestrator queries live LTP at spawn time and computes
#     trigger = round_to_tick(LTP * (1 + ltp_offset_pct))
# This way bots fire within minutes regardless of where the market is.
#
# Convention:
#   - FX:       +5 bps above LTP  (0.0005)
#   - Equities: +5 bps above LTP  (0.0005)
#   - CFDs:    +10 bps above LTP  (0.0010)
#   - Futures:  +5 bps above LTP  (0.0005)

# FX pool (4 majors, 4 client_ids: 83-86)
_FX_POOL = [
    {"symbol": "EURUSD", "trigger": 1.17005, "ltp_offset_pct": 0.0005,
     "stop_pct": 0.001, "offset_fixed": 0.0005,
     "qty": 25000, "port": 7497, "client_id": 83},
    {"symbol": "GBPUSD", "trigger": 1.34500, "ltp_offset_pct": 0.0005,
     "stop_pct": 0.001, "offset_fixed": 0.0005,
     "qty": 25000, "port": 7497, "client_id": 84},
    {"symbol": "USDJPY", "trigger": 153.500, "ltp_offset_pct": 0.0005,
     "stop_pct": 0.001, "offset_fixed": 0.05,
     "qty": 25000, "port": 7497, "client_id": 85},
    {"symbol": "AUDUSD", "trigger": 0.66500, "ltp_offset_pct": 0.0005,
     "stop_pct": 0.001, "offset_fixed": 0.0005,
     "qty": 25000, "port": 7497, "client_id": 86},
]

# US equity pool (4 mega-caps, client_ids: 87-90)
_EQUITY_POOL = [
    {"symbol": "AAPL", "trigger": 235.00, "ltp_offset_pct": 0.0005,
     "stop_pct": 0.005, "offset_fixed": 0.05,
     "qty": 10, "port": 7497, "client_id": 87},
    {"symbol": "MSFT", "trigger": 430.00, "ltp_offset_pct": 0.0005,
     "stop_pct": 0.005, "offset_fixed": 0.05,
     "qty": 5,  "port": 7497, "client_id": 88},
    {"symbol": "TSLA", "trigger": 260.00, "ltp_offset_pct": 0.0005,
     "stop_pct": 0.005, "offset_fixed": 0.05,
     "qty": 5,  "port": 7497, "client_id": 89},
    {"symbol": "NVDA", "trigger": 145.00, "ltp_offset_pct": 0.0005,
     "stop_pct": 0.005, "offset_fixed": 0.05,
     "qty": 10, "port": 7497, "client_id": 90},
]

# CFD pool (index CFDs, client_ids: 91-93)
_CFD_POOL = [
    {"symbol": "IBUS500", "trigger": 7600.00, "ltp_offset_pct": 0.0010,
     "stop_pct": 0.005, "offset_fixed": 1.00,
     "qty": 1, "port": 7497, "client_id": 91},
    {"symbol": "IBDE40",  "trigger": 19000.0, "ltp_offset_pct": 0.0010,
     "stop_pct": 0.005, "offset_fixed": 2.00,
     "qty": 1, "port": 7497, "client_id": 92},
    {"symbol": "IBUK100", "trigger": 8200.0,  "ltp_offset_pct": 0.0010,
     "stop_pct": 0.005, "offset_fixed": 1.00,
     "qty": 1, "port": 7497, "client_id": 93},
]

# Futures pool (CME index + WTI, client_ids: 94-96)
_FUTURES_POOL = [
    {"symbol": "ES", "trigger": 7600.00, "ltp_offset_pct": 0.0005,
     "stop_pct": 0.005, "offset_fixed": 0.50,
     "qty": 1, "port": 7497, "client_id": 94},
    {"symbol": "NQ", "trigger": 25000.0, "ltp_offset_pct": 0.0005,
     "stop_pct": 0.005, "offset_fixed": 1.00,
     "qty": 1, "port": 7497, "client_id": 95},
    {"symbol": "CL", "trigger": 75.00, "ltp_offset_pct": 0.0005,
     "stop_pct": 0.005, "offset_fixed": 0.05,
     "qty": 1, "port": 7497, "client_id": 96},
]


# ════════════════════════════════════════════════════════════════════════════
# THE CATALOG — 37 scenarios
# ════════════════════════════════════════════════════════════════════════════

CATALOG: tuple[Scenario, ...] = (

    # ═══════════════════════════════════════════════════════════
    # P00 — 60-SECOND SMOKE TEST
    # First thing you should ever run. Proves the basics work
    # before you waste time on long campaigns.
    # ═══════════════════════════════════════════════════════════
    Scenario(
        id="P00_smoke_60s",
        name="60-second smoke — connect + place + verify",
        description="Fastest possible end-to-end. Spawn 1 EURUSD bot, "
                    "trigger 2 bps above LTP. Within 60s you should see "
                    "SUBMITTED + (likely) FILLED. No long hold.",
        category="happy",
        bot_args={**_DEFAULT_EURUSD, "ltp_offset_pct": 0.0002},  # 2 bps
        actions=(StartBot(), StopCampaign(at_seconds=60)),
        expect=AuditExpect(
            must_have_events=("SUBMITTED",),
            must_not_have_events=("POSITION_AUTO_FLAT", "SHORTING_PREVENTED"),
        ),
        tags=("smoke", "fast"),
    ),

    # A. HAPPY PATH ─────────────────────────────────────────────
    Scenario(
        id="P01_clean_bracket_lifecycle",
        name="Clean bracket lifecycle, no disruptions",
        description="Baseline: bot enters, fills, holds, exits naturally.",
        category="happy",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), WaitUntilFilled(timeout_seconds=600),
                 StopCampaign(at_seconds=900)),
        expect=AuditExpect(
            must_have_events=("SUBMITTED", "BRACKET_SUBMITTED", "FILLED",
                              "CHILD_STOP_MODIFIED", "COMMISSION_REPORT"),
            must_not_have_events=("POSITION_AUTO_FLAT", "STALE_SELL_REJECTED",
                                  "STOP_PRICE_DIVERGENCE",
                                  "DUPLICATE_SELL_GUARD_FALLBACK_TO_PLACE"),
            must_have_logs=("Position check OK",),
            final_state="IN_POSITION", final_position_open=True,
            invariants_clean=("PRICE_ON_VENUE_GRID", "MODIFY_NOT_REPLACE",
                              "SELL_STOP_BELOW_ENTRY"),
        ),
        tags=("baseline", "smoke"),
    ),
    Scenario(
        id="P02_multiple_sequential_cycles",
        name="Multiple sequential cycles same day",
        description="3 complete entry→exit→re-entry cycles in one session.",
        category="happy",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), WaitUntilFilled(1800), StopCampaign(2400)),
        expect=AuditExpect(
            must_have_events=("FILLED", "FILLED", "FILLED"),
            must_not_have_events=("POSITION_AUTO_FLAT",),
            invariants_clean=("PRICE_ON_VENUE_GRID", "MODIFY_NOT_REPLACE",
                              "POSITION_QTY_MATCH"),
        ),
        tags=("baseline", "long_running"),
    ),
    Scenario(
        id="P03_partial_fills",
        name="BUY entry fills in pieces",
        description="Large qty likely partial-fills. Engine tracks cum qty and modifies SL per partial.",
        category="happy",
        bot_args={**_DEFAULT_EURUSD, "qty": 100000},
        actions=(StartBot(), WaitUntilFilled(300), StopCampaign(600)),
        expect=AuditExpect(
            must_have_events=("SUBMITTED", "FILLED"),
            must_have_logs=("PARTIAL", "CHILD_STOP_MID_PARTIAL"),
            invariants_clean=("POSITION_QTY_MATCH", "SELL_STOP_BELOW_ENTRY",
                              "MODIFY_NOT_REPLACE"),
        ),
        tags=("partial_fill",),
    ),

    # B. CONNECTION RESILIENCE ─────────────────────────────────
    Scenario(
        id="P04_disconnect_while_monitoring",
        name="Kill TWS while bot is MONITORING (no position)",
        description="No position, no order can be wrongly placed. Auto-recover on TWS restart.",
        category="connection",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), KillTWS(30), StartTWS(120), StopCampaign(240)),
        expect=AuditExpect(
            must_have_logs=("skipped — gateway DISCONNECTED",
                            "ib_async socket is back up", "Position check OK"),
            must_not_have_events=("POSITION_AUTO_FLAT", "SHORTING_PREVENTED"),
            invariants_clean=("PRICE_ON_VENUE_GRID",),
        ),
        tags=("disconnect", "critical"),
    ),
    Scenario(
        id="P05_disconnect_while_in_position",
        name="Kill TWS while LONG with active SL",
        description="THE 2026-06-06 BUG. Must NOT fold to FLAT during disconnect.",
        category="connection",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), WaitUntilFilled(600), KillTWS(30),
                 StartTWS(180), StopCampaign(300)),
        expect=AuditExpect(
            must_have_logs=("skipped — gateway DISCONNECTED",
                            "ib_async socket is back up"),
            must_not_have_events=("POSITION_AUTO_FLAT", "ORPHAN_CANCELLED"),
            final_state="IN_POSITION", final_position_open=True,
            invariants_clean=("POSITION_QTY_MATCH", "SELL_STOP_BELOW_ENTRY"),
        ),
        tags=("disconnect", "critical", "live_position"),
    ),
    Scenario(
        id="P06_long_disconnect_window",
        name="15-minute disconnect window",
        description="Simulate IB Gateway daily-reboot window.",
        category="connection",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), KillTWS(60), StartTWS(900), StopCampaign(1200)),
        expect=AuditExpect(
            must_have_logs=("skipped — gateway DISCONNECTED",
                            "ib_async socket is back up"),
            must_not_have_events=("POSITION_AUTO_FLAT", "TRIPWIRE_LOST_PENDING"),
        ),
        tags=("disconnect", "endurance"),
    ),
    Scenario(
        id="P07_disconnect_during_order_placement",
        name="TWS dies WHILE the engine is placing the bracket",
        description="Race: did the order make it? Reconcile must adopt or place fresh, no duplicates.",
        category="connection",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), KillTWS(5), StartTWS(60), StopCampaign(300)),
        expect=AuditExpect(
            must_not_have_events=("STALE_SELL_REJECTED",
                                  "DUPLICATE_SELL_GUARD_FALLBACK_TO_PLACE"),
            invariants_clean=("MODIFY_NOT_REPLACE",),
        ),
        tags=("disconnect", "race_condition", "critical"),
    ),
    Scenario(
        id="P08_repeated_quick_disconnects",
        name="5 disconnect+reconnect cycles in rapid succession",
        description="Stress-test reconnect detection.",
        category="connection",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(),
                 KillTWS(30),  StartTWS(60),
                 KillTWS(90),  StartTWS(120),
                 KillTWS(150), StartTWS(180),
                 KillTWS(210), StartTWS(240),
                 KillTWS(270), StartTWS(300),
                 StopCampaign(420)),
        expect=AuditExpect(
            must_have_logs=("ib_async socket is back up",) * 5,
            must_not_have_events=("POSITION_AUTO_FLAT",),
        ),
        tags=("disconnect", "stress"),
    ),
    Scenario(
        id="P09_disconnect_during_partial_fill",
        name="TWS dies between partial fills",
        description="Reconcile must replay missed partial, continue with the right cum qty.",
        category="connection",
        bot_args={**_DEFAULT_EURUSD, "qty": 100000},
        actions=(StartBot(), WaitUntilFilled(60), KillTWS(70),
                 StartTWS(180), StopCampaign(420)),
        expect=AuditExpect(
            must_have_logs=("Reconcile: found", "Position check OK"),
            invariants_clean=("POSITION_QTY_MATCH",),
        ),
        tags=("disconnect", "partial_fill"),
    ),

    # C. ORDER LIFECYCLE ───────────────────────────────────────
    Scenario(
        id="P10_manual_cancel_buy_before_fill",
        name="Operator cancels the BUY entry via TWS",
        description="OCA should cancel both legs; engine re-places after debounce.",
        category="lifecycle",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), ManualCancelInTWS(30, "BUY_PARENT"),
                 StopCampaign(300)),
        expect=AuditExpect(
            must_have_events=("SUBMITTED", "CANCELLED"),
            must_have_logs=("Entry order missing — re-placing",),
        ),
        tags=("manual_cancel",),
    ),
    Scenario(
        id="P11_manual_cancel_sl_while_long",
        name="Operator cancels the protective SL via TWS",
        description="Health-check Probe 1 must detect NAKED_POSITION and re-arm within 30s.",
        category="lifecycle",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), WaitUntilFilled(),
                 ManualCancelInTWS(10, "SELL_STOP"), StopCampaign(120)),
        expect=AuditExpect(
            must_have_logs=("NAKED POSITION", "re-arming"),
            invariants_clean=("SELL_STOP_BELOW_ENTRY",),
        ),
        tags=("manual_cancel", "naked_position", "critical"),
    ),
    Scenario(
        id="P12_manual_modify_sl_in_tws",
        name="Operator modifies SL via TWS (drag on chart)",
        description="STOP_PRICE_DIVERGENCE detector must fire CRITICAL alert within 30s.",
        category="lifecycle",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), WaitUntilFilled(),
                 ManualModifyInTWS(10, 1.16500), StopCampaign(90)),
        expect=AuditExpect(
            must_have_events=("STOP_PRICE_DIVERGENCE",),
        ),
        tags=("manual_modify", "divergence"),
    ),
    Scenario(
        id="P13_off_grid_trigger_snaps",
        name="Engine snaps off-grid trigger correctly",
        description="--trigger NOT on tick grid; engine snaps before submission; no rejection.",
        category="lifecycle",
        bot_args={**_DEFAULT_EURUSD, "trigger": 1.17003},
        actions=(StartBot(), StopCampaign(60)),
        expect=AuditExpect(
            must_have_logs=("WARNING: --trigger", "is NOT on the EURUSD tick grid"),
            must_not_have_events=("REJECTED",),
        ),
        tags=("precision",),
    ),
    Scenario(
        id="P14_excessive_qty_risk_block",
        name="Risk gate refuses entry (excessive qty)",
        description="qty large enough to breach exposure cap. Risk gate emits REJECTED in audit.",
        category="lifecycle",
        bot_args={**_DEFAULT_EURUSD, "qty": 10_000_000},
        actions=(StartBot(), StopCampaign(60)),
        expect=AuditExpect(
            must_have_events=("REJECTED",),
            must_have_logs=("ENTRY BLOCKED BY RISK",),
        ),
        tags=("risk_gate",),
    ),
    Scenario(
        id="P15_natural_sl_trigger",
        name="Market moves to SL — natural exit",
        description="Round-trip with correct PnL accounting.",
        category="lifecycle",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), WaitUntilFilled(), StopCampaign(3600)),
        expect=AuditExpect(
            must_have_events=("FILLED", "FILLED", "COMMISSION_REPORT"),
            invariants_clean=("POSITION_QTY_MATCH",),
        ),
        tags=("natural_exit", "long_running"),
    ),

    # D. STATE PERSISTENCE ─────────────────────────────────────
    Scenario(
        id="P16_clean_restart_no_position",
        name="Restart while MONITORING (no position)",
        description="Resume monitoring with previous trigger; no new orders.",
        category="state",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), RestartBot(60), StopCampaign(180)),
        expect=AuditExpect(
            must_have_logs=("Restored MONITORING",),
            must_not_have_events=("POSITION_AUTO_FLAT",),
        ),
        tags=("restart", "smoke"),
    ),
    Scenario(
        id="P17_clean_restart_with_position",
        name="Restart while IN_POSITION (with active SL)",
        description="Adopts orphan SELL via DUPLICATE_SELL_GUARD; no duplication.",
        category="state",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), WaitUntilFilled(), RestartBot(15),
                 StopCampaign(180)),
        expect=AuditExpect(
            must_have_logs=("Restored IN_POSITION", "Adopted orphan"),
            must_not_have_events=("POSITION_AUTO_FLAT",),
            invariants_clean=("POSITION_QTY_MATCH", "SELL_STOP_BELOW_ENTRY"),
        ),
        tags=("restart", "critical"),
    ),
    Scenario(
        id="P18_restart_mid_bracket",
        name="Kill bot between bracket SUBMITTED and FILL",
        description="Reconcile must adopt the resting bracket from IBKR.",
        category="state",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), KillBot(5), StartBot(15), StopCampaign(120)),
        expect=AuditExpect(
            must_have_logs=("Reconcile: found",),
            must_not_have_events=("DUPLICATE_SELL_GUARD_FALLBACK_TO_PLACE",),
        ),
        tags=("restart", "race_condition", "critical"),
    ),
    Scenario(
        id="P19_restart_mid_modify",
        name="Kill bot right after BUY fill before SL modify",
        description="Bracket child still at stale initial stop; reconcile must modify it.",
        category="state",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), WaitUntilFilled(), KillBot(1), StartBot(10),
                 StopCampaign(180)),
        expect=AuditExpect(
            must_have_logs=("BRACKET RECOVERY", "modify"),
            invariants_clean=("SELL_STOP_BELOW_ENTRY",),
        ),
        tags=("restart", "race_condition"),
    ),
    Scenario(
        id="P20_restart_different_trigger",
        name="Restart with different --trigger CLI arg",
        description="Saved previous_breakout_level wins for the current cycle.",
        category="state",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), WaitUntilFilled(),
                 RestartBot(15, same_args=False),
                 StopCampaign(180)),
        expect=AuditExpect(must_have_logs=("Restored",)),
        tags=("restart", "config_change"),
    ),
    Scenario(
        id="P21_restart_pnl_recompute",
        name="Restart triggers PNL recompute",
        description="Stale state-file pnl overwritten by broker-truth via PNL_RECOMPUTED.",
        category="state",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), WaitUntilFilled(), RestartBot(30),
                 StopCampaign(120)),
        expect=AuditExpect(
            must_have_events=("PNL_RECOMPUTED",),
            must_have_logs=("PNL-RECOMPUTE",),
        ),
        tags=("restart", "pnl"),
    ),

    # E. MARKET CONDITIONS ─────────────────────────────────────
    Scenario(
        id="P22_outside_session_fx",
        name="FX bot during weekend (session closed)",
        description="Engine PAUSES; no orders placed.",
        category="market",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), StopCampaign(60)),
        expect=AuditExpect(
            must_have_logs=("Closed at startup", "engine PAUSED"),
            must_not_have_events=("SUBMITTED",),
        ),
        tags=("session",),
    ),
    Scenario(
        id="P23_outside_rth_equity",
        name="Equity bot during pre-market",
        description="Outside-RTH: engine defers or runs with outsideRth=True.",
        category="market",
        bot_args=_DEFAULT_AAPL,
        actions=(StartBot(), StopCampaign(120)),
        expect=AuditExpect(must_have_logs=("session",)),
        tags=("session", "equity"),
    ),
    Scenario(
        id="P24_entry_cutoff_window",
        name="Bot enters end-of-session entry cutoff",
        description="Last N minutes refuse new entries; existing position remains protected.",
        category="market",
        bot_args=_DEFAULT_AAPL,
        actions=(StartBot(), StopCampaign(600)),
        expect=AuditExpect(must_have_logs=("end-of-session cutoff",)),
        tags=("session",),
    ),
    Scenario(
        id="P25_overnight_hold_fx",
        name="FX position held across UTC midnight",
        description="Daily counters reset cleanly; position + SL unaffected.",
        category="market",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), WaitUntilFilled(), StopCampaign(21600)),
        expect=AuditExpect(
            must_have_logs=("daily reset",),
            invariants_clean=("POSITION_QTY_MATCH", "SELL_STOP_BELOW_ENTRY"),
        ),
        tags=("session", "endurance"),
    ),

    # F. MULTI-INSTRUMENT ──────────────────────────────────────
    Scenario(
        id="P26_three_bots_parallel",
        name="EURUSD + IBUS500 + AAPL bots simultaneous",
        description="3 client_ids; risk gate sees combined exposure.",
        category="multi_instrument",
        bot_args={"multi": [_DEFAULT_EURUSD, _DEFAULT_IBUS500, _DEFAULT_AAPL]},
        actions=(StartBot(), StopCampaign(1800)),
        expect=AuditExpect(
            invariants_clean=("PRICE_ON_VENUE_GRID", "POSITION_QTY_MATCH"),
        ),
        tags=("multi_instrument", "endurance"),
    ),
    Scenario(
        id="P27_multiple_fx_pairs",
        name="EURUSD + USDJPY simultaneously",
        description="Different tick grids + sessions; both bots stay sane.",
        category="multi_instrument",
        bot_args={"multi": [
            _DEFAULT_EURUSD,
            {"symbol": "USDJPY", "trigger": 153.500, "stop_pct": 0.001,
             "offset_fixed": 0.05, "qty": 25000, "port": 7497, "client_id": 24},
        ]},
        actions=(StartBot(), StopCampaign(1800)),
        expect=AuditExpect(invariants_clean=("PRICE_ON_VENUE_GRID",)),
        tags=("multi_instrument", "fx"),
    ),
    Scenario(
        id="P28_risk_gate_blocks_third_bot",
        name="Combined exposure blocks the 3rd bot from entering",
        description="Bots 1+2 hold positions; bot 3 risk-blocked by combined cap.",
        category="multi_instrument",
        bot_args={"multi": "see_actions"},
        actions=(StartBot(), StopCampaign(600)),
        expect=AuditExpect(
            must_have_events=("REJECTED",),
            must_have_logs=("Combined exposure",),
        ),
        tags=("risk_gate", "multi_instrument"),
    ),

    # G. RISK GATES ────────────────────────────────────────────
    Scenario(
        id="P29_max_consec_losses",
        name="Hit max-consecutive-losses limit",
        description="Tight stop forces losses; circuit breaker halts entries after limit.",
        category="risk",
        bot_args={**_DEFAULT_EURUSD, "stop_pct": 0.0001},
        actions=(StartBot(), StopCampaign(3600)),
        expect=AuditExpect(must_have_logs=("CIRCUIT BREAKER", "consec_losses")),
        tags=("risk_gate",),
    ),
    Scenario(
        id="P30_max_trades_per_day",
        name="Hit max-trades-per-day limit",
        description="After N round-trips, refuse new entries even on favorable signal.",
        category="risk",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), StopCampaign(3600)),
        expect=AuditExpect(must_have_logs=("max_trades_per_day",)),
        tags=("risk_gate",),
    ),
    Scenario(
        id="P31_daily_loss_circuit_break",
        name="Daily PnL crosses loss limit → circuit break",
        description="Accumulated losses pause new entries; existing position management continues.",
        category="risk",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), StopCampaign(7200)),
        expect=AuditExpect(must_have_logs=("CIRCUIT BREAKER",)),
        tags=("risk_gate", "long_running"),
    ),
    Scenario(
        id="P32_entry_rejection_backoff",
        name="ENTRY_BUY repeatedly rejected → backoff fires",
        description="Margin/qty rejection; engine backs off for 300s instead of flooding orders.",
        category="risk",
        bot_args={**_DEFAULT_EURUSD, "qty": 50_000_000},
        actions=(StartBot(), StopCampaign(400)),
        expect=AuditExpect(
            must_have_events=("REJECTED",),
            must_have_logs=("ENTRY_REJECTION_BACKOFF",),
        ),
        tags=("risk_gate", "rejection"),
    ),

    # H. ORPHAN RECOVERY ───────────────────────────────────────
    Scenario(
        id="P33_preexisting_position_at_startup",
        name="Manual BUY in TWS, then start bot",
        description="Orphan position detected; bot refuses to start OR adopts cleanly per policy.",
        category="orphan",
        bot_args=_DEFAULT_EURUSD,
        actions=(SeedPreExistingPosition(0, 25000), StartBot(5),
                 StopCampaign(300)),
        expect=AuditExpect(must_have_logs=("ORPHAN",)),
        tags=("orphan", "critical"),
    ),
    Scenario(
        id="P34_preexisting_sell_stop",
        name="Resting SELL STP from prior session, no position",
        description="Orphan order, no matching position; engine should cancel the stale order.",
        category="orphan",
        bot_args=_DEFAULT_EURUSD,
        actions=(SeedPreExistingStop(0, 25000, 1.16500), StartBot(5),
                 StopCampaign(180)),
        expect=AuditExpect(must_have_logs=("orphan", "cancel")),
        tags=("orphan",),
    ),
    Scenario(
        id="P35_adoption_of_consistent_bracket",
        name="Pre-existing bracket from prior bot session adopted",
        description="Both legs at IBKR + state file consistent → bot resumes without duplicates.",
        category="orphan",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(0), KillBot(5), StartBot(10), StopCampaign(180)),
        expect=AuditExpect(
            must_have_logs=("Reconcile: found",),
            must_not_have_events=("DUPLICATE_SELL_GUARD_FALLBACK_TO_PLACE",),
        ),
        tags=("orphan", "restart"),
    ),
    Scenario(
        id="P36_two_bots_same_symbol_different_clients",
        name="Two bots same symbol, different client_ids",
        description="Each holds own position; risk gate sees combined exposure.",
        category="orphan",
        bot_args={"multi": "two_same_symbol"},
        actions=(StartBot(), StopCampaign(1200)),
        expect=AuditExpect(invariants_clean=("POSITION_QTY_MATCH",)),
        tags=("multi_instrument", "risk_gate"),
    ),
    # I. STRESS / CONCURRENT MULTI-BOT ────────────────────────────
    # These hammer the engine across asset classes simultaneously.
    Scenario(
        id="P38_stress_fx_pool_4_concurrent",
        name="4 FX bots concurrent (EURUSD + GBPUSD + USDJPY + AUDUSD)",
        description="4 different FX pairs, 4 different tick grids, all running "
                    "live against paper account. Engine must keep per-bot state "
                    "isolated. Combined notional ≈ $100k.",
        category="stress",
        bot_args={"multi": _FX_POOL},
        actions=(StartBot(), StopCampaign(1200)),
        expect=AuditExpect(
            invariants_clean=("PRICE_ON_VENUE_GRID", "POSITION_QTY_MATCH",
                              "SELL_STOP_BELOW_ENTRY", "MODIFY_NOT_REPLACE"),
            must_not_have_events=("POSITION_AUTO_FLAT", "SHORTING_PREVENTED",
                                  "STALE_SELL_REJECTED",
                                  "DUPLICATE_SELL_GUARD_FALLBACK_TO_PLACE"),
        ),
        tags=("stress", "multi_instrument", "fx", "concurrent"),
    ),
    Scenario(
        id="P39_stress_equity_pool_4_concurrent",
        name="4 US-equity bots concurrent (AAPL + MSFT + TSLA + NVDA)",
        description="4 mega-cap equities, RTH-only, all bots simultaneous. "
                    "Tick grids = $0.01; engine must round entries correctly.",
        category="stress",
        bot_args={"multi": _EQUITY_POOL},
        actions=(StartBot(), StopCampaign(1200)),
        expect=AuditExpect(
            invariants_clean=("PRICE_ON_VENUE_GRID", "POSITION_QTY_MATCH",
                              "SELL_STOP_BELOW_ENTRY"),
            must_not_have_events=("POSITION_AUTO_FLAT", "SHORTING_PREVENTED"),
        ),
        tags=("stress", "multi_instrument", "equity", "concurrent"),
    ),
    Scenario(
        id="P40_stress_cfd_pool_3_concurrent",
        name="3 index-CFD bots concurrent (IBUS500 + IBDE40 + IBUK100)",
        description="Index CFDs across US, DE, UK markets. Different sessions, "
                    "different tick grids, different currencies.",
        category="stress",
        bot_args={"multi": _CFD_POOL},
        actions=(StartBot(), StopCampaign(1200)),
        expect=AuditExpect(
            invariants_clean=("PRICE_ON_VENUE_GRID", "POSITION_QTY_MATCH"),
            must_not_have_events=("POSITION_AUTO_FLAT",),
        ),
        tags=("stress", "multi_instrument", "cfd", "concurrent"),
    ),
    Scenario(
        id="P41_stress_futures_pool_3_concurrent",
        name="3 futures bots concurrent (ES + NQ + CL)",
        description="CME index + WTI futures. Margin product, point-value "
                    "scaling — engine must compute notional + risk correctly.",
        category="stress",
        bot_args={"multi": _FUTURES_POOL},
        actions=(StartBot(), StopCampaign(1200)),
        expect=AuditExpect(
            invariants_clean=("PRICE_ON_VENUE_GRID", "POSITION_QTY_MATCH"),
            must_not_have_events=("POSITION_AUTO_FLAT",),
        ),
        tags=("stress", "multi_instrument", "futures", "concurrent"),
    ),
    Scenario(
        id="P42_MEGA_14_bots_all_asset_classes",
        name="14 bots concurrent — FX(4) + EQUITY(4) + CFD(3) + FUTURES(3)",
        description="The ULTIMATE stress test. 14 bots, 14 unique client_ids, "
                    "every asset class the engine supports running at once on "
                    "the same paper account. If anything is going to break, "
                    "it'll break here.",
        category="stress",
        bot_args={"multi": _FX_POOL + _EQUITY_POOL + _CFD_POOL + _FUTURES_POOL},
        actions=(StartBot(), StopCampaign(1800)),
        expect=AuditExpect(
            invariants_clean=("PRICE_ON_VENUE_GRID", "POSITION_QTY_MATCH",
                              "SELL_STOP_BELOW_ENTRY", "MODIFY_NOT_REPLACE"),
            must_not_have_events=("POSITION_AUTO_FLAT", "SHORTING_PREVENTED",
                                  "STALE_SELL_REJECTED",
                                  "DUPLICATE_SELL_GUARD_FALLBACK_TO_PLACE"),
        ),
        tags=("stress", "mega", "multi_instrument", "concurrent", "critical"),
    ),
    Scenario(
        id="P43_MEGA_14_bots_with_disconnect_chaos",
        name="14 concurrent bots + TWS kill mid-campaign",
        description="Same 14-bot mega test, but TWS dies at t=300s and recovers "
                    "at t=420s. ALL 14 bots must independently survive the "
                    "disconnect without phantom-flatting or shorting.",
        category="stress",
        bot_args={"multi": _FX_POOL + _EQUITY_POOL + _CFD_POOL + _FUTURES_POOL},
        actions=(StartBot(), KillTWS(300), StartTWS(420), StopCampaign(900)),
        expect=AuditExpect(
            invariants_clean=("POSITION_QTY_MATCH", "SELL_STOP_BELOW_ENTRY"),
            must_have_logs=("skipped — gateway DISCONNECTED",
                            "ib_async socket is back up"),
            must_not_have_events=("POSITION_AUTO_FLAT", "SHORTING_PREVENTED"),
        ),
        tags=("stress", "mega", "disconnect", "critical"),
    ),

    Scenario(
        id="P37_reset_flow_end_to_end",
        name="--reset CLI flow cancels + flattens cleanly",
        description="Cancels all orders, flattens position, then starts fresh.",
        category="orphan",
        bot_args=_DEFAULT_EURUSD,
        actions=(StartBot(), WaitUntilFilled(), KillBot(15),
                 RestartBot(30, same_args=False), StopCampaign(180)),
        expect=AuditExpect(
            must_have_logs=("reset", "cancel", "flatten"),
            final_position_open=False,
        ),
        tags=("reset", "critical"),
    ),

)


# ════════════════════════════════════════════════════════════════════════════
# LOOKUPS
# ════════════════════════════════════════════════════════════════════════════

def by_id(scenario_id: str) -> Scenario:
    for s in CATALOG:
        if s.id == scenario_id:
            return s
    raise KeyError(f"Unknown scenario: {scenario_id}")


def by_category(category: str) -> tuple[Scenario, ...]:
    return tuple(s for s in CATALOG if s.category == category)


def by_tag(tag: str) -> tuple[Scenario, ...]:
    return tuple(s for s in CATALOG if tag in s.tags)


def list_all() -> tuple[Scenario, ...]:
    return CATALOG


__all__ = [
    "StartBot", "KillBot", "RestartBot", "KillTWS", "StartTWS",
    "ManualCancelInTWS", "ManualModifyInTWS",
    "SeedPreExistingPosition", "SeedPreExistingStop",
    "WaitUntilFilled", "StopCampaign",
    "AuditExpect", "Scenario", "CATALOG",
    "by_id", "by_category", "by_tag", "list_all",
]

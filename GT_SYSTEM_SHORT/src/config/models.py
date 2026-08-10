from dataclasses import dataclass, field
from datetime import datetime, time, timezone, timedelta
from decimal import Decimal
from enum import Enum
from typing import Optional, Any
import json
import os
import asyncio

# ZoneInfo gives us a real America/New_York timezone that handles EDT/EST
# (DST) automatically. Available in stdlib from Python 3.9. tzdata is
# bundled by ib_async, so the data is on disk even on Windows where stdlib
# zoneinfo defers to it.
#
# Exposed as ET_ZONE (public) so other modules can compute ET-relative
# wall-clock events (the daily-reset scheduler uses this to fire at
# midnight ET, not local time — important when the bot runs on EC2 in
# Virginia OR on a developer laptop in IST).
try:
    from zoneinfo import ZoneInfo
    ET_ZONE = ZoneInfo("America/New_York")
except Exception:
    # Fallback: fixed UTC-5 if zoneinfo unavailable (DST won't roll, but
    # the system still runs). Bot logs a warning at startup in this case.
    ET_ZONE = timezone(timedelta(hours=-5))

# Internal alias kept for the session helpers below — they were written
# against the underscore-prefixed name.
_ET = ET_ZONE


# === Session window for US equities (default: RTH = Regular Trading Hours) ===
# Default: 09:30 - 16:00 ET, Mon-Fri.
#
# Tightened from the previous ETH (04:00-20:00) because the user's IBKR
# account routes pre/post-market orders through routes that frequently
# reject them — sending an exit at 16:15 fails, leaving a position
# unintentionally naked. RTH-only eliminates that class of failure.
#
# ENTRY_CUTOFF_BUFFER_MIN is the safety margin at end-of-session:
# the engine refuses to place new BUY entries within this many minutes
# of close AND cancels any working BUY orders when the buffer opens.
#
# Historical rationale (now obsolete): the 5-minute default existed
# because of the legacy non-bracket entry path — engine had to place
# the protective SL AFTER the BUY filled, and if a fill happened at
# 15:59:59 the SL placement would race the bell. Could fail and leave
# the position naked overnight.
#
# Post-bracket-migration: the entry is now a single bracket order
# (parent BUY STP-LMT + child SELL STP, atomic via parentId+transmit
# in `place_bracket_buy_stop_market`). The child rests at IBKR the
# instant the bracket is accepted — BEFORE the parent fills. So a
# last-second fill is already protected: there's no "engine places
# SL after fill" gap to worry about. We can keep entering right up
# to the bell. Default 0 → no cutoff buffer.
#
# Operators who still want a buffer (e.g. to avoid the last-minute
# closing-cross volatility hit) can re-enable via the env var:
#   GT_ENTRY_CUTOFF_BUFFER_MIN=5 python run_live.py ...
#
# All four knobs overridable via env so an operator can widen to ETH
# (e.g. GT_SESSION_END_HOUR=20) without code edits.
SESSION_START_HOUR_ET = int(os.environ.get("GT_SESSION_START_HOUR", "9"))
SESSION_START_MIN_ET = int(os.environ.get("GT_SESSION_START_MIN", "30"))
SESSION_END_HOUR_ET = int(os.environ.get("GT_SESSION_END_HOUR", "16"))
SESSION_END_MIN_ET = int(os.environ.get("GT_SESSION_END_MIN", "0"))
ENTRY_CUTOFF_BUFFER_MIN = int(os.environ.get("GT_ENTRY_CUTOFF_BUFFER_MIN", "0"))


def _et_minutes_of_day(et_dt: datetime) -> int:
    """ET wall-clock as minutes since midnight (hour×60 + minute).

    Used internally for window comparisons — keeps the math symmetric
    when window edges have minute precision (e.g. 9:30, not 9:00)."""
    return et_dt.hour * 60 + et_dt.minute


def session_is_open(now_utc: Optional[datetime] = None) -> bool:
    """Return True if `now_utc` falls inside the configured RTH window.

    "Open" means the engine is active — managing positions, placing
    protective stops, processing ticks. It does NOT distinguish
    entries-allowed vs entries-blocked; for the last-5-min entry
    cutoff use `entries_allowed()` instead.

    Args:
        now_utc: timezone-aware UTC datetime; defaults to datetime.now(UTC).

    Returns:
        True iff Mon–Fri (ET) and SESSION_START ≤ now < SESSION_END.

    Resting GTC orders at IBKR are NOT cancelled when the session
    closes — they keep protecting open positions until next-day RTH.
    """
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et = now.astimezone(_ET)
    # Mon = 0 ... Sun = 6
    if et.weekday() >= 5:
        return False
    start = SESSION_START_HOUR_ET * 60 + SESSION_START_MIN_ET
    end = SESSION_END_HOUR_ET * 60 + SESSION_END_MIN_ET
    return start <= _et_minutes_of_day(et) < end


def entries_allowed(now_utc: Optional[datetime] = None) -> bool:
    """Return True if new BUY entries may be placed right now.

    Stricter than `session_is_open`: also returns False during the
    `ENTRY_CUTOFF_BUFFER_MIN` minutes immediately before close. The
    last 5 minutes (default) are reserved for protective-stop placement
    on any in-flight fills — no new exposure starts in that window.

    The engine consults this in two places:
      1. Entry-placement path: refuses to send a new BUY if False.
      2. Session controller: when this transitions True→False mid-
         session, cancels any working BUY orders so they can't fill
         in the cutoff window.
    """
    if not session_is_open(now_utc):
        return False
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et = now.astimezone(_ET)
    cutoff_minute = (SESSION_END_HOUR_ET * 60 + SESSION_END_MIN_ET) - ENTRY_CUTOFF_BUFFER_MIN
    return _et_minutes_of_day(et) < cutoff_minute


def seconds_until_session_open(now_utc: Optional[datetime] = None) -> float:
    """Seconds from now until the next RTH session opens.

    Used by the session controller to sleep efficiently when the market
    is closed — wake up exactly when the window opens, not poll every
    minute. Returns 0 if already inside the window. Honors minute
    precision (9:30 is exactly 30min after the hour, not 9:00)."""
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if session_is_open(now):
        return 0.0
    et = now.astimezone(_ET)
    candidate = et.replace(
        hour=SESSION_START_HOUR_ET, minute=SESSION_START_MIN_ET,
        second=0, microsecond=0,
    )
    if candidate <= et:
        candidate = candidate + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate = candidate + timedelta(days=1)
    return max(0.0, (candidate - et).total_seconds())


def seconds_until_entry_cutoff(now_utc: Optional[datetime] = None) -> float:
    """Seconds until new entries become blocked at end-of-session.

    Returns 0 if we're already in the cutoff window OR outside RTH.
    Used by the session controller to wake at exactly the right moment
    (15:55 ET by default) to cancel pending BUY orders + flip the
    entries-allowed gate."""
    now = now_utc if now_utc is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not session_is_open(now):
        return 0.0
    et = now.astimezone(_ET)
    cutoff = et.replace(
        hour=SESSION_END_HOUR_ET, minute=SESSION_END_MIN_ET,
        second=0, microsecond=0,
    ) - timedelta(minutes=ENTRY_CUTOFF_BUFFER_MIN)
    if cutoff <= et:
        return 0.0
    return (cutoff - et).total_seconds()


# === Commission Constants (IBKR Tiered, ≤ 300k monthly volume tier) ===
#
# Earlier code used `filled_qty × $0.35` per side, which was MASSIVELY
# over-counting: a 50-share trade was billed at $17.50/side when IBKR
# actually charges only $0.35 (the per-order minimum). Real-world
# accuracy matters here because realized-P&L gates, commission rollups
# in the dashboards, and the daily-loss circuit breaker all read these
# numbers — overcounting commission means we under-report profitable
# strategies and over-block on loss limits.
#
# IBKR Tiered (Stock, US, ≤ 300,000 shares/month):
#   base       = max($0.35, min(qty × $0.0035, trade_value × 1%))
#   + regulatory (small, side-dependent — SEC + FINRA TAF apply on SELL only):
#       SEC Transaction Fee   = trade_value × 0.0000206   [SELL only]
#       FINRA TAF             = qty × 0.000195            [SELL only]
#       FINRA CAT             = qty × 0.000003            [both sides]
#       NSCC / DTC clearing   = qty × 0.00020             [both sides]
#
# Pass-through fees (NYSE / FINRA, computed against base commission)
# are < 0.1% and omitted — they round into noise vs the SEC + clearing
# numbers and would couple commission to exchange routing, which we
# don't track per-order yet. Easy to add later as another helper.
#
# Override the base rate via env var if you're on a different tier:
#   GT_IBKR_TIERED_RATE=0.0020  (300k–3M monthly volume)
import os as _os_for_commission
IBKR_TIERED_RATE = float(_os_for_commission.environ.get("GT_IBKR_TIERED_RATE", "0.0035"))
IBKR_MIN_PER_ORDER = float(_os_for_commission.environ.get("GT_IBKR_MIN_PER_ORDER", "0.35"))
IBKR_MAX_FRACTION = float(_os_for_commission.environ.get("GT_IBKR_MAX_FRACTION", "0.01"))

# Regulatory fee rates — IBKR pass-through, fixed by the regulators.
_SEC_RATE = 0.0000206       # × trade value, SELL only
_FINRA_TAF_RATE = 0.000195  # × qty, SELL only
_FINRA_CAT_RATE = 0.000003  # × qty, both sides
_CLEARING_RATE = 0.00020    # × qty, both sides


def calc_ibkr_base_commission(qty: int, price: float) -> float:
    """Tiered base commission only (no regulatory / clearing).

    Formula (in this exact order):
        1. raw   = qty × $0.0035
        2. floor = max($0.35, raw)          ← minimum-per-order kicks in
        3. final = min(floor, trade × 1%)   ← maximum-per-order caps it

    Order matters in the edge case where the 1% cap is LESS than the
    $0.35 minimum — i.e., for very small notional trades (penny stocks
    or tiny qty). IBKR's published rule is "Maximum per order: 1% of
    Trade Value" — that's a hard ceiling, so the cap wins over the
    floor when they conflict. Example: 5 sh × $0.50 = $2.50 trade,
    1% cap = $0.025, floor would say $0.35 — IBKR bills $0.025.

    The previous implementation did `max(MIN, min(raw, cap))` which
    over-billed those tiny trades by ~10×. For typical equity tickers
    ($50+) the cap never binds so the two orderings agree.

    Used independently in places that want to show 'just the IBKR fee'
    vs the full all-in cost. Callers that want the full picture should
    use `calc_ibkr_commission(qty, price, side)` instead.
    """
    if qty <= 0 or price <= 0:
        return 0.0
    raw = qty * IBKR_TIERED_RATE
    with_floor = max(IBKR_MIN_PER_ORDER, raw)
    return min(with_floor, qty * price * IBKR_MAX_FRACTION)


def calc_ibkr_regulatory_fees(qty: int, price: float, side: str) -> float:
    """SEC + FINRA TAF + FINRA CAT + NSCC/DTC clearing.

    `side` is the OrderSide value (BUY/SELL) — SEC + TAF only apply on
    sells. Defaults to no SEC/TAF when side is unknown (safer for
    display: under-reports rather than over-reports cost).
    """
    if qty <= 0 or price <= 0:
        return 0.0
    fees = 0.0
    # CAT and clearing apply both sides
    fees += qty * _FINRA_CAT_RATE
    fees += qty * _CLEARING_RATE
    # SEC + TAF — sales only
    if isinstance(side, str) and side.upper() == "SELL":
        fees += qty * price * _SEC_RATE
        fees += qty * _FINRA_TAF_RATE
    return fees


def calc_ibkr_commission(qty: int, price: float, side: str = "BUY") -> float:
    """Full IBKR all-in commission for one side of a trade.

    side=BUY:  base + CAT + clearing
    side=SELL: base + CAT + clearing + SEC + FINRA TAF

    Worked examples (sanity):
      50 sh @ $200 = $10,000 trade        (typical equity)
        raw=$0.175 → floor=$0.35 → cap=$100 → base = $0.35
        BUY  ≈ $0.36 ;  SELL ≈ $0.58

      1000 sh @ $50 = $50,000 trade       (larger entry)
        raw=$3.50 → floor=$3.50 → cap=$500 → base = $3.50
        BUY  ≈ $3.70 ;  SELL ≈ $4.92

      5 sh @ $0.50 = $2.50 trade          (penny stock edge case)
        raw=$0.0175 → floor=$0.35 → cap=$0.025 → base = $0.025
        Cap wins — IBKR can't charge more than 1% of trade value.
    """
    return calc_ibkr_base_commission(qty, price) + calc_ibkr_regulatory_fees(qty, price, side)


# ── Backward-compat shims ──────────────────────────────────────────────
# `COMMISSION_PER_SHARE` was used as a flat per-share rate (the bug).
# Kept as 0.35 — the *minimum per order* under tiered — so legacy code
# that does `qty × COMMISSION_PER_SHARE` still produces "something
# vaguely commission-shaped" and doesn't crash on import. New code MUST
# call `calc_ibkr_commission(qty, price, side)` for correctness. Every
# call site in the engine + dashboards has been migrated.
COMMISSION_PER_SHARE = float(_os_for_commission.environ.get("GT_COMMISSION_PER_SHARE", "0.35"))
MIN_COMMISSION = IBKR_MIN_PER_ORDER
ROUND_TRIP_COMMISSION = 0.0  # deprecated; depends on qty + price


class ConnectionStatus(str, Enum):
    """IBKR connection status."""
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    ERROR = "ERROR"


class OrderType(str, Enum):
    """Order types."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderSide(str, Enum):
    """Order sides."""
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    """Order status."""
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class OrderType(str, Enum):
    """Order types - senior quant grade."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"  # Trigger + limit price


class TradeState(str, Enum):
    """Trade state machine states."""
    IDLE = "IDLE"
    INITIALIZING = "INITIALIZING"
    CONNECTING = "CONNECTING"
    CHECKING_IBKR = "CHECKING_IBKR"
    MONITORING = "MONITORING"
    WAITING_REENTRY = "WAITING_REENTRY"
    PRE_TRADE_RISK = "PRE_TRADE_RISK"
    ORDER_ENTRY = "ORDER_ENTRY"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACKNOWLEDGED = "ORDER_ACKNOWLEDGED"
    IN_POSITION = "IN_POSITION"
    EXIT_POSITION = "EXIT_POSITION"
    STOP_LOSS_HIT = "STOP_LOSS_HIT"
    RECONNECTING = "RECONNECTING"
    CIRCUIT_BROKEN = "CIRCUIT_BROKEN"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    STOPPED = "STOPPED"


@dataclass(slots=True)
class Config:
    """Lean trading config - loaded from env vars only."""
    # IBKR
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4001
    ibkr_client_id: int = 1

    # Strategy
    ticker: str = "NVDA"
    # 0.0 is a SENTINEL meaning "not configured". run_live.py refuses to
    # start when trigger_price <= 0 and no state file is available — the
    # previous default of 225.00 silently kicked in when neither --trigger
    # nor GT_TRIGGER_PRICE was supplied, which placed real orders at $225
    # regardless of the actual symbol (very bad if you forgot the flag).
    trigger_price: float = 0.0
    stop_loss_pct: float = 0.01  # 1% stop loss
    # 0 is a SENTINEL meaning "not configured". run_live.py refuses to
    # start when quantity <= 0 and no state file is available. Position
    # sizing must be explicit — a silent default would happily place real
    # orders at the wrong size if --qty was forgotten.
    quantity: int = 0

    # Stop-limit buffer (trigger ↔ limit gap), modular across price ranges.
    # The effective offset is: max(sl_limit_offset, scale * context_price).
    # - SELL exit: context = (entry - stop) × offset_stop_fraction.
    #     The buffer scales with how far your stop sits below entry, so wider
    #     stops get wider limit buffers automatically.
    # - BUY entry: context = trigger_price × offset_entry_pct.
    #     The buffer scales with absolute price level (5 bps of $230 = $0.12,
    #     of $500 = $0.25), so high-priced stocks get a buffer wide enough to
    #     cross the bid-ask spread.
    # Set offset_stop_fraction=0 and offset_entry_pct=0 to disable scaling
    # and use the flat sl_limit_offset as a fixed-dollar buffer (old behavior).
    sl_limit_offset: float = 0.05         # minimum buffer in dollars (floor)
    offset_stop_fraction: float = 0.05    # 5% of stop distance, for SELL exits
    offset_entry_pct: float = 0.0005      # 5 bps of trigger, for BUY entries

    # ── Partial-fill handling (senior-quant pattern) ─────────────────
    # If a BUY entry fills in pieces (40 + 35 + ... ) and the remainder
    # doesn't complete, we want to bail quickly rather than wait
    # minutes with most of the position unprotected.
    #
    # Behavior:
    #   * Engine state updates on every partial (cumulative weighted avg).
    #   * Protective SELL stop is placed ONCE when the BUY fully completes
    #     (sized to total, stop computed from weighted-avg fill price).
    #     This matches the senior-quant rule: one SL per cycle, sized and
    #     priced from the true vwap of the entry.
    #   * If the BUY is partially filled and stuck for `partial_fill_timeout_s`,
    #     the chase task modifies the BUY's limit up by
    #     `partial_fill_chase_offset` (one IBKR `placeOrder` modify call —
    #     atomic, preserves existing partial fills).
    #   * After `partial_fill_max_chases` attempts, cancel the BUY remainder
    #     and place the SL on whatever did fill. The position is protected
    #     within ~`timeout * (max_chases + 1)` seconds worst case.
    #
    # Defaults — 10s × 2 chases = 20s worst-case naked window before the
    # protective stop lands (vs 180s with the old 60s × 3 defaults).
    # The next re-entry cycle will target `config.quantity` again — the
    # partial result of this cycle does not poison the qty of the next.
    #
    # `partial_fill_chase_offset = 0.0` is a SENTINEL meaning "auto: use
    # the same offset you used on entry". Rationale: if you decided
    # `sl_limit_offset=$0.20` was the right buffer for this symbol's
    # spread / liquidity, that's also the right amount to bump per chase
    # — otherwise a fixed-$0.05 chase on a $0.20-offset symbol can't
    # clear the same liquidity gap that needed $0.20 to enter.
    # Set GT_PARTIAL_FILL_CHASE_OFFSET=N explicitly to override (e.g.
    # `0.10` for a wider chase than entry, or `0.02` to chase smaller
    # than entry).
    partial_fill_timeout_s: float = 10.0
    partial_fill_chase_offset: float = 0.0
    partial_fill_max_chases: int = 2

    # Risk
    daily_loss_limit_pct: float = -0.90
    max_consecutive_losses: int = 300
    max_trades_per_day: int = 50
    # Hard dollar gates (override the older %-of-equity gates above).
    # `max_position_value_usd`: portfolio-wide cap on total FILLED
    # exposure. A new entry is rejected when (current filled exposure
    # across all bots + this order) would exceed the cap, so filled
    # exposure never crosses it. Pending/unfilled orders do NOT count.
    # `max_daily_loss_usd`: absolute daily-P&L floor — when realized
    # P&L for today drops to -$2,000, all new entries are blocked until
    # the daily reset. Does NOT replace the percentage gate above; both
    # fire independently, so whichever trips first wins.
    # Raised 2026-06-02: $25k → $50k per operator decision.
    # Raised 2026-07-03: $50k → $150k per operator decision.
    max_position_value_usd: float = 150000.0
    max_daily_loss_usd: float = 2000.0

    # Trading mode
    paper_trading: bool = True

    # --cfd: route this symbol as a generic CFD (contract-for-difference)
    # instead of the underlying equity/spot. The broker qualifies the real
    # CFD contract + minTick. Default False → equity/FX resolution unchanged.
    cfd: bool = False

    # Entry order type. Default False = the historical behaviour: rest a SELL
    # STP-LMT at `trigger_price` and let IBKR fire it when LTP falls to the
    # level. True = enter with a MARKET order the moment the engine places the
    # bracket, with no resting trigger at all.
    #
    # Use market entry ONLY when something upstream has already detected the
    # breakdown and launches this bot in response (e.g. the RTH trendline
    # runner). In that arrangement the trigger has by definition already been
    # met, so a resting stop is redundant — and a limit the tape has already
    # passed would simply never fill.
    #
    # The trade-off, stated plainly: a MARKET entry has no price floor. It
    # fills at whatever the book bids when it lands, which may be worse than
    # `trigger_price` if the market moved between the breakdown and this bot
    # connecting. STP-LMT can miss the trade; MARKET cannot miss it but can get
    # a worse price for it. `trigger_price` is still required — the protective
    # child cover is sized from it until the parent's real fill price is known.
    # Set via --market or GT_ENTRY_MARKET=1.
    entry_market: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        """Load config from environment variables."""
        return cls(
            ibkr_host=os.environ.get("GT_IBKR_HOST", "127.0.0.1"),
            ibkr_port=int(os.environ.get("GT_IBKR_PORT", "4002")),
            ibkr_client_id=int(os.environ.get("GT_IBKR_CLIENT_ID", "1")),
            ticker=os.environ.get("GT_TICKER", "NVDA"),
            trigger_price=float(os.environ.get("GT_TRIGGER_PRICE", "0.0")),
            stop_loss_pct=float(os.environ.get("GT_STOP_LOSS_PCT", "0.01")),
            sl_limit_offset=float(os.environ.get("GT_SL_LIMIT_OFFSET", "0.05")),
            offset_stop_fraction=float(os.environ.get("GT_OFFSET_STOP_FRACTION", "0.05")),
            offset_entry_pct=float(os.environ.get("GT_OFFSET_ENTRY_PCT", "0.0005")),
            partial_fill_timeout_s=float(os.environ.get("GT_PARTIAL_FILL_TIMEOUT_S", "10")),
            # 0.0 sentinel → at chase time, falls back to sl_limit_offset
            # so the chase bump matches the entry offset by default.
            partial_fill_chase_offset=float(os.environ.get("GT_PARTIAL_FILL_CHASE_OFFSET", "0.0")),
            partial_fill_max_chases=int(os.environ.get("GT_PARTIAL_FILL_MAX_CHASES", "2")),
            quantity=int(os.environ.get("GT_QUANTITY", "0")),
            daily_loss_limit_pct=float(os.environ.get("GT_DAILY_LOSS_LIMIT", "-0.02")),
            max_consecutive_losses=int(os.environ.get("GT_MAX_CONSECUTIVE_LOSSES", "300")),
            max_trades_per_day=int(os.environ.get("GT_MAX_TRADES", "50")),
            # NOTE: kept in sync with the dataclass default above (line ~447).
            # If you change one, change the other — they are independent
            # entry points (constructor default vs env-fallback) and a mismatch
            # silently caps you at the lower of the two.
            max_position_value_usd=float(os.environ.get("GT_MAX_POSITION_VALUE_USD", "150000")),
            max_daily_loss_usd=float(os.environ.get("GT_MAX_DAILY_LOSS_USD", "2000")),
            paper_trading=os.environ.get("GT_PAPER", "true").lower() == "true",
            cfd=os.environ.get("GT_CFD", "").strip() in ("1", "true", "True"),
            entry_market=os.environ.get("GT_ENTRY_MARKET", "").strip()
            in ("1", "true", "True"),
        )


@dataclass(slots=True)
class Order:
    """Lean order representation."""
    order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    avg_fill_price: Optional[float] = None
    created_at: datetime = field(default_factory=datetime.now)


@dataclass(slots=True)
class Position:
    """Lean position representation."""
    symbol: str
    quantity: int
    avg_cost: float
    market_value: float = 0.0

    def unrealized_pnl(self, current_price: float) -> float:
        return (current_price - self.avg_cost) * self.quantity


@dataclass(slots=True)
class TradeContext:
    """Trade cycle context - persisted to disk."""
    trade_id: str = ""
    ticker: str = ""
    entry_price: Optional[float] = None
    entry_time: Optional[datetime] = None
    stop_loss_price: Optional[float] = None
    highest_price: Optional[float] = None
    quantity: int = 0
    state: TradeState = TradeState.IDLE
    consecutive_losses: int = 0
    reentry_count: int = 0
    daily_pnl: float = 0.0

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "ticker": self.ticker,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "stop_loss_price": self.stop_loss_price,
            "highest_price": self.highest_price,
            "quantity": self.quantity,
            "state": self.state.value,
            "consecutive_losses": self.consecutive_losses,
            "reentry_count": self.reentry_count,
            "daily_pnl": self.daily_pnl,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TradeContext":
        return cls(
            trade_id=data.get("trade_id", ""),
            ticker=data.get("ticker", ""),
            entry_price=data.get("entry_price"),
            entry_time=datetime.fromisoformat(data["entry_time"]) if data.get("entry_time") else None,
            stop_loss_price=data.get("stop_loss_price"),
            highest_price=data.get("highest_price"),
            quantity=data.get("quantity", 0),
            state=TradeState(data.get("state", "IDLE")),
            consecutive_losses=data.get("consecutive_losses", 0),
            reentry_count=data.get("reentry_count", 0),
            daily_pnl=data.get("daily_pnl", 0.0),
        )


@dataclass(slots=True)
class OrderRecord:
    """Immutable order record. Tracks full lifecycle."""
    order_id: str
    symbol: str
    side: OrderSide
    qty: int
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None       # For LIMIT/STOP_LIMIT orders
    stop_price: Optional[float] = None       # For STOP/STOP_LIMIT orders (trigger)
    status: OrderStatus = OrderStatus.PENDING
    submitted_at: datetime = field(default_factory=datetime.now)
    filled_at: Optional[datetime] = None
    avg_fill_price: Optional[float] = None
    filled_qty: int = 0
    commission: float = 0.0
    signal_price: Optional[float] = None      # Price when signal was generated
    # Running cumulative commission as REPORTED by IBKR via the
    # commissionReport event, across all partial fills of this order.
    # `None` = no broker report seen yet → fall back to the modeled
    # `calc_ibkr_commission` formula. This is the path live-mode takes:
    # for every fillEvent ib_async also exposes
    # `fill.commissionReport.commission` (IBKR's authoritative all-in
    # number, post all regulatory + clearing fees). Paper mode + offline
    # replay leave this None and the formula fallback takes over so the
    # display still shows a sensible estimate.
    broker_commission: Optional[float] = None

    def calculate_commission(self) -> float:
        """Per-side commission for the cumulative filled qty.

        Source precedence (live mode):
          1. `self.broker_commission`  ← IBKR's authoritative number,
             populated by the broker layer from
             `fill.commissionReport.commission` for each execution and
             accumulated across partial fills. All-in: base + SEC +
             FINRA TAF + CAT + clearing + pass-throughs. Penny-exact.
          2. Modeled `calc_ibkr_commission(filled_qty, avg_fill_price,
             side)` — used in paper mode, replay, and the brief window
             before IBKR's commissionReport lands. Close to reality but
             approximate (pass-through fees are omitted, side-dependent
             SEC/TAF are estimated from avg_fill_price).

        Both paths return a RUNNING CUMULATIVE for everything filled so
        far on this order, so the engine's `_pending_buy_commission`
        diff at engine.py:505 keeps working unchanged whichever source
        is active.
        """
        # Live IBKR path — exact number from the broker.
        if self.broker_commission is not None and self.broker_commission > 0:
            return float(self.broker_commission)
        # Modeled fallback.
        if self.filled_qty == 0 or not self.avg_fill_price or self.avg_fill_price <= 0:
            return 0.0
        side_str = self.side.value if hasattr(self.side, "value") else str(self.side)

        # Multi-asset: route through the symbol's AssetSpec commission
        # policy when available. The legacy `calc_ibkr_commission` formula
        # is equity-specific ($0.0035/share + SEC/FINRA); applying it to
        # a 25,000-unit EURUSD trade returns $92 — IBKR's actual FX fee
        # is 0.20bps of notional = ~$5.86. Same blowup for ES futures
        # ($0.85/contract flat) and CFDs. Resolve the spec by symbol;
        # if the spec is non-equity, use it. Equity (and any unknown
        # symbol) keeps the byte-identical legacy path so historical
        # commission numbers in audit logs don't shift retroactively.
        try:
            from src.assets import resolve as _resolve_spec
            from src.assets.enum import AssetClass as _AC
            from src.assets.types import Quantity as _Q, price as _P
            from decimal import Decimal as _D
            spec = _resolve_spec(self.symbol)
            if spec.asset_class is not _AC.US_EQUITY:
                qty_typed = _Q(_D(str(self.filled_qty)), spec.sizing.expected_unit)
                price_typed = _P(str(self.avg_fill_price))
                # `Side` is Literal["BUY","SELL"] — just pass the string.
                money = spec.commission.estimate(qty_typed, price_typed, side_str)
                return float(money.amount)
        except Exception:
            # Spec resolution / commission policy failed — fall back to
            # the legacy formula rather than reporting $0. Worst case
            # the operator sees an inflated equity-style estimate, which
            # is the pre-multi-asset behaviour.
            pass

        return calc_ibkr_commission(self.filled_qty, float(self.avg_fill_price), side_str)

    def is_complete(self) -> bool:
        """Order is fully filled."""
        return self.filled_qty >= self.qty and self.status == OrderStatus.FILLED


class OrderRegistry:
    """Thread-safe order tracking with idempotent fill recording.

    Hardening over the original "pure Python dict":
        - threading.Lock protects mutating ops (submit/on_fill/on_cancel).
          IBKR fillEvent and statusEvent fire from the ib_async event
          thread; the engine accesses the registry from the asyncio loop
          thread; reconnect can race with normal updates. A single
          fine-grained lock around the mutation block is plenty for our
          throughput (~10s of order ops per cycle, not 10K).
        - on_fill() is idempotent on (order_id, exec_id). IBKR replays
          fills on reconnect — without dedup, `filled_qty += qty` would
          double-count and corrupt P&L. exec_id is the per-execution
          unique key IBKR provides (fill.execution.execId); we keep a
          bounded LRU set of seen IDs per order to discard replays.
    """

    __slots__ = ('_orders', '_ts', '_lock', '_seen_execs')

    # Cap the per-order seen-exec set so a long-running partial-fill
    # stream doesn't grow unbounded. 256 is way more than any real order
    # will see (partial fills usually number in the single digits even
    # for liquidity-providing strategies).
    _EXEC_DEDUP_PER_ORDER = 256

    def __init__(self):
        import threading
        self._orders: dict[str, OrderRecord] = {}
        self._ts = datetime.now
        self._lock = threading.Lock()
        # order_id → set of execution_ids we've already applied
        self._seen_execs: dict[str, set] = {}

    def submit(self, order: OrderRecord) -> str:
        """Register a new order."""
        with self._lock:
            self._orders[order.order_id] = order
            # Reset dedup for re-used order_ids (the engine reuses the
            # same engine_id across cycles e.g. SL_SELL_1_NVDA).
            self._seen_execs.pop(order.order_id, None)
        return order.order_id

    def get(self, order_id: str) -> Optional[OrderRecord]:
        """Get order by ID. Read is lock-free — dict.get is GIL-atomic
        and OrderRecord fields settle quickly after writes."""
        return self._orders.get(order_id)

    def on_fill(self, order_id: str, qty: int, price: float, exec_id: Optional[str] = None,
                fill_time: Optional[datetime] = None,
                broker_commission: Optional[float] = None):
        """Record partial or full fill. Idempotent on exec_id.

        When `exec_id` is provided (live IBKR path), duplicate calls with
        the same exec_id are silently no-op'd. Without exec_id (paper sim,
        legacy callers) the fill is applied unconditionally — paper has no
        replay scenario so dedup isn't needed there.

        `fill_time` (optional) is the broker's authoritative fill timestamp
        (e.g. `fill.execution.time` from ib_async). When provided, it is
        stored as the order's `filled_at` so the audit row and dashboard
        reflect the real broker fill time, not the moment our code happened
        to process it. Critical for replayed fills after a disconnect —
        without it the audit log lies about when the fill actually happened.
        Defaults to `self._ts()` (now) for the normal live-fill path where
        "now" is within microseconds of the broker timestamp anyway.

        `broker_commission` (optional, in dollars) is IBKR's reported
        commission for THIS execution alone, taken from
        `fill.commissionReport.commission`. We accumulate it onto
        `order.broker_commission` so that across all partial fills the
        order ends up with the exact total IBKR charged. None for paper
        mode + the brief window where commissionReport hasn't landed
        yet — `calculate_commission` falls back to the modeled formula
        in that case.
        """
        with self._lock:
            if order_id not in self._orders:
                return
            order = self._orders[order_id]

            # Dedup on exec_id if provided
            if exec_id is not None:
                seen = self._seen_execs.setdefault(order_id, set())
                if exec_id in seen:
                    return  # already applied, IBKR replayed
                # Bound the set; if it ever overflows in pathological
                # cases, drop the oldest by recreating with the most
                # recent half (cheap, rare).
                if len(seen) >= self._EXEC_DEDUP_PER_ORDER:
                    seen.clear()
                seen.add(exec_id)

            order.filled_qty += qty

            # Calculate running average fill price
            if order.filled_qty > 0:
                prev_total = (order.avg_fill_price or 0) * (order.filled_qty - qty)
                order.avg_fill_price = (prev_total + price * qty) / order.filled_qty

            # Accumulate IBKR's reported commission for this execution,
            # if any. Across partial fills these add up to the order's
            # true total. Skipping when 0/None preserves the formula
            # fallback path in `calculate_commission`.
            if broker_commission is not None and broker_commission > 0:
                order.broker_commission = (order.broker_commission or 0.0) + float(broker_commission)

            # Check if fully filled
            if order.filled_qty >= order.qty:
                order.status = OrderStatus.FILLED
                # Strip tzinfo if caller supplied a tz-aware datetime (ib_async
                # fills are UTC-aware). We do NOT convert UTC→local — preserve
                # the broker's native clock values stripped of tz.
                if fill_time is not None:
                    if getattr(fill_time, 'tzinfo', None) is not None:
                        order.filled_at = fill_time.replace(tzinfo=None)
                    else:
                        order.filled_at = fill_time
                else:
                    order.filled_at = self._ts()
                order.commission = order.calculate_commission()

    def on_cancel(self, order_id: str):
        """Mark order as cancelled."""
        with self._lock:
            if order_id in self._orders:
                self._orders[order_id].status = OrderStatus.CANCELLED

    def on_reject(self, order_id: str):
        """Mark order as rejected (terminal, not filled)."""
        with self._lock:
            if order_id in self._orders:
                self._orders[order_id].status = OrderStatus.REJECTED

    def get_filled_orders(self, symbol: str = None) -> list[OrderRecord]:
        """Get all filled orders, optionally filtered by symbol."""
        return [
            o for o in self._orders.values()
            if o.status == OrderStatus.FILLED
            and (symbol is None or o.symbol == symbol)
        ]

    def total_commission(self, symbol: str = None) -> float:
        """Total commission paid."""
        return sum(
            o.commission for o in self.get_filled_orders(symbol)
        )

    def get_today_trades(self, symbol: str = None) -> int:
        """Count of filled trades today."""
        today = self._ts().date()
        return len([
            o for o in self._orders.values()
            if o.submitted_at.date() == today
            and o.status == OrderStatus.FILLED
            and (symbol is None or o.symbol == symbol)
        ])

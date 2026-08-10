#!/usr/bin/env python3
"""
EOD Excel Report — multi-tab institutional-grade trading report.
================================================================

A single-command Excel workbook for senior MD review. Reads from the
combined audit folder produced by `combine_audit.py` (data/audit/<DATE>/
_combined/) plus per-ticker feed.csv files for OHLC.

OUTPUT: data/reports/EOD_<YYYY-MM-DD>.xlsx

TABS:
    1. Dashboard         Headline KPIs, equity curve, strategy + ticker
                         breakdown, anomaly summary.
    2. Trade Log         Every fill with strategy column, P&L, slippage,
                         commission, cycle id, sortable + filterable.
    3. Open Positions    Shorts still held at session end with unrealized
                         P&L, SL distance, trough, hold duration.
    4. Pending Orders    Working orders still resting at IBKR.
    5. Previous Orders   Closed / cancelled / rejected audit trail.
    6. PnL Account       Portfolio-level: gross/net/commission/slippage,
                         equity curve, drawdown, strategy attribution.
    7. PnL Ticker        Per-ticker breakdown: cycles, hit rate, gross,
                         commission, net, best/worst, avg hold, volume.
    8. OHLC              Per-ticker session OHLC, VWAP, range, tick count.

DESIGN:
    Pure standard library for data loading (csv, gzip).
    openpyxl for Excel — the standard Python library for .xlsx.
    OOP — each tab is a TabBuilder subclass; one StyleRegistry centralizes
    every fill, font, border, and number format.
    Palette — shades of black + light pastels. Professional, readable,
    not garish.
    Strategy column — present on every relevant tab. Currently hardcoded
    to "strat1"; designed so future strategies just need a `strategy_of(...)`
    classifier function call.

USAGE:
    python3 scripts/eod_excel_report.py                    # today
    python3 scripts/eod_excel_report.py --date 2026-05-27  # specific
    python3 scripts/eod_excel_report.py --date 20260527    # YYYYMMDD
    python3 scripts/eod_excel_report.py --out report.xlsx  # custom path
    python3 scripts/eod_excel_report.py --account "Acct U12345"

The script is READ-ONLY against audit data. It only WRITES the .xlsx
output file. Safe to run during market hours or after close.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import statistics
import subprocess
import sys
from abc import ABC, abstractmethod
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ── Dependency check ──────────────────────────────────────────────────────
try:
    from openpyxl import Workbook
    from openpyxl.styles import (
        Font, PatternFill, Alignment, Border, Side, NamedStyle,
    )
    from openpyxl.utils import get_column_letter
    from openpyxl.chart import LineChart, BarChart, PieChart, Reference
    from openpyxl.chart.label import DataLabelList
    from openpyxl.formatting.rule import (
        ColorScaleRule, DataBarRule, CellIsRule, FormulaRule,
    )
    from openpyxl.worksheet.table import Table, TableStyleInfo
    from openpyxl.comments import Comment
except ImportError:
    print(
        "ERROR: openpyxl not installed.\n"
        "Install with:  pip install openpyxl\n"
        "Or:            pip install openpyxl --break-system-packages   (if PEP 668)",
        file=sys.stderr,
    )
    sys.exit(1)


# ── Paths ─────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_ROOT = REPO_ROOT / "data" / "audit"
REPORT_ROOT = REPO_ROOT / "data" / "reports"


# ══════════════════════════════════════════════════════════════════════════
#                              STYLE REGISTRY
# ══════════════════════════════════════════════════════════════════════════

class Palette:
    """Refined institutional palette — desaturated tones, monochromatic
    primary, single accent per semantic role.

    Design principle: shades of black + warm-grey neutrals carry the
    information hierarchy; muted pastels are reserved for semantic
    coding (positive/negative/info/warning). The result reads like a
    Bloomberg terminal-export rather than a colorful spreadsheet.

    ARGB hex strings (FF<RGB>) per openpyxl convention.
    """
    # ── Primary: monochromatic, warm-grey based ──────────────────────
    INK = "FF111827"            # near-black, page headers (deeper than #1E1E1E)
    GRAPHITE = "FF1F2937"       # secondary headers
    CHARCOAL = "FF374151"       # body emphasis
    SLATE = "FF4B5563"           # secondary body
    GREY = "FF6B7280"            # labels, captions
    GREY_MED = "FF9CA3AF"        # muted secondary
    GREY_LINE = "FFE5E7EB"       # hairline borders, section rules
    GREY_BG = "FFF3F4F6"         # subtle band background
    OFF_WHITE = "FFF9FAFB"       # zebra-stripe shade (1% grey)
    WHITE = "FFFFFFFF"

    # ── Semantic accents (desaturated for institutional feel) ────────
    # Positive / wins
    POS_INK = "FF065F46"         # dark green text
    POS_TINT = "FFD1FAE5"        # light green background
    POS_BAR = "FF10B981"         # chart data-bar color

    # Negative / losses
    NEG_INK = "FF991B1B"         # dark red text
    NEG_TINT = "FFFEE2E2"        # light red background
    NEG_BAR = "FFEF4444"         # chart data-bar color

    # Info / neutral focus
    INFO_INK = "FF1E40AF"        # dark blue text
    INFO_TINT = "FFDBEAFE"       # light blue background

    # Warning / pending
    WARN_INK = "FFB45309"        # dark amber
    WARN_TINT = "FFFEF3C7"       # light amber

    # Special / strategy / categorical
    SPEC_INK = "FF6D28D9"        # dark violet
    SPEC_TINT = "FFEDE9FE"       # light violet

    # Teal — used for analytics / OHLC
    TEAL_INK = "FF0F766E"
    TEAL_TINT = "FFCCFBF1"

    # ── Single chart accent (used everywhere a chart needs one color) ─
    CHART_PRIMARY = "FF1F2937"   # dark monochrome line
    CHART_FILL = "FF93C5FD"      # soft fill under area chart
    CHART_DRAWDOWN = "FFEF4444"  # drawdown red

    # ── Legacy aliases (kept so existing tab code doesn't break) ─────
    BLACK = INK
    GREY_LIGHT = GREY_LINE
    GREY_LIGHTER = GREY_BG
    PASTEL_GREEN = POS_TINT
    PASTEL_GREEN_DEEP = POS_BAR
    PASTEL_RED = NEG_TINT
    PASTEL_RED_DEEP = NEG_BAR
    PASTEL_BLUE = INFO_TINT
    PASTEL_BLUE_DEEP = INFO_INK
    PASTEL_YELLOW = WARN_TINT
    PASTEL_PURPLE = SPEC_TINT
    PASTEL_TEAL = TEAL_TINT
    PASTEL_PEACH = "FFFFEDD5"


class StyleRegistry:
    """Centralized cell-style factory.

    Design choices for institutional/quant feel:
      * Inter-like sans-serif (Calibri stays — it ships with Excel and looks
        clean at all sizes).
      * Strong typographic hierarchy: 24pt → 12pt → 10pt → 8pt.
      * Minimal borders. We use light-grey RULE LINES under headers and
        between sections instead of box borders around every cell —
        information density without cage-like grids.
      * Whitespace as separator: empty rows between sections, generous
        row heights on titles and KPI cards.
      * Semantic fills only on cells that encode meaning (P&L sign,
        status tags). Other cells stay white or off-white.
    """

    # ── Fonts (typographic hierarchy) ────────────────────────────────
    F_HERO = Font(name="Calibri", size=36, bold=True, color=Palette.INK)
    F_HERO_POS = Font(name="Calibri", size=36, bold=True, color=Palette.POS_INK)
    F_HERO_NEG = Font(name="Calibri", size=36, bold=True, color=Palette.NEG_INK)
    F_HERO_LABEL = Font(name="Calibri", size=9, bold=True, color=Palette.GREY,
                        )  # uppercase label above hero
    F_TITLE = Font(name="Calibri", size=22, bold=True, color=Palette.INK)
    F_SUBTITLE = Font(name="Calibri", size=10, color=Palette.GREY, italic=False)
    F_SECTION = Font(name="Calibri", size=11, bold=True, color=Palette.INK)
    F_SECTION_TAG = Font(name="Calibri", size=8, bold=True, color=Palette.GREY)
    F_HEADER = Font(name="Calibri", size=9, bold=True, color=Palette.WHITE)
    F_BODY = Font(name="Calibri", size=10, color=Palette.CHARCOAL)
    F_BODY_BOLD = Font(name="Calibri", size=10, bold=True, color=Palette.INK)
    F_BODY_DIM = Font(name="Calibri", size=10, color=Palette.GREY)
    F_SMALL = Font(name="Calibri", size=9, color=Palette.GREY)
    F_KPI_LABEL = Font(name="Calibri", size=8, bold=True, color=Palette.GREY)
    F_KPI_VALUE = Font(name="Calibri", size=18, bold=True, color=Palette.INK)
    F_KPI_POS = Font(name="Calibri", size=18, bold=True, color=Palette.POS_INK)
    F_KPI_NEG = Font(name="Calibri", size=18, bold=True, color=Palette.NEG_INK)
    F_KPI_DELTA = Font(name="Calibri", size=9, color=Palette.GREY, italic=True)
    F_FOOTER = Font(name="Calibri", size=8, color=Palette.GREY, italic=True)
    F_TOTAL_LABEL = Font(name="Calibri", size=10, bold=True, color=Palette.INK)

    # ── Fills ─────────────────────────────────────────────────────────
    FILL_HEADER_BLACK = PatternFill("solid", fgColor=Palette.INK)
    FILL_HEADER_GRAPHITE = PatternFill("solid", fgColor=Palette.GRAPHITE)
    FILL_ZEBRA = PatternFill("solid", fgColor=Palette.OFF_WHITE)
    FILL_KPI_CARD = PatternFill("solid", fgColor=Palette.GREY_BG)
    FILL_TOTAL_ROW = PatternFill("solid", fgColor=Palette.GREY_BG)
    FILL_GREEN = PatternFill("solid", fgColor=Palette.POS_TINT)
    FILL_RED = PatternFill("solid", fgColor=Palette.NEG_TINT)
    FILL_BLUE = PatternFill("solid", fgColor=Palette.INFO_TINT)
    FILL_YELLOW = PatternFill("solid", fgColor=Palette.WARN_TINT)
    FILL_PURPLE = PatternFill("solid", fgColor=Palette.SPEC_TINT)
    FILL_TEAL = PatternFill("solid", fgColor=Palette.TEAL_TINT)
    FILL_PEACH = PatternFill("solid", fgColor=Palette.PASTEL_PEACH)
    FILL_SECTION_BAR = PatternFill("solid", fgColor=Palette.GREY_LINE)
    FILL_NONE = PatternFill(fill_type=None)

    # ── Alignments ────────────────────────────────────────────────────
    ALIGN_LEFT = Alignment(horizontal="left", vertical="center", indent=1)
    ALIGN_LEFT_FLUSH = Alignment(horizontal="left", vertical="center")
    ALIGN_RIGHT = Alignment(horizontal="right", vertical="center", indent=1)
    ALIGN_CENTER = Alignment(horizontal="center", vertical="center")
    ALIGN_KPI_LABEL = Alignment(horizontal="left", vertical="bottom", indent=1)
    ALIGN_KPI_VALUE = Alignment(horizontal="left", vertical="center", indent=1)
    ALIGN_HERO = Alignment(horizontal="left", vertical="center", indent=1)

    # ── Borders (minimal — used only as rule lines, not cages) ───────
    SIDE_HAIRLINE = Side(style="thin", color=Palette.GREY_LINE)
    SIDE_MED = Side(style="thin", color=Palette.GREY_MED)
    SIDE_THICK = Side(style="medium", color=Palette.INK)
    BORDER_BOTTOM_HAIRLINE = Border(bottom=SIDE_HAIRLINE)
    BORDER_BOTTOM_MED = Border(bottom=SIDE_MED)
    BORDER_BOTTOM_THICK = Border(bottom=SIDE_THICK)
    BORDER_TOP_HAIRLINE = Border(top=SIDE_HAIRLINE)
    BORDER_TOP_THICK = Border(top=SIDE_THICK)
    BORDER_NONE = Border()

    # ── Number formats ───────────────────────────────────────────────
    FMT_MONEY = '_($* #,##0.00_);[Red]_($* (#,##0.00);_(* "-"??_);_(@_)'
    FMT_MONEY_SIGNED = '+$#,##0.00;-$#,##0.00;$0.00'
    FMT_MONEY_K = '$#,##0.00,"K";-$#,##0.00,"K";$0'   # display in thousands
    FMT_INT = '#,##0;-#,##0;0'
    FMT_PCT = '0.0%;[Red]-0.0%;0.0%'
    FMT_PCT_SIGNED = '+0.00%;-0.00%;0.00%'
    FMT_PCT_2 = '0.00%'
    FMT_PX = '#,##0.0000'
    FMT_BPS = '+0.0" bps";-0.0" bps";0.0" bps"'
    FMT_RATIO = '0.00'
    FMT_TIME = 'hh:mm:ss'
    FMT_DATETIME = 'yyyy-mm-dd hh:mm:ss'
    FMT_DURATION = '[h]"h "mm"m"'


# ══════════════════════════════════════════════════════════════════════════
#                              DATA LOADER
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Cycle:
    """One round-trip: entry BUY → exit SELL. Computed from FILLED rows."""
    cycle_id: str
    ticker: str
    strategy: str
    entry_time: Optional[datetime] = None
    entry_price: float = 0.0
    entry_qty: int = 0
    entry_commission: float = 0.0
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    exit_qty: int = 0
    exit_commission: float = 0.0
    peak_price: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    exit_reason: str = ""
    closed: bool = False

    @property
    def hold_seconds(self) -> float:
        if self.entry_time and self.exit_time:
            return (self.exit_time - self.entry_time).total_seconds()
        return 0.0

    @property
    def round_trip_commission(self) -> float:
        return self.entry_commission + self.exit_commission


class AuditDataLoader:
    """Loads combined + per-ticker audit CSVs from the date folder.

    Pure stdlib CSV reading — no pandas. Handles transparent .gz reading
    on the per-ticker feed.csv files.
    """

    def __init__(self, date_folder: Path):
        self.date_folder = date_folder
        self.combined_dir = date_folder / "_combined"

        self.orders: List[Dict[str, str]] = self._read(self.combined_dir / "order.csv")
        self.states: List[Dict[str, str]] = self._read(self.combined_dir / "state.csv")
        self.pnls: List[Dict[str, str]] = self._read(self.combined_dir / "pnl.csv")

        # Tickers, sorted alphabetically.
        seen = set()
        order = []
        for r in self.orders:
            t = (r.get("ticker") or "").strip()
            if t and t not in seen:
                seen.add(t)
                order.append(t)
        self.tickers: List[str] = sorted(order)

        # Computed once for reuse:
        self.cycles: List[Cycle] = self._build_cycles()

    # ── CSV reading ──────────────────────────────────────────────────
    @staticmethod
    def _read(path: Path) -> List[Dict[str, str]]:
        if not path.exists():
            return []
        try:
            with open(path, "r", newline="", encoding="utf-8", errors="replace") as f:
                return list(csv.DictReader(f))
        except Exception as e:
            print(f"WARN: failed to read {path}: {e}", file=sys.stderr)
            return []

    @staticmethod
    def _read_gz_or_plain(path_base: Path) -> List[Dict[str, str]]:
        """Read <path_base>.csv or <path_base>.csv.gz, whichever exists."""
        plain = path_base.with_suffix(".csv")
        gz = Path(str(path_base) + ".csv.gz")
        target = plain if plain.exists() else (gz if gz.exists() else None)
        if target is None:
            return []
        opener = gzip.open if target.suffix == ".gz" else open
        try:
            with opener(target, "rt", newline="", encoding="utf-8", errors="replace") as f:
                return list(csv.DictReader(f))
        except Exception as e:
            print(f"WARN: failed to read {target}: {e}", file=sys.stderr)
            return []

    def load_feed_for_ticker(self, ticker: str) -> List[Dict[str, str]]:
        """Load the per-ticker feed.csv (tick stream). Used for OHLC."""
        ticker_dir = self.date_folder / ticker
        for candidate in (ticker_dir / "feed.csv", ticker_dir / "feed.csv.gz"):
            if candidate.exists():
                opener = gzip.open if candidate.suffix == ".gz" else open
                try:
                    with opener(candidate, "rt", newline="", encoding="utf-8",
                                errors="replace") as f:
                        return list(csv.DictReader(f))
                except Exception as e:
                    print(f"WARN: failed to read {candidate}: {e}", file=sys.stderr)
                    return []
        return []

    # ── Helpers ──────────────────────────────────────────────────────
    @staticmethod
    def parse_ts(s: str) -> Optional[datetime]:
        """Parse an ISO-8601 timestamp into a tz-NAIVE datetime.

        Mixed tz handling: the audit pipeline writes both tz-aware UTC
        (from ib_async fill events) and tz-naive local-clock timestamps
        depending on the source. To make all comparisons safe across
        the report, we strip tzinfo on every parse so everything sorts
        as naive datetimes.
        """
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            return dt
        except Exception:
            return None

    @staticmethod
    def f(x, default=0.0) -> float:
        if x is None or x == "":
            return default
        try:
            return float(x)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def i(x, default=0) -> int:
        if x is None or x == "":
            return default
        try:
            return int(float(x))
        except (ValueError, TypeError):
            return default

    @staticmethod
    def strategy_of(order_id: str) -> str:
        """Classify an order to a strategy name. Currently all orders are
        produced by 'strat1' (the fixed-stop-loss breakout strategy).
        Extend this when additional strategies come online — match on
        engine_id prefix or another classifier."""
        return "strat1"

    # ── Cycle assembly ───────────────────────────────────────────────
    def _build_cycles(self) -> List[Cycle]:
        """Pair FILLED BUYs with FILLED SELLs (FIFO per ticker) into
        round-trip cycles. The SELL FILLED row carries the engine's
        round-trip pnl figure — we use it directly rather than recomputing
        so the report matches the engine's own books."""
        cycles: List[Cycle] = []
        # Group fills by ticker, ordered by timestamp.
        per_ticker: Dict[str, List[Dict]] = defaultdict(list)
        for r in self.orders:
            if r.get("event") != "FILLED":
                continue
            t = (r.get("ticker") or "").strip()
            if t:
                per_ticker[t].append(r)
        for t, rows in per_ticker.items():
            rows.sort(key=lambda r: r.get("timestamp", ""))
            open_buys: List[Cycle] = []
            for r in rows:
                side = (r.get("side") or "").strip()
                ts = self.parse_ts(r.get("timestamp", ""))
                px = self.f(r.get("fill_price"))
                qty = self.i(r.get("qty"))
                comm = self.f(r.get("commission"))
                pnl = self.f(r.get("pnl"))
                oid = r.get("order_id") or ""
                # Cycle id is everything after the _n suffix (the cycle_seq fix);
                # if no suffix, fall back to the full order id.
                if "_n" in oid:
                    cycle_id = oid.rsplit("_n", 1)[-1]
                    cycle_id = f"{t}-c{cycle_id}"
                else:
                    cycle_id = oid
                strategy = self.strategy_of(oid)

                if side == "BUY":
                    c = Cycle(cycle_id=cycle_id, ticker=t, strategy=strategy)
                    c.entry_time = ts
                    c.entry_price = px
                    c.entry_qty = qty
                    c.entry_commission = comm
                    open_buys.append(c)
                elif side == "SELL" and open_buys:
                    # Pair with the OLDEST open buy (FIFO).
                    c = open_buys.pop(0)
                    c.exit_time = ts
                    c.exit_price = px
                    c.exit_qty = qty
                    c.exit_commission = comm
                    c.net_pnl = pnl  # engine-computed round-trip net pnl
                    c.gross_pnl = (px - c.entry_price) * qty
                    c.exit_reason = (r.get("reason") or "").strip()
                    c.closed = True
                    cycles.append(c)
            # Any remaining open_buys are unclosed positions for the day.
            for c in open_buys:
                cycles.append(c)
        # Sort chronologically by entry time
        cycles.sort(key=lambda c: c.entry_time or datetime.min)
        return cycles

    # ── Convenience views ────────────────────────────────────────────
    def filled_orders(self) -> List[Dict]:
        return [r for r in self.orders if r.get("event") == "FILLED"]

    def submitted_orders(self) -> List[Dict]:
        return [r for r in self.orders if r.get("event") == "SUBMITTED"]

    def cancelled_orders(self) -> List[Dict]:
        return [r for r in self.orders
                if r.get("event") in ("CANCELLED", "REJECTED")]

    def anomaly_orders(self) -> List[Dict]:
        anom = {
            "CHILD_STOP_MODIFY_FAILED", "ORPHAN_CANCEL_VERIFY_TIMEOUT",
            "PHANTOM_SELL_REJECTED", "SHORTING_PREVENTED",
            "BRACKET_CHILD_CANCELLED", "STOP_LOSS_MARKET_FALLBACK",
            "CIRCUIT_BREAK",
        }
        return [r for r in self.orders if r.get("event") in anom]


# ══════════════════════════════════════════════════════════════════════════
#                            TAB BUILDER BASE
# ══════════════════════════════════════════════════════════════════════════

class TabBuilder(ABC):
    """Abstract base for one tab in the workbook."""

    name: str = "Tab"
    tab_color: str = "FF1E1E1E"  # the tab strip color at the bottom

    def __init__(self, wb: Workbook, loader: AuditDataLoader,
                 style: StyleRegistry, date_str: str, account: str):
        self.wb = wb
        self.loader = loader
        self.style = style
        self.date_str = date_str  # YYYY-MM-DD
        self.account = account

    def create_sheet(self):
        ws = self.wb.create_sheet(self.name)
        ws.sheet_properties.tabColor = self.tab_color
        ws.sheet_view.showGridLines = False
        return ws

    @abstractmethod
    def build(self) -> None:
        ...

    # ── Common helpers ───────────────────────────────────────────────
    def add_sheet_title(self, ws, row: int, title: str, subtitle: str = None) -> int:
        """Write the standard sheet title block with a thin underline.

        Returns next free row (after a blank row for breathing space).
        """
        # Title
        t = ws.cell(row=row, column=1, value=title)
        t.font = self.style.F_TITLE
        t.alignment = self.style.ALIGN_LEFT_FLUSH
        ws.row_dimensions[row].height = 36
        # Subtitle on the row below
        next_row = row + 1
        if subtitle:
            s = ws.cell(row=next_row, column=1, value=subtitle)
            s.font = self.style.F_SUBTITLE
            s.alignment = self.style.ALIGN_LEFT_FLUSH
            ws.row_dimensions[next_row].height = 16
            next_row += 1
        # Thin INK underline that spans columns A..J on the row below
        rule_row = next_row
        for c in range(1, 12):
            ws.cell(row=rule_row, column=c).border = self.style.BORDER_BOTTOM_THICK
        ws.row_dimensions[rule_row].height = 6
        # Empty breathing row
        return rule_row + 2

    def add_section_header(self, ws, row: int, label: str, cols_span: int = 1,
                            tag: str = None, subtitle: str = None,
                            count: int = None) -> int:
        """Section header with thin INK rule line beneath (institutional feel).

        Layout:
            [BIG LABEL]                                  [count badge] [TAG]
            [optional italic subtitle ─ explains the section]
            ─────────────────────────────── (hairline rule)

        Args:
            label    : main section heading
            cols_span: how many columns the rule line spans
            tag      : small uppercase right-aligned label (e.g. "P&L")
            subtitle : one-line italic description shown under the label
            count    : if set, shown as "N=12" badge to the right of label
        """
        # Label
        c1 = ws.cell(row=row, column=1, value=label)
        c1.font = self.style.F_SECTION
        c1.alignment = self.style.ALIGN_LEFT_FLUSH
        ws.row_dimensions[row].height = 22

        # N=count badge (institutional habit — readers immediately know
        # how big the row-set is before scanning)
        if count is not None and cols_span > 1:
            badge_col = max(2, cols_span - (3 if tag else 0))
            bc = ws.cell(row=row, column=badge_col,
                         value=f"N = {count:,}")
            bc.font = self.style.F_SECTION_TAG
            bc.alignment = self.style.ALIGN_RIGHT

        # Right-edge tag (small grey uppercase)
        if tag and cols_span > 1:
            c_tag = ws.cell(row=row, column=cols_span, value=tag.upper())
            c_tag.font = self.style.F_SECTION_TAG
            c_tag.alignment = self.style.ALIGN_RIGHT

        # Optional subtitle row
        next_row = row + 1
        if subtitle:
            sc = ws.cell(row=next_row, column=1, value=subtitle)
            sc.font = self.style.F_SMALL
            sc.alignment = self.style.ALIGN_LEFT_FLUSH
            ws.row_dimensions[next_row].height = 14
            next_row += 1

        # Thin rule line under the label/subtitle block
        for c in range(1, cols_span + 1):
            ws.cell(row=next_row, column=c).border = self.style.BORDER_BOTTOM_HAIRLINE
        ws.row_dimensions[next_row].height = 4
        return next_row + 1

    def add_blank_row(self, ws, row: int, height: float = 10) -> int:
        """Add an empty spacer row of the given height. Returns next row."""
        ws.row_dimensions[row].height = height
        return row + 1

    def write_table(self, ws, start_row: int, headers: List[str],
                    rows: List[List[Any]], col_widths: List[float] = None,
                    money_cols: Iterable[int] = (),
                    pct_cols: Iterable[int] = (),
                    int_cols: Iterable[int] = (),
                    px_cols: Iterable[int] = (),
                    signed_money_cols: Iterable[int] = (),
                    pnl_color_col: Optional[int] = None,
                    pnl_databar_col: Optional[int] = None,
                    add_filter: bool = True,
                    header_height: float = 26,
                    body_height: float = 20) -> int:
        """Institutional-style table.

        Visual choices:
          * Headers: bold white on INK fill, generous height (26pt default).
          * Body: no per-cell borders; only a single hairline rule below
            the header band. Whitespace + alternating fill carry the
            row structure.
          * Subtle zebra: 1% grey on odd body rows.
          * P&L coloring: only the colored TEXT (not fill) for the P&L cell,
            unless `pnl_databar_col` is given — in which case data bars
            replace the color fill (more sophisticated, less "highlighter").
          * Optional auto-filter + frozen header.

        Returns first free row AFTER the table.
        """
        money_cols = set(money_cols)
        pct_cols = set(pct_cols)
        int_cols = set(int_cols)
        px_cols = set(px_cols)
        signed_money_cols = set(signed_money_cols)
        numeric_cols = money_cols | pct_cols | int_cols | px_cols | signed_money_cols

        # Header band
        for c, h in enumerate(headers, 1):
            cell = ws.cell(row=start_row, column=c, value=h)
            cell.font = self.style.F_HEADER
            cell.fill = self.style.FILL_HEADER_BLACK
            cell.alignment = self.style.ALIGN_CENTER
        ws.row_dimensions[start_row].height = header_height
        # Hairline rule under the header
        rule_row = start_row  # the header's own bottom border
        for c in range(1, len(headers) + 1):
            ws.cell(row=rule_row, column=c).border = self.style.BORDER_BOTTOM_HAIRLINE

        # Body
        for ri, row in enumerate(rows, 1):
            r = start_row + ri
            ws.row_dimensions[r].height = body_height
            # Zebra stripe (subtle off-white)
            zebra = (ri % 2 == 0)
            for ci, v in enumerate(row, 1):
                cell = ws.cell(row=r, column=ci, value=v)
                cell.font = self.style.F_BODY
                cell.alignment = (self.style.ALIGN_RIGHT
                                  if ci in numeric_cols
                                  else self.style.ALIGN_LEFT)
                # Number formats
                if ci in money_cols:
                    cell.number_format = self.style.FMT_MONEY
                elif ci in signed_money_cols:
                    cell.number_format = self.style.FMT_MONEY_SIGNED
                elif ci in pct_cols:
                    cell.number_format = self.style.FMT_PCT
                elif ci in int_cols:
                    cell.number_format = self.style.FMT_INT
                elif ci in px_cols:
                    cell.number_format = self.style.FMT_PX
                if zebra:
                    cell.fill = self.style.FILL_ZEBRA

            # P&L coloring — change TEXT color (not fill) for institutional feel.
            # The fill version is reserved for status badges.
            if pnl_color_col is not None and pnl_color_col <= len(row):
                v = row[pnl_color_col - 1]
                if isinstance(v, (int, float)) and v != 0:
                    pnl_cell = ws.cell(row=r, column=pnl_color_col)
                    pnl_cell.font = (Font(name="Calibri", size=10, bold=True,
                                          color=Palette.POS_INK) if v > 0
                                     else Font(name="Calibri", size=10, bold=True,
                                               color=Palette.NEG_INK))

        # Final bottom rule under the last data row — visual closure for the table
        if rows:
            last_data_row = start_row + len(rows)
            for c in range(1, len(headers) + 1):
                ws.cell(row=last_data_row, column=c).border = self.style.BORDER_BOTTOM_HAIRLINE

        # Conditional data bars on a P&L column (sophisticated alternative to fill)
        if pnl_databar_col is not None and rows:
            from openpyxl.formatting.rule import DataBarRule, FormulaRule
            col_letter = get_column_letter(pnl_databar_col)
            rng = f"{col_letter}{start_row + 1}:{col_letter}{start_row + len(rows)}"
            # Positive bar (green)
            ws.conditional_formatting.add(
                rng,
                DataBarRule(start_type="num", start_value=0,
                            end_type="max", color=Palette.POS_BAR[2:],
                            showValue=True, gradient=False),
            )
            # Negative bar (red) via separate rule for cells < 0
            ws.conditional_formatting.add(
                rng,
                DataBarRule(start_type="min", start_value=0,
                            end_type="num", end_value=0,
                            color=Palette.NEG_BAR[2:],
                            showValue=True, gradient=False),
            )

        # Column widths
        if col_widths:
            for ci, w in enumerate(col_widths, 1):
                ws.column_dimensions[get_column_letter(ci)].width = w

        # Freeze header row + auto-filter
        ws.freeze_panes = ws.cell(row=start_row + 1, column=1).coordinate
        if add_filter and rows:
            last_col_letter = get_column_letter(len(headers))
            ws.auto_filter.ref = f"A{start_row}:{last_col_letter}{start_row + len(rows)}"

        return start_row + len(rows) + 2

    def write_hero_metric(self, ws, row: int, label: str, value: float,
                          fmt: str, color: str = "neutral") -> int:
        """The single biggest number on the page — e.g. Net P&L.

        Pattern:
            <small uppercase label>
            <huge number>
            <thin underline rule>
        """
        # Label
        lc = ws.cell(row=row, column=1, value=label.upper())
        lc.font = self.style.F_HERO_LABEL
        lc.alignment = self.style.ALIGN_HERO
        ws.row_dimensions[row].height = 14

        # Big value
        vc = ws.cell(row=row + 1, column=1, value=value)
        if color == "positive" or (color == "neutral" and isinstance(value, (int, float)) and value > 0):
            vc.font = self.style.F_HERO_POS
        elif color == "negative" or (color == "neutral" and isinstance(value, (int, float)) and value < 0):
            vc.font = self.style.F_HERO_NEG
        else:
            vc.font = self.style.F_HERO
        vc.alignment = self.style.ALIGN_HERO
        vc.number_format = fmt
        ws.row_dimensions[row + 1].height = 48
        return row + 3

    def write_kpi_grid(self, ws, start_row: int, items: List[Tuple],
                       cols: int = 4) -> int:
        """Card grid: each item = (label, value, fmt, kind)
        kind ∈ {"money", "money_signed", "int", "pct", "text", "ratio"}.
        Returns the next free row.
        """
        # Each card spans 2 worksheet columns horizontally; each card uses 3 rows
        # (label, value, spacer). Multiple cards laid out in `cols` columns.
        rows_needed = -(-len(items) // cols)  # ceiling division
        for i, (label, value, fmt, kind) in enumerate(items):
            r_block = i // cols  # 0, 1, 2 ...
            c_block = i % cols   # 0..cols-1
            r = start_row + r_block * 4
            c = 1 + c_block * 2

            # Label (small uppercase grey)
            lc = ws.cell(row=r, column=c, value=label.upper())
            lc.font = self.style.F_KPI_LABEL
            lc.fill = self.style.FILL_KPI_CARD
            lc.alignment = self.style.ALIGN_KPI_LABEL
            ws.row_dimensions[r].height = 16

            # Value (big bold)
            vc = ws.cell(row=r + 1, column=c, value=value)
            if kind in ("money", "money_signed") and isinstance(value, (int, float)):
                vc.font = (self.style.F_KPI_POS if value > 0
                           else self.style.F_KPI_NEG if value < 0
                           else self.style.F_KPI_VALUE)
                vc.number_format = (self.style.FMT_MONEY_SIGNED
                                    if kind == "money_signed" else self.style.FMT_MONEY)
            elif kind == "int":
                vc.font = self.style.F_KPI_VALUE
                vc.number_format = self.style.FMT_INT
            elif kind == "pct":
                vc.font = self.style.F_KPI_VALUE
                vc.number_format = self.style.FMT_PCT
            elif kind == "ratio":
                vc.font = self.style.F_KPI_VALUE
                vc.number_format = self.style.FMT_RATIO
            else:  # text
                vc.font = self.style.F_KPI_VALUE
            vc.fill = self.style.FILL_KPI_CARD
            vc.alignment = self.style.ALIGN_KPI_VALUE
            ws.row_dimensions[r + 1].height = 32

            # Thin rule under the card
            rc = ws.cell(row=r + 2, column=c)
            rc.fill = self.style.FILL_KPI_CARD
            rc.border = self.style.BORDER_BOTTOM_HAIRLINE
            ws.row_dimensions[r + 2].height = 4

            # Merge label and value across 2 columns for breathing room
            ws.merge_cells(start_row=r, start_column=c, end_row=r, end_column=c + 1)
            ws.merge_cells(start_row=r + 1, start_column=c, end_row=r + 1, end_column=c + 1)
            ws.merge_cells(start_row=r + 2, start_column=c, end_row=r + 2, end_column=c + 1)

        return start_row + rows_needed * 4 + 1


# ══════════════════════════════════════════════════════════════════════════
#                             1. DASHBOARD TAB
# ══════════════════════════════════════════════════════════════════════════

class DashboardTab(TabBuilder):
    """Senior-MD dashboard. Hero metric → quant KPI grid → equity curve →
    drawdown → distribution → strategy & ticker breakdowns → anomalies."""

    name = "Dashboard"
    tab_color = "FF111827"

    def build(self) -> None:
        ws = self.create_sheet()
        # Column widths: A is wider (for labels), then uniform body cols
        ws.column_dimensions["A"].width = 24
        for col in "BCDEFGHIJ":
            ws.column_dimensions[col].width = 16

        # Title
        row = self.add_sheet_title(
            ws, 1,
            "GT SYSTEM",
            f"End-of-day report  ·  {self.date_str}  ·  {self.account}  ·  "
            f"generated {datetime.now():%Y-%m-%d %H:%M:%S}",
        )

        # ── HERO METRIC ────────────────────────────────────────────
        kpis = self._compute_kpis()
        row = self.write_hero_metric(
            ws, row,
            label=f"Net P&L  ·  {kpis['trades']} cycles  ·  {kpis['wins']}W / {kpis['losses']}L  ·  hit {kpis['hit_rate']*100:.1f}%",
            value=kpis["net_pnl"],
            fmt=self.style.FMT_MONEY_SIGNED,
            color="neutral",
        )
        row = self.add_blank_row(ws, row, 6)

        # ── PRIMARY KPI GRID (4 across × 2 rows = 8 metrics) ────────
        row = self.add_section_header(
            ws, row, "Performance Snapshot",
            cols_span=8, tag="session",
            subtitle="Core P&L and execution metrics for the session.",
        )
        primary_kpis = [
            ("Gross P&L",       kpis["gross_pnl"],   self.style.FMT_MONEY_SIGNED, "money_signed"),
            ("Commission",      -kpis["commission"], self.style.FMT_MONEY,        "money_signed"),
            ("Slippage",        -kpis["slippage"],   self.style.FMT_MONEY,        "money_signed"),
            ("Max Drawdown",    kpis["max_dd"],      self.style.FMT_MONEY_SIGNED, "money_signed"),
            ("Avg Cycle P&L",   kpis["avg_cycle"],   self.style.FMT_MONEY_SIGNED, "money_signed"),
            ("Notional Traded", kpis["notional"],    self.style.FMT_MONEY,        "money"),
            ("Avg Hold",        kpis["avg_hold"],    "",                          "text"),
            ("Tickers Traded",  kpis["n_tickers"],   self.style.FMT_INT,          "int"),
        ]
        row = self.write_kpi_grid(ws, row, primary_kpis, cols=4)
        row = self.add_blank_row(ws, row, 12)

        # ── EQUITY CURVE + DRAWDOWN (stacked) ──────────────────────
        row = self.add_section_header(
            ws, row, "Equity Curve & Drawdown",
            cols_span=10, tag="cumulative",
            subtitle="Net P&L accrual per closed cycle (top) and underwater drawdown from running peak (bottom).",
        )
        eq_data, dd_data = self._equity_and_drawdown()
        chart_anchor_row = row

        # Hidden data block (columns L, M, N — to the right, out of sight)
        DATA_COL = 12
        ws.cell(row=chart_anchor_row, column=DATA_COL, value="Cycle").font = self.style.F_SMALL
        ws.cell(row=chart_anchor_row, column=DATA_COL + 1, value="Equity").font = self.style.F_SMALL
        ws.cell(row=chart_anchor_row, column=DATA_COL + 2, value="Drawdown").font = self.style.F_SMALL
        for i, (eq, dd) in enumerate(zip(eq_data, dd_data), 1):
            ws.cell(row=chart_anchor_row + i, column=DATA_COL, value=i)
            ws.cell(row=chart_anchor_row + i, column=DATA_COL + 1, value=eq).number_format = self.style.FMT_MONEY_SIGNED
            ws.cell(row=chart_anchor_row + i, column=DATA_COL + 2, value=dd).number_format = self.style.FMT_MONEY_SIGNED

        if eq_data:
            # Equity line chart (area-style)
            eq_chart = LineChart()
            eq_chart.title = None
            eq_chart.style = 2
            eq_chart.height = 7
            eq_chart.width = 18
            eq_chart.y_axis.title = None
            eq_chart.x_axis.title = None
            eq_chart.legend = None
            data_ref = Reference(ws, min_col=DATA_COL + 1, min_row=chart_anchor_row,
                                 max_col=DATA_COL + 1, max_row=chart_anchor_row + len(eq_data))
            cats_ref = Reference(ws, min_col=DATA_COL, min_row=chart_anchor_row + 1,
                                 max_col=DATA_COL, max_row=chart_anchor_row + len(eq_data))
            eq_chart.add_data(data_ref, titles_from_data=True)
            eq_chart.set_categories(cats_ref)
            self._style_chart_line(eq_chart, 0, Palette.CHART_PRIMARY)
            ws.add_chart(eq_chart, f"A{chart_anchor_row + 1}")

            # Drawdown chart below the equity chart
            dd_chart = LineChart()
            dd_chart.title = None
            dd_chart.style = 2
            dd_chart.height = 4
            dd_chart.width = 18
            dd_chart.y_axis.title = None
            dd_chart.x_axis.title = None
            dd_chart.legend = None
            dd_ref = Reference(ws, min_col=DATA_COL + 2, min_row=chart_anchor_row,
                               max_col=DATA_COL + 2, max_row=chart_anchor_row + len(eq_data))
            dd_chart.add_data(dd_ref, titles_from_data=True)
            dd_chart.set_categories(cats_ref)
            self._style_chart_line(dd_chart, 0, Palette.CHART_DRAWDOWN)
            ws.add_chart(dd_chart, f"A{chart_anchor_row + 16}")

        # Skip past chart area
        row = chart_anchor_row + 26

        # ── P&L DISTRIBUTION HISTOGRAM ────────────────────────────
        cycles_count = len([c for c in self.loader.cycles if c.closed])
        row = self.add_section_header(
            ws, row, "Cycle P&L Distribution",
            cols_span=8, tag="histogram",
            subtitle="Cycles bucketed by net P&L. Read the shape: tall left tail = many small losses, fat right tail = a few big winners.",
            count=cycles_count,
        )
        bins, counts = self._pnl_distribution()
        if bins:
            hist_anchor = row
            ws.cell(row=hist_anchor, column=DATA_COL, value="Bin").font = self.style.F_SMALL
            ws.cell(row=hist_anchor, column=DATA_COL + 1, value="Count").font = self.style.F_SMALL
            for i, (bin_label, count) in enumerate(zip(bins, counts), 1):
                ws.cell(row=hist_anchor + i, column=DATA_COL, value=bin_label)
                ws.cell(row=hist_anchor + i, column=DATA_COL + 1, value=count)

            hist = BarChart()
            hist.title = None
            hist.style = 2
            hist.height = 6
            hist.width = 18
            hist.legend = None
            hist.y_axis.title = "Cycles"
            hist.x_axis.title = "P&L Bucket ($)"
            h_data = Reference(ws, min_col=DATA_COL + 1, min_row=hist_anchor,
                               max_col=DATA_COL + 1, max_row=hist_anchor + len(bins))
            h_cats = Reference(ws, min_col=DATA_COL, min_row=hist_anchor + 1,
                               max_col=DATA_COL, max_row=hist_anchor + len(bins))
            hist.add_data(h_data, titles_from_data=True)
            hist.set_categories(h_cats)
            self._style_chart_bar(hist, 0, Palette.CHART_PRIMARY)
            ws.add_chart(hist, f"A{hist_anchor + 1}")
            row = hist_anchor + 14

        # ── PER-STRATEGY P&L ──────────────────────────────────────
        row = self.add_section_header(
            ws, row, "Per-Strategy P&L",
            cols_span=9, tag="attribution",
            subtitle="P&L attribution by strategy. Add new strategy classifiers in AuditDataLoader.strategy_of() as they come online.",
        )
        strat = self._strategy_breakdown()
        s_headers = ["Strategy", "Cycles", "Wins", "Losses", "Hit Rate",
                     "Gross P&L", "Commission", "Slippage", "Net P&L"]
        s_rows = []
        for s, st in sorted(strat.items()):
            s_rows.append([
                s, st["cycles"], st["wins"], st["losses"],
                (st["wins"] / st["cycles"] if st["cycles"] else 0),
                st["gross"], st["commission"], st["slippage"], st["net"],
            ])
        row = self.write_table(
            ws, row, s_headers, s_rows,
            col_widths=[14, 9, 9, 9, 11, 14, 14, 14, 14],
            money_cols=[7, 8], signed_money_cols=[6, 9],
            int_cols=[2, 3, 4], pct_cols=[5],
            pnl_color_col=9,
        )

        # ── PER-TICKER SNAPSHOT ────────────────────────────────────
        n_tickers = len({c.ticker for c in self.loader.cycles})
        row = self.add_section_header(
            ws, row, "Per-Ticker Snapshot",
            cols_span=10, tag="by symbol",
            subtitle="Cycle stats per symbol with end-of-session position status.",
            count=n_tickers,
        )
        per_t = self._ticker_breakdown()
        t_headers = ["Ticker", "Strategy", "Cycles", "Hit Rate",
                     "Net P&L", "Best Cycle", "Worst Cycle",
                     "Avg Hold", "Notional", "Status"]
        t_rows = []
        for t in sorted(per_t.keys()):
            d = per_t[t]
            t_rows.append([
                t, d["strategy"], d["cycles"],
                (d["wins"] / d["cycles"] if d["cycles"] else 0),
                d["net"], d["best"], d["worst"], self._fmt_hold(d["avg_hold"]),
                d["notional"], "OPEN" if d["open"] else "FLAT",
            ])
        row = self.write_table(
            ws, row, t_headers, t_rows,
            col_widths=[10, 10, 9, 11, 14, 14, 14, 11, 16, 10],
            money_cols=[9], signed_money_cols=[5, 6, 7],
            int_cols=[3], pct_cols=[4],
            pnl_color_col=5,
        )

        # Color the STATUS column based on OPEN/FLAT
        first_status_row = row - len(t_rows) - 1
        for ri, t_row in enumerate(t_rows):
            r = first_status_row + ri
            status_cell = ws.cell(row=r, column=10)
            if t_row[9] == "OPEN":
                status_cell.font = Font(name="Calibri", size=10, bold=True,
                                        color=Palette.INFO_INK)
                status_cell.fill = self.style.FILL_BLUE
                status_cell.alignment = self.style.ALIGN_CENTER

        # ── ANOMALIES ──────────────────────────────────────────────
        anomalies = self.loader.anomaly_orders()
        row = self.add_section_header(
            ws, row, "Operational Anomalies",
            cols_span=10, tag="ops",
            subtitle="Engine-side safety events that fired during the session. Empty = clean run.",
            count=len(anomalies) if anomalies else None,
        )
        if not anomalies:
            cell = ws.cell(row=row, column=1,
                           value="✓  Clean session — no anomalies, no manual interventions.")
            cell.font = Font(name="Calibri", size=10, bold=True, color=Palette.POS_INK)
            cell.fill = self.style.FILL_GREEN
            cell.alignment = self.style.ALIGN_LEFT
            ws.row_dimensions[row].height = 24
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=10)
            row += 2
        else:
            a_headers = ["Time", "Ticker", "Event", "Order ID", "Detail"]
            a_rows = []
            for a in anomalies[:30]:
                ts = a.get("timestamp", "")[:19]
                a_rows.append([ts, a.get("ticker", ""), a.get("event", ""),
                               a.get("order_id", ""),
                               (a.get("reason") or "")[:80]])
            row = self.write_table(
                ws, row, a_headers, a_rows,
                col_widths=[20, 10, 28, 30, 60],
            )

        # ── FOOTER ────────────────────────────────────────────────
        row = self.add_blank_row(ws, row, 12)
        f_cell = ws.cell(row=row, column=1,
                          value=f"Source: data/audit/{self.date_str.replace('-','')}/_combined/  ·  "
                                f"Engine: {self._git_sha()}  ·  "
                                f"Strategy: strat1 (fixed-stop-loss breakout re-entry)")
        f_cell.font = self.style.F_FOOTER
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=10)

    # ── KPI computation ─────────────────────────────────────────────
    def _compute_kpis(self) -> Dict[str, Any]:
        cycles = [c for c in self.loader.cycles if c.closed]
        if not cycles:
            return {"net_pnl": 0, "gross_pnl": 0, "commission": 0, "slippage": 0,
                    "trades": 0, "wins": 0, "losses": 0, "hit_rate": 0,
                    "max_dd": 0, "avg_hold": "—", "avg_cycle": 0,
                    "notional": 0, "n_tickers": 0}
        net_pnl = sum(c.net_pnl for c in cycles)
        gross_pnl = sum(c.gross_pnl for c in cycles)
        commission = sum(c.round_trip_commission for c in cycles)
        slip = sum(abs(self.loader.f(r.get("slippage")))
                   for r in self.loader.filled_orders())
        wins = sum(1 for c in cycles if c.net_pnl > 0)
        losses = sum(1 for c in cycles if c.net_pnl <= 0)
        hit = (wins / len(cycles)) if cycles else 0
        avg_hold = sum(c.hold_seconds for c in cycles) / len(cycles)
        notional = sum(c.entry_price * c.entry_qty for c in cycles)
        n_tickers = len({c.ticker for c in cycles})
        # Max drawdown of cumulative net pnl
        cum = peak = max_dd = 0.0
        for c in cycles:
            cum += c.net_pnl
            peak = max(peak, cum)
            max_dd = min(max_dd, cum - peak)
        return {
            "net_pnl": net_pnl, "gross_pnl": gross_pnl,
            "commission": commission, "slippage": slip,
            "trades": len(cycles), "wins": wins, "losses": losses,
            "hit_rate": hit, "max_dd": max_dd,
            "avg_hold": self._fmt_hold(avg_hold),
            "avg_cycle": net_pnl / len(cycles),
            "notional": notional, "n_tickers": n_tickers,
        }

    def _compute_quant_metrics(self) -> Dict[str, Any]:
        """Quant-grade trade metrics: profit factor, expectancy, Sharpe, etc."""
        cycles = [c for c in self.loader.cycles if c.closed]
        empty = {k: 0.0 for k in (
            "profit_factor", "expectancy", "avg_win", "avg_loss",
            "win_loss_ratio", "sharpe", "sortino",
            "largest_win", "largest_loss",
        )}
        if not cycles:
            return empty
        pnls = [c.net_pnl for c in cycles]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses)) or 1e-9
        profit_factor = gross_win / gross_loss

        avg_win = (gross_win / len(wins)) if wins else 0.0
        avg_loss = (sum(losses) / len(losses)) if losses else 0.0  # negative
        hit = len(wins) / len(cycles)
        # Expectancy: per-trade expected P&L
        expectancy = hit * avg_win + (1 - hit) * avg_loss
        # Win/loss ratio
        wl = (avg_win / abs(avg_loss)) if avg_loss else 0.0
        # Sharpe: mean / stdev of cycle returns. Cycle as the time unit.
        if len(pnls) > 1:
            mean = sum(pnls) / len(pnls)
            var = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
            stdev = var ** 0.5
            sharpe = (mean / stdev) if stdev > 0 else 0.0
            # Sortino: downside-deviation only
            downside = [p for p in pnls if p < mean]
            if len(downside) > 1:
                d_var = sum((p - mean) ** 2 for p in downside) / (len(downside) - 1)
                d_std = d_var ** 0.5
                sortino = (mean / d_std) if d_std > 0 else 0.0
            else:
                sortino = 0.0
        else:
            sharpe = sortino = 0.0
        largest_win = max(pnls)
        largest_loss = min(pnls)
        return {
            "profit_factor": profit_factor,
            "expectancy": expectancy,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "win_loss_ratio": wl,
            "sharpe": sharpe,
            "sortino": sortino,
            "largest_win": largest_win,
            "largest_loss": largest_loss,
        }

    # ── Data series for charts ──────────────────────────────────────
    def _equity_and_drawdown(self) -> Tuple[List[float], List[float]]:
        cycles = sorted([c for c in self.loader.cycles if c.closed],
                        key=lambda x: x.exit_time or datetime.min)
        eq = []
        dd = []
        cum = peak = 0.0
        for c in cycles:
            cum += c.net_pnl
            peak = max(peak, cum)
            eq.append(cum)
            dd.append(cum - peak)
        return eq, dd

    def _pnl_distribution(self, n_bins: int = 12) -> Tuple[List[str], List[int]]:
        cycles = [c.net_pnl for c in self.loader.cycles if c.closed]
        if not cycles:
            return [], []
        lo, hi = min(cycles), max(cycles)
        if lo == hi:
            return [f"{lo:+.0f}"], [len(cycles)]
        # Symmetric bins around zero if data spans both
        edges = []
        step = (hi - lo) / n_bins
        for i in range(n_bins + 1):
            edges.append(lo + i * step)
        counts = [0] * n_bins
        for p in cycles:
            idx = min(n_bins - 1, max(0, int((p - lo) / step)))
            counts[idx] += 1
        labels = []
        for i in range(n_bins):
            a, b = edges[i], edges[i + 1]
            labels.append(f"{a:+.0f}…{b:+.0f}")
        return labels, counts

    @staticmethod
    def _style_chart_line(chart, series_idx: int, color: str) -> None:
        """Apply institutional styling to a LineChart series."""
        from openpyxl.chart.shapes import GraphicalProperties
        from openpyxl.drawing.line import LineProperties
        from openpyxl.drawing.fill import ColorChoice
        try:
            s = chart.series[series_idx]
            line = LineProperties(solidFill=color[2:], w=18000)  # ~1.5pt
            s.graphicalProperties = GraphicalProperties(ln=line)
            # Hide markers for a clean line
            from openpyxl.chart.marker import Marker
            s.marker = Marker(symbol="none")
        except Exception:
            pass

    @staticmethod
    def _style_chart_bar(chart, series_idx: int, color: str) -> None:
        from openpyxl.chart.shapes import GraphicalProperties
        from openpyxl.drawing.fill import ColorChoice
        try:
            s = chart.series[series_idx]
            s.graphicalProperties = GraphicalProperties(solidFill=color[2:])
        except Exception:
            pass

    # ── Breakdowns ──────────────────────────────────────────────────
    def _strategy_breakdown(self) -> Dict[str, Dict]:
        out: Dict[str, Dict] = defaultdict(lambda: {
            "cycles": 0, "wins": 0, "losses": 0, "gross": 0.0, "net": 0.0,
            "commission": 0.0, "slippage": 0.0, "best": 0.0, "worst": 0.0,
        })
        for c in self.loader.cycles:
            if not c.closed:
                continue
            d = out[c.strategy]
            d["cycles"] += 1
            d["gross"] += c.gross_pnl
            d["net"] += c.net_pnl
            d["commission"] += c.round_trip_commission
            if c.net_pnl > 0:
                d["wins"] += 1
            else:
                d["losses"] += 1
            d["best"] = max(d["best"], c.net_pnl)
            d["worst"] = min(d["worst"], c.net_pnl)
        # Slippage attributed per strategy via filled orders
        for r in self.loader.filled_orders():
            d = out[self.loader.strategy_of(r.get("order_id", ""))]
            d["slippage"] += abs(self.loader.f(r.get("slippage")))
        return out

    def _ticker_breakdown(self) -> Dict[str, Dict]:
        out: Dict[str, Dict] = defaultdict(lambda: {
            "strategy": "strat1", "cycles": 0, "wins": 0, "net": 0.0,
            "best": 0.0, "worst": 0.0, "avg_hold": 0.0, "notional": 0.0, "open": False,
        })
        holds_by_ticker: Dict[str, List[float]] = defaultdict(list)
        for c in self.loader.cycles:
            d = out[c.ticker]
            d["strategy"] = c.strategy
            if c.closed:
                d["cycles"] += 1
                d["net"] += c.net_pnl
                d["best"] = max(d["best"], c.net_pnl)
                d["worst"] = min(d["worst"], c.net_pnl)
                d["notional"] += c.entry_price * c.entry_qty
                holds_by_ticker[c.ticker].append(c.hold_seconds)
                if c.net_pnl > 0:
                    d["wins"] += 1
            else:
                d["open"] = True
                d["notional"] += c.entry_price * c.entry_qty
        for t, holds in holds_by_ticker.items():
            if holds:
                out[t]["avg_hold"] = sum(holds) / len(holds)
        return out

    def _equity_curve_data(self) -> List[float]:
        cum = 0.0
        out = []
        for c in sorted([c for c in self.loader.cycles if c.closed],
                        key=lambda x: x.exit_time or datetime.min):
            cum += c.net_pnl
            out.append(cum)
        return out

    @staticmethod
    def _fmt_hold(seconds: float) -> str:
        if not seconds or seconds < 1:
            return "—"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        if h:
            return f"{h}h{m:02d}m"
        if m:
            return f"{m}m{s:02d}s"
        return f"{s}s"

    @staticmethod
    def _git_sha() -> str:
        try:
            out = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=REPO_ROOT, stderr=subprocess.DEVNULL, timeout=2,
            )
            return out.decode().strip()
        except Exception:
            return "n/a"


# ══════════════════════════════════════════════════════════════════════════
#                            2. TRADE LOG TAB
# ══════════════════════════════════════════════════════════════════════════

class TradeLogTab(TabBuilder):
    name = "Trade Log"
    tab_color = "FF374151"

    def build(self) -> None:
        ws = self.create_sheet()
        n_fills = len(self.loader.filled_orders())
        row = self.add_sheet_title(
            ws, 1,
            "Trade Log",
            f"{self.date_str}  ·  {self.account}  ·  "
            f"{n_fills} fills across {len(self.loader.tickers)} tickers  ·  "
            f"every BUY and SELL execution with strategy tag, sorted by P&L impact",
        )

        headers = [
            "Timestamp", "Strategy", "Ticker", "Side", "Order Type", "Qty",
            "Signal Px", "Fill Px", "Slippage ($)", "Slippage (bps)",
            "Commission", "Gross P&L", "Net P&L", "Cycle ID",
            "Engine Order ID", "State at Time", "Reason",
        ]

        rows = []
        for r in self.loader.filled_orders():
            ts = self.loader.parse_ts(r.get("timestamp", ""))
            ts_val = ts if ts else r.get("timestamp", "")
            qty = self.loader.i(r.get("qty"))
            signal_px = self.loader.f(r.get("signal_price"))
            fill_px = self.loader.f(r.get("fill_price"))
            slip = self.loader.f(r.get("slippage"))
            slip_bps = (slip / signal_px * 10000) if signal_px else 0
            comm = self.loader.f(r.get("commission"))
            pnl = self.loader.f(r.get("pnl"))
            side = (r.get("side") or "").strip()
            gross = (slip * qty) if side == "SELL" else 0.0  # rough; gross at cycle level lives in PnL Ticker tab
            oid = r.get("order_id") or ""
            cycle = ("c" + oid.rsplit("_n", 1)[-1]) if "_n" in oid else ""
            rows.append([
                ts_val,
                self.loader.strategy_of(oid),
                r.get("ticker", ""),
                side,
                r.get("order_type", ""),
                qty,
                signal_px,
                fill_px,
                slip,
                slip_bps,
                comm,
                gross,
                pnl if side == "SELL" else None,
                cycle,
                oid,
                r.get("state_at_time", ""),
                (r.get("reason") or "")[:80],
            ])

        last_row = self.write_table(
            ws, row, headers, rows,
            col_widths=[22, 10, 8, 6, 16, 8, 11, 11, 12, 13, 12, 13, 13, 9, 32, 16, 50],
            money_cols=[11], signed_money_cols=[9, 12, 13],
            int_cols=[6], px_cols=[7, 8],
            pnl_color_col=13,
        )

        # Datetime formatting on first column
        for ri in range(row + 1, last_row):
            cell = ws.cell(row=ri, column=1)
            if isinstance(cell.value, datetime):
                cell.number_format = self.style.FMT_DATETIME

        # bps format on column 10
        for ri in range(row + 1, last_row):
            ws.cell(row=ri, column=10).number_format = self.style.FMT_BPS


# ══════════════════════════════════════════════════════════════════════════
#                            3. OPEN POSITIONS TAB
# ══════════════════════════════════════════════════════════════════════════

class OpenPositionsTab(TabBuilder):
    name = "Open Positions"
    tab_color = "FF10B981"

    def build(self) -> None:
        ws = self.create_sheet()
        row = self.add_sheet_title(
            ws, 1,
            "Open Positions",
            f"{self.date_str}  ·  {self.account}  ·  "
            f"Shorts still held at session close — entry, trough, stop, distance from stop",
        )

        # Find last state.csv row per ticker WHERE position_open == True
        opens = self._find_open_positions()
        if not opens:
            ws.cell(row=row, column=1,
                    value="No open positions at session end — engine is fully flat.").font = \
                Font(name="Calibri", size=11, color="FF1F7A2E", italic=True)
            ws.cell(row=row, column=1).fill = self.style.FILL_GREEN
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
            return

        headers = [
            "Ticker", "Strategy", "Qty", "Entry VWAP", "Trough Price",
            "Stop Loss", "Stop Distance ($)", "Stop Distance (%)",
            "Entry Time", "Hold So Far",
        ]
        rows = []
        for t, d in sorted(opens.items()):
            entry = d["entry_price"]
            stop = d["stop_loss"]
            # SHORT INVERSION (P11): a short's protective stop sits ABOVE entry,
            # so stop-distance = stop - entry (keeps it positive, like the long case).
            dist_dollar = stop - entry if entry and stop else 0
            dist_pct = (dist_dollar / entry) if entry else 0
            entry_ts = d.get("entry_time")
            hold = (self._last_seen_time(d) - entry_ts).total_seconds() if entry_ts else 0
            rows.append([
                t, d["strategy"], d["qty"], entry, d["trough"], stop,
                dist_dollar, dist_pct,
                entry_ts if entry_ts else "",
                DashboardTab._fmt_hold(hold),
            ])

        last_row = self.write_table(
            ws, row, headers, rows,
            col_widths=[10, 10, 8, 14, 14, 14, 16, 16, 22, 14],
            int_cols=[3], px_cols=[4, 5, 6],
            money_cols=[7], pct_cols=[8],
        )

        # Datetime cells (col 9)
        for ri in range(row + 1, last_row):
            cell = ws.cell(row=ri, column=9)
            if isinstance(cell.value, datetime):
                cell.number_format = self.style.FMT_DATETIME

    def _find_open_positions(self) -> Dict[str, Dict]:
        """Walk state.csv per ticker, find last snapshot where position_open."""
        out: Dict[str, Dict] = {}
        # Group state rows by ticker
        per_ticker: Dict[str, List[Dict]] = defaultdict(list)
        for s in self.loader.states:
            per_ticker[(s.get("ticker") or "").strip()].append(s)
        for t, rows in per_ticker.items():
            if not t:
                continue
            rows.sort(key=lambda r: r.get("timestamp", ""))
            last = rows[-1] if rows else None
            if not last:
                continue
            is_open = str(last.get("position_open", "")).lower() in ("true", "1")
            if not is_open:
                continue
            # Find entry time = earliest state where position_open went true
            entry_time = None
            for r in rows:
                if str(r.get("position_open", "")).lower() in ("true", "1"):
                    entry_time = self.loader.parse_ts(r.get("timestamp", ""))
                    break
            out[t] = {
                "strategy": "strat1",
                "qty": self.loader.i(last.get("config_qty")),
                "entry_price": self.loader.f(last.get("entry_price")),
                # SHORT INVERSION (P11): state.csv column is now `lowest_price`
                # (the cycle trough) — a short's meaningful extreme, not a peak.
                "trough": self.loader.f(last.get("lowest_price")),
                "stop_loss": self.loader.f(last.get("stop_loss")),
                "entry_time": entry_time,
                "_last": last,
            }
        return out

    def _last_seen_time(self, d: Dict) -> datetime:
        return self.loader.parse_ts(d["_last"].get("timestamp", "")) or datetime.now()


# ══════════════════════════════════════════════════════════════════════════
#                            4. PENDING ORDERS TAB
# ══════════════════════════════════════════════════════════════════════════

class PendingOrdersTab(TabBuilder):
    name = "Pending Orders"
    tab_color = "FFB45309"

    def build(self) -> None:
        ws = self.create_sheet()
        row = self.add_sheet_title(
            ws, 1,
            "Pending Orders",
            f"{self.date_str}  ·  {self.account}  ·  "
            f"Working orders still at the broker — submitted but not yet filled, cancelled, or rejected",
        )

        pending = self._find_pending_orders()
        if not pending:
            ws.cell(row=row, column=1,
                    value="No working orders at session end.").font = \
                Font(name="Calibri", size=11, color=Palette.SLATE, italic=True)
            ws.cell(row=row, column=1).fill = self.style.FILL_YELLOW
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
            return

        headers = [
            "Ticker", "Strategy", "Side", "Order Type", "Qty",
            "Stop Price", "Limit Price", "Submitted At", "Engine Order ID",
        ]
        rows = []
        for r in pending:
            oid = r.get("order_id") or ""
            rows.append([
                r.get("ticker", ""),
                self.loader.strategy_of(oid),
                (r.get("side") or "").strip(),
                r.get("order_type", ""),
                self.loader.i(r.get("qty")),
                self.loader.f(r.get("stop_price")),
                self.loader.f(r.get("limit_price")),
                self.loader.parse_ts(r.get("timestamp", "")) or r.get("timestamp", ""),
                oid,
            ])

        last_row = self.write_table(
            ws, row, headers, rows,
            col_widths=[10, 10, 6, 16, 8, 13, 13, 22, 32],
            int_cols=[5], px_cols=[6, 7],
        )
        for ri in range(row + 1, last_row):
            cell = ws.cell(row=ri, column=8)
            if isinstance(cell.value, datetime):
                cell.number_format = self.style.FMT_DATETIME

    def _find_pending_orders(self) -> List[Dict]:
        """Find SUBMITTED/BRACKET_SUBMITTED orders that don't have a
        terminal status row (FILLED, CANCELLED, REJECTED) for the same
        order_id."""
        terminal_ids = set()
        for r in self.loader.orders:
            ev = r.get("event", "")
            if ev in ("FILLED", "CANCELLED", "REJECTED"):
                oid = r.get("order_id") or ""
                if oid:
                    terminal_ids.add(oid)
        # For FILLED, also require it was fully filled (no easy way without
        # tracking partial vs complete — assume any FILLED == terminal here)
        pending = []
        for r in self.loader.orders:
            if r.get("event") in ("SUBMITTED", "BRACKET_SUBMITTED"):
                oid = r.get("order_id") or ""
                if oid and oid not in terminal_ids:
                    pending.append(r)
        return pending


# ══════════════════════════════════════════════════════════════════════════
#                            5. PREVIOUS ORDERS TAB
# ══════════════════════════════════════════════════════════════════════════

class PreviousOrdersTab(TabBuilder):
    name = "Previous Orders"
    tab_color = "FF6B7280"

    def build(self) -> None:
        ws = self.create_sheet()
        row = self.add_sheet_title(
            ws, 1,
            "Previous Orders",
            f"{self.date_str}  ·  {self.account}  ·  "
            f"Cancelled and rejected orders — full audit trail of every non-active order",
        )

        # Closed-cycle terminal events: FILLED, CANCELLED, REJECTED
        terminal = [r for r in self.loader.orders
                    if r.get("event") in ("CANCELLED", "REJECTED")]
        if not terminal:
            ws.cell(row=row, column=1,
                    value="No cancelled or rejected orders this session.").font = \
                Font(name="Calibri", size=11, color="FF1F7A2E", italic=True)
            ws.cell(row=row, column=1).fill = self.style.FILL_GREEN
            ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
            return

        headers = [
            "Timestamp", "Ticker", "Strategy", "Event", "Side",
            "Order Type", "Qty", "Stop Px", "Limit Px", "Engine Order ID", "Reason",
        ]
        rows = []
        for r in terminal:
            oid = r.get("order_id") or ""
            rows.append([
                self.loader.parse_ts(r.get("timestamp", "")) or r.get("timestamp", ""),
                r.get("ticker", ""),
                self.loader.strategy_of(oid),
                r.get("event", ""),
                (r.get("side") or "").strip(),
                r.get("order_type", ""),
                self.loader.i(r.get("qty")),
                self.loader.f(r.get("stop_price")),
                self.loader.f(r.get("limit_price")),
                oid,
                (r.get("reason") or "")[:60],
            ])
        last_row = self.write_table(
            ws, row, headers, rows,
            col_widths=[22, 10, 10, 12, 6, 16, 8, 12, 12, 30, 50],
            int_cols=[7], px_cols=[8, 9],
        )
        for ri in range(row + 1, last_row):
            cell = ws.cell(row=ri, column=1)
            if isinstance(cell.value, datetime):
                cell.number_format = self.style.FMT_DATETIME


# ══════════════════════════════════════════════════════════════════════════
#                            6. PnL ACCOUNT TAB
# ══════════════════════════════════════════════════════════════════════════

class PnlAccountTab(TabBuilder):
    name = "PnL Account"
    tab_color = "FF1E40AF"

    def build(self) -> None:
        ws = self.create_sheet()
        row = self.add_sheet_title(
            ws, 1,
            "Account P&L",
            f"{self.date_str}  ·  {self.account}  ·  "
            f"Portfolio-level summary + cycle-by-cycle detail across all tickers and strategies",
        )

        cycles = [c for c in self.loader.cycles if c.closed]

        # ── Summary card ────────────────────────────────────────────
        row = self.add_section_header(ws, row, "Session Summary", cols_span=4)
        gross = sum(c.gross_pnl for c in cycles)
        comm = sum(c.round_trip_commission for c in cycles)
        slip = sum(abs(self.loader.f(r.get("slippage")))
                   for r in self.loader.filled_orders())
        net = sum(c.net_pnl for c in cycles)
        notional = sum(c.entry_price * c.entry_qty for c in cycles)

        summary = [
            ("Gross P&L", gross, self.style.FMT_MONEY_SIGNED),
            ("Commission", comm, self.style.FMT_MONEY),
            ("Slippage", slip, self.style.FMT_MONEY),
            ("Net P&L", net, self.style.FMT_MONEY_SIGNED),
            ("Total Cycles", len(cycles), self.style.FMT_INT),
            ("Notional Traded", notional, self.style.FMT_MONEY),
            ("Avg Cycle P&L", (net / len(cycles)) if cycles else 0, self.style.FMT_MONEY_SIGNED),
            ("Commission Drag", (comm / gross if gross else 0), self.style.FMT_PCT),
        ]
        for i, (label, value, fmt) in enumerate(summary):
            r = row + (i // 2)
            c = 1 + (i % 2) * 2
            lc = ws.cell(row=r, column=c, value=label)
            lc.font = self.style.F_KPI_LABEL
            lc.fill = self.style.FILL_KPI_CARD
            lc.alignment = self.style.ALIGN_KPI_LABEL
            vc = ws.cell(row=r, column=c + 1, value=value)
            vc.font = self.style.F_BODY_BOLD
            vc.fill = self.style.FILL_KPI_CARD
            vc.alignment = self.style.ALIGN_RIGHT
            vc.number_format = fmt
        ws.column_dimensions["A"].width = 22
        ws.column_dimensions["B"].width = 18
        ws.column_dimensions["C"].width = 22
        ws.column_dimensions["D"].width = 18
        row += (len(summary) // 2) + 2

        # ── Cycle-by-cycle table ────────────────────────────────────
        row = self.add_section_header(ws, row, "Cycle-by-Cycle Detail", cols_span=12)
        headers = [
            "Cycle ID", "Ticker", "Strategy", "Entry Time", "Entry Px", "Qty",
            "Exit Time", "Exit Px", "Hold", "Gross P&L", "Commission", "Net P&L", "Exit Reason",
        ]
        rows = []
        for c in cycles:
            rows.append([
                c.cycle_id, c.ticker, c.strategy,
                c.entry_time if c.entry_time else "",
                c.entry_price, c.entry_qty,
                c.exit_time if c.exit_time else "",
                c.exit_price,
                DashboardTab._fmt_hold(c.hold_seconds),
                c.gross_pnl, c.round_trip_commission, c.net_pnl,
                c.exit_reason or "",
            ])
        last_row = self.write_table(
            ws, row, headers, rows,
            col_widths=[14, 9, 9, 22, 12, 8, 22, 12, 11, 13, 13, 13, 22],
            int_cols=[6], px_cols=[5, 8],
            money_cols=[11], signed_money_cols=[10, 12],
            pnl_color_col=12,
        )
        # Datetime cells (cols 4 and 7)
        for ri in range(row + 1, last_row):
            for col_n in (4, 7):
                cell = ws.cell(row=ri, column=col_n)
                if isinstance(cell.value, datetime):
                    cell.number_format = self.style.FMT_DATETIME


# ══════════════════════════════════════════════════════════════════════════
#                            7. PnL TICKER TAB
# ══════════════════════════════════════════════════════════════════════════

class PnlTickerTab(TabBuilder):
    name = "PnL Ticker"
    tab_color = "FF6D28D9"

    def build(self) -> None:
        ws = self.create_sheet()
        row = self.add_sheet_title(
            ws, 1,
            "Per-Ticker P&L",
            f"{self.date_str}  ·  {self.account}  ·  "
            f"Per-symbol attribution — cycles, hit rate, gross/commission/slippage/net, "
            f"best/worst, qty + notional, average hold",
        )

        per_ticker: Dict[str, Dict] = defaultdict(lambda: {
            "strategy": "strat1", "cycles": 0, "wins": 0, "losses": 0,
            "gross": 0.0, "commission": 0.0, "net": 0.0, "best": 0.0, "worst": 0.0,
            "notional": 0.0, "qty_traded": 0, "avg_hold": 0.0,
            "_holds": [],
        })
        for c in self.loader.cycles:
            if not c.closed:
                continue
            d = per_ticker[c.ticker]
            d["strategy"] = c.strategy
            d["cycles"] += 1
            d["gross"] += c.gross_pnl
            d["commission"] += c.round_trip_commission
            d["net"] += c.net_pnl
            d["best"] = max(d["best"], c.net_pnl)
            d["worst"] = min(d["worst"], c.net_pnl)
            d["notional"] += c.entry_price * c.entry_qty
            d["qty_traded"] += c.entry_qty
            d["_holds"].append(c.hold_seconds)
            if c.net_pnl > 0:
                d["wins"] += 1
            else:
                d["losses"] += 1

        # Slippage per ticker (sum of absolute slippages across all fills)
        slip_per_ticker: Dict[str, float] = defaultdict(float)
        for r in self.loader.filled_orders():
            slip_per_ticker[r.get("ticker", "")] += abs(self.loader.f(r.get("slippage")))

        headers = [
            "Ticker", "Strategy", "Cycles", "Wins", "Losses", "Hit Rate",
            "Gross P&L", "Commission", "Slippage", "Net P&L",
            "Best", "Worst", "Avg Cycle", "Qty Traded", "Notional", "Avg Hold",
        ]
        rows = []
        for t in sorted(per_ticker.keys()):
            d = per_ticker[t]
            avg_hold = (sum(d["_holds"]) / len(d["_holds"])) if d["_holds"] else 0
            hit = (d["wins"] / d["cycles"]) if d["cycles"] else 0
            avg_cycle = (d["net"] / d["cycles"]) if d["cycles"] else 0
            rows.append([
                t, d["strategy"], d["cycles"], d["wins"], d["losses"], hit,
                d["gross"], d["commission"], slip_per_ticker.get(t, 0), d["net"],
                d["best"], d["worst"], avg_cycle,
                d["qty_traded"], d["notional"],
                DashboardTab._fmt_hold(avg_hold),
            ])
        # Totals row
        if rows:
            total = [
                "TOTAL", "—",
                sum(r[2] for r in rows),
                sum(r[3] for r in rows),
                sum(r[4] for r in rows),
                (sum(r[3] for r in rows) / sum(r[2] for r in rows)) if sum(r[2] for r in rows) else 0,
                sum(r[6] for r in rows),
                sum(r[7] for r in rows),
                sum(r[8] for r in rows),
                sum(r[9] for r in rows),
                max(r[10] for r in rows),
                min(r[11] for r in rows),
                (sum(r[9] for r in rows) / sum(r[2] for r in rows)) if sum(r[2] for r in rows) else 0,
                sum(r[13] for r in rows),
                sum(r[14] for r in rows),
                "—",
            ]
            rows.append(total)

        last_row = self.write_table(
            ws, row, headers, rows,
            col_widths=[10, 10, 8, 8, 8, 10, 13, 13, 13, 13, 13, 13, 13, 11, 16, 12],
            int_cols=[3, 4, 5, 14], pct_cols=[6],
            money_cols=[8, 9, 15], signed_money_cols=[7, 10, 11, 12, 13],
            pnl_color_col=10,
        )
        # Bold the totals row
        if rows:
            for c in range(1, len(headers) + 1):
                cell = ws.cell(row=last_row - 1, column=c)
                cell.font = self.style.F_BODY_BOLD
                cell.fill = PatternFill("solid", fgColor=Palette.GREY_LIGHT)


# ══════════════════════════════════════════════════════════════════════════
#                            8. OHLC TAB
# ══════════════════════════════════════════════════════════════════════════

class OhlcTab(TabBuilder):
    name = "OHLC"
    tab_color = "FF0F766E"

    def build(self) -> None:
        ws = self.create_sheet()
        row = self.add_sheet_title(
            ws, 1,
            "Session OHLC",
            f"{self.date_str}  ·  {self.account}  ·  "
            f"Per-ticker open / high / low / close + VWAP, computed from the IBKR tick feed",
        )

        headers = [
            "Ticker", "Strategy", "Session Open", "Session Close",
            "Open", "High", "Low", "Close",
            "Range ($)", "Range (%)", "VWAP", "Volume", "Tick Count",
            "Avg Tick Rate (per s)",
        ]
        rows = []
        for ticker in self.loader.tickers:
            feed = self.loader.load_feed_for_ticker(ticker)
            if not feed:
                rows.append([ticker, "strat1", "—", "—", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
                continue
            o, h, l, c, vol, vwap, n_ticks, ts_first, ts_last = self._compute_ohlc(feed)
            rng = h - l
            rng_pct = (rng / o) if o else 0
            duration = (ts_last - ts_first).total_seconds() if (ts_first and ts_last) else 0
            tick_rate = (n_ticks / duration) if duration > 0 else 0
            rows.append([
                ticker, "strat1",
                ts_first if ts_first else "—",
                ts_last if ts_last else "—",
                o, h, l, c, rng, rng_pct, vwap, vol, n_ticks, tick_rate,
            ])

        last_row = self.write_table(
            ws, row, headers, rows,
            col_widths=[10, 10, 22, 22, 12, 12, 12, 12, 12, 11, 12, 14, 14, 18],
            px_cols=[5, 6, 7, 8, 9, 11],
            pct_cols=[10], int_cols=[12, 13],
        )
        for ri in range(row + 1, last_row):
            for col_n in (3, 4):
                cell = ws.cell(row=ri, column=col_n)
                if isinstance(cell.value, datetime):
                    cell.number_format = self.style.FMT_DATETIME
            ws.cell(row=ri, column=14).number_format = '0.00'

    def _compute_ohlc(self, feed: List[Dict]) -> Tuple:
        """Walk the tick stream and compute session OHLC + VWAP."""
        o = h = l = c = 0.0
        vol = 0
        notional_x_qty = 0.0
        vwap_qty = 0
        n_ticks = 0
        ts_first = ts_last = None
        for row in feed:
            ltp = self.loader.f(row.get("ltp"))
            ltp_size = self.loader.i(row.get("ltp_size"))
            ts = self.loader.parse_ts(row.get("timestamp", ""))
            tick_type = (row.get("tick_type") or "").strip().upper()
            if ltp <= 0:
                continue
            n_ticks += 1
            if ts:
                if ts_first is None or ts < ts_first:
                    ts_first = ts
                if ts_last is None or ts > ts_last:
                    ts_last = ts
            if o == 0:
                o = ltp
            c = ltp
            if h == 0 or ltp > h:
                h = ltp
            if l == 0 or ltp < l:
                l = ltp
            # Only count TRADE ticks toward volume / VWAP
            if tick_type in ("", "TRADE") and ltp_size > 0:
                vol += ltp_size
                notional_x_qty += ltp * ltp_size
                vwap_qty += ltp_size
        vwap = (notional_x_qty / vwap_qty) if vwap_qty > 0 else 0
        return o, h, l, c, vol, vwap, n_ticks, ts_first, ts_last


# ══════════════════════════════════════════════════════════════════════════
#                             WORKBOOK BUILDER
# ══════════════════════════════════════════════════════════════════════════

class EodExcelReport:
    """Top-level orchestrator — builds the workbook from the audit data."""

    TAB_CLASSES = [
        DashboardTab,
        TradeLogTab,
        OpenPositionsTab,
        PendingOrdersTab,
        PreviousOrdersTab,
        PnlAccountTab,
        PnlTickerTab,
        OhlcTab,
    ]

    def __init__(self, *, date_str: str, account: str, loader: AuditDataLoader):
        self.date_str = date_str  # YYYY-MM-DD
        self.account = account
        self.loader = loader

        self.wb = Workbook()
        # Remove default sheet
        default = self.wb.active
        if default is not None:
            self.wb.remove(default)

        self.style = StyleRegistry()

    def build(self) -> Workbook:
        for cls in self.TAB_CLASSES:
            tab = cls(self.wb, self.loader, self.style, self.date_str, self.account)
            tab.build()
        # Set the Dashboard as the active sheet on open
        self.wb.active = 0
        return self.wb

    def save(self, out_path: Path) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Workbook metadata
        self.wb.properties.creator = "GT System"
        self.wb.properties.title = f"GT EOD Report {self.date_str}"
        self.wb.properties.subject = f"End-of-day trading report — {self.date_str}"
        self.wb.save(out_path)


# ══════════════════════════════════════════════════════════════════════════
#                                   CLI
# ══════════════════════════════════════════════════════════════════════════

def parse_date_arg(s: str) -> str:
    s = s.strip()
    if s.lower() == "today":
        return datetime.now().strftime("%Y%m%d")
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s.replace("-", "")
    if len(s) == 8 and s.isdigit():
        return s
    raise ValueError(f"Unrecognized date: {s!r}. Use YYYY-MM-DD, YYYYMMDD, or 'today'.")


def pretty_date(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def main():
    p = argparse.ArgumentParser(
        description=("Generate the multi-tab EOD Excel report for senior MD review."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--date", default="today",
                   help="Session date (YYYY-MM-DD, YYYYMMDD, or 'today'). Default: today")
    p.add_argument("--out", default=None,
                   help="Output xlsx path. Default: data/reports/EOD_<DATE>.xlsx")
    p.add_argument("--account", default="Acct DU0000",
                   help="Account label shown in headers. Default: 'Acct DU0000'")
    args = p.parse_args()

    try:
        yyyymmdd = parse_date_arg(args.date)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    date_folder = AUDIT_ROOT / yyyymmdd
    if not date_folder.exists():
        print(f"ERROR: no audit folder at {date_folder}", file=sys.stderr)
        sys.exit(1)

    # Hint if the combined folder doesn't exist yet
    if not (date_folder / "_combined").exists():
        print(f"WARN: {date_folder}/_combined/ doesn't exist. Run first:")
        print(f"      python3 scripts/combine_audit.py --date {yyyymmdd}")
        print(f"      ...then re-run this script.")
        sys.exit(1)

    print(f"[EOD-XLSX] Loading audit data for {pretty_date(yyyymmdd)} …")
    loader = AuditDataLoader(date_folder)
    print(f"[EOD-XLSX]   {len(loader.orders)} order rows, "
          f"{len(loader.states)} state rows, {len(loader.pnls)} pnl rows, "
          f"{len(loader.tickers)} tickers, {len(loader.cycles)} cycles")

    out = Path(args.out) if args.out else (REPORT_ROOT / f"EOD_{pretty_date(yyyymmdd)}.xlsx")
    print(f"[EOD-XLSX] Building workbook → {out}")

    report = EodExcelReport(
        date_str=pretty_date(yyyymmdd),
        account=args.account,
        loader=loader,
    )
    wb = report.build()
    report.save(out)

    print(f"[EOD-XLSX] Done. {out}")
    print(f"[EOD-XLSX] Tabs: {', '.join(s.title for s in wb.worksheets)}")


if __name__ == "__main__":
    main()

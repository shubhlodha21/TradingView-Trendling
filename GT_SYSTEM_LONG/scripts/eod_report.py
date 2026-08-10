#!/usr/bin/env python3
"""End-of-Day Trade Report — single-page PDF report generator.

Reads from the existing audit pipeline (data/audit/<YYYYMMDD>/<TICKER>/*.csv)
and emits a print-quality PDF summary suitable for daily handoff to senior
quant / PM.

USAGE
    python scripts/eod_report.py                       # today's session
    python scripts/eod_report.py --date 2026-05-27     # specific date
    python scripts/eod_report.py --date 20260527       # YYYYMMDD also accepted
    python scripts/eod_report.py --out report.pdf      # custom output path
    python scripts/eod_report.py --ticker TSLA         # single-ticker report

The script is READ-ONLY against audit data. It does not touch the engine,
broker, state files, or any working order. Safe to run during market hours
or after close. If the audit folder is empty / missing, an empty-day PDF
is produced with a clear "no trades" message.

OUTPUT
    data/reports/EOD_<YYYY-MM-DD>.pdf (default)
"""

import argparse
import io
import subprocess
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

# ── Dependency checks ─────────────────────────────────────────────────────
try:
    import pandas as pd
except ImportError:
    print("ERROR: pandas not installed. Run: pip install pandas", file=sys.stderr)
    sys.exit(1)

try:
    import matplotlib
    matplotlib.use("Agg")  # headless backend — no display required
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter
except ImportError:
    print("ERROR: matplotlib not installed. Run: pip install matplotlib", file=sys.stderr)
    sys.exit(1)

try:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image,
        KeepTogether,
    )
except ImportError:
    print("ERROR: reportlab not installed. Run: pip install reportlab", file=sys.stderr)
    sys.exit(1)


# ── Paths ─────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_ROOT = REPO_ROOT / "data" / "audit"
REPORT_ROOT = REPO_ROOT / "data" / "reports"


# ── Anomaly event names surfaced in the NOTES section ─────────────────────
ORDER_ANOMALIES = {
    "CHILD_STOP_MODIFY_FAILED",
    "ORPHAN_CANCEL_VERIFY_TIMEOUT",
    "PHANTOM_SELL_REJECTED",
    "SHORTING_PREVENTED",
    "BRACKET_CHILD_CANCELLED",
    "STOP_LOSS_MARKET_FALLBACK",
    "CIRCUIT_BREAK",
    "REJECTED",
    "CANCELLED",
}
STATE_ANOMALIES = {
    "POSITION_MISMATCH",
    "POSITION_AUTO_FLAT",
    "TRIPWIRE_LOST_PENDING",
}


# ── Helpers ───────────────────────────────────────────────────────────────
def parse_date_arg(s: str) -> str:
    """Accept YYYY-MM-DD or YYYYMMDD, return canonical YYYYMMDD for path."""
    s = s.strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s.replace("-", "")
    if len(s) == 8 and s.isdigit():
        return s
    # Pandas-style "today"
    if s.lower() == "today":
        return datetime.now().strftime("%Y%m%d")
    raise ValueError(f"Unrecognized date format: {s!r}. Use YYYY-MM-DD or YYYYMMDD.")


def pretty_date(yyyymmdd: str) -> str:
    """20260527 -> 2026-05-27."""
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def fmt_money(x: float, signed: bool = False) -> str:
    """Format dollar amount. `signed=True` always shows + or -."""
    if x is None or pd.isna(x):
        return "$0.00"
    sign = "+" if signed and x >= 0 else ""
    return f"{sign}${x:,.2f}" if x >= 0 else f"-${abs(x):,.2f}"


def fmt_pct(x: float) -> str:
    if x is None or pd.isna(x):
        return "0.0%"
    return f"{x:.1f}%"


def get_git_sha() -> str:
    """Best-effort short git SHA; empty string if not in a repo."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, stderr=subprocess.DEVNULL, timeout=2,
        )
        return out.decode().strip()
    except Exception:
        return ""


# ── Data loading ──────────────────────────────────────────────────────────
def _read_csv_with_optional_gz(ticker_dir: Path, name: str) -> pd.DataFrame:
    """Read <ticker_dir>/<name> or <name>.gz, whichever exists."""
    for candidate in (ticker_dir / name, ticker_dir / f"{name}.gz"):
        if candidate.exists():
            try:
                return pd.read_csv(candidate)
            except Exception as e:
                print(f"WARN: failed to read {candidate}: {e}", file=sys.stderr)
                return pd.DataFrame()
    return pd.DataFrame()


def load_audit_for_date(yyyymmdd: str, only_ticker: str = None) -> dict:
    """Load all CSVs for the given date. Returns dict of DataFrames keyed
    by 'orders', 'states', 'pnls' — each concatenated across tickers with
    a 'ticker' column added.
    """
    date_folder = AUDIT_ROOT / yyyymmdd
    out = {"orders": [], "states": [], "pnls": []}

    if not date_folder.exists():
        return {k: pd.DataFrame() for k in out}

    for ticker_dir in sorted(date_folder.iterdir()):
        if not ticker_dir.is_dir():
            continue
        ticker = ticker_dir.name
        if only_ticker and ticker.upper() != only_ticker.upper():
            continue

        for kind, fname in (("orders", "order.csv"),
                            ("states", "state.csv"),
                            ("pnls", "pnl.csv")):
            df = _read_csv_with_optional_gz(ticker_dir, fname)
            if not df.empty:
                df["ticker"] = ticker
                out[kind].append(df)

    return {k: (pd.concat(v, ignore_index=True) if v else pd.DataFrame())
            for k, v in out.items()}


# ── Aggregation ───────────────────────────────────────────────────────────
def compute_kpis(orders: pd.DataFrame) -> dict:
    """Compute headline KPIs from FILLED order rows."""
    empty = {
        "net_pnl": 0.0, "gross_pnl": 0.0, "commission": 0.0, "slippage": 0.0,
        "trades": 0, "wins": 0, "losses": 0, "hit_rate": 0.0, "max_dd": 0.0,
    }
    if orders is None or orders.empty or "event" not in orders.columns:
        return empty

    filled = orders[orders["event"] == "FILLED"].copy()
    if filled.empty:
        return empty

    # SELL FILLED rows carry round-trip pnl (the engine writes this).
    sells = filled[filled["side"] == "SELL"].copy()
    sells["pnl_num"] = pd.to_numeric(sells.get("pnl"), errors="coerce").fillna(0.0)

    net_pnl = float(sells["pnl_num"].sum())

    commission_series = pd.to_numeric(filled.get("commission"), errors="coerce").fillna(0.0)
    commission = float(commission_series.sum())

    # Gross = net + commission (since net already deducted commission)
    gross_pnl = net_pnl + commission

    slip_series = pd.to_numeric(filled.get("slippage"), errors="coerce").fillna(0.0)
    slippage = float(slip_series.abs().sum())  # absolute cost figure

    trades = int(len(sells))
    wins = int((sells["pnl_num"] > 0).sum())
    losses = int((sells["pnl_num"] <= 0).sum())
    hit_rate = (wins / trades * 100.0) if trades > 0 else 0.0

    # Max drawdown of cumulative net pnl over the session.
    if trades > 0:
        sells_sorted = sells.sort_values("timestamp")
        cum = sells_sorted["pnl_num"].cumsum()
        running_max = cum.cummax()
        dd = cum - running_max
        max_dd = float(dd.min()) if len(dd) else 0.0
    else:
        max_dd = 0.0

    return {
        "net_pnl": net_pnl, "gross_pnl": gross_pnl, "commission": commission,
        "slippage": slippage, "trades": trades, "wins": wins, "losses": losses,
        "hit_rate": hit_rate, "max_dd": max_dd,
    }


def per_ticker_summary(orders: pd.DataFrame) -> pd.DataFrame:
    """Roll up per-ticker stats. One row per ticker, sorted by net pnl desc."""
    if orders is None or orders.empty or "event" not in orders.columns:
        return pd.DataFrame()

    filled = orders[orders["event"] == "FILLED"].copy()
    if filled.empty:
        return pd.DataFrame()

    sells = filled[filled["side"] == "SELL"].copy()
    if sells.empty:
        return pd.DataFrame()

    sells["pnl_num"] = pd.to_numeric(sells.get("pnl"), errors="coerce").fillna(0.0)
    sells["timestamp_dt"] = pd.to_datetime(sells["timestamp"], errors="coerce")

    # Per cycle: pair each SELL with its corresponding BUY in the same ticker.
    # We can't always reconstruct hold time without engine state, so
    # approximate via BUY FILLED timestamps grouped by ticker (FIFO).
    buys = filled[filled["side"] == "BUY"].copy()
    buys["timestamp_dt"] = pd.to_datetime(buys["timestamp"], errors="coerce")

    rollup = []
    for ticker, group in sells.groupby("ticker"):
        cycles = len(group)
        net = float(group["pnl_num"].sum())
        wins = int((group["pnl_num"] > 0).sum())
        hit = (wins / cycles * 100.0) if cycles > 0 else 0.0
        best = float(group["pnl_num"].max()) if cycles > 0 else 0.0
        worst = float(group["pnl_num"].min()) if cycles > 0 else 0.0

        # Hold time: pair each SELL with the most recent earlier BUY for the ticker.
        ticker_buys = buys[buys["ticker"] == ticker].sort_values("timestamp_dt")
        holds_sec = []
        for _, sell_row in group.sort_values("timestamp_dt").iterrows():
            t_sell = sell_row["timestamp_dt"]
            earlier = ticker_buys[ticker_buys["timestamp_dt"] <= t_sell]
            if len(earlier):
                t_buy = earlier["timestamp_dt"].iloc[-1]
                if pd.notna(t_buy) and pd.notna(t_sell):
                    holds_sec.append((t_sell - t_buy).total_seconds())
        avg_hold_sec = sum(holds_sec) / len(holds_sec) if holds_sec else 0.0

        rollup.append({
            "Ticker": ticker,
            "Cycles": cycles,
            "Net": net,
            "Hit%": hit,
            "Best": best,
            "Worst": worst,
            "AvgHold": avg_hold_sec,
        })

    df = pd.DataFrame(rollup).sort_values("Net", ascending=False)
    return df


def collect_anomalies(orders: pd.DataFrame, states: pd.DataFrame) -> list:
    """Return list of dicts {time, ticker, event, detail} for anomalies."""
    out = []
    if orders is not None and not orders.empty and "event" in orders.columns:
        mask = orders["event"].isin(ORDER_ANOMALIES)
        for _, r in orders[mask].sort_values("timestamp").iterrows():
            out.append({
                "time": str(r.get("timestamp", ""))[:19],
                "ticker": r.get("ticker", ""),
                "event": r.get("event", ""),
                "detail": str(r.get("reason", ""))[:120],
            })
    if states is not None and not states.empty and "event" in states.columns:
        mask = states["event"].isin(STATE_ANOMALIES)
        for _, r in states[mask].sort_values("timestamp").iterrows():
            out.append({
                "time": str(r.get("timestamp", ""))[:19],
                "ticker": r.get("ticker", ""),
                "event": r.get("event", ""),
                "detail": f"state={r.get('state','')}",
            })
    return out


# ── Equity curve PNG ──────────────────────────────────────────────────────
def build_equity_curve_png(orders: pd.DataFrame) -> bytes:
    """Render the intraday equity curve as PNG bytes (returned via BytesIO)."""
    fig, ax = plt.subplots(figsize=(7.5, 1.6), dpi=150)

    if orders is None or orders.empty or "event" not in orders.columns:
        ax.text(0.5, 0.5, "No trades for this session",
                ha="center", va="center", transform=ax.transAxes,
                fontsize=10, color="#666")
        ax.set_xticks([]); ax.set_yticks([])
    else:
        sells = orders[(orders["event"] == "FILLED") & (orders["side"] == "SELL")].copy()
        if sells.empty:
            ax.text(0.5, 0.5, "No closed trades for this session",
                    ha="center", va="center", transform=ax.transAxes,
                    fontsize=10, color="#666")
            ax.set_xticks([]); ax.set_yticks([])
        else:
            sells["timestamp_dt"] = pd.to_datetime(sells["timestamp"], errors="coerce")
            sells["pnl_num"] = pd.to_numeric(sells.get("pnl"), errors="coerce").fillna(0.0)
            sells = sells.sort_values("timestamp_dt").dropna(subset=["timestamp_dt"])
            sells["cum_pnl"] = sells["pnl_num"].cumsum()

            # Step curve so the equity jumps at each fill.
            t = sells["timestamp_dt"].tolist()
            y = sells["cum_pnl"].tolist()

            ax.fill_between(t, y, 0, alpha=0.25, color="#3DADFF", step="post")
            ax.plot(t, y, drawstyle="steps-post", linewidth=1.8, color="#007AD2")
            ax.axhline(0, color="#888", linewidth=0.6, linestyle="--")
            ax.xaxis.set_major_formatter(DateFormatter("%H:%M"))
            ax.set_ylabel("Net P&L ($)", fontsize=8)
            ax.set_title("Intraday Equity Curve", fontsize=9, loc="left",
                         fontweight="bold")
            ax.grid(True, alpha=0.3)
            ax.tick_params(labelsize=7)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


# ── PDF rendering ─────────────────────────────────────────────────────────
DARK = colors.HexColor("#1E1E1E")
GREY_LIGHT = colors.HexColor("#EEEEEE")
GREY_MED = colors.HexColor("#B3B3B3")
GREY_DARK = colors.HexColor("#666666")
ACCENT = colors.HexColor("#3DADFF")
POSITIVE = colors.HexColor("#3E9B4B")
NEGATIVE = colors.HexColor("#DC3009")


def build_header_table(yyyymmdd: str, account_label: str) -> Table:
    """Two-cell header: title left, date+account right."""
    styles = getSampleStyleSheet()
    title = Paragraph(
        '<b><font size="14" color="#1E1E1E">GT SYSTEM — EOD Trade Report</font></b>',
        styles["Normal"],
    )
    meta = Paragraph(
        f'<para align="right"><b>{pretty_date(yyyymmdd)}</b>'
        f'&nbsp;&nbsp;&nbsp;<font color="#666666">{account_label}</font></para>',
        styles["Normal"],
    )
    t = Table([[title, meta]], colWidths=[4.2 * inch, 3.3 * inch])
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 1.2, DARK),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("VALIGN", (0, 0), (-1, 0), "BOTTOM"),
    ]))
    return t


def _hex(c: colors.Color) -> str:
    """ReportLab Color → '#RRGGBB'."""
    return "#{:02X}{:02X}{:02X}".format(int(c.red * 255), int(c.green * 255), int(c.blue * 255))


def build_kpi_strip(k: dict) -> Table:
    """Two-row KPI grid: net/trades/wl/hit; gross/comm/slip/dd."""
    def cell(label, value, color=DARK):
        # Escape '&' for ReportLab's inline-XML parser. Without this,
        # "NET P&L" renders as "NET P&L;" because the parser tries to
        # interpret "&L" as an HTML entity.
        label_x = label.replace("&", "&amp;")
        value_x = value.replace("&", "&amp;") if isinstance(value, str) else str(value)
        styles = getSampleStyleSheet()
        return Paragraph(
            f'<font size="7" color="#666666">{label_x}</font><br/>'
            f'<b><font size="11" color="{_hex(color)}">{value_x}</font></b>',
            styles["Normal"],
        )

    net_color = POSITIVE if k["net_pnl"] >= 0 else NEGATIVE
    gross_color = POSITIVE if k["gross_pnl"] >= 0 else NEGATIVE
    dd_color = NEGATIVE if k["max_dd"] < 0 else DARK

    row1 = [
        cell("NET P&L", fmt_money(k["net_pnl"], signed=True), net_color),
        cell("TRADES", str(k["trades"]), DARK),
        cell("WIN / LOSS", f'{k["wins"]} / {k["losses"]}', DARK),
        cell("HIT", fmt_pct(k["hit_rate"]), DARK),
    ]
    row2 = [
        cell("GROSS", fmt_money(k["gross_pnl"], signed=True), gross_color),
        cell("COMMISSION", fmt_money(k["commission"]), DARK),
        cell("SLIPPAGE", fmt_money(k["slippage"]), DARK),
        cell("MAX DD", fmt_money(k["max_dd"], signed=True), dd_color),
    ]
    cw = [1.87 * inch, 1.87 * inch, 1.88 * inch, 1.88 * inch]
    t = Table([row1, row2], colWidths=cw, rowHeights=[0.45 * inch, 0.45 * inch])
    t.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, GREY_MED),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, GREY_LIGHT),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def build_trades_table(orders: pd.DataFrame, max_rows: int = 18) -> Table:
    """Trade-by-trade table (FILLED rows only). Truncates with "N more" note."""
    header = ["Time", "Ticker", "Side", "Qty", "Px", "Comm", "Slip", "P&L", "Cycle"]

    if orders is None or orders.empty or "event" not in orders.columns:
        rows = [["—", "—", "—", "—", "—", "—", "—", "—", "—"]]
        truncated = 0
    else:
        filled = orders[orders["event"] == "FILLED"].copy()
        if filled.empty:
            rows = [["—", "—", "—", "—", "—", "—", "—", "—", "—"]]
            truncated = 0
        else:
            filled["timestamp_dt"] = pd.to_datetime(filled["timestamp"], errors="coerce")
            filled = filled.sort_values("timestamp_dt")
            # Sort: by absolute P&L impact descending so the most-impactful rows
            # surface first; preserves time-order tie-breaking via stable sort.
            filled["pnl_num"] = pd.to_numeric(filled.get("pnl"), errors="coerce").fillna(0.0)
            filled["sort_key"] = filled["pnl_num"].abs()
            filled = filled.sort_values(["sort_key", "timestamp_dt"], ascending=[False, True])

            total = len(filled)
            shown = filled.head(max_rows)
            truncated = max(0, total - max_rows)

            # Re-sort displayed rows chronologically for readability.
            shown = shown.sort_values("timestamp_dt")

            rows = []
            for _, r in shown.iterrows():
                t = r.get("timestamp_dt")
                t_str = t.strftime("%H:%M:%S") if pd.notna(t) else "--:--:--"
                ticker = str(r.get("ticker", ""))
                side = str(r.get("side", ""))
                qty = int(float(r.get("qty", 0))) if pd.notna(r.get("qty")) else 0
                px = pd.to_numeric(r.get("fill_price"), errors="coerce")
                px_str = f"{px:.2f}" if pd.notna(px) and px > 0 else "—"
                comm = pd.to_numeric(r.get("commission"), errors="coerce")
                comm_str = f"{comm:.2f}" if pd.notna(comm) and comm > 0 else "—"
                slip = pd.to_numeric(r.get("slippage"), errors="coerce")
                slip_str = f"{slip:+.2f}" if pd.notna(slip) and abs(slip) > 0.001 else "—"
                pnl = r.get("pnl_num", 0.0)
                pnl_str = fmt_money(pnl, signed=True) if side == "SELL" else "—"
                oid = str(r.get("order_id", ""))
                # Extract cycle suffix _nN if present
                cycle = ""
                if "_n" in oid:
                    cycle = "C" + oid.rsplit("_n", 1)[-1]
                rows.append([t_str, ticker, side, str(qty), px_str,
                             comm_str, slip_str, pnl_str, cycle])

    data = [header] + rows
    cw = [0.85 * inch, 0.65 * inch, 0.45 * inch, 0.45 * inch, 0.7 * inch,
          0.55 * inch, 0.55 * inch, 0.85 * inch, 0.45 * inch]
    t = Table(data, colWidths=cw, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E1E1E")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN", (3, 0), (8, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, DARK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F7F7")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    # Color SELL P&L cells positive/negative
    for i, row in enumerate(rows, start=1):
        pnl_cell = row[7]
        if pnl_cell != "—":
            if "+" in pnl_cell:
                style.append(("TEXTCOLOR", (7, i), (7, i), POSITIVE))
            elif "-" in pnl_cell:
                style.append(("TEXTCOLOR", (7, i), (7, i), NEGATIVE))

    t.setStyle(TableStyle(style))
    return t, truncated


def build_per_ticker_table(df: pd.DataFrame) -> Table:
    """Per-ticker summary table."""
    header = ["Ticker", "Cycles", "Net", "Hit%", "Best", "Worst", "Avg Hold"]
    rows = [header]
    if df is None or df.empty:
        rows.append(["—", "—", "—", "—", "—", "—", "—"])
    else:
        for _, r in df.iterrows():
            hold_s = int(r["AvgHold"])
            hold_str = f"{hold_s//60}m{hold_s%60:02d}s" if hold_s > 0 else "—"
            rows.append([
                r["Ticker"],
                str(int(r["Cycles"])),
                fmt_money(r["Net"], signed=True),
                fmt_pct(r["Hit%"]),
                fmt_money(r["Best"], signed=True),
                fmt_money(r["Worst"], signed=True),
                hold_str,
            ])
    cw = [0.8 * inch, 0.7 * inch, 1.1 * inch, 0.6 * inch, 1.0 * inch, 1.0 * inch, 0.9 * inch]
    t = Table(rows, colWidths=cw, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E1E1E")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, DARK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F7F7")]),
    ]
    # Color Net column
    if df is not None and not df.empty:
        for i, (_, r) in enumerate(df.iterrows(), start=1):
            color = POSITIVE if r["Net"] >= 0 else NEGATIVE
            style.append(("TEXTCOLOR", (2, i), (2, i), color))

    t.setStyle(TableStyle(style))
    return t


def build_notes_paragraph(anomalies: list, max_lines: int = 5) -> Paragraph:
    styles = getSampleStyleSheet()
    if not anomalies:
        text = '<font size="8" color="#3E9B4B"><b>NOTES:</b> Clean session — no anomalies, no manual interventions.</font>'
    else:
        lines = [f'<font size="8"><b>NOTES ({len(anomalies)}):</b></font>']
        for a in anomalies[:max_lines]:
            lines.append(
                f'<font size="7" color="#666666">'
                f'{a["time"]} · {a["ticker"]} · <b>{a["event"]}</b> — {a["detail"]}'
                f'</font>'
            )
        if len(anomalies) > max_lines:
            lines.append(
                f'<font size="7" color="#666666">…and {len(anomalies) - max_lines} more '
                f'(see data/audit/&lt;DATE&gt;/&lt;TICKER&gt;/order.csv)</font>'
            )
        text = "<br/>".join(lines)
    return Paragraph(text, ParagraphStyle("notes", leading=10))


def build_footer_paragraph(yyyymmdd: str, sha: str) -> Paragraph:
    audit_path = AUDIT_ROOT / yyyymmdd
    return Paragraph(
        f'<font size="6.5" color="#888888">'
        f'<i>Signed: GT Engine'
        + (f' @ {sha}' if sha else '')
        + f'  ·  Audit refs: {audit_path}  ·  Generated {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</i>'
        f'</font>',
        ParagraphStyle("footer"),
    )


def render_pdf(out_path: Path, *, yyyymmdd: str, account_label: str,
               orders: pd.DataFrame, states: pd.DataFrame, pnls: pd.DataFrame) -> None:
    """Render the single-page EOD PDF to `out_path`."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=LETTER,
        leftMargin=0.55 * inch, rightMargin=0.55 * inch,
        topMargin=0.45 * inch, bottomMargin=0.45 * inch,
        title=f"GT EOD REPORT{pretty_date(yyyymmdd)}",
        author="Audit System",
    )

    story = []

    # 1. Header
    story.append(build_header_table(yyyymmdd, account_label))
    story.append(Spacer(1, 0.1 * inch))

    # 2. KPI strip
    kpis = compute_kpis(orders)
    story.append(build_kpi_strip(kpis))
    story.append(Spacer(1, 0.12 * inch))

    # 3. Equity curve PNG
    eq_buf = build_equity_curve_png(orders)
    img = Image(eq_buf, width=7.4 * inch, height=1.3 * inch)
    story.append(img)
    story.append(Spacer(1, 0.06 * inch))

    # 4. Trades table heading + table
    styles = getSampleStyleSheet()
    story.append(Paragraph(
        '<font size="9"><b>TRADES</b> (top by P&amp;L impact, time-ordered)</font>',
        ParagraphStyle("h", spaceAfter=2),
    ))
    trades_tbl, truncated = build_trades_table(orders, max_rows=10)
    story.append(trades_tbl)
    if truncated > 0:
        story.append(Paragraph(
            f'<font size="7" color="#666666"><i>'
            f'…{truncated} more fills (see data/audit/{yyyymmdd}/&lt;TICKER&gt;/order.csv)'
            f'</i></font>',
            ParagraphStyle("trunc", spaceBefore=2),
        ))
    story.append(Spacer(1, 0.08 * inch))

    # 5. Per-ticker summary
    story.append(Paragraph(
        '<font size="9"><b>PER-TICKER SUMMARY</b></font>',
        ParagraphStyle("h", spaceAfter=2),
    ))
    pt = per_ticker_summary(orders)
    story.append(build_per_ticker_table(pt))
    story.append(Spacer(1, 0.08 * inch))

    # 6. Notes / anomalies (cap to 5 lines to keep one-page layout)
    anomalies = collect_anomalies(orders, states)
    story.append(build_notes_paragraph(anomalies, max_lines=5))
    story.append(Spacer(1, 0.06 * inch))

    # 7. Footer
    story.append(build_footer_paragraph(yyyymmdd, get_git_sha()))

    doc.build(story)


# ── CLI ───────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Generate the EOD Trade PDF report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--date", default="today",
                   help="Session date (YYYY-MM-DD, YYYYMMDD, or 'today'). Default: today")
    p.add_argument("--out", default=None,
                   help="Output PDF path. Default: data/reports/EOD_<DATE>.pdf")
    p.add_argument("--ticker", default=None,
                   help="Generate single-ticker report (default: all tickers combined)")
    p.add_argument("--account", default="Acct DU0000",
                   help="Account label shown in the header. Default: 'Acct DU0000'")
    args = p.parse_args()

    try:
        yyyymmdd = parse_date_arg(args.date)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    print(f"[EOD] Loading audit data for {pretty_date(yyyymmdd)}"
          + (f" (ticker={args.ticker})" if args.ticker else "") + " …")
    data = load_audit_for_date(yyyymmdd, only_ticker=args.ticker)

    if data["orders"].empty and data["states"].empty:
        print(f"[EOD] WARNING: no audit data found for {pretty_date(yyyymmdd)} "
              f"(folder: {AUDIT_ROOT / yyyymmdd}). "
              f"Producing empty-day PDF.", file=sys.stderr)

    out = Path(args.out) if args.out else (REPORT_ROOT / f"EOD_{pretty_date(yyyymmdd)}.pdf")
    if args.ticker:
        out = out.with_name(f"EOD_{pretty_date(yyyymmdd)}_{args.ticker.upper()}.pdf")

    print(f"[EOD] Rendering → {out}")
    render_pdf(
        out_path=out,
        yyyymmdd=yyyymmdd,
        account_label=args.account,
        orders=data["orders"],
        states=data["states"],
        pnls=data["pnls"],
    )
    print(f"[EOD] Done. {out}")


if __name__ == "__main__":
    main()

"""
Production-Alerting Infrastructure

Implements Jane Street-style event-based monitoring:
- Explicit edge case enumeration
- Symptom-based alerting
- Defense-in-depth risk checks
- "Trade Too Good" anomaly detection

Philosophy:
- Every order is critical - NO silently failing
- Alert on symptoms, not causes
- Defense in depth - multiple independent checks
- Catalog every edge case explicitly
"""
from typing import Callable, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from collections import defaultdict
import queue
import threading
import time


class AlertSeverity(str, Enum):
    """Alert severity levels."""
    CRITICAL = "CRITICAL"   # Money at risk, immediate action
    HIGH = "HIGH"           # Significant issue, urgent
    MEDIUM = "MEDIUM"       # Investigate soon
    LOW = "LOW"             # Log and review later


@dataclass
class Alert:
    """A production alert - explicit edge case."""
    code: str                           # Unique alert code
    severity: AlertSeverity
    message: str                        # Human readable message
    timestamp: datetime = field(default_factory=datetime.now)
    context: dict = field(default_factory=dict)
    correlation_id: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "context": self.context,
            "correlation_id": self.correlation_id,
        }


class AlertChannel:
    """
    Destination for alerts.

    Implementations:
    - SlackChannel: Real-time notification
    - PagerdutyChannel: On-call escalation
    - FileChannel: Audit log
    - MetricsChannel: Prometheus
    """

    def send(self, alert: Alert) -> None:
        raise NotImplementedError


class SlackChannel(AlertChannel):
    """Non-blocking, rate-limited Slack channel.

    Previously this class blocked the caller for 200–800ms per Slack send
    (synchronous `urllib.request.urlopen`). With alerts at a few per session
    that was invisible; once you start sending trade-event pings (FILLED,
    REJECTED, …) the engine would block on every fill. This rewrite moves
    the HTTP send to a background worker thread, fronted by a bounded
    queue + token-bucket rate limiter.

    Contract:
      * `send(alert)` and `send_trade(event, **fields)` are non-blocking
        and never raise. They enqueue and return immediately.
      * Worker thread drains the queue, waits for rate-limit tokens,
        POSTs via urllib (still in-thread but off the hot path), retries
        once on 429.
      * Queue full → drop + increment `_dropped`. Visible in shutdown
        stats so a wedged worker isn't silent.
      * `close()` drains pending messages up to a deadline, then stops
        the worker. Called from run_live.py shutdown.

    Slack incoming-webhook limits this respects:
      * ~1 message/sec sustained, short bursts to ~5–10 before HTTP 429.
      * The token bucket (sustained 4/sec, burst 10) keeps us well clear.
    """

    # Token-bucket parameters. 4/sec sustained matches Slack's documented
    # comfort zone; burst 10 covers the open-bell spike where multiple
    # symbols hit simultaneously.
    _RATE_PER_SEC = 4.0
    _BURST = 10.0
    # Queue size — at 4 msgs/sec sustained we can drain 1000 in 4 minutes,
    # so even a multi-minute Slack outage doesn't OOM us.
    _QUEUE_SIZE = 1000

    def __init__(self, webhook_url: str, channel: str = "#trading-alerts"):
        self.webhook_url = webhook_url
        self.channel = channel
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=self._QUEUE_SIZE)
        self._stopped = False
        self._sent = 0
        self._dropped = 0
        self._http_errors = 0
        # Token bucket — refilled on every send by elapsed time.
        self._tokens = float(self._BURST)
        self._last_refill = time.monotonic()
        self._bucket_lock = threading.Lock()
        # Daemon worker so a stuck Slack endpoint never blocks process exit.
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="SlackWorker"
        )
        self._worker.start()

    # ── Public send paths ──────────────────────────────────────────

    def send(self, alert: Alert) -> None:
        """AlertChannel interface — non-blocking."""
        self._enqueue(self._format_alert(alert))

    def send_trade(self, event: str, **fields: Any) -> None:
        """Trade-event notification — non-blocking, Block Kit formatted.

        Bypasses the alert history (this isn't an anomaly; it's an
        operational event). Common `event` values: FILLED, REJECTED,
        SESSION_START, SESSION_END, RECONCILE_REPLAYED.
        """
        self._enqueue(self._format_trade(event, fields))

    def close(self, timeout: float = 2.0) -> dict:
        """Drain pending messages then stop the worker.

        Best-effort drain: waits up to `timeout` seconds for the queue to
        empty (subject to the rate limit), then signals the worker to
        exit. Returns stats so the shutdown summary can surface drops.
        """
        deadline = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.05)
        self._stopped = True
        # The worker checks _stopped between gets; a daemon thread + the
        # 0.5s get-timeout means it exits within half a second of this.
        self._worker.join(timeout=1.0)
        return {
            "sent": self._sent,
            "dropped": self._dropped,
            "http_errors": self._http_errors,
            "queued_at_exit": self._queue.qsize(),
        }

    # ── Internals ──────────────────────────────────────────────────

    def _enqueue(self, payload: dict) -> None:
        if self._stopped:
            return
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self._dropped += 1
            # First and every 100th drop printed so a chronically slow
            # Slack endpoint becomes visible without flooding stderr.
            if self._dropped == 1 or self._dropped % 100 == 0:
                print(
                    f"[Slack] queue full — dropped {self._dropped} message(s); "
                    f"webhook is slow or unreachable",
                    flush=True,
                )

    def _worker_loop(self) -> None:
        import json
        import urllib.request
        import urllib.error
        while not self._stopped:
            try:
                payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._take_token()
            try:
                data = json.dumps(payload).encode()
                req = urllib.request.Request(
                    self.webhook_url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
                if status == 200:
                    self._sent += 1
                else:
                    self._http_errors += 1
            except urllib.error.HTTPError as e:
                self._http_errors += 1
                # Slack throttling — wait a beat and re-enqueue once.
                if e.code == 429:
                    time.sleep(1.0)
                    try:
                        self._queue.put_nowait(payload)
                    except queue.Full:
                        self._dropped += 1
            except Exception:
                # urlopen timeout, DNS, TLS, etc. Don't let it kill the worker.
                self._http_errors += 1

    def _take_token(self) -> None:
        """Wait for a token; refill at _RATE_PER_SEC, cap at _BURST."""
        while not self._stopped:
            with self._bucket_lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    self._BURST, self._tokens + elapsed * self._RATE_PER_SEC
                )
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Compute exact sleep needed for next token.
                need = (1.0 - self._tokens) / self._RATE_PER_SEC
            time.sleep(min(need, 0.25))  # cap so close() can interrupt

    # ── Formatting ─────────────────────────────────────────────────

    def _format_alert(self, alert: Alert) -> dict:
        """Legacy attachments format for AlertManager alerts."""
        return {
            "channel": self.channel,
            "attachments": [{
                "color": self._color_for_severity(alert.severity),
                "fields": [
                    {"title": "Alert", "value": alert.code, "short": True},
                    {"title": "Severity", "value": alert.severity.value, "short": True},
                    {"title": "Message", "value": alert.message},
                    {"title": "Context", "value": str(alert.context)[:1000]},
                ]
            }]
        }

    def _format_trade(self, event: str, f: dict) -> dict:
        """Block Kit format for trade events — readable on mobile.

        Recognized fields (all optional): symbol, side, qty, fill_price,
        signal_price, slippage, pnl, state, cycle_id, reason, latency_ms.
        Unknown fields are appended as plain `key: value` rows so callers
        can include extra context without changing the formatter.
        """
        emoji = {
            "FILLED": ":large_green_circle:",
            "REJECTED": ":red_circle:",
            "SESSION_START": ":bell:",
            "SESSION_END": ":zzz:",
            "RECONCILE_REPLAYED": ":arrows_counterclockwise:",
        }.get(event, ":information_source:")

        symbol = f.get("symbol", "?")
        side = str(f.get("side", "")).upper()
        qty = f.get("qty", "?")
        header = f"{emoji}  *{event}*  ·  `{symbol}`  {side} {qty}"

        # Build a 2-column field grid in display priority. Skip rows whose
        # value is None or zero-y to keep the message compact.
        rows: list[tuple[str, str]] = []
        fp = f.get("fill_price")
        if fp is not None:
            rows.append(("fill", f"${float(fp):.4f}"))
        sp = f.get("signal_price")
        if sp is not None:
            rows.append(("signal", f"${float(sp):.4f}"))
        sl = f.get("slippage")
        if sl is not None:
            rows.append(("slip", f"${float(sl):+.4f}"))
        pnl = f.get("pnl")
        if pnl is not None and pnl != 0:
            rows.append(("P&L", f"${float(pnl):+.2f}"))
        lat = f.get("latency_ms")
        if lat is not None:
            rows.append(("latency", f"{float(lat):.0f}ms"))
        st = f.get("state")
        if st:
            rows.append(("state", str(st)))
        rs = f.get("reason")
        if rs:
            rows.append(("reason", str(rs)[:200]))

        # Anything else the caller passed
        known = {"symbol", "side", "qty", "fill_price", "signal_price",
                 "slippage", "pnl", "state", "cycle_id", "reason", "latency_ms"}
        for k, v in f.items():
            if k in known or v is None:
                continue
            rows.append((k, str(v)[:200]))

        block_fields = []
        for k, v in rows:
            block_fields.append({"type": "mrkdwn", "text": f"*{k}*\n{v}"})

        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        ]
        # Block Kit `fields` array maxes at 10 entries, 2000 chars each.
        # Slice in 10s in case a caller throws lots of context at us.
        for i in range(0, len(block_fields), 10):
            blocks.append({"type": "section", "fields": block_fields[i:i + 10]})

        cid = f.get("cycle_id")
        if cid:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"_cycle_ `{cid}`"}],
            })

        return {"channel": self.channel, "blocks": blocks}

    def _color_for_severity(self, severity: AlertSeverity) -> str:
        return {
            AlertSeverity.CRITICAL: "#ff0000",
            AlertSeverity.HIGH: "#ff6600",
            AlertSeverity.MEDIUM: "#ffcc00",
            AlertSeverity.LOW: "#00cc00",
        }.get(severity, "#cccccc")


class TeamsChannel(AlertChannel):
    """Microsoft Teams Incoming Webhook (Power Automate / Workflow style).

    Mirrors `SlackChannel`'s design exactly — daemon worker thread, token
    bucket rate limiter, bounded queue, retry-on-429, close() drain — only
    the payload format differs. We post Adaptive Cards (the standard
    payload type the Microsoft "Post to a channel when a webhook request
    is received" Workflow template accepts).

    Why Adaptive Cards over the legacy MessageCard format:
      * Microsoft is deprecating the Office 365 Connectors (and their
        MessageCard schema) through 2024-25. The Workflow webhook is the
        forward-compatible path.
      * Adaptive Cards render natively in Teams desktop, mobile, and web.
      * Color coding via the `style` attribute on Containers, matching
        our severity → color convention.

    Severity → Adaptive Card style:
        CRITICAL → "attention" (red)
        HIGH     → "warning"   (orange)
        MEDIUM   → "accent"    (blue)
        LOW      → "good"      (green)
    """

    _RATE_PER_SEC = 4.0   # Teams Workflows are comfortable at ~4-5 msgs/sec
    _BURST = 10.0
    _QUEUE_SIZE = 1000

    def __init__(self, webhook_url: str, channel_label: str = "trading-alerts"):
        self.webhook_url = webhook_url
        # `channel_label` is purely cosmetic — Teams webhooks are bound to
        # one channel at the workflow level, so we don't need to specify it
        # in the payload. We carry the label for log messages only.
        self.channel_label = channel_label
        self._queue: "queue.Queue[dict]" = queue.Queue(maxsize=self._QUEUE_SIZE)
        self._stopped = False
        self._sent = 0
        self._dropped = 0
        self._http_errors = 0
        self._tokens = float(self._BURST)
        self._last_refill = time.monotonic()
        self._bucket_lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="TeamsWorker"
        )
        self._worker.start()

    # ── Public send paths (same interface as SlackChannel) ───────────

    def send(self, alert: Alert) -> None:
        self._enqueue(self._format_alert(alert))

    def send_trade(self, event: str, **fields: Any) -> None:
        self._enqueue(self._format_trade(event, fields))

    def close(self, timeout: float = 2.0) -> dict:
        deadline = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < deadline:
            time.sleep(0.05)
        self._stopped = True
        self._worker.join(timeout=1.0)
        return {
            "sent": self._sent,
            "dropped": self._dropped,
            "http_errors": self._http_errors,
            "queued_at_exit": self._queue.qsize(),
        }

    # ── Internals (queue / worker / token bucket — identical to Slack) ──

    def _enqueue(self, payload: dict) -> None:
        if self._stopped:
            return
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 100 == 0:
                print(
                    f"[Teams] queue full — dropped {self._dropped} message(s); "
                    f"webhook is slow or unreachable",
                    flush=True,
                )

    def _worker_loop(self) -> None:
        import json
        import urllib.request
        import urllib.error
        while not self._stopped:
            try:
                payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._take_token()
            try:
                data = json.dumps(payload).encode()
                req = urllib.request.Request(
                    self.webhook_url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
                # Power Automate workflow webhooks return 202 Accepted on
                # successful queue rather than 200; treat both as success.
                if status in (200, 202):
                    self._sent += 1
                else:
                    self._http_errors += 1
            except urllib.error.HTTPError as e:
                self._http_errors += 1
                # Teams Workflows respond with 429 when rate-limited too.
                if e.code == 429:
                    time.sleep(1.0)
                    try:
                        self._queue.put_nowait(payload)
                    except queue.Full:
                        self._dropped += 1
            except Exception:
                self._http_errors += 1

    def _take_token(self) -> None:
        while not self._stopped:
            with self._bucket_lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(
                    self._BURST, self._tokens + elapsed * self._RATE_PER_SEC
                )
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                need = (1.0 - self._tokens) / self._RATE_PER_SEC
            time.sleep(min(need, 0.25))

    # ── Adaptive Card formatting ────────────────────────────────────

    def _style_for_severity(self, severity: AlertSeverity) -> str:
        return {
            AlertSeverity.CRITICAL: "attention",
            AlertSeverity.HIGH:     "warning",
            AlertSeverity.MEDIUM:   "accent",
            AlertSeverity.LOW:      "good",
        }.get(severity, "default")

    def _color_for_severity(self, severity: AlertSeverity) -> str:
        # Adaptive Card text color names (different from container styles)
        return {
            AlertSeverity.CRITICAL: "attention",
            AlertSeverity.HIGH:     "warning",
            AlertSeverity.MEDIUM:   "accent",
            AlertSeverity.LOW:      "good",
        }.get(severity, "default")

    def _wrap_card(self, card_body: list, severity_style: str = "default") -> dict:
        """Wrap an Adaptive Card body in the Teams Workflow envelope.

        The "Post to a channel" Workflow template expects:
            { "type": "message", "attachments": [ { contentType, content } ] }
        """
        return {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "msteams": {"width": "Full"},
                    "body": [
                        {
                            "type": "Container",
                            "style": severity_style,
                            "bleed": True,
                            "items": card_body,
                        }
                    ],
                },
            }],
        }

    def _format_alert(self, alert: Alert) -> dict:
        """Adaptive Card for AlertManager alerts (anomaly path)."""
        text_color = self._color_for_severity(alert.severity)
        body: list = [
            {
                "type": "TextBlock",
                "text": f"GT System Alert · {alert.code}",
                "weight": "Bolder",
                "size": "Large",
                "color": text_color,
                "wrap": True,
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "Severity", "value": alert.severity.value},
                    {"title": "Code", "value": alert.code},
                    {"title": "Time", "value": alert.timestamp.strftime("%Y-%m-%d %H:%M:%S")},
                ],
            },
            {
                "type": "TextBlock",
                "text": alert.message,
                "wrap": True,
                "spacing": "Medium",
            },
        ]
        # Optional context dict — render as facts if present + compact enough
        if alert.context:
            ctx_facts = []
            for k, v in list(alert.context.items())[:10]:  # cap at 10
                ctx_facts.append({"title": str(k), "value": str(v)[:200]})
            if ctx_facts:
                body.append({
                    "type": "TextBlock", "text": "Context",
                    "weight": "Bolder", "spacing": "Medium",
                    "color": "Default",
                })
                body.append({"type": "FactSet", "facts": ctx_facts})

        if alert.correlation_id:
            body.append({
                "type": "TextBlock",
                "text": f"cycle: `{alert.correlation_id}`",
                "size": "Small",
                "color": "Default",
                "isSubtle": True,
                "spacing": "Small",
            })

        return self._wrap_card(body, severity_style=self._style_for_severity(alert.severity))

    def _format_trade(self, event: str, f: dict) -> dict:
        """Adaptive Card for trade events (FILLED, REJECTED, etc.)."""
        # Style hint by event type — FILLED is good (green), REJECTED is
        # attention (red), most others are accent (blue).
        style = {
            "FILLED": "good",
            "REJECTED": "attention",
            "SESSION_START": "accent",
            "SESSION_END": "default",
            "RECONCILE_REPLAYED": "warning",
        }.get(event, "accent")

        symbol = f.get("symbol", "?")
        side = str(f.get("side", "")).upper()
        qty = f.get("qty", "?")
        header_text = f"{event}  ·  {symbol}  {side} {qty}"

        body: list = [
            {
                "type": "TextBlock",
                "text": header_text,
                "weight": "Bolder",
                "size": "Large",
                "wrap": True,
            },
        ]

        # Build a FactSet of the recognized fields in display priority
        facts = []
        fp = f.get("fill_price")
        if fp is not None:
            facts.append({"title": "Fill", "value": f"${float(fp):.4f}"})
        sp = f.get("signal_price")
        if sp is not None:
            facts.append({"title": "Signal", "value": f"${float(sp):.4f}"})
        sl = f.get("slippage")
        if sl is not None:
            facts.append({"title": "Slippage", "value": f"${float(sl):+.4f}"})
        pnl = f.get("pnl")
        if pnl is not None and pnl != 0:
            facts.append({"title": "P&L", "value": f"${float(pnl):+.2f}"})
        lat = f.get("latency_ms")
        if lat is not None:
            facts.append({"title": "Latency", "value": f"{float(lat):.0f} ms"})
        st = f.get("state")
        if st:
            facts.append({"title": "State", "value": str(st)})
        rs = f.get("reason")
        if rs:
            facts.append({"title": "Reason", "value": str(rs)[:200]})

        # Catch-all for unrecognized fields
        known = {"symbol", "side", "qty", "fill_price", "signal_price",
                 "slippage", "pnl", "state", "cycle_id", "reason", "latency_ms"}
        for k, v in f.items():
            if k in known or v is None:
                continue
            facts.append({"title": str(k), "value": str(v)[:200]})

        if facts:
            body.append({"type": "FactSet", "facts": facts})

        cid = f.get("cycle_id")
        if cid:
            body.append({
                "type": "TextBlock",
                "text": f"cycle: `{cid}`",
                "size": "Small",
                "isSubtle": True,
                "spacing": "Small",
            })

        return self._wrap_card(body, severity_style=style)


class PrintChannel(AlertChannel):
    """Print alerts to stdout - for development."""

    # ANSI colour per severity, matches dashboard palette.
    _COLOR = {
        AlertSeverity.CRITICAL: "\033[1;31m",  # bold red
        AlertSeverity.HIGH:     "\033[31m",     # red
        AlertSeverity.MEDIUM:   "\033[33m",     # yellow
        AlertSeverity.LOW:      "\033[36m",     # cyan
    }
    _RESET = "\033[0m"

    def send(self, alert: Alert) -> None:
        clr = self._COLOR.get(alert.severity, "")
        ts = alert.timestamp.strftime('%H:%M:%S')
        print(f"{clr}[ALERT {ts} {alert.severity.value}] {alert.code} — {alert.message}{self._RESET}")
        if alert.context:
            print(f"  context: {alert.context}")


class FileChannel(AlertChannel):
    """Append alerts to a daily JSONL file under `data/alerts/`.

    One alert per line, fully self-describing (code, severity, message,
    timestamp, context, correlation_id). JSONL is grep-friendly and
    ingestible by any log analysis tool (jq, DuckDB, Splunk, etc.).
    File rolls daily by date.

    Thread-safe: a lock serialises writes from multiple callers (the
    AlertManager.raise_alert path can be invoked from the asyncio loop
    AND from threaded contexts like the audit writer).
    """

    def __init__(self, directory: str = "data/alerts"):
        self._directory = directory
        self._lock = threading.Lock()
        self._current_date: Optional[str] = None
        self._fh = None
        try:
            from pathlib import Path
            Path(self._directory).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"[FileChannel] failed to create {self._directory}: {e}")

    def _ensure_open(self) -> None:
        from pathlib import Path
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y%m%d")
        if self._current_date == today and self._fh is not None:
            return
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
        path = Path(self._directory) / f"alerts_{today}.jsonl"
        try:
            self._fh = open(path, "a", buffering=1)  # line-buffered
            self._current_date = today
        except Exception as e:
            print(f"[FileChannel] open {path} failed: {e}")
            self._fh = None

    def send(self, alert: Alert) -> None:
        import json as _json
        with self._lock:
            self._ensure_open()
            if self._fh is None:
                return
            try:
                self._fh.write(_json.dumps(alert.to_dict()) + "\n")
                self._fh.flush()
            except Exception as e:
                print(f"[FileChannel] write failed: {e}")


def build_default_alert_manager(
    directory: str = "data/alerts",
    enable_stdout: bool = True,
) -> "AlertManager":
    """Construct an AlertManager wired to file + stdout, and Teams if env set.

    Wired channels:
        - FileChannel (always, unless directory creation fails)
        - PrintChannel (if `enable_stdout`)
        - TeamsChannel (only if env var GT_TEAMS_WEBHOOK_URL is set)
        - SlackChannel (only if env var GT_SLACK_WEBHOOK is set — left
          intact so you can fall back instantly by setting the env var)

    Migration note (2026-06-01): switched primary alerts from Slack to
    Teams per Option A. The SlackChannel class is preserved and still
    wires up if the legacy env var is set, so this is a config-only
    switch — no code changes needed to revert.

    Returns a ready-to-use AlertManager. Adding more channels later is
    `mgr.add_channel(YourChannel(...))`.
    """
    mgr = AlertManager()
    try:
        mgr.add_channel(FileChannel(directory=directory))
    except Exception as e:
        print(f"[AlertManager] FileChannel disabled: {e}")
    if enable_stdout:
        mgr.add_channel(PrintChannel())
    import os as _os

    # ── Primary alert sink: Microsoft Teams ──────────────────────────
    teams_webhook = _os.environ.get("GT_TEAMS_WEBHOOK_URL", "").strip()
    if teams_webhook:
        try:
            mgr.add_channel(TeamsChannel(webhook_url=teams_webhook))
            print(f"[AlertManager] Teams channel ENABLED (webhook configured)")
        except Exception as e:
            print(f"[AlertManager] Teams channel init failed: {e}")
    else:
        print(f"[AlertManager] Teams channel inert (set GT_TEAMS_WEBHOOK_URL to enable)")

    # ── Optional legacy fallback: Slack ──────────────────────────────
    # Only enabled if GT_SLACK_WEBHOOK is still set. Useful for the
    # transition period or per-event side-channel routing.
    slack_webhook = _os.environ.get("GT_SLACK_WEBHOOK", "").strip()
    if slack_webhook:
        try:
            mgr.add_channel(SlackChannel(webhook_url=slack_webhook))
            print(f"[AlertManager] Slack channel ENABLED (legacy fallback)")
        except Exception as e:
            print(f"[AlertManager] Slack channel init failed: {e}")

    return mgr


class AlertManager:
    """
    Central alert management.

    Key features:
    - Explicit edge case enumeration (ALERT_CODES)
    - Symptom-based alerts (not causes)
    - Defense in depth tracking
    - Alert history for analysis

    Usage:
        alerts = AlertManager()
        alerts.add_channel(PrintChannel())

        # Raise explicit alert
        alerts.raise_alert(
            code="ORDER_PRICE_ANOMALY",
            severity=AlertSeverity.HIGH,
            message="Order price differs from market by >1%",
            context={"symbol": "AAPL", "order_price": 150.0, "market_price": 148.5}
        )
    """

    # Explicitly cataloged alert codes
    # Every edge case must have a code here
    ALERT_CODES = {
        # Order-related (every order is critical)
        "ORDER_REJECTED": "Order was rejected by exchange",
        "ORDER_FILL_TIMEOUT": "Order not filled within expected time",
        "ORDER_PARTIAL_FILL": "Order only partially filled",
        "ORDER_CANCEL_FAILED": "Failed to cancel order",
        "ORDER_MODIFY_FAILED": "Failed to modify order",

        # Price anomalies (Jane Street "Trade Too Good" style)
        "PRICE_JUMP_DETECTED": "Price moved >5% in single update",
        "PRICE_STALE": "Price data is stale",
        "BID_ASK_SPREAD_WIDE": "Bid-ask spread >2% of price",
        "PRICE_VOLATILITY_HIGH": "Volatility >3x normal",

        # Risk checks
        "RISK_POSITION_LIMIT": "Position exceeds limit",
        "RISK_LOSS_THRESHOLD": "Unrealized loss exceeds threshold",
        "RISK_DAILY_PNL_NEGATIVE": "Daily P&L is negative",
        "RISK_EXPOSURE_HIGH": "Total exposure >80% of capital",

        # System health
        "HEARTBEAT_MISSING": "No heartbeat for >30 seconds",
        "CONNECTION_LOST": "IBKR connection lost",
        "RECONNECTION_FAILED": "Failed to reconnect after 3 attempts",
        "CIRCUIT_BREAKER_OPEN": "Circuit breaker triggered",

        # Trading hours
        "OUTSIDE_MARKET_HOURS": "Order attempted outside market hours",
        "PRE_MARKET_CLOSE": "Approaching market close with open positions",

        # Data integrity
        "TICK_SEQUENCE_GAP": "Gap detected in tick sequence",
        "DUPLICATE_ORDER_ID": "Duplicate order ID detected",
        "POSITION_MISMATCH": "Position doesn't match expected",

        # Anomaly detection (Jane Street "Trade Too Good")
        "PNL_TOO_GOOD": "P&L suspiciously high - possible bug in trading stack",
        "PNL_LOSS_EXCESSIVE": "Excessive losses vs baseline",
        "VOLUME_ANOMALY": "Volume share of market is anomalous",

        # Engine-state invariants (health-check loop + reactive paths)
        "NAKED_POSITION": "Position open at broker but no protective BUY-cover stop",
        "STOP_LOSS_MARKET_FALLBACK": "Gap-up at protective-stop placement: submitted MARKET BUY (cover) because LTP had already crossed above intended stop",
        "ENTRY_ORDER_MISSING": "Engine state MONITORING/WAITING_REENTRY but no SELL (breakdown) order resting",
        "TRIPWIRE_LOST_PENDING": "Saved pending order absent from broker on restart",
        "STALE_FEED": "No market data tick received within threshold",
        "CIRCUIT_BREAKER_DRAWDOWN": "Equity drawdown exceeded threshold within session",
        "SESSION_OPENED": "ETH session window opened - engine resuming",
        "SESSION_CLOSED": "ETH session window closed - engine pausing",

        # Startup-time refusal codes — exit-without-trading conditions
        # caught by engine.start() before any feed subscription or order.
        # Listed here so the AlertManager doesn't print "Unknown alert code"
        # noise; behaviour is unchanged.
        "STARTUP_REFUSED_NAKED": "Refused to start: broker holds a position unaccounted for by saved state",
        "STARTUP_REFUSED_CONFLICT": "Refused to start: another client_id has an active order for this ticker",
        "CUSTOM_ORPHAN_ADOPTED": "Adopted broker-held position into this client_id (orphan / lost-fill recovery)",
    }

    def __init__(self):
        self._channels: list[AlertChannel] = []
        self._history: list[Alert] = []
        self._lock = threading.Lock()
        self._alert_counts: dict[str, int] = defaultdict(int)

    def add_channel(self, channel: AlertChannel) -> None:
        self._channels.append(channel)

    def raise_alert(
        self,
        code: str,
        severity: AlertSeverity,
        message: str,
        context: Optional[dict] = None,
        correlation_id: str = "",
    ) -> None:
        """
        Raise an explicit alert.

        Args:
            code: Alert code from ALERT_CODES (or custom)
            severity: CRITICAL/HIGH/MEDIUM/LOW
            message: Human-readable description
            context: Relevant data for debugging
            correlation_id: Links to order/trade/cycle
        """
        # Verify alert code is known
        if code not in self.ALERT_CODES and not code.startswith("CUSTOM_"):
            print(f"[AlertManager] WARNING: Unknown alert code: {code}")

        alert = Alert(
            code=code,
            severity=severity,
            message=message,
            context=context or {},
            correlation_id=correlation_id,
        )

        # Log to history
        with self._lock:
            self._history.append(alert)
            self._alert_counts[code] += 1

        # Send to all channels
        for channel in self._channels:
            try:
                channel.send(alert)
            except Exception as e:
                print(f"[AlertManager] Channel error: {e}")

        return alert

    def notify_trade(self, event: str, **fields: Any) -> None:
        """Push a trade-event notification to channels that support it.

        Unlike `raise_alert`, this does NOT add to alert history (so it
        doesn't pollute the dashboard's ALERTS panel, which is reserved
        for anomalies). It just fans the event out to channels that
        implement `send_trade` (currently SlackChannel).

        Use for normal operational events that you want visible on Slack
        in real time: FILLED, REJECTED, SESSION_START, etc. The full
        audit row still goes to data/audit/order_*.csv via AuditManager.
        """
        for channel in self._channels:
            send_trade = getattr(channel, "send_trade", None)
            if callable(send_trade):
                try:
                    send_trade(event, **fields)
                except Exception as e:
                    print(f"[AlertManager] notify_trade error: {e}")

    def close(self) -> dict:
        """Best-effort drain + shutdown of any channel that supports it.

        Returns aggregated stats from SlackChannel.close() so the run_live.py
        shutdown summary can surface drops. Other channels (File, Print)
        don't need shutdown — line-buffered files flush on close.
        """
        stats: dict = {}
        for ch in self._channels:
            closer = getattr(ch, "close", None)
            if callable(closer):
                try:
                    s = closer()
                    if isinstance(s, dict):
                        stats[type(ch).__name__] = s
                except Exception as e:
                    print(f"[AlertManager] close({type(ch).__name__}) error: {e}")
        return stats

    def get_history(
        self,
        count: int = 100,
        severity: Optional[AlertSeverity] = None,
    ) -> list[Alert]:
        """Get recent alerts."""
        with self._lock:
            history = list(self._history)
        if severity:
            history = [a for a in history if a.severity == severity]
        return history[-count:]

    def get_counts(self) -> dict[str, int]:
        """Get alert counts by code."""
        with self._lock:
            return dict(self._alert_counts)


class AnomalyDetector:
    """
    Jane Street "Trade Too Good" style anomaly detection.

    Detects when something is "wrong with the world" without
    knowing the specific root cause.

    Metrics tracked:
    - P&L vs historical baseline
    - Order fill rate
    - Volume vs market volume
    - Price volatility vs baseline
    """

    def __init__(
        self,
        alert_manager: AlertManager,
        correlation_id: str = "",
    ):
        self.alerts = alert_manager
        self.correlation_id = correlation_id

        # Baseline metrics (set during warm-up)
        self._baseline_pnl_per_hour = 0.0
        self._baseline_volatility = 0.0
        self._baseline_fill_rate = 0.0

        # Current metrics
        self._current_pnl = 0.0
        self._current_trades = 0
        self._current_volume = 0

        # Anomaly thresholds
        self._pnl_threshold_pct = 0.5  # Alert if P&L >50% from expected
        self._volume_threshold_pct = 0.5  # Alert if volume unusual

    def set_baseline(
        self,
        pnl_per_hour: float,
        volatility: float,
        fill_rate: float,
    ) -> None:
        """Set baseline metrics for anomaly detection."""
        self._baseline_pnl_per_hour = pnl_per_hour
        self._baseline_volatility = volatility
        self._baseline_fill_rate = fill_rate

    def record_trade(self, pnl: float, volume: int) -> None:
        """Record a completed trade for anomaly detection."""
        self._current_pnl += pnl
        self._current_trades += 1
        self._current_volume += volume

    def check_pnl_anomaly(self) -> None:
        """
        Check if P&L is suspiciously good (Trade Too Good alert).

        Jane Street's favorite alert: "We made too much money"
        This catches bugs that are producing profitable-but-wrong behavior.
        """
        if self._baseline_pnl_per_hour == 0:
            return

        # Calculate current P&L rate
        hours_elapsed = max(1, self._current_trades / 10)  # Rough estimate
        current_rate = self._current_pnl / hours_elapsed

        # Check for anomaly
        if current_rate > self._baseline_pnl_per_hour * (1 + self._pnl_threshold_pct):
            self.alerts.raise_alert(
                code="PNL_TOO_GOOD",
                severity=AlertSeverity.CRITICAL,
                message=f"P&L rate {current_rate:.2f} is {self._pnl_threshold_pct*100}%+ above baseline {self._baseline_pnl_per_hour:.2f}",
                context={
                    "current_rate": current_rate,
                    "baseline_rate": self._baseline_pnl_per_hour,
                    "current_pnl": self._current_pnl,
                    "current_trades": self._current_trades,
                },
                correlation_id=self.correlation_id,
            )

        # Also alert if losing too much (shouldn't happen in paper, but catch bugs)
        if current_rate < self._baseline_pnl_per_hour * (1 - self._pnl_threshold_pct) * 2:
            self.alerts.raise_alert(
                code="PNL_LOSS_EXCESSIVE",
                severity=AlertSeverity.HIGH,
                message=f"Excessive losses: {current_rate:.2f} vs baseline",
                context={
                    "current_rate": current_rate,
                    "baseline_rate": self._baseline_pnl_per_hour,
                },
                correlation_id=self.correlation_id,
            )

    def check_volume_anomaly(self, market_volume: int) -> None:
        """
        Check if our volume is unusual vs market.

        If we're normally 1% of volume and suddenly we're 30%, something is wrong.
        """
        if market_volume == 0 or self._current_volume == 0:
            return

        our_share = self._current_volume / market_volume

        # If our share is >10x normal, that's suspicious
        if our_share > 0.1:  # >10% of market volume
            self.alerts.raise_alert(
                code="VOLUME_ANOMALY",
                severity=AlertSeverity.CRITICAL,
                message=f"Volume anomaly: {our_share*100:.1f}% of market volume",
                context={
                    "our_volume": self._current_volume,
                    "market_volume": market_volume,
                    "our_share_pct": our_share * 100,
                },
                correlation_id=self.correlation_id,
            )

    def check_all(self, market_volume: int = 0) -> None:
        """Run all anomaly checks."""
        self.check_pnl_anomaly()
        if market_volume > 0:
            self.check_volume_anomaly(market_volume)

"""
Unit tests for QuantLogger.
"""
import json
import io
from src.strategy.logging import QuantLogger, LogLevel


class TestQuantLogger:
    """Test QuantLogger JSON output."""

    def test_log_outputs_json(self):
        """Log outputs valid JSON."""
        buf = io.StringIO()
        logger = QuantLogger(output=buf)
        logger.log("TEST_EVENT", foo="bar", count=42)

        line = buf.getvalue().strip()
        record = json.loads(line)

        assert record["event"] == "TEST_EVENT"
        assert record["foo"] == "bar"
        assert record["count"] == 42
        assert "ts" in record
        assert record["level"] == "INFO"

    def test_log_includes_timestamp(self):
        """Log includes ISO timestamp."""
        buf = io.StringIO()
        logger = QuantLogger(output=buf)
        logger.log("TEST")

        line = buf.getvalue().strip()
        record = json.loads(line)

        assert "ts" in record
        # Should be parseable as ISO format
        from datetime import datetime
        datetime.fromisoformat(record["ts"])

    def test_log_includes_cycle_id(self):
        """Log includes cycle_id."""
        buf = io.StringIO()
        logger = QuantLogger(output=buf, trade_cycle_id="ABC123")
        logger.log("TEST")

        line = buf.getvalue().strip()
        record = json.loads(line)

        assert record["cycle_id"] == "ABC123"

    def test_debug_level(self):
        """debug() outputs DEBUG level."""
        buf = io.StringIO()
        logger = QuantLogger(output=buf)
        logger.debug("DEBUG_EVENT")

        line = buf.getvalue().strip()
        record = json.loads(line)

        assert record["level"] == "DEBUG"
        assert record["event"] == "DEBUG_EVENT"

    def test_warn_level(self):
        """warn() outputs WARN level."""
        buf = io.StringIO()
        logger = QuantLogger(output=buf)
        logger.warn("WARN_EVENT")

        line = buf.getvalue().strip()
        record = json.loads(line)

        assert record["level"] == "WARN"

    def test_error_level(self):
        """error() outputs ERROR level."""
        buf = io.StringIO()
        logger = QuantLogger(output=buf)
        logger.error("ERROR_EVENT")

        line = buf.getvalue().strip()
        record = json.loads(line)

        assert record["level"] == "ERROR"

    def test_order_submitted(self):
        """order_submitted() logs correctly."""
        buf = io.StringIO()
        logger = QuantLogger(output=buf)
        logger.order_submitted(
            order_id="ORDER_1",
            symbol="INFY",
            side="BUY",
            qty=100,
            order_type="MARKET",
            limit_price=1800.0,
        )

        line = buf.getvalue().strip()
        record = json.loads(line)

        assert record["event"] == "ORDER_SUBMITTED"
        assert record["order_id"] == "ORDER_1"
        assert record["symbol"] == "INFY"
        assert record["side"] == "BUY"
        assert record["qty"] == 100
        assert record["order_type"] == "MARKET"
        assert record["limit_price"] == 1800.0

    def test_order_filled_with_latency(self):
        """order_filled() logs with latency."""
        buf = io.StringIO()
        logger = QuantLogger(output=buf)

        # First submit to track latency
        logger.order_submitted(
            order_id="ORDER_1",
            symbol="INFY",
            side="BUY",
            qty=100,
            order_type="MARKET",
        )

        logger.order_filled(
            order_id="ORDER_1",
            qty=100,
            price=1800.0,
            commission=1.0,
        )

        # Get last line (order_filled output)
        lines = buf.getvalue().strip().split('\n')
        record = json.loads(lines[-1])

        assert record["event"] == "ORDER_FILLED"
        assert record["order_id"] == "ORDER_1"
        assert record["qty"] == 100
        assert record["price"] == 1800.0
        assert record["commission"] == 1.0
        assert "latency_ms" in record

    def test_trade_entry(self):
        """trade_entry() logs correctly."""
        buf = io.StringIO()
        logger = QuantLogger(output=buf)
        logger.trade_entry(
            price=1800.0,
            qty=100,
            stop_loss=1764.0,
            order_id="ORDER_1",
        )

        line = buf.getvalue().strip()
        record = json.loads(line)

        assert record["event"] == "TRADE_ENTRY"
        assert record["price"] == 1800.0
        assert record["qty"] == 100
        assert record["stop_loss"] == 1764.0

    def test_trade_exit(self):
        """trade_exit() logs correctly."""
        buf = io.StringIO()
        logger = QuantLogger(output=buf)
        logger.trade_exit(
            price=1764.0,
            pnl=-4000.0,
            reason="STOP_LOSS",
            order_id="ORDER_2",
        )

        line = buf.getvalue().strip()
        record = json.loads(line)

        assert record["event"] == "TRADE_EXIT"
        assert record["price"] == 1764.0
        assert record["pnl"] == -4000.0
        assert record["reason"] == "STOP_LOSS"

    def test_risk_rejected(self):
        """risk_rejected() logs with WARN level."""
        buf = io.StringIO()
        logger = QuantLogger(output=buf)
        logger.risk_rejected(reason="Order too large", order_value=100000.0)

        line = buf.getvalue().strip()
        record = json.loads(line)

        assert record["event"] == "RISK_REJECTED"
        assert record["level"] == "WARN"
        assert record["reason"] == "Order too large"
        assert record["order_value"] == 100000.0

    def test_set_cycle_id(self):
        """set_cycle_id() updates cycle_id."""
        buf = io.StringIO()
        logger = QuantLogger(output=buf, trade_cycle_id="OLD")
        logger.set_cycle_id("NEW")

        logger.log("TEST")

        line = buf.getvalue().strip()
        record = json.loads(line)

        assert record["cycle_id"] == "NEW"

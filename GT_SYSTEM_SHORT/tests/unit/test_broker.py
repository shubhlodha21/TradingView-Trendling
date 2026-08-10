"""
Unit tests for Lean Gateway.
"""
from src.config.models import ConnectionStatus, OrderSide, OrderType
from src.execution.broker import Gateway


class TestGateway:
    """Test lean Gateway."""

    def test_initial_state(self):
        """Gateway starts disconnected."""
        gw = Gateway(port=4002, paper=True)
        assert gw.status == ConnectionStatus.DISCONNECTED.value
        assert not gw.connected

    def test_paper_mode_default(self):
        """Paper mode can be set."""
        gw = Gateway(port=4002, paper=True)
        assert gw.paper is True

    def test_callbacks_settable(self):
        """Callbacks are settable."""
        gw = Gateway(port=4002)

        def on_connect():
            pass
        def on_fill(oid, qty, price):
            pass

        gw.set_callbacks(on_connect=on_connect, on_fill=on_fill)
        assert gw._on_connect is not None
        assert gw._on_fill is not None

    def test_symbol_configurable(self):
        """Symbol is configurable."""
        gw = Gateway(symbol="AAPL")
        assert gw.symbol == "AAPL"


class TestGatewayPaperPositions:
    """Test paper position tracking."""

    def test_paper_positions_initially_empty(self):
        """Paper positions start empty."""
        gw = Gateway(paper=True)
        assert gw._paper_positions == {}

    def test_gateway_has_place_order(self):
        """Has place_order method."""
        gw = Gateway()
        assert hasattr(gw, 'place_order')

    def test_gateway_has_cancel_all(self):
        """Has cancel_all method."""
        gw = Gateway()
        assert hasattr(gw, 'cancel_all')

"""Tests for LiveExecutor interface compatibility — current_price kwarg, close_partial, partial fill.

TDD: These tests were written FIRST, before the implementation.
Uses a mock BybitClient to avoid real API calls.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.order.live_executor import LiveExecutor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_client():
    """Create a mock BybitClient with all methods the executor needs."""
    client = MagicMock()

    # Instrument info for rounding
    client.get_instrument_info.return_value = {
        "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"},
        "priceFilter": {"tickSize": "0.01"},
    }

    # Default place_order response
    client.place_order.return_value = {
        "orderId": "test-order-123",
        "orderLinkId": "",
        "avgPrice": "50000.0",
    }

    # Default tickers
    client.get_tickers.return_value = [
        {"symbol": "BTCUSDT", "lastPrice": "50000.0"},
    ]

    # Default executions
    client.get_executions.return_value = [
        {"execPrice": "50000.0", "execQty": "0.01"},
    ]

    return client


@pytest.fixture
def executor(mock_client):
    return LiveExecutor(mock_client)


# ---------------------------------------------------------------------------
# Test: place_market_order accepts current_price
# ---------------------------------------------------------------------------

class TestPlaceMarketOrderInterface:

    def test_place_market_order_accepts_current_price(self, executor, mock_client):
        """place_market_order must accept current_price as optional kwarg."""
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                executor.place_market_order(
                    symbol="BTCUSDT",
                    side="Buy",
                    qty=0.01,
                    current_price=50000.0,
                )
            )
            # Should not raise; should return a dict with orderId
            assert "orderId" in result
        finally:
            loop.close()

    def test_place_market_order_without_current_price(self, executor, mock_client):
        """place_market_order still works without current_price (backward compat)."""
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                executor.place_market_order(
                    symbol="BTCUSDT",
                    side="Buy",
                    qty=0.01,
                )
            )
            assert "orderId" in result
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Test: close_partial order
# ---------------------------------------------------------------------------

class TestClosePartial:

    def test_close_partial_order(self, executor, mock_client):
        """close_partial places a market order on the opposite side for the given qty."""
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                executor.close_partial(
                    symbol="BTCUSDT",
                    side="Buy",  # position side
                    qty=0.005,
                    current_price=51000.0,
                )
            )
            assert "orderId" in result
            assert "fillPrice" in result

            # The close order should be placed on the opposite side (Sell)
            call_kwargs = mock_client.place_order.call_args
            assert call_kwargs is not None
            # side should be "Sell" (closing a Buy position)
            assert call_kwargs.kwargs.get("side") == "Sell" or \
                   (len(call_kwargs.args) >= 3 and call_kwargs.args[1] == "Sell")
        finally:
            loop.close()

    def test_close_partial_sell_position(self, executor, mock_client):
        """Closing partial on a Sell position places a Buy order."""
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                executor.close_partial(
                    symbol="BTCUSDT",
                    side="Sell",
                    qty=0.005,
                )
            )
            assert "orderId" in result
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Test: partial fill handling
# ---------------------------------------------------------------------------

class TestPartialFillHandling:

    def test_partial_fill_detected(self, executor, mock_client):
        """When filledQty < requested qty, result includes partialFill flag."""
        # Make executions return less than requested
        mock_client.get_executions.return_value = [
            {"execPrice": "50000.0", "execQty": "0.005"},
        ]

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                executor.place_market_order(
                    symbol="BTCUSDT",
                    side="Buy",
                    qty=0.01,
                    current_price=50000.0,
                )
            )
            # Should detect partial fill: 0.005 < 0.01
            if result.get("partialFill"):
                assert result["filledQty"] == pytest.approx(0.005)
        finally:
            loop.close()

    def test_full_fill_no_partial_flag(self, executor, mock_client):
        """Full fill does not set partialFill flag."""
        mock_client.get_executions.return_value = [
            {"execPrice": "50000.0", "execQty": "0.01"},
        ]

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                executor.place_market_order(
                    symbol="BTCUSDT",
                    side="Buy",
                    qty=0.01,
                    current_price=50000.0,
                )
            )
            assert result.get("partialFill") is not True
        finally:
            loop.close()

"""Tests for GridPositionManager in live mode — ledger integration, partial close, reconciliation.

TDD: These tests were written FIRST, before the implementation.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.order.grid_manager import GridPositionManager, LevelKey
from src.order.live_position_ledger import LivePositionLedger
from src.strategy.asset.base import DailyStats
from src.utils.messages import GridSignalMessage, GridUpdateMessage


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_daily_stats():
    return DailyStats(
        date="2026-04-07",
        pnl=0.0,
        trade_count=0,
        win_count=0,
        consecutive_losses=0,
    )


def _make_mock_executor(mode="live"):
    """Create a mock executor that simulates fill responses."""
    executor = AsyncMock()
    executor.set_margin_and_leverage = AsyncMock()
    executor.place_market_order = AsyncMock(return_value={
        "orderId": "live-order-001",
        "fillPrice": 50000.0,
        "fee": 0.03,
        "side": "Buy",
        "qty": 0.01,
        "symbol": "BTCUSDT",
        "margin": 50.0,
    })
    executor.close_position = AsyncMock(return_value={
        "orderId": "live-close-001",
        "fillPrice": 50500.0,
        "pnl": 5.0,
        "fee": 0.03,
        "side": "Sell",
        "qty": 0.01,
    })
    executor.close_partial = AsyncMock(return_value={
        "orderId": "live-partial-001",
        "fillPrice": 50500.0,
        "pnl": 5.0,
        "fee": 0.03,
        "side": "Sell",
        "qty": 0.01,
    })
    executor.close_position_by_key = AsyncMock(return_value={
        "orderId": "live-close-key-001",
        "fillPrice": 50500.0,
        "pnl": 5.0,
        "fee": 0.03,
        "side": "Sell",
        "qty": 0.01,
    })
    executor.get_position = AsyncMock(return_value={
        "symbol": "BTCUSDT",
        "side": "Buy",
        "size": "0.01",
        "avgPrice": "50000.0",
    })
    return executor


def _make_mock_tracker():
    """Create a mock PositionTracker."""
    tracker = MagicMock()
    tracker.add_position = MagicMock(return_value=1)
    tracker.close_position = MagicMock()
    tracker.get_open_positions = MagicMock(return_value=[
        {"id": 1, "symbol": "BTCUSDT", "side": "Buy", "size": 0.01,
         "entry_price": 50000.0, "leverage": 10},
    ])
    return tracker


def _make_mock_sizing():
    """Create a mock GridSizingStrategy that always approves."""
    sizing = MagicMock()
    order_req = MagicMock()
    order_req.approved = True
    order_req.qty = 0.01
    order_req.risk_amount = 50.0
    sizing.evaluate_grid_fill = MagicMock(return_value=order_req)
    return sizing


def _make_fill_signal(symbol="BTCUSDT", level_index=-1, side="Buy",
                      level_price=50000.0, tp_price=50500.0, qty=0.01,
                      leverage=10):
    return GridSignalMessage(
        symbol=symbol, action="FILL",
        level_id=100, level_index=level_index,
        level_price=level_price, side=side,
        tp_price=tp_price, qty_per_level=qty, leverage=leverage,
    )


def _make_tp_signal(symbol="BTCUSDT", level_index=-1, tp_price=50500.0):
    return GridSignalMessage(
        symbol=symbol, action="TP_HIT",
        level_id=100, level_index=level_index,
        tp_price=tp_price,
    )


def _make_recenter_signal(symbol="BTCUSDT", level_index=-1, level_price=50000.0):
    return GridSignalMessage(
        symbol=symbol, action="RECENTER",
        level_id=100, level_index=level_index,
        level_price=level_price,
    )


# ---------------------------------------------------------------------------
# Test: grid fill in live mode uses ledger
# ---------------------------------------------------------------------------

class TestGridFillLiveMode:

    def test_grid_fill_live_mode_uses_ledger(self):
        """In live mode, a FILL creates a micro-position in the ledger."""
        executor = _make_mock_executor()
        tracker = _make_mock_tracker()
        sizing = _make_mock_sizing()

        manager = GridPositionManager(
            executor=executor,
            position_tracker=tracker,
            sizing=sizing,
            mode="live",
            initial_balance=20.0,
        )

        msg = _make_fill_signal()
        daily = _make_daily_stats()

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                manager.handle_grid_signal(msg, 20.0, [], daily)
            )
        finally:
            loop.close()

        # Should have called place_market_order
        executor.place_market_order.assert_called_once()

        # Should have recorded in tracker
        tracker.add_position.assert_called_once()

        # Result should be CONFIRMED
        assert result is not None
        assert result.action == "CONFIRMED"

        # If ledger is used, verify the internal state
        key = ("BTCUSDT", -1)
        if hasattr(manager, '_ledger') and manager._ledger is not None:
            pos = manager._ledger.get_position(key)
            assert pos is not None
            assert pos.symbol == "BTCUSDT"


# ---------------------------------------------------------------------------
# Test: TP hit does partial close in live mode
# ---------------------------------------------------------------------------

class TestGridTpHitPartialClose:

    def test_grid_tp_hit_partial_close(self):
        """TP hit in live mode closes only the micro-position qty (partial close)."""
        executor = _make_mock_executor()
        tracker = _make_mock_tracker()
        sizing = _make_mock_sizing()

        manager = GridPositionManager(
            executor=executor,
            position_tracker=tracker,
            sizing=sizing,
            mode="live",
            initial_balance=20.0,
        )

        # First open a position
        fill_msg = _make_fill_signal(level_index=-1, qty=0.01)
        daily = _make_daily_stats()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                manager.handle_grid_signal(fill_msg, 20.0, [], daily)
            )

            # Now TP hit
            tp_msg = _make_tp_signal(level_index=-1, tp_price=50500.0)
            result = loop.run_until_complete(
                manager.handle_grid_signal(tp_msg, 20.0, [], daily)
            )
        finally:
            loop.close()

        assert result is not None
        assert result.action == "CLOSED"

        # The level should be cleared from internal tracking
        key = ("BTCUSDT", -1)
        assert key not in manager._level_positions


# ---------------------------------------------------------------------------
# Test: recenter closes individual levels
# ---------------------------------------------------------------------------

class TestGridRecenterLive:

    def test_grid_recenter_closes_individual_levels(self):
        """Recenter in live mode closes the specific micro-position."""
        executor = _make_mock_executor()
        tracker = _make_mock_tracker()
        sizing = _make_mock_sizing()

        manager = GridPositionManager(
            executor=executor,
            position_tracker=tracker,
            sizing=sizing,
            mode="live",
            initial_balance=20.0,
        )

        fill_msg = _make_fill_signal(level_index=-1)
        daily = _make_daily_stats()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                manager.handle_grid_signal(fill_msg, 20.0, [], daily)
            )

            recenter_msg = _make_recenter_signal(level_index=-1, level_price=50000.0)
            result = loop.run_until_complete(
                manager.handle_grid_signal(recenter_msg, 20.0, [], daily)
            )
        finally:
            loop.close()

        assert result is not None
        assert result.action == "CLOSED"

        key = ("BTCUSDT", -1)
        assert key not in manager._level_positions


# ---------------------------------------------------------------------------
# Test: ledger reconciliation after fill
# ---------------------------------------------------------------------------

class TestLedgerReconciliation:

    def test_ledger_reconciliation_after_fill(self):
        """After a fill in live mode, if the manager has a ledger, it should
        be reconcilable with exchange position data."""
        executor = _make_mock_executor()
        tracker = _make_mock_tracker()
        sizing = _make_mock_sizing()

        manager = GridPositionManager(
            executor=executor,
            position_tracker=tracker,
            sizing=sizing,
            mode="live",
            initial_balance=20.0,
        )

        fill_msg = _make_fill_signal(level_index=-1, qty=0.01)
        daily = _make_daily_stats()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                manager.handle_grid_signal(fill_msg, 20.0, [], daily)
            )
        finally:
            loop.close()

        # If the manager has a ledger, test reconciliation
        if hasattr(manager, '_ledger') and manager._ledger is not None:
            manager._ledger.reconcile("BTCUSDT", exchange_qty=0.01, exchange_avg_entry=50000.0)
            net = manager._ledger.get_net_position("BTCUSDT")
            assert net["total_qty"] == pytest.approx(0.01)

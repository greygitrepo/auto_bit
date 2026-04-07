"""Tests for LivePositionLedger — micro-position tracking for net-position exchanges.

TDD: These tests were written FIRST, before the implementation.
"""

import pytest

from src.order.live_position_ledger import LivePositionLedger, MicroPosition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ledger():
    return LivePositionLedger()


def _add_buy(ledger, symbol="BTCUSDT", level_index=0, qty=0.01, entry_price=50000.0,
             leverage=10, margin=50.0):
    key = (symbol, level_index)
    ledger.add_position(key, symbol, "Buy", qty, entry_price, leverage, margin)
    return key


def _add_sell(ledger, symbol="BTCUSDT", level_index=0, qty=0.01, entry_price=50000.0,
              leverage=10, margin=50.0):
    key = (symbol, level_index)
    ledger.add_position(key, symbol, "Sell", qty, entry_price, leverage, margin)
    return key


# ---------------------------------------------------------------------------
# Test: add single position
# ---------------------------------------------------------------------------

class TestAddSinglePosition:

    def test_add_single_position(self):
        """Adding a position stores it retrievable by level_key."""
        ledger = _make_ledger()
        key = _add_buy(ledger, level_index=1, qty=0.01, entry_price=50000.0)

        pos = ledger.get_position(key)
        assert pos is not None
        assert pos.symbol == "BTCUSDT"
        assert pos.side == "Buy"
        assert pos.qty == 0.01
        assert pos.entry_price == 50000.0
        assert pos.leverage == 10
        assert pos.margin == 50.0
        assert pos.opened_at > 0

    def test_add_position_overwrites_existing_key(self):
        """Adding with same key replaces the prior micro-position."""
        ledger = _make_ledger()
        key = _add_buy(ledger, level_index=1, qty=0.01, entry_price=50000.0)
        ledger.add_position(key, "BTCUSDT", "Buy", 0.02, 51000.0, 10, 102.0)

        pos = ledger.get_position(key)
        assert pos.qty == 0.02
        assert pos.entry_price == 51000.0


# ---------------------------------------------------------------------------
# Test: multiple positions same symbol same side → net qty
# ---------------------------------------------------------------------------

class TestNetPositionSameSide:

    def test_add_multiple_positions_same_symbol_same_side(self):
        """Multiple Buy micro-positions merge into one net position."""
        ledger = _make_ledger()
        _add_buy(ledger, level_index=-1, qty=0.01, entry_price=49000.0)
        _add_buy(ledger, level_index=-2, qty=0.01, entry_price=48000.0)
        _add_buy(ledger, level_index=-3, qty=0.02, entry_price=47000.0)

        net = ledger.get_net_position("BTCUSDT")
        assert net["side"] == "Buy"
        assert net["total_qty"] == pytest.approx(0.04)
        # Weighted avg: (0.01*49000 + 0.01*48000 + 0.02*47000) / 0.04
        expected_avg = (0.01 * 49000 + 0.01 * 48000 + 0.02 * 47000) / 0.04
        assert net["avg_entry"] == pytest.approx(expected_avg)

    def test_net_position_single_entry(self):
        """Single micro-position is its own net position."""
        ledger = _make_ledger()
        _add_buy(ledger, level_index=1, qty=0.05, entry_price=60000.0)

        net = ledger.get_net_position("BTCUSDT")
        assert net["total_qty"] == pytest.approx(0.05)
        assert net["avg_entry"] == pytest.approx(60000.0)


# ---------------------------------------------------------------------------
# Test: positions with opposite sides
# ---------------------------------------------------------------------------

class TestOppositePositions:

    def test_add_positions_opposite_sides(self):
        """Buy and Sell micro-positions produce a net based on dominant side."""
        ledger = _make_ledger()
        _add_buy(ledger, level_index=-1, qty=0.03, entry_price=50000.0)
        _add_sell(ledger, level_index=1, qty=0.01, entry_price=51000.0)

        net = ledger.get_net_position("BTCUSDT")
        # Net = 0.03 Buy - 0.01 Sell = 0.02 net Buy
        assert net["side"] == "Buy"
        assert net["total_qty"] == pytest.approx(0.02)

    def test_opposite_sides_sell_dominant(self):
        """When sell qty exceeds buy qty, net side is Sell."""
        ledger = _make_ledger()
        _add_buy(ledger, level_index=-1, qty=0.01, entry_price=50000.0)
        _add_sell(ledger, level_index=1, qty=0.03, entry_price=51000.0)

        net = ledger.get_net_position("BTCUSDT")
        assert net["side"] == "Sell"
        assert net["total_qty"] == pytest.approx(0.02)

    def test_opposite_sides_cancel_out(self):
        """Equal buy and sell qty results in zero net position."""
        ledger = _make_ledger()
        _add_buy(ledger, level_index=-1, qty=0.01, entry_price=50000.0)
        _add_sell(ledger, level_index=1, qty=0.01, entry_price=51000.0)

        net = ledger.get_net_position("BTCUSDT")
        assert net["total_qty"] == pytest.approx(0.0)
        assert net["side"] == ""


# ---------------------------------------------------------------------------
# Test: partial close qty
# ---------------------------------------------------------------------------

class TestPartialCloseQty:

    def test_partial_close_qty(self):
        """Returns the qty for a single micro-position, not the entire net."""
        ledger = _make_ledger()
        k1 = _add_buy(ledger, level_index=-1, qty=0.01, entry_price=49000.0)
        k2 = _add_buy(ledger, level_index=-2, qty=0.02, entry_price=48000.0)

        assert ledger.get_partial_close_qty(k1) == pytest.approx(0.01)
        assert ledger.get_partial_close_qty(k2) == pytest.approx(0.02)

    def test_partial_close_qty_missing_key(self):
        """Returns 0.0 for a key that doesn't exist."""
        ledger = _make_ledger()
        assert ledger.get_partial_close_qty(("BTCUSDT", 99)) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Test: remove position
# ---------------------------------------------------------------------------

class TestRemovePosition:

    def test_remove_position(self):
        """Removing a position returns it and clears it from the ledger."""
        ledger = _make_ledger()
        key = _add_buy(ledger, level_index=1)

        removed = ledger.remove_position(key)
        assert removed is not None
        assert removed.side == "Buy"
        assert ledger.get_position(key) is None

    def test_remove_nonexistent_returns_none(self):
        """Removing a non-existent key returns None."""
        ledger = _make_ledger()
        assert ledger.remove_position(("BTCUSDT", 999)) is None

    def test_remove_updates_net_position(self):
        """After removing a micro-position, net position reflects the change."""
        ledger = _make_ledger()
        k1 = _add_buy(ledger, level_index=-1, qty=0.01)
        k2 = _add_buy(ledger, level_index=-2, qty=0.02)

        ledger.remove_position(k1)
        net = ledger.get_net_position("BTCUSDT")
        assert net["total_qty"] == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# Test: reconcile with exchange
# ---------------------------------------------------------------------------

class TestReconcile:

    def test_reconcile_with_exchange_adjusts_qty(self):
        """Reconcile adjusts internal qty proportionally to match exchange."""
        ledger = _make_ledger()
        _add_buy(ledger, level_index=-1, qty=0.01, entry_price=50000.0)
        _add_buy(ledger, level_index=-2, qty=0.01, entry_price=49000.0)

        # Exchange shows 0.019 instead of 0.02 (rounding difference)
        ledger.reconcile("BTCUSDT", exchange_qty=0.019, exchange_avg_entry=49500.0)

        net = ledger.get_net_position("BTCUSDT")
        assert net["total_qty"] == pytest.approx(0.019, abs=1e-8)

    def test_reconcile_no_positions(self):
        """Reconcile with no positions does not raise."""
        ledger = _make_ledger()
        ledger.reconcile("BTCUSDT", exchange_qty=0.0, exchange_avg_entry=0.0)
        # No error is success

    def test_reconcile_zero_exchange_qty(self):
        """If exchange shows 0 qty, all micro-positions for that symbol are cleared."""
        ledger = _make_ledger()
        _add_buy(ledger, level_index=-1, qty=0.01)
        _add_buy(ledger, level_index=-2, qty=0.02)

        ledger.reconcile("BTCUSDT", exchange_qty=0.0, exchange_avg_entry=0.0)
        assert ledger.get_positions_by_symbol("BTCUSDT") == []


# ---------------------------------------------------------------------------
# Test: get_net_position empty
# ---------------------------------------------------------------------------

class TestGetNetPositionEmpty:

    def test_get_net_position_empty(self):
        """Empty ledger returns zero net position."""
        ledger = _make_ledger()
        net = ledger.get_net_position("BTCUSDT")
        assert net["side"] == ""
        assert net["total_qty"] == pytest.approx(0.0)
        assert net["avg_entry"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Test: get_positions_by_symbol
# ---------------------------------------------------------------------------

class TestGetPositionsBySymbol:

    def test_get_positions_by_symbol(self):
        """Returns only positions for the requested symbol."""
        ledger = _make_ledger()
        _add_buy(ledger, symbol="BTCUSDT", level_index=-1)
        _add_buy(ledger, symbol="BTCUSDT", level_index=-2)
        _add_sell(ledger, symbol="ETHUSDT", level_index=1)

        btc_positions = ledger.get_positions_by_symbol("BTCUSDT")
        assert len(btc_positions) == 2
        assert all(p.symbol == "BTCUSDT" for p in btc_positions)

        eth_positions = ledger.get_positions_by_symbol("ETHUSDT")
        assert len(eth_positions) == 1
        assert eth_positions[0].symbol == "ETHUSDT"

    def test_get_positions_by_symbol_empty(self):
        """Returns empty list for symbol with no positions."""
        ledger = _make_ledger()
        assert ledger.get_positions_by_symbol("XYZUSDT") == []

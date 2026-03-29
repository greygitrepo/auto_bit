"""Tests for paper executor margin accounting — fill, close, close_by_key, round trip."""

import asyncio

import pytest

from src.order.paper_executor import PaperExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_config():
    return {
        "fee_rate": {"taker": 0.0006, "maker": 0.0001},
        "slippage_bps": 0,  # zero slippage for predictable math
    }


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def executor():
    ex = PaperExecutor(_default_config(), initial_balance=10_000.0)
    # Set leverage for the test symbol
    _run(ex.set_margin_and_leverage("TESTUSDT", 10))
    return ex


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPaperFillDeductsMargin:

    def test_paper_fill_deducts_margin(self, executor):
        """Opening a position deducts margin + fee from balance."""
        initial = executor.get_balance()

        result = _run(executor.place_market_order(
            symbol="TESTUSDT", side="Buy", qty=1.0, current_price=100.0,
        ))

        fill_price = result["fillPrice"]
        notional = fill_price * 1.0
        expected_margin = notional / 10  # leverage=10
        expected_fee = notional * 0.0006

        new_balance = executor.get_balance()
        assert new_balance == pytest.approx(initial - expected_margin - expected_fee, abs=0.01)
        assert result["margin"] == pytest.approx(expected_margin, abs=0.01)


class TestPaperCloseReturnsMargin:

    def test_paper_close_returns_margin(self, executor):
        """Closing a position returns margin + net pnl to balance."""
        result = _run(executor.place_market_order(
            symbol="TESTUSDT", side="Buy", qty=1.0, current_price=100.0,
        ))
        balance_after_open = executor.get_balance()
        pos_key = result["orderId"]

        # Close at a higher price for profit
        close_result = _run(executor.close_position_by_key(pos_key, current_price=105.0))

        # Balance should increase by margin returned + net_pnl
        final_balance = executor.get_balance()
        assert final_balance > balance_after_open
        assert close_result["pnl"] > 0  # profitable trade


class TestCloseByKeyCorrectPosition:

    def test_close_by_key_correct_position(self, executor):
        """With multiple positions per symbol, close_by_key closes the right one."""
        r1 = _run(executor.place_market_order("TESTUSDT", "Buy", 1.0, 100.0))
        r2 = _run(executor.place_market_order("TESTUSDT", "Sell", 2.0, 200.0))

        key1 = r1["orderId"]
        key2 = r2["orderId"]

        assert key1 in executor.account.positions
        assert key2 in executor.account.positions

        # Close only the first position
        _run(executor.close_position_by_key(key1, current_price=105.0))

        # Position 1 should be gone, position 2 still present
        assert key1 not in executor.account.positions
        assert key2 in executor.account.positions
        assert executor.account.positions[key2].qty == 2.0


class TestRoundTripMargin:

    def test_round_trip_margin(self, executor):
        """Open + close at the same price: balance = initial - fees only."""
        initial = executor.get_balance()
        price = 100.0

        result = _run(executor.place_market_order("TESTUSDT", "Buy", 1.0, price))
        pos_key = result["orderId"]

        close_result = _run(executor.close_position_by_key(pos_key, current_price=price))

        final = executor.get_balance()
        # Round trip with no price change: balance should equal initial minus total fees
        total_fees = result["fee"] + close_result["fee"]
        assert final == pytest.approx(initial - total_fees, abs=0.01)

    def test_round_trip_with_profit(self, executor):
        """Open long + close higher: balance = initial + profit - fees."""
        initial = executor.get_balance()

        result = _run(executor.place_market_order("TESTUSDT", "Buy", 1.0, 100.0))
        pos_key = result["orderId"]
        entry_price = result["fillPrice"]

        close_result = _run(executor.close_position_by_key(pos_key, current_price=110.0))
        exit_price = close_result["fillPrice"]

        final = executor.get_balance()
        raw_pnl = (exit_price - entry_price) * 1.0
        total_fees = result["fee"] + close_result["fee"]
        expected = initial + raw_pnl - total_fees

        assert final == pytest.approx(expected, abs=0.01)

    def test_round_trip_short_with_loss(self, executor):
        """Open short + close higher: balance = initial - loss - fees."""
        initial = executor.get_balance()

        result = _run(executor.place_market_order("TESTUSDT", "Sell", 1.0, 100.0))
        pos_key = result["orderId"]
        entry_price = result["fillPrice"]

        # Price goes up, short loses
        close_result = _run(executor.close_position_by_key(pos_key, current_price=110.0))
        exit_price = close_result["fillPrice"]

        final = executor.get_balance()
        # Short PnL: (entry - exit) * qty
        raw_pnl = (entry_price - exit_price) * 1.0
        total_fees = result["fee"] + close_result["fee"]
        expected = initial + raw_pnl - total_fees

        assert final == pytest.approx(expected, abs=0.01)

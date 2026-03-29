"""Tests for GridEngine — grid creation, fill detection, TP hits, recycling, recentering."""

import pytest

from src.strategy.position.base import (
    BiasDirection,
    GridAction,
    GridLevel,
    GridLevelStatus,
    GridState,
)
from src.strategy.position.grid_engine import GridEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_config(**overrides):
    cfg = {
        "num_levels": 10,
        "default_buy_levels": 5,
        "default_sell_levels": 5,
        "range_atr_multiplier": 2.5,
        "min_range_pct": 1.0,
        "max_range_pct": 8.0,
        "recenter_threshold_pct": 1.5,
        "max_open_levels": 6,
    }
    cfg.update(overrides)
    return cfg


def _make_engine(**overrides):
    return GridEngine(_default_config(**overrides))


def _create_symmetric_grid(engine=None):
    """Create a basic symmetric grid at center=100, atr giving a nice range."""
    if engine is None:
        engine = _make_engine()
    # atr_1h=2.0 => raw_range=5.0 => 5% of 100 => within [1%,8%] => clamped to 5.0
    return engine.create_grid(
        center_price=100.0,
        atr_1h=2.0,
        bias_direction=BiasDirection.NEUTRAL,
        level_shift=0,
        qty_per_level=1.0,
        leverage=5,
        mode="paper",
        symbol="TESTUSDT",
    )


# ---------------------------------------------------------------------------
# Grid creation
# ---------------------------------------------------------------------------


class TestCreateGrid:

    def test_create_grid_symmetric(self):
        """10 levels: 5 buy below center, 5 sell above center, evenly spaced."""
        engine = _make_engine()
        state = _create_symmetric_grid(engine)

        assert state.num_buy_levels == 5
        assert state.num_sell_levels == 5
        assert len(state.levels) == 10

        buy_levels = sorted(
            [lv for lv in state.levels if lv.side == "Buy"],
            key=lambda l: l.level_index,
        )
        sell_levels = sorted(
            [lv for lv in state.levels if lv.side == "Sell"],
            key=lambda l: l.level_index,
        )

        assert len(buy_levels) == 5
        assert len(sell_levels) == 5

        spacing = state.grid_spacing
        assert spacing > 0

        # Buy levels should be below center, each one spacing further away
        for i, lv in enumerate(buy_levels):
            # level_index goes -5, -4, -3, -2, -1 when sorted
            expected_idx = -(5 - i)
            assert lv.level_index == expected_idx
            expected_price = 100.0 - spacing * abs(expected_idx)
            assert lv.price == pytest.approx(expected_price, abs=1e-6)
            assert lv.status == GridLevelStatus.PENDING

        # Sell levels above center
        for i, lv in enumerate(sell_levels):
            expected_idx = i + 1
            assert lv.level_index == expected_idx
            expected_price = 100.0 + spacing * expected_idx
            assert lv.price == pytest.approx(expected_price, abs=1e-6)
            assert lv.status == GridLevelStatus.PENDING

        # Spacing should be uniform within buy and sell groups.
        # There is a gap of 2*spacing across center (no level at center itself).
        buy_prices = sorted(lv.price for lv in state.levels if lv.side == "Buy")
        for j in range(1, len(buy_prices)):
            assert buy_prices[j] - buy_prices[j - 1] == pytest.approx(spacing, abs=1e-6)

        sell_prices = sorted(lv.price for lv in state.levels if lv.side == "Sell")
        for j in range(1, len(sell_prices)):
            assert sell_prices[j] - sell_prices[j - 1] == pytest.approx(spacing, abs=1e-6)

        # Gap across center is 2 * spacing (buy at center-spacing, sell at center+spacing)
        assert sell_prices[0] - buy_prices[-1] == pytest.approx(2 * spacing, abs=1e-6)

    def test_create_grid_with_bias(self):
        """Bullish bias (positive level_shift) allocates more buy levels."""
        engine = _make_engine()
        state = engine.create_grid(
            center_price=100.0,
            atr_1h=2.0,
            bias_direction=BiasDirection.BULLISH,
            level_shift=2,
            qty_per_level=1.0,
            leverage=5,
        )
        assert state.num_buy_levels == 7  # 5 + 2
        assert state.num_sell_levels == 3  # 10 - 7
        buy_count = sum(1 for lv in state.levels if lv.side == "Buy")
        sell_count = sum(1 for lv in state.levels if lv.side == "Sell")
        assert buy_count == 7
        assert sell_count == 3


# ---------------------------------------------------------------------------
# Fill detection
# ---------------------------------------------------------------------------


class TestCheckFills:

    def test_check_fills_buy(self):
        """Candle low touching a buy level produces a FILL signal."""
        engine = _make_engine()
        state = _create_symmetric_grid(engine)
        spacing = state.grid_spacing

        # The first buy level is at center - spacing
        buy_lv = [lv for lv in state.levels if lv.level_index == -1][0]
        target_price = buy_lv.price

        candle = {"low": target_price, "high": 100.0, "timestamp": 1000}
        signals = engine.check_fills(candle, state.levels)

        assert len(signals) >= 1
        fill_sig = [s for s in signals if s.level_index == -1][0]
        assert fill_sig.action == GridAction.FILL
        assert fill_sig.side == "Buy"
        assert buy_lv.status == GridLevelStatus.FILLED

    def test_check_fills_sell(self):
        """Candle high touching a sell level produces a FILL signal."""
        engine = _make_engine()
        state = _create_symmetric_grid(engine)

        sell_lv = [lv for lv in state.levels if lv.level_index == 1][0]
        target_price = sell_lv.price

        candle = {"low": 100.0, "high": target_price, "timestamp": 2000}
        signals = engine.check_fills(candle, state.levels)

        assert len(signals) >= 1
        fill_sig = [s for s in signals if s.level_index == 1][0]
        assert fill_sig.action == GridAction.FILL
        assert fill_sig.side == "Sell"
        assert sell_lv.status == GridLevelStatus.FILLED

    def test_check_fills_max_open(self):
        """No more than max_open_levels can be filled at once."""
        engine = _make_engine(max_open_levels=2)
        state = _create_symmetric_grid(engine)

        # A candle that spans the entire grid — all levels would be touched
        all_prices = [lv.price for lv in state.levels]
        candle = {"low": min(all_prices) - 1, "high": max(all_prices) + 1, "timestamp": 3000}
        signals = engine.check_fills(candle, state.levels)

        assert len(signals) == 2
        filled = [lv for lv in state.levels if lv.status == GridLevelStatus.FILLED]
        assert len(filled) == 2


# ---------------------------------------------------------------------------
# TP hit detection
# ---------------------------------------------------------------------------


class TestCheckTpHits:

    def test_check_tp_hits(self):
        """TP price reached on a FILLED/TP_SET level marks it COMPLETED."""
        engine = _make_engine()
        state = _create_symmetric_grid(engine)

        # Manually fill a buy level and set TP_SET
        buy_lv = [lv for lv in state.levels if lv.level_index == -1][0]
        buy_lv.status = GridLevelStatus.TP_SET
        buy_lv.fill_price = buy_lv.price

        # TP for buy level is one spacing above the buy price
        candle = {"low": buy_lv.price, "high": buy_lv.tp_price, "timestamp": 4000}
        signals = engine.check_tp_hits(candle, state.levels)

        assert len(signals) == 1
        assert signals[0].action == GridAction.TP_HIT
        assert buy_lv.status == GridLevelStatus.COMPLETED


# ---------------------------------------------------------------------------
# Recycle completed levels
# ---------------------------------------------------------------------------


class TestRecycleCompleted:

    def test_recycle_completed(self):
        """Completed levels are reset back to PENDING."""
        engine = _make_engine()
        state = _create_symmetric_grid(engine)

        # Mark two levels as COMPLETED
        state.levels[0].status = GridLevelStatus.COMPLETED
        state.levels[0].fill_price = 99.0
        state.levels[0].pnl = 0.5
        state.levels[1].status = GridLevelStatus.COMPLETED

        count = engine.recycle_completed(state.levels)
        assert count == 2
        assert state.levels[0].status == GridLevelStatus.PENDING
        assert state.levels[0].fill_price == 0.0
        assert state.levels[0].pnl == 0.0
        assert state.levels[1].status == GridLevelStatus.PENDING


# ---------------------------------------------------------------------------
# Recentering
# ---------------------------------------------------------------------------


class TestShouldRecenter:

    def test_should_recenter(self):
        """Price drifting beyond threshold triggers recenter."""
        engine = _make_engine(recenter_threshold_pct=1.5)
        state = _create_symmetric_grid(engine)

        # 1.5% drift = threshold
        # Price at 101.6 => drift = 1.6% > 1.5% => True
        assert engine.should_recenter(101.6, state) is True

        # Price at 100.5 => drift = 0.5% < 1.5% => False
        assert engine.should_recenter(100.5, state) is False

    def test_should_recenter_zero_center(self):
        """Zero center price should not trigger recenter."""
        engine = _make_engine()
        state = GridState(center_price=0.0)
        assert engine.should_recenter(100.0, state) is False


# ---------------------------------------------------------------------------
# Range clamping
# ---------------------------------------------------------------------------


class TestRangeClamping:

    def test_range_clamping_min(self):
        """Very small ATR is clamped to min_range_pct of center price."""
        engine = _make_engine(min_range_pct=1.0, max_range_pct=8.0)
        # atr=0.001 => raw_range=0.0025 => min_range = 100*0.01 = 1.0
        state = engine.create_grid(
            center_price=100.0,
            atr_1h=0.001,
            bias_direction=BiasDirection.NEUTRAL,
            level_shift=0,
            qty_per_level=1.0,
            leverage=5,
        )
        assert state.grid_range == pytest.approx(1.0, abs=1e-6)

    def test_range_clamping_max(self):
        """Very large ATR is clamped to max_range_pct of center price."""
        engine = _make_engine(min_range_pct=1.0, max_range_pct=8.0)
        # atr=100 => raw_range=250 => max_range = 100*0.08 = 8.0
        state = engine.create_grid(
            center_price=100.0,
            atr_1h=100.0,
            bias_direction=BiasDirection.NEUTRAL,
            level_shift=0,
            qty_per_level=1.0,
            leverage=5,
        )
        assert state.grid_range == pytest.approx(8.0, abs=1e-6)

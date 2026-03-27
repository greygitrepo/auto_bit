"""Tests for Phase 2 trailing stop entry_price protection logic."""

import pytest
from src.strategy.position.base import TrailingStopState
from src.strategy.position.momentum_scalper import TrailingStopManager


class TestTrailingStopEntryPriceProtection:
    """Verify trailing_sl never crosses entry_price."""

    def _make_state(self, entry_price, activation_price, side="LONG"):
        return TrailingStopState(
            active=False,
            activation_price=activation_price,
            entry_price=entry_price,
        )

    def _config(self, callback_atr_multiplier=0.5):
        return {
            "activation_r": 1.0,
            "callback_atr_multiplier": callback_atr_multiplier,
        }

    # ---- LONG side ----

    def test_long_activation_trailing_sl_not_below_entry(self):
        """LONG: trailing_sl >= entry_price on activation."""
        entry = 100.0
        sl_distance = 1.0  # ATR*2.0
        state = TrailingStopManager.create_initial_state(entry, sl_distance, "LONG", activation_r=1.0)
        # activation_price = 101.0
        assert state.entry_price == entry
        assert state.activation_price == 101.0

        # Activate at exactly activation_price, with large callback
        atr = 2.0  # callback = 2.0 * 0.5 = 1.0, which would put trailing_sl at 100.0
        state, close = TrailingStopManager.update(state, 101.0, "LONG", atr, self._config())
        assert state.active is True
        assert state.trailing_sl >= entry, (
            f"trailing_sl {state.trailing_sl} must be >= entry {entry}"
        )

    def test_long_activation_large_callback_clamped_to_entry(self):
        """LONG: when callback > (current_price - entry), trailing_sl is clamped to entry."""
        entry = 100.0
        state = TrailingStopManager.create_initial_state(entry, 1.0, "LONG", activation_r=1.0)

        # callback_distance = 3.0 * 0.5 = 1.5, current_price - 1.5 = 99.5 < entry
        atr = 3.0
        state, close = TrailingStopManager.update(state, 101.0, "LONG", atr, self._config())
        assert state.active is True
        assert state.trailing_sl == entry  # clamped to entry_price

    def test_long_highwater_update_protects_entry(self):
        """LONG: high-water mark update keeps trailing_sl >= entry."""
        entry = 100.0
        state = TrailingStopManager.create_initial_state(entry, 1.0, "LONG", activation_r=1.0)

        # Activate normally
        state, _ = TrailingStopManager.update(state, 101.5, "LONG", 0.5, self._config())
        assert state.active is True
        assert state.trailing_sl >= entry

        # New high with large callback
        atr = 4.0  # callback = 2.0
        state, _ = TrailingStopManager.update(state, 101.6, "LONG", atr, self._config())
        assert state.trailing_sl >= entry

    def test_long_normal_case_trailing_sl_above_entry(self):
        """LONG: normal case where trailing_sl is naturally above entry."""
        entry = 100.0
        state = TrailingStopManager.create_initial_state(entry, 1.0, "LONG", activation_r=1.0)

        # Price well above entry, small callback
        state, _ = TrailingStopManager.update(state, 103.0, "LONG", 0.4, self._config())
        assert state.active is True
        # trailing_sl = 103.0 - 0.2 = 102.8 > entry naturally
        assert state.trailing_sl == pytest.approx(102.8, abs=0.01)

    # ---- SHORT side ----

    def test_short_activation_trailing_sl_not_above_entry(self):
        """SHORT: trailing_sl <= entry_price on activation."""
        entry = 100.0
        sl_distance = 1.0
        state = TrailingStopManager.create_initial_state(entry, sl_distance, "SHORT", activation_r=1.0)
        # activation_price = 99.0
        assert state.activation_price == 99.0

        # Large callback: callback = 2.0*0.5 = 1.0, trailing = 99.0 + 1.0 = 100.0 == entry
        atr = 2.0
        state, close = TrailingStopManager.update(state, 99.0, "SHORT", atr, self._config())
        assert state.active is True
        assert state.trailing_sl <= entry, (
            f"trailing_sl {state.trailing_sl} must be <= entry {entry}"
        )

    def test_short_activation_large_callback_clamped_to_entry(self):
        """SHORT: when callback would push trailing_sl above entry, clamp it."""
        entry = 100.0
        state = TrailingStopManager.create_initial_state(entry, 1.0, "SHORT", activation_r=1.0)

        # callback = 3.0*0.5 = 1.5, trailing = 99.0 + 1.5 = 100.5 > entry → clamp
        atr = 3.0
        state, close = TrailingStopManager.update(state, 99.0, "SHORT", atr, self._config())
        assert state.active is True
        assert state.trailing_sl == entry  # clamped

    def test_short_lowwater_update_protects_entry(self):
        """SHORT: low-water mark update keeps trailing_sl <= entry."""
        entry = 100.0
        state = TrailingStopManager.create_initial_state(entry, 1.0, "SHORT", activation_r=1.0)

        # Activate
        state, _ = TrailingStopManager.update(state, 98.5, "SHORT", 0.5, self._config())
        assert state.active is True
        assert state.trailing_sl <= entry

        # New low with large callback
        atr = 4.0
        state, _ = TrailingStopManager.update(state, 98.4, "SHORT", atr, self._config())
        assert state.trailing_sl <= entry

    def test_short_normal_case_trailing_sl_below_entry(self):
        """SHORT: normal case where trailing_sl is naturally below entry."""
        entry = 100.0
        state = TrailingStopManager.create_initial_state(entry, 1.0, "SHORT", activation_r=1.0)

        # Price well below entry, small callback
        state, _ = TrailingStopManager.update(state, 97.0, "SHORT", 0.4, self._config())
        assert state.active is True
        # trailing_sl = 97.0 + 0.2 = 97.2 < entry naturally
        assert state.trailing_sl == pytest.approx(97.2, abs=0.01)


class TestTrailingStopStateEntryPrice:
    """Verify entry_price field in TrailingStopState."""

    def test_default_entry_price(self):
        state = TrailingStopState()
        assert state.entry_price == 0.0

    def test_create_initial_state_stores_entry_price(self):
        state = TrailingStopManager.create_initial_state(
            entry_price=105.5, sl_distance=1.0, side="LONG", activation_r=1.0
        )
        assert state.entry_price == 105.5

    def test_create_initial_state_short_stores_entry_price(self):
        state = TrailingStopManager.create_initial_state(
            entry_price=200.0, sl_distance=2.0, side="SHORT", activation_r=1.0
        )
        assert state.entry_price == 200.0
        assert state.activation_price == 198.0

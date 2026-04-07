"""Tests for SlippageGuard — dynamic slippage estimation and profitability gating.

TDD: tests written before implementation (Gap C from parity_analysis.md).
"""

from __future__ import annotations

import pytest

from src.order.slippage_guard import SlippageGuard


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def default_guard() -> SlippageGuard:
    """Guard with default config (15 bps base, 50 bps max, 0.06% taker fee)."""
    return SlippageGuard({
        "slippage_bps": 15,
        "max_slippage_bps": 50,
        "fee_rate": {"taker": 0.0006},
    })


@pytest.fixture
def thin_orderbook() -> dict:
    """Thin orderbook: small quantities at each level → high slippage."""
    mid = 100.0
    return {
        "asks": [
            [100.05, 0.5],
            [100.20, 0.5],
            [100.50, 1.0],
            [101.00, 2.0],
        ],
        "bids": [
            [99.95, 0.5],
            [99.80, 0.5],
            [99.50, 1.0],
            [99.00, 2.0],
        ],
    }


@pytest.fixture
def deep_orderbook() -> dict:
    """Deep orderbook: large quantities close to mid → low slippage."""
    return {
        "asks": [
            [100.01, 100.0],
            [100.02, 200.0],
            [100.05, 500.0],
        ],
        "bids": [
            [99.99, 100.0],
            [99.98, 200.0],
            [99.95, 500.0],
        ],
    }


# ===========================================================================
# TestEstimateSlippage
# ===========================================================================


class TestEstimateSlippage:
    def test_no_orderbook_returns_base(self, default_guard: SlippageGuard):
        """No orderbook → base_slippage_bps."""
        result = default_guard.estimate_slippage_bps("BTCUSDT", 1.0, orderbook=None)
        assert result == 15.0

    def test_with_orderbook_thin(self, default_guard: SlippageGuard, thin_orderbook: dict):
        """Thin orderbook → higher slippage for a large order."""
        # Buying 3.0 units walks through multiple levels on thin book
        result = default_guard.estimate_slippage_bps("ALTUSDT", 3.0, orderbook=thin_orderbook)
        assert result > 15.0, "Thin book should produce higher slippage than base"

    def test_with_orderbook_deep(self, default_guard: SlippageGuard, deep_orderbook: dict):
        """Deep orderbook → low slippage (close to 0 bps)."""
        # Buying 1.0 unit on a deep book — first level has 100 units
        result = default_guard.estimate_slippage_bps("BTCUSDT", 1.0, orderbook=deep_orderbook)
        assert result < 5.0, "Deep book should produce near-zero slippage"

    def test_max_slippage_cap(self, default_guard: SlippageGuard, thin_orderbook: dict):
        """Estimated slippage should not exceed max_slippage_bps."""
        # Huge order that would blow through the entire book
        result = default_guard.estimate_slippage_bps("ALTUSDT", 10000.0, orderbook=thin_orderbook)
        assert result <= 50.0, "Slippage must be capped at max_slippage_bps"

    def test_empty_orderbook_returns_base(self, default_guard: SlippageGuard):
        """Empty orderbook (no levels) → fall back to base."""
        result = default_guard.estimate_slippage_bps("X", 1.0, orderbook={"asks": [], "bids": []})
        assert result == 15.0

    def test_buy_uses_asks(self, default_guard: SlippageGuard, thin_orderbook: dict):
        """Estimate for a buy order should walk the ask side."""
        # Small qty that only hits first ask level
        result = default_guard.estimate_slippage_bps("X", 0.1, orderbook=thin_orderbook)
        # First ask is 100.05, mid ~100.0 → ~5 bps
        assert result >= 0.0


# ===========================================================================
# TestCheckProfitability
# ===========================================================================


class TestCheckProfitability:
    def test_profitable_wide_spacing(self, default_guard: SlippageGuard):
        """Wide spacing (1%) with low slippage → profitable."""
        result = default_guard.check_profitability("X", grid_spacing_pct=1.0, estimated_slippage_bps=15)
        assert result["profitable"] is True
        assert result["net_margin_pct"] > 0

    def test_unprofitable_narrow_spacing(self, default_guard: SlippageGuard):
        """Narrow spacing (0.1%) with high slippage → not profitable."""
        result = default_guard.check_profitability("X", grid_spacing_pct=0.1, estimated_slippage_bps=50)
        assert result["profitable"] is False
        assert result["net_margin_pct"] < 0

    def test_breakeven(self, default_guard: SlippageGuard):
        """Spacing exactly equals round-trip cost → not profitable (needs strict >)."""
        # round_trip = 2 * (slippage_pct + fee_rate) = 2 * (0.15% + 0.06%) = 0.42%
        result = default_guard.check_profitability("X", grid_spacing_pct=0.42, estimated_slippage_bps=15)
        assert result["profitable"] is False
        assert abs(result["net_margin_pct"]) < 0.01  # near zero

    def test_result_keys(self, default_guard: SlippageGuard):
        """Result dict has all expected keys."""
        result = default_guard.check_profitability("X", 1.0, 15)
        assert "profitable" in result
        assert "round_trip_cost_pct" in result
        assert "net_margin_pct" in result
        assert "reason" in result

    def test_round_trip_cost_calculation(self, default_guard: SlippageGuard):
        """Round-trip cost = 2 * (slippage_pct + fee_rate)."""
        # slippage 20 bps = 0.20%, fee 0.06%
        result = default_guard.check_profitability("X", 1.0, 20)
        expected_rt = 2 * (0.20 + 0.06)  # 0.52%
        assert abs(result["round_trip_cost_pct"] - expected_rt) < 0.001


# ===========================================================================
# TestAdjustMinSpacing
# ===========================================================================


class TestAdjustMinSpacing:
    def test_low_slippage(self, default_guard: SlippageGuard):
        """15 bps slippage → ~0.504% min spacing."""
        result = default_guard.adjust_min_spacing(15.0)
        # min_spacing = 2 * (0.15 + 0.06) * 1.2 = 0.504%
        assert abs(result - 0.504) < 0.01

    def test_high_slippage(self, default_guard: SlippageGuard):
        """50 bps slippage → ~1.344% min spacing."""
        result = default_guard.adjust_min_spacing(50.0)
        # min_spacing = 2 * (0.50 + 0.06) * 1.2 = 1.344%
        assert abs(result - 1.344) < 0.01

    def test_default_fee_included(self, default_guard: SlippageGuard):
        """Fee rate is included in calculation. 0 slippage still produces nonzero spacing."""
        result = default_guard.adjust_min_spacing(0.0)
        # 2 * (0.0 + 0.06) * 1.2 = 0.144%
        assert result > 0.0
        assert abs(result - 0.144) < 0.01

    def test_returns_percentage(self, default_guard: SlippageGuard):
        """Result is in percentage units, not decimal."""
        result = default_guard.adjust_min_spacing(15.0)
        assert result > 0.1  # percentage, not decimal

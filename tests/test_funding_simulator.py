"""Tests for FundingSimulator — simulates Bybit 8h funding charges in paper mode.

TDD: tests written before implementation (Gap E from parity_analysis.md).
"""

from __future__ import annotations

import calendar
from datetime import datetime, timezone

import pytest

from src.order.funding_simulator import FundingSimulator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_ts(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    """Create a UTC unix timestamp."""
    dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    return dt.timestamp()


def _make_position(
    symbol: str = "BTCUSDT",
    side: str = "Buy",
    size: float = 1.0,
    entry_price: float = 50000.0,
    leverage: int = 5,
) -> dict:
    return {
        "symbol": symbol,
        "side": side,
        "size": size,
        "entry_price": entry_price,
        "leverage": leverage,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sim() -> FundingSimulator:
    return FundingSimulator()


@pytest.fixture
def sim_disabled() -> FundingSimulator:
    return FundingSimulator({"funding_simulation": {"enabled": False}})


# ===========================================================================
# TestUpdateRate
# ===========================================================================


class TestUpdateRate:
    def test_update_and_retrieve(self, sim: FundingSimulator):
        sim.update_rate("BTCUSDT", 0.0001)
        assert sim._funding_rates["BTCUSDT"] == 0.0001

    def test_update_overwrites(self, sim: FundingSimulator):
        sim.update_rate("BTCUSDT", 0.0001)
        sim.update_rate("BTCUSDT", 0.0003)
        assert sim._funding_rates["BTCUSDT"] == 0.0003


# ===========================================================================
# TestCheckAndApply
# ===========================================================================


class TestCheckAndApply:
    def test_funding_applied_at_interval(self, sim: FundingSimulator):
        """Funding applied when 8h interval passes."""
        sim.update_rate("BTCUSDT", 0.0001)  # 0.01%
        pos = _make_position()

        # Set last funding to 00:00
        t0 = _utc_ts(2026, 1, 1, 0, 0)
        sim._last_funding_times["BTCUSDT"] = t0

        # Now it's 08:01 — 8h interval passed
        t1 = _utc_ts(2026, 1, 1, 8, 1)
        results = sim.check_and_apply([pos], current_time=t1)
        assert len(results) == 1
        assert results[0]["symbol"] == "BTCUSDT"

    def test_no_funding_before_interval(self, sim: FundingSimulator):
        """No charge if interval hasn't passed."""
        sim.update_rate("BTCUSDT", 0.0001)
        pos = _make_position()

        # Last funding at 00:00, current at 07:59 → no new interval
        t0 = _utc_ts(2026, 1, 1, 0, 0)
        sim._last_funding_times["BTCUSDT"] = t0
        t1 = _utc_ts(2026, 1, 1, 7, 59)
        results = sim.check_and_apply([pos], current_time=t1)
        assert len(results) == 0

    def test_long_positive_rate_pays(self, sim: FundingSimulator):
        """Long + positive rate = negative payment (pays funding)."""
        sim.update_rate("BTCUSDT", 0.0001)
        pos = _make_position(side="Buy", size=1.0, entry_price=50000.0)

        t0 = _utc_ts(2026, 1, 1, 0, 0)
        sim._last_funding_times["BTCUSDT"] = t0
        t1 = _utc_ts(2026, 1, 1, 8, 1)

        results = sim.check_and_apply([pos], current_time=t1)
        assert len(results) == 1
        # Long pays when rate is positive → negative payment
        assert results[0]["funding_payment"] < 0
        # position_value = size * entry_price = 50000
        # payment = -50000 * 0.0001 = -5.0
        assert abs(results[0]["funding_payment"] - (-5.0)) < 0.01

    def test_short_positive_rate_receives(self, sim: FundingSimulator):
        """Short + positive rate = positive payment (receives funding)."""
        sim.update_rate("BTCUSDT", 0.0001)
        pos = _make_position(side="Sell", size=1.0, entry_price=50000.0)

        t0 = _utc_ts(2026, 1, 1, 0, 0)
        sim._last_funding_times["BTCUSDT"] = t0
        t1 = _utc_ts(2026, 1, 1, 8, 1)

        results = sim.check_and_apply([pos], current_time=t1)
        assert len(results) == 1
        assert results[0]["funding_payment"] > 0
        assert abs(results[0]["funding_payment"] - 5.0) < 0.01

    def test_long_negative_rate_receives(self, sim: FundingSimulator):
        """Long + negative rate = positive payment (receives funding)."""
        sim.update_rate("BTCUSDT", -0.0002)
        pos = _make_position(side="Buy", size=1.0, entry_price=50000.0)

        t0 = _utc_ts(2026, 1, 1, 0, 0)
        sim._last_funding_times["BTCUSDT"] = t0
        t1 = _utc_ts(2026, 1, 1, 8, 1)

        results = sim.check_and_apply([pos], current_time=t1)
        assert len(results) == 1
        assert results[0]["funding_payment"] > 0
        assert abs(results[0]["funding_payment"] - 10.0) < 0.01

    def test_multiple_positions(self, sim: FundingSimulator):
        """Multiple positions processed correctly."""
        sim.update_rate("BTCUSDT", 0.0001)
        sim.update_rate("ETHUSDT", 0.0002)

        positions = [
            _make_position("BTCUSDT", "Buy", 1.0, 50000.0),
            _make_position("ETHUSDT", "Sell", 10.0, 3000.0),
        ]

        t0 = _utc_ts(2026, 1, 1, 0, 0)
        sim._last_funding_times["BTCUSDT"] = t0
        sim._last_funding_times["ETHUSDT"] = t0
        t1 = _utc_ts(2026, 1, 1, 8, 1)

        results = sim.check_and_apply(positions, current_time=t1)
        assert len(results) == 2
        symbols = {r["symbol"] for r in results}
        assert symbols == {"BTCUSDT", "ETHUSDT"}

    def test_disabled_returns_empty(self, sim_disabled: FundingSimulator):
        """Disabled simulator returns empty list."""
        sim_disabled.update_rate("BTCUSDT", 0.0001)
        pos = _make_position()
        t0 = _utc_ts(2026, 1, 1, 0, 0)
        sim_disabled._last_funding_times["BTCUSDT"] = t0
        t1 = _utc_ts(2026, 1, 1, 8, 1)
        results = sim_disabled.check_and_apply([pos], current_time=t1)
        assert len(results) == 0

    def test_first_call_initializes_last_funding(self, sim: FundingSimulator):
        """First call for a symbol initializes last_funding_time without charging."""
        sim.update_rate("NEWCOIN", 0.0001)
        pos = _make_position("NEWCOIN")
        t1 = _utc_ts(2026, 1, 1, 8, 1)
        results = sim.check_and_apply([pos], current_time=t1)
        # First call should initialize, not charge
        assert len(results) == 0
        assert "NEWCOIN" in sim._last_funding_times


# ===========================================================================
# TestNextFundingTime
# ===========================================================================


class TestNextFundingTime:
    def test_next_funding_from_morning(self, sim: FundingSimulator):
        """Before 08:00 UTC → next at 08:00."""
        t = _utc_ts(2026, 1, 1, 5, 30)
        nxt = sim.get_next_funding_time(current_time=t)
        expected = _utc_ts(2026, 1, 1, 8, 0)
        assert nxt == expected

    def test_next_funding_from_afternoon(self, sim: FundingSimulator):
        """After 16:00 UTC → next at 00:00 next day."""
        t = _utc_ts(2026, 1, 1, 17, 0)
        nxt = sim.get_next_funding_time(current_time=t)
        expected = _utc_ts(2026, 1, 2, 0, 0)
        assert nxt == expected

    def test_next_funding_between_8_and_16(self, sim: FundingSimulator):
        """Between 08:00 and 16:00 → next at 16:00."""
        t = _utc_ts(2026, 1, 1, 12, 0)
        nxt = sim.get_next_funding_time(current_time=t)
        expected = _utc_ts(2026, 1, 1, 16, 0)
        assert nxt == expected

    def test_next_funding_exact_boundary(self, sim: FundingSimulator):
        """Exactly at 08:00 → next at 16:00 (not 08:00 again)."""
        t = _utc_ts(2026, 1, 1, 8, 0)
        nxt = sim.get_next_funding_time(current_time=t)
        expected = _utc_ts(2026, 1, 1, 16, 0)
        assert nxt == expected


# ===========================================================================
# TestEstimateDailyCost
# ===========================================================================


class TestEstimateDailyCost:
    def test_daily_cost_positive_rate(self, sim: FundingSimulator):
        """3 funding events x rate x notional for long position."""
        sim.update_rate("BTCUSDT", 0.0001)
        positions = [_make_position("BTCUSDT", "Buy", 1.0, 50000.0)]
        cost = sim.estimate_daily_funding_cost(positions)
        # 3 * 50000 * 0.0001 = 15.0 (cost for longs paying)
        assert abs(cost - 15.0) < 0.01

    def test_daily_cost_no_positions(self, sim: FundingSimulator):
        """Empty positions → 0."""
        cost = sim.estimate_daily_funding_cost([])
        assert cost == 0.0

    def test_daily_cost_short_positive_rate(self, sim: FundingSimulator):
        """Short + positive rate → negative cost (receives money)."""
        sim.update_rate("BTCUSDT", 0.0001)
        positions = [_make_position("BTCUSDT", "Sell", 1.0, 50000.0)]
        cost = sim.estimate_daily_funding_cost(positions)
        # Short receives → cost is negative
        assert cost < 0

    def test_daily_cost_mixed(self, sim: FundingSimulator):
        """Mixed positions should net out correctly."""
        sim.update_rate("BTCUSDT", 0.0001)
        sim.update_rate("ETHUSDT", 0.0001)
        positions = [
            _make_position("BTCUSDT", "Buy", 1.0, 50000.0),   # pays 15
            _make_position("ETHUSDT", "Sell", 10.0, 3000.0),   # receives 9
        ]
        cost = sim.estimate_daily_funding_cost(positions)
        # 15 - 9 = 6
        assert abs(cost - 6.0) < 0.01

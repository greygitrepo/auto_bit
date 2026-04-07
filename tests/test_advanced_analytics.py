"""Tests for AdvancedAnalytics — risk-adjusted returns, drawdown, attribution, rolling metrics.

Uses an in-memory SQLite database with known test data so every assertion
is deterministic.
"""

from __future__ import annotations

import math
import sqlite3

import pytest

from src.utils.db import DatabaseManager
from src.tracker.advanced_analytics import AdvancedAnalytics


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path):
    """Return a DatabaseManager backed by a temp file (schema auto-created)."""
    db_path = str(tmp_path / "test.db")
    return DatabaseManager(db_path=db_path)


@pytest.fixture()
def analytics(db):
    """Return an AdvancedAnalytics instance in paper mode."""
    return AdvancedAnalytics(db, mode="paper")


def _insert_daily(db, date: str, mode: str, starting: float, ending: float,
                  pnl: float, trade_count: int, win_count: int):
    db.upsert_daily_performance(
        date=date, mode=mode,
        starting_balance=starting, ending_balance=ending,
        pnl=pnl, trade_count=trade_count, win_count=win_count,
    )


def _insert_trade(db, *, mode="paper", symbol="BTCUSDT", side="Buy",
                  size=1.0, entry_price=100.0, exit_price=110.0,
                  pnl=10.0, fee=0.1, leverage=5, strategy="grid",
                  entry_time=1700000000, exit_time=1700003600,
                  exit_reason="TP", exit_type="TP"):
    db.insert_trade(
        mode=mode, symbol=symbol, side=side, size=size,
        entry_price=entry_price, exit_price=exit_price,
        pnl=pnl, fee=fee, leverage=leverage, strategy=strategy,
        entry_time=entry_time, exit_time=exit_time,
        entry_reason=None, exit_reason=exit_reason, exit_type=exit_type,
    )


# ---------------------------------------------------------------------------
# TestSharpeRatio
# ---------------------------------------------------------------------------

class TestSharpeRatio:
    def test_sharpe_positive_returns(self, db, analytics):
        """Consistent positive daily returns should give high Sharpe."""
        for i in range(10):
            bal = 100 + i * 2
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          bal, bal + 2, 2.0, 3, 2)
        sr = analytics.sharpe_ratio(days=9999)
        assert sr > 5.0  # very consistent positive returns

    def test_sharpe_zero_trades(self, analytics):
        """No daily_performance rows should return 0.0."""
        assert analytics.sharpe_ratio(days=30) == 0.0

    def test_sharpe_volatile_returns(self, db, analytics):
        """High variance returns should give lower Sharpe than consistent."""
        # Alternating +10 / -8 => mean = +1, but high std
        for i in range(10):
            pnl = 10.0 if i % 2 == 0 else -8.0
            bal = 100 + sum(10.0 if j % 2 == 0 else -8.0 for j in range(i + 1))
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          bal - pnl, bal, pnl, 2, 1 if pnl > 0 else 0)
        sr = analytics.sharpe_ratio(days=9999)
        assert 0.0 < sr < 5.0

    def test_sharpe_all_same_returns(self, db, analytics):
        """Zero std dev (all same percentage returns) should return inf."""
        # Use same starting_balance so pnl/starting_balance is identical each day
        for i in range(5):
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          100.0, 101.0, 1.0, 2, 2)
        sr = analytics.sharpe_ratio(days=9999)
        assert math.isinf(sr) or sr > 100

    def test_sharpe_with_risk_free_rate(self, db, analytics):
        """Risk-free rate should reduce the Sharpe ratio."""
        # Use slightly varying returns so std > 0
        returns = [2.0, 2.1, 1.9, 2.2, 1.8, 2.3, 1.7, 2.4, 1.6, 2.5]
        bal = 100.0
        for i, r in enumerate(returns):
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          bal, bal + r, r, 3, 2)
            bal += r
        sr_no_rf = analytics.sharpe_ratio(days=9999, risk_free_rate=0.0)
        sr_with_rf = analytics.sharpe_ratio(days=9999, risk_free_rate=0.05)
        assert sr_with_rf < sr_no_rf


# ---------------------------------------------------------------------------
# TestSortinoRatio
# ---------------------------------------------------------------------------

class TestSortinoRatio:
    def test_sortino_no_negative_returns(self, db, analytics):
        """All positive returns -> inf (no downside deviation)."""
        for i in range(5):
            bal = 100 + i * 2
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          bal, bal + 2, 2.0, 2, 2)
        sr = analytics.sortino_ratio(days=9999)
        assert math.isinf(sr)

    def test_sortino_mixed_returns(self, db, analytics):
        """Known mixed returns produce a positive Sortino."""
        returns = [3.0, -1.0, 2.0, -2.0, 4.0, -0.5, 1.0]
        bal = 100.0
        for i, r in enumerate(returns):
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          bal, bal + r, r, 2, 1 if r > 0 else 0)
            bal += r
        sr = analytics.sortino_ratio(days=9999)
        assert sr > 0.0

    def test_sortino_zero_trades(self, analytics):
        assert analytics.sortino_ratio(days=30) == 0.0


# ---------------------------------------------------------------------------
# TestMaxDrawdown
# ---------------------------------------------------------------------------

class TestMaxDrawdown:
    def test_max_drawdown_simple(self, db, analytics):
        """Equity curve: 100->120->90->110. Max DD = (120-90)/120 = 25%."""
        balances = [100, 120, 90, 110]
        for i, bal in enumerate(balances):
            prev = balances[i - 1] if i > 0 else 100
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          prev, bal, bal - prev, 1, 1 if bal > prev else 0)
        dd = analytics.max_drawdown(days=9999)
        assert abs(dd["max_dd_pct"] - 25.0) < 0.1
        assert abs(dd["max_dd_amount"] - 30.0) < 0.1

    def test_max_drawdown_no_trades(self, analytics):
        """No daily_performance rows -> 0% drawdown."""
        dd = analytics.max_drawdown(days=30)
        assert dd["max_dd_pct"] == 0.0

    def test_max_drawdown_always_winning(self, db, analytics):
        """Monotonically increasing equity -> 0% drawdown."""
        for i in range(5):
            bal = 100 + i * 5
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          bal, bal + 5, 5.0, 2, 2)
        dd = analytics.max_drawdown(days=9999)
        assert dd["max_dd_pct"] == 0.0

    def test_current_drawdown(self, db, analytics):
        """If current equity < peak, current_dd_pct should be > 0."""
        balances = [100, 120, 110]  # peak=120, current=110
        for i, bal in enumerate(balances):
            prev = balances[i - 1] if i > 0 else 100
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          prev, bal, bal - prev, 1, 1 if bal > prev else 0)
        dd = analytics.max_drawdown(days=9999)
        assert dd["current_dd_pct"] > 0.0
        expected = (120 - 110) / 120 * 100
        assert abs(dd["current_dd_pct"] - expected) < 0.1


# ---------------------------------------------------------------------------
# TestDrawdownSeries
# ---------------------------------------------------------------------------

class TestDrawdownSeries:
    def test_series_length_matches_days(self, db, analytics):
        """Output length should match daily_performance rows."""
        for i in range(5):
            bal = 100 + i
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          bal, bal + 1, 1.0, 1, 1)
        series = analytics.drawdown_series(days=9999)
        assert len(series) == 5

    def test_series_drawdown_values(self, db, analytics):
        """Verify drawdown_pct at each point."""
        balances = [100, 120, 90, 110]
        for i, bal in enumerate(balances):
            prev = balances[i - 1] if i > 0 else 100
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          prev, bal, bal - prev, 1, 1)
        series = analytics.drawdown_series(days=9999)
        # At 100: peak=100, dd=0
        assert series[0]["drawdown_pct"] == 0.0
        # At 120: peak=120, dd=0
        assert series[1]["drawdown_pct"] == 0.0
        # At 90: peak=120, dd = 25%
        assert abs(series[2]["drawdown_pct"] - 25.0) < 0.1
        # At 110: peak=120, dd = (120-110)/120 ~= 8.33%
        assert abs(series[3]["drawdown_pct"] - 8.33) < 0.1


# ---------------------------------------------------------------------------
# TestCalmarRatio
# ---------------------------------------------------------------------------

class TestCalmarRatio:
    def test_calmar_positive(self, db, analytics):
        """Known return and drawdown produce expected Calmar."""
        # Equity: 100 -> 120 -> 90 -> 130
        # Total return over period: (130-100)/100 = 30%
        # Max DD = (120-90)/120 = 25%
        balances = [100, 120, 90, 130]
        for i, bal in enumerate(balances):
            prev = balances[i - 1] if i > 0 else 100
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          prev, bal, bal - prev, 1, 1)
        calmar = analytics.calmar_ratio(days=9999)
        assert calmar > 0.0

    def test_calmar_zero_drawdown(self, db, analytics):
        """Zero drawdown -> inf."""
        for i in range(5):
            bal = 100 + i * 5
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          bal, bal + 5, 5.0, 2, 2)
        calmar = analytics.calmar_ratio(days=9999)
        assert math.isinf(calmar)


# ---------------------------------------------------------------------------
# TestSymbolAttribution
# ---------------------------------------------------------------------------

class TestSymbolAttribution:
    def _seed_multi_symbol(self, db):
        """Insert trades for BTC and ETH with known PnL."""
        base_time = 1700000000
        # BTC: 3 trades, +10, +5, -3 => total +12
        for i, pnl in enumerate([10.0, 5.0, -3.0]):
            _insert_trade(db, symbol="BTCUSDT", pnl=pnl,
                          entry_time=base_time + i * 3600,
                          exit_time=base_time + i * 3600 + 1800)
        # ETH: 2 trades, +2, -1 => total +1
        for i, pnl in enumerate([2.0, -1.0]):
            _insert_trade(db, symbol="ETHUSDT", pnl=pnl,
                          entry_time=base_time + (i + 3) * 3600,
                          exit_time=base_time + (i + 3) * 3600 + 1800)

    def test_attribution_multiple_symbols(self, db, analytics):
        self._seed_multi_symbol(db)
        attr = analytics.symbol_attribution(days=9999)
        assert len(attr) == 2
        btc = next(a for a in attr if a["symbol"] == "BTCUSDT")
        assert btc["trade_count"] == 3
        assert abs(btc["total_pnl"] - 12.0) < 0.01

    def test_pnl_contribution_sums_to_100(self, db, analytics):
        self._seed_multi_symbol(db)
        attr = analytics.symbol_attribution(days=9999)
        total_contrib = sum(a["pnl_contribution_pct"] for a in attr)
        assert abs(total_contrib - 100.0) < 0.1

    def test_sorted_by_pnl(self, db, analytics):
        self._seed_multi_symbol(db)
        attr = analytics.symbol_attribution(days=9999)
        assert attr[0]["total_pnl"] >= attr[1]["total_pnl"]

    def test_best_worst_trade(self, db, analytics):
        self._seed_multi_symbol(db)
        attr = analytics.symbol_attribution(days=9999)
        btc = next(a for a in attr if a["symbol"] == "BTCUSDT")
        assert btc["best_trade"] == 10.0
        assert btc["worst_trade"] == -3.0


# ---------------------------------------------------------------------------
# TestHourlyPerformance
# ---------------------------------------------------------------------------

class TestHourlyPerformance:
    def test_24_hours(self, db, analytics):
        """Should cover hours 0-23 (only hours with trades appear)."""
        # Insert trades at hour 10 and hour 14
        # 1700000000 is 2023-11-14 22:13:20 UTC, but we'll use exit_time for hour
        # hour=10 => need exit_time where hour=10 UTC
        # 2025-01-10 10:00:00 UTC = 1736503200
        _insert_trade(db, exit_time=1736503200, pnl=5.0)
        _insert_trade(db, exit_time=1736503200 + 60, pnl=3.0)
        # hour=14 => +4h = 1736517600
        _insert_trade(db, exit_time=1736517600, pnl=-2.0)
        result = analytics.hourly_performance(days=9999)
        hours = {r["hour"] for r in result}
        assert 10 in hours
        assert 14 in hours
        h10 = next(r for r in result if r["hour"] == 10)
        assert h10["trade_count"] == 2
        assert abs(h10["total_pnl"] - 8.0) < 0.01

    def test_known_hour_distribution(self, db, analytics):
        """Win rate at specific hour should be correct."""
        # 2 wins at hour 10
        _insert_trade(db, exit_time=1736503200, pnl=5.0)
        _insert_trade(db, exit_time=1736503260, pnl=3.0)
        # 1 loss at hour 10
        _insert_trade(db, exit_time=1736503320, pnl=-1.0)
        result = analytics.hourly_performance(days=9999)
        h10 = next(r for r in result if r["hour"] == 10)
        assert abs(h10["win_rate"] - 2 / 3) < 0.01


# ---------------------------------------------------------------------------
# TestWeekdayPerformance
# ---------------------------------------------------------------------------

class TestWeekdayPerformance:
    def test_7_days(self, db, analytics):
        """Should return entries for weekdays that have trades."""
        # 1736503200 = 2025-01-10 10:00 UTC => Friday (weekday=4)
        _insert_trade(db, exit_time=1736503200, pnl=5.0)
        # Next day Saturday (weekday=5)
        _insert_trade(db, exit_time=1736503200 + 86400, pnl=-2.0)
        result = analytics.weekday_performance(days=9999)
        days_present = {r["weekday"] for r in result}
        assert 4 in days_present  # Friday
        assert 5 in days_present  # Saturday

    def test_weekday_pnl(self, db, analytics):
        _insert_trade(db, exit_time=1736503200, pnl=5.0)
        _insert_trade(db, exit_time=1736503260, pnl=3.0)
        result = analytics.weekday_performance(days=9999)
        fri = next(r for r in result if r["weekday"] == 4)
        assert abs(fri["total_pnl"] - 8.0) < 0.01


# ---------------------------------------------------------------------------
# TestRollingMetrics
# ---------------------------------------------------------------------------

class TestRollingMetrics:
    def test_rolling_sharpe_window(self, db, analytics):
        """Window produces expected number of data points."""
        # Need at least window_days of daily data
        for i in range(14):
            bal = 100 + i
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          bal, bal + 1, 1.0, 1, 1)
        result = analytics.rolling_sharpe(window_days=7, total_days=9999)
        # With 14 days of data and a 7-day window, we should get ~8 data points
        assert len(result) >= 7
        assert "date" in result[0]
        assert "sharpe_ratio" in result[0]

    def test_rolling_win_rate(self, db, analytics):
        """Known win/loss sequence produces correct rolling rates."""
        base = 1700000000
        # 10 wins then 10 losses
        for i in range(20):
            pnl = 5.0 if i < 10 else -3.0
            _insert_trade(db, pnl=pnl,
                          entry_time=base + i * 3600,
                          exit_time=base + i * 3600 + 1800)
        result = analytics.rolling_win_rate(window_trades=10)
        assert len(result) > 0
        # First window (trades 0-9) should be 100% win rate
        assert result[0]["win_rate"] == 1.0
        # Last window (trades 10-19) should be 0% win rate
        assert result[-1]["win_rate"] == 0.0

    def test_rolling_sharpe_empty(self, analytics):
        """No data should return empty list."""
        assert analytics.rolling_sharpe() == []

    def test_rolling_win_rate_fewer_than_window(self, db, analytics):
        """Fewer trades than window should return empty."""
        _insert_trade(db, pnl=5.0)
        result = analytics.rolling_win_rate(window_trades=20)
        assert result == []


# ---------------------------------------------------------------------------
# TestFullReport
# ---------------------------------------------------------------------------

class TestFullReport:
    def test_report_contains_all_keys(self, db, analytics):
        """Full report dict has all expected sections."""
        # Seed some data
        for i in range(5):
            bal = 100 + i * 2
            _insert_daily(db, f"2025-01-{10+i:02d}", "paper",
                          bal, bal + 2, 2.0, 2, 1)
        _insert_trade(db, pnl=5.0)

        report = analytics.full_report(days=9999)
        expected_keys = {
            "sharpe_ratio", "sortino_ratio", "max_drawdown", "calmar_ratio",
            "symbol_attribution", "hourly_performance", "weekday_performance",
            "rolling_sharpe", "rolling_win_rate", "drawdown_series",
        }
        assert expected_keys.issubset(set(report.keys()))

    def test_report_empty_db(self, analytics):
        """Full report on empty DB should not crash."""
        report = analytics.full_report(days=30)
        assert report["sharpe_ratio"] == 0.0
        assert report["max_drawdown"]["max_dd_pct"] == 0.0


# ---------------------------------------------------------------------------
# TestModeIsolation
# ---------------------------------------------------------------------------

class TestModeIsolation:
    def test_paper_does_not_see_live(self, db):
        """Paper analytics should not include live-mode data."""
        _insert_daily(db, "2025-01-10", "live", 100, 110, 10.0, 5, 5)
        paper_analytics = AdvancedAnalytics(db, mode="paper")
        sr = paper_analytics.sharpe_ratio(days=9999)
        assert sr == 0.0  # no paper data

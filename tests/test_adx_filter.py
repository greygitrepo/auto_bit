"""Tests for ADX trend strength filter and improved confidence scoring."""

import pytest
import pandas as pd
import numpy as np
from src.indicators.technical import IndicatorEngine
from src.strategy.position.momentum_scalper import MomentumScalper
from src.strategy.position.base import SignalType


class TestADXIndicator:
    """Verify ADX calculation is added to indicators."""

    def test_adx_column_exists(self):
        """calculate_all() should include adx_14 column."""
        np.random.seed(42)
        n = 100
        close = 100 + np.cumsum(np.random.randn(n) * 0.5)
        high = close + np.random.rand(n) * 0.5
        low = close - np.random.rand(n) * 0.5
        df = pd.DataFrame({
            "open": close - np.random.randn(n) * 0.1,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.random.randint(1000, 10000, n).astype(float),
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min"),
        })
        result = IndicatorEngine.calculate_all(df)
        assert "adx_14" in result.columns
        # Last values should not be NaN (enough data)
        assert not pd.isna(result["adx_14"].iloc[-1])

    def test_adx_range(self):
        """ADX should be between 0 and 100."""
        np.random.seed(123)
        n = 100
        close = 50 + np.cumsum(np.random.randn(n) * 1.0)
        df = pd.DataFrame({
            "open": close - np.random.randn(n) * 0.2,
            "high": close + abs(np.random.randn(n) * 0.5),
            "low": close - abs(np.random.randn(n) * 0.5),
            "close": close,
            "volume": np.random.randint(500, 5000, n).astype(float),
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min"),
        })
        result = IndicatorEngine.calculate_all(df)
        valid = result["adx_14"].dropna()
        assert (valid >= 0).all()
        assert (valid <= 100).all()


class TestADXEntryFilter:
    """Verify ADX filter blocks entries in ranging markets."""

    def _make_scalper(self, adx_threshold=20):
        config = {
            "adx_threshold": adx_threshold,
            "ema_fast": 5, "ema_mid": 10, "ema_slow": 20,
            "rsi_long_range": [30, 80], "rsi_short_range": [20, 70],
            "volume_multiplier": 0.5,
            "vwap_enabled": False,
            "higher_tf": {"enabled": False},
            "follow_scanner_direction": False,
            "exit": {
                "stop_loss": {"atr_multiplier": 2.0, "min_pct": 0.5, "max_pct": 2.0},
                "take_profit": {"risk_reward_ratio": 2.0},
                "trailing_stop": {"activation_r": 0.5, "callback_atr_multiplier": 0.8},
                "strategy_exit": {"ema_cross_exit": False, "rsi_reversal_exit": False, "volume_dry_exit": False},
                "time_limit": {"max_holding_minutes": 90},
            },
        }
        return MomentumScalper(config=config)

    def _make_trending_df(self, trend="up"):
        """Create a DataFrame that passes EMA/RSI/volume conditions with a given ADX."""
        np.random.seed(42)
        n = 50
        if trend == "up":
            close = 100 + np.arange(n) * 0.5 + np.random.randn(n) * 0.1
        else:
            close = 150 - np.arange(n) * 0.5 + np.random.randn(n) * 0.1
        high = close + 0.3
        low = close - 0.3
        df = pd.DataFrame({
            "open": close - 0.1 if trend == "up" else close + 0.1,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.full(n, 5000.0),
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min"),
        })
        return IndicatorEngine.calculate_all(df)

    def test_adx_filter_blocks_low_adx(self):
        """When ADX is below threshold, evaluate should return HOLD."""
        # Create ranging/choppy data with low ADX
        np.random.seed(42)
        n = 50
        close = 100 + np.sin(np.arange(n) * 0.3) * 0.5 + np.random.randn(n) * 0.1
        df = pd.DataFrame({
            "open": close - 0.05,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": np.full(n, 5000.0),
            "timestamp": pd.date_range("2026-01-01", periods=n, freq="5min"),
        })
        df_5m = IndicatorEngine.calculate_all(df)
        scalper = self._make_scalper(adx_threshold=50)  # high threshold
        df_15m = pd.DataFrame()
        result = scalper.evaluate("TESTUSDT", df_5m, df_15m, None, None)
        assert result.signal == SignalType.HOLD
        assert "ADX" in result.reason

    def test_adx_filter_allows_high_adx(self):
        """When ADX is above threshold, evaluate should proceed to entry check."""
        scalper = self._make_scalper(adx_threshold=1)  # very low threshold
        df_5m = self._make_trending_df("up")
        df_15m = pd.DataFrame()
        result = scalper.evaluate("TESTUSDT", df_5m, df_15m, None, None)
        # Should not be blocked by ADX (may still be HOLD for other reasons)
        if result.signal == SignalType.HOLD:
            assert "ADX" not in result.reason


class TestImprovedConfidence:
    """Verify confidence scoring includes ADX and candle body."""

    def test_confidence_includes_adx_boost(self):
        scalper = MomentumScalper()
        np.random.seed(42)
        n = 50
        close = 100 + np.arange(n) * 0.3
        df = pd.DataFrame({
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(n, 3000.0),
        })
        df = IndicatorEngine.calculate_all(df)

        conf = scalper._compute_confidence(df, "LONG")
        assert 0.0 <= conf <= 1.0
        # With a trending dataset, ADX should boost confidence above base 0.4
        assert conf > 0.4

    def test_confidence_bounded(self):
        scalper = MomentumScalper()
        np.random.seed(99)
        n = 50
        close = np.full(n, 100.0)
        df = pd.DataFrame({
            "open": close, "high": close + 0.1, "low": close - 0.1,
            "close": close, "volume": np.full(n, 1000.0),
        })
        df = IndicatorEngine.calculate_all(df)
        conf = scalper._compute_confidence(df, "SHORT")
        assert 0.0 <= conf <= 1.0

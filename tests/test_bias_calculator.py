"""Tests for BiasCalculator — directional bias from EMA, funding rate, and market trend."""

import pytest
import pandas as pd

from src.strategy.position.base import BiasDirection
from src.strategy.position.bias_calculator import BiasCalculator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_config(**overrides):
    cfg = {
        "bias": {
            "enabled": True,
            "ema_periods": [20, 50],
            "ema_weight": 0.5,
            "btc_eth_weight": 0.2,
            "funding_rate": {
                "enabled": True,
                "extreme_threshold": 0.01,
                "weight": 0.3,
            },
            "max_level_shift": 3,
        }
    }
    if overrides:
        for k, v in overrides.items():
            if k == "enabled":
                cfg["bias"]["enabled"] = v
            elif k == "funding_enabled":
                cfg["bias"]["funding_rate"]["enabled"] = v
            else:
                cfg["bias"][k] = v
    return cfg


def _make_df(ema_20: float, ema_50: float) -> pd.DataFrame:
    """Create a minimal 1h DataFrame with EMA columns."""
    return pd.DataFrame([{"ema_20": ema_20, "ema_50": ema_50}])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNeutralBias:

    def test_neutral_bias(self):
        """Mixed signals produce NEUTRAL direction."""
        calc = BiasCalculator(_default_config())
        # EMAs equal => ema_bias=0, mixed trends => market_bias=0, no funding
        df = _make_df(100.0, 100.0)
        direction, magnitude, shift = calc.compute(
            df_1h=df,
            funding_rate=0.0,
            btc_trend="mixed",
            eth_trend="mixed",
        )
        assert direction == BiasDirection.NEUTRAL
        assert shift == 0


class TestBullishBias:

    def test_bullish_bias(self):
        """All bullish signals produce BULLISH with positive shift."""
        calc = BiasCalculator(_default_config())
        # EMA fast well above slow => ema_bias ~ +1.0
        # BTC + ETH both bull => market_bias = +1.0
        # Negative funding => bullish (shorts pay longs)
        df = _make_df(102.0, 100.0)  # spread=2% => clamped to +1.0
        direction, magnitude, shift = calc.compute(
            df_1h=df,
            funding_rate=-0.01,  # negative => bullish funding bias = +1.0
            btc_trend="bull",
            eth_trend="bull",
        )
        assert direction == BiasDirection.BULLISH
        assert magnitude > 0.3
        assert shift > 0


class TestBearishBias:

    def test_bearish_bias(self):
        """All bearish signals produce BEARISH with negative shift."""
        calc = BiasCalculator(_default_config())
        # EMA fast well below slow => ema_bias ~ -1.0
        # BTC + ETH both bear => market_bias = -1.0
        # Positive funding => bearish
        df = _make_df(98.0, 100.0)  # spread=-2% => clamped to -1.0
        direction, magnitude, shift = calc.compute(
            df_1h=df,
            funding_rate=0.01,  # positive => bearish funding bias = -1.0
            btc_trend="bear",
            eth_trend="bear",
        )
        assert direction == BiasDirection.BEARISH
        assert magnitude < -0.3
        assert shift < 0


class TestFundingRateBias:

    def test_funding_rate_bias(self):
        """Extreme positive funding pushes toward bearish bias."""
        calc = BiasCalculator(_default_config())
        # Neutral EMAs and market, only funding signal
        df = _make_df(100.0, 100.0)
        direction, magnitude, shift = calc.compute(
            df_1h=df,
            funding_rate=0.01,  # extreme positive => funding_bias = -1.0
            btc_trend="mixed",
            eth_trend="mixed",
        )
        # funding_weight=0.3, funding_bias=-1.0 => total = -0.3
        # magnitude exactly -0.3 => direction threshold is < -0.3 for BEARISH
        # So this should be NEUTRAL at the boundary
        assert magnitude == pytest.approx(-0.3, abs=0.01)

        # Push harder: funding above threshold
        direction2, magnitude2, shift2 = calc.compute(
            df_1h=df,
            funding_rate=0.02,  # 2x threshold => funding_bias = -2 clamped to -1.0
            btc_trend="mixed",
            eth_trend="mixed",
        )
        # Still -0.3 because funding_bias clamps at -1.0 and weight is 0.3
        assert magnitude2 == pytest.approx(-0.3, abs=0.01)


class TestDisabledBias:

    def test_disabled_bias(self):
        """When bias is disabled, always returns NEUTRAL with 0 shift."""
        calc = BiasCalculator(_default_config(enabled=False))
        df = _make_df(110.0, 100.0)
        direction, magnitude, shift = calc.compute(
            df_1h=df,
            funding_rate=-0.05,
            btc_trend="bull",
            eth_trend="bull",
        )
        assert direction == BiasDirection.NEUTRAL
        assert magnitude == 0.0
        assert shift == 0

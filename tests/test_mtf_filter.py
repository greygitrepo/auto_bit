"""Tests for MTFFilter — multi-timeframe analysis for grid trading decisions.

TDD: these tests are written FIRST, then the implementation.
"""

import pytest
import pandas as pd
import numpy as np

from src.strategy.position.mtf_filter import MTFFilter, MTFSignal, MTFAnalysis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_mtf_config(**overrides):
    cfg = {
        "enabled": True,
        "require_15m_alignment": True,
        "require_1h_alignment": False,
        "weight_5m": 0.3,
        "weight_15m": 0.4,
        "weight_1h": 0.3,
        "ema_fast": 20,
        "ema_slow": 50,
        "rsi_period": 14,
        "rsi_bullish_threshold": 55,
        "rsi_bearish_threshold": 45,
        "trend_spacing_multiplier": 1.3,
        "range_spacing_multiplier": 0.9,
        "conflicting_reduce_levels": 2,
    }
    cfg.update(overrides)
    return cfg


def _make_tf_df(
    ema_fast: float = 100.0,
    ema_slow: float = 100.0,
    rsi: float = 50.0,
    close: float = 100.0,
    vwap: float = 100.0,
    rows: int = 1,
) -> pd.DataFrame:
    """Create a minimal DataFrame with indicator columns for a single timeframe."""
    data = {
        "open": [close] * rows,
        "high": [close * 1.01] * rows,
        "low": [close * 0.99] * rows,
        "close": [close] * rows,
        "volume": [1000.0] * rows,
        "ema_20": [ema_fast] * rows,
        "ema_50": [ema_slow] * rows,
        "rsi_14": [rsi] * rows,
        "vwap": [vwap] * rows,
        "atr_14": [close * 0.02] * rows,
    }
    return pd.DataFrame(data)


def _make_bullish_df(rows: int = 1) -> pd.DataFrame:
    """DF with bullish indicators: EMA fast > slow, RSI > 55, close > VWAP."""
    return _make_tf_df(ema_fast=105.0, ema_slow=100.0, rsi=62.0, close=106.0, vwap=100.0, rows=rows)


def _make_bearish_df(rows: int = 1) -> pd.DataFrame:
    """DF with bearish indicators: EMA fast < slow, RSI < 45, close < VWAP."""
    return _make_tf_df(ema_fast=95.0, ema_slow=100.0, rsi=38.0, close=94.0, vwap=100.0, rows=rows)


def _make_neutral_df(rows: int = 1) -> pd.DataFrame:
    """DF with neutral indicators: EMA fast ~ slow, RSI ~50."""
    return _make_tf_df(ema_fast=100.0, ema_slow=100.0, rsi=50.0, close=100.0, vwap=100.0, rows=rows)


# ---------------------------------------------------------------------------
# TestTimeframeAnalysis — _analyze_timeframe
# ---------------------------------------------------------------------------


class TestTimeframeAnalysis:

    def test_bullish_ema_crossover_rsi(self):
        """Fast EMA > Slow EMA + RSI > 55 -> BULLISH."""
        filt = MTFFilter(_default_mtf_config())
        df = _make_bullish_df()
        result = filt._analyze_timeframe(df, "5m")
        assert result == MTFSignal.BULLISH.value

    def test_bearish_ema_crossover_rsi(self):
        """Fast EMA < Slow EMA + RSI < 45 -> BEARISH."""
        filt = MTFFilter(_default_mtf_config())
        df = _make_bearish_df()
        result = filt._analyze_timeframe(df, "15m")
        assert result == MTFSignal.BEARISH.value

    def test_neutral_mixed_signals(self):
        """EMA bullish but RSI neutral, close near VWAP -> NEUTRAL.
        Only EMA is bullish (1 of 3 sub-signals), so overall is NEUTRAL.
        """
        filt = MTFFilter(_default_mtf_config())
        # EMA fast > slow (bullish), RSI 50 (neutral), close ~ vwap (neutral)
        df = _make_tf_df(ema_fast=105.0, ema_slow=100.0, rsi=50.0, close=100.2, vwap=100.0)
        result = filt._analyze_timeframe(df, "5m")
        assert result == MTFSignal.NEUTRAL.value

    def test_empty_dataframe(self):
        """Empty df -> NEUTRAL (graceful fallback)."""
        filt = MTFFilter(_default_mtf_config())
        df = pd.DataFrame()
        result = filt._analyze_timeframe(df, "5m")
        assert result == MTFSignal.NEUTRAL.value

    def test_missing_columns(self):
        """Missing indicator columns -> NEUTRAL."""
        filt = MTFFilter(_default_mtf_config())
        df = pd.DataFrame({"close": [100.0], "volume": [1000.0]})
        result = filt._analyze_timeframe(df, "1h")
        assert result == MTFSignal.NEUTRAL.value

    def test_nan_values_fallback(self):
        """NaN indicator values -> NEUTRAL."""
        filt = MTFFilter(_default_mtf_config())
        df = _make_tf_df(ema_fast=float("nan"), ema_slow=float("nan"), rsi=float("nan"))
        result = filt._analyze_timeframe(df, "5m")
        assert result == MTFSignal.NEUTRAL.value


# ---------------------------------------------------------------------------
# TestMTFAnalysis — analyze()
# ---------------------------------------------------------------------------


class TestMTFAnalysis:

    def test_all_bullish_aligned(self):
        """All TFs bullish -> ALIGNED, BULLISH, strength near 1.0."""
        filt = MTFFilter(_default_mtf_config())
        analysis = filt.analyze(
            df_5m=_make_bullish_df(),
            df_15m=_make_bullish_df(),
            df_1h=_make_bullish_df(),
        )
        assert analysis.signal_5m == MTFSignal.BULLISH.value
        assert analysis.signal_15m == MTFSignal.BULLISH.value
        assert analysis.signal_1h == MTFSignal.BULLISH.value
        assert analysis.alignment == "ALIGNED"
        assert analysis.strength >= 0.8
        assert analysis.recommended_action == "TRADE"

    def test_all_bearish_aligned(self):
        """All TFs bearish -> ALIGNED, BEARISH, strength near 1.0."""
        filt = MTFFilter(_default_mtf_config())
        analysis = filt.analyze(
            df_5m=_make_bearish_df(),
            df_15m=_make_bearish_df(),
            df_1h=_make_bearish_df(),
        )
        assert analysis.signal_5m == MTFSignal.BEARISH.value
        assert analysis.signal_15m == MTFSignal.BEARISH.value
        assert analysis.signal_1h == MTFSignal.BEARISH.value
        assert analysis.alignment == "ALIGNED"
        assert analysis.strength >= 0.8
        assert analysis.recommended_action == "TRADE"

    def test_mixed_signals_partial(self):
        """5m+15m agree, 1h disagrees -> PARTIAL alignment."""
        filt = MTFFilter(_default_mtf_config())
        analysis = filt.analyze(
            df_5m=_make_bullish_df(),
            df_15m=_make_bullish_df(),
            df_1h=_make_bearish_df(),
        )
        assert analysis.signal_5m == MTFSignal.BULLISH.value
        assert analysis.signal_15m == MTFSignal.BULLISH.value
        assert analysis.signal_1h == MTFSignal.BEARISH.value
        assert analysis.alignment == "PARTIAL"
        assert 0.3 <= analysis.strength <= 0.8
        assert analysis.recommended_action == "REDUCE"

    def test_all_conflicting(self):
        """5m bull, 15m bear, 1h neutral -> CONFLICTING."""
        filt = MTFFilter(_default_mtf_config())
        analysis = filt.analyze(
            df_5m=_make_bullish_df(),
            df_15m=_make_bearish_df(),
            df_1h=_make_neutral_df(),
        )
        assert analysis.alignment == "CONFLICTING"
        assert analysis.strength < 0.5
        assert analysis.recommended_action == "SKIP"

    def test_all_neutral(self):
        """All TFs neutral -> NEUTRAL, ALIGNED."""
        filt = MTFFilter(_default_mtf_config())
        analysis = filt.analyze(
            df_5m=_make_neutral_df(),
            df_15m=_make_neutral_df(),
            df_1h=_make_neutral_df(),
        )
        assert analysis.signal_5m == MTFSignal.NEUTRAL.value
        assert analysis.signal_15m == MTFSignal.NEUTRAL.value
        assert analysis.signal_1h == MTFSignal.NEUTRAL.value
        assert analysis.alignment == "ALIGNED"
        # Neutral aligned has moderate strength
        assert analysis.strength >= 0.5


# ---------------------------------------------------------------------------
# TestShouldCreateGrid
# ---------------------------------------------------------------------------


class TestShouldCreateGrid:

    def test_create_allowed_when_aligned(self):
        """Grid creation allowed when TFs are aligned or partial."""
        filt = MTFFilter(_default_mtf_config())
        aligned = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BULLISH", signal_1h="BULLISH",
            alignment="ALIGNED", strength=0.9, recommended_action="TRADE",
        )
        assert filt.should_create_grid(aligned) is True

        partial = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BULLISH", signal_1h="BEARISH",
            alignment="PARTIAL", strength=0.5, recommended_action="REDUCE",
        )
        assert filt.should_create_grid(partial) is True

    def test_create_blocked_when_conflicting(self):
        """Grid creation blocked when TFs strongly conflict."""
        filt = MTFFilter(_default_mtf_config())
        conflicting = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BEARISH", signal_1h="NEUTRAL",
            alignment="CONFLICTING", strength=0.2, recommended_action="SKIP",
        )
        assert filt.should_create_grid(conflicting) is False

    def test_create_allowed_when_disabled(self):
        """MTF disabled -> always allow creation."""
        filt = MTFFilter(_default_mtf_config(enabled=False))
        conflicting = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BEARISH", signal_1h="NEUTRAL",
            alignment="CONFLICTING", strength=0.2, recommended_action="SKIP",
        )
        assert filt.should_create_grid(conflicting) is True


# ---------------------------------------------------------------------------
# TestShouldAllowFill
# ---------------------------------------------------------------------------


class TestShouldAllowFill:

    def test_buy_allowed_when_bullish(self):
        """Buy fill allowed in bullish MTF."""
        filt = MTFFilter(_default_mtf_config())
        bullish = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BULLISH", signal_1h="BULLISH",
            alignment="ALIGNED", strength=0.9, recommended_action="TRADE",
        )
        assert filt.should_allow_fill(bullish, "Buy") is True

    def test_buy_blocked_when_strongly_bearish(self):
        """Buy fill blocked when all TFs bearish (strongly bearish)."""
        filt = MTFFilter(_default_mtf_config())
        bearish = MTFAnalysis(
            signal_5m="BEARISH", signal_15m="BEARISH", signal_1h="BEARISH",
            alignment="ALIGNED", strength=0.9, recommended_action="TRADE",
        )
        assert filt.should_allow_fill(bearish, "Buy") is False

    def test_sell_allowed_when_bearish(self):
        """Sell fill allowed in bearish MTF."""
        filt = MTFFilter(_default_mtf_config())
        bearish = MTFAnalysis(
            signal_5m="BEARISH", signal_15m="BEARISH", signal_1h="BEARISH",
            alignment="ALIGNED", strength=0.9, recommended_action="TRADE",
        )
        assert filt.should_allow_fill(bearish, "Sell") is True

    def test_sell_blocked_when_strongly_bullish(self):
        """Sell fill blocked when all TFs bullish (strongly bullish)."""
        filt = MTFFilter(_default_mtf_config())
        bullish = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BULLISH", signal_1h="BULLISH",
            alignment="ALIGNED", strength=0.9, recommended_action="TRADE",
        )
        assert filt.should_allow_fill(bullish, "Sell") is False

    def test_fill_allowed_when_neutral(self):
        """Both sides allowed in neutral MTF."""
        filt = MTFFilter(_default_mtf_config())
        neutral = MTFAnalysis(
            signal_5m="NEUTRAL", signal_15m="NEUTRAL", signal_1h="NEUTRAL",
            alignment="ALIGNED", strength=0.5, recommended_action="TRADE",
        )
        assert filt.should_allow_fill(neutral, "Buy") is True
        assert filt.should_allow_fill(neutral, "Sell") is True

    def test_fill_allowed_when_disabled(self):
        """MTF disabled -> always allow fill."""
        filt = MTFFilter(_default_mtf_config(enabled=False))
        bearish = MTFAnalysis(
            signal_5m="BEARISH", signal_15m="BEARISH", signal_1h="BEARISH",
            alignment="ALIGNED", strength=0.9, recommended_action="TRADE",
        )
        assert filt.should_allow_fill(bearish, "Buy") is True

    def test_buy_allowed_when_partial_bearish(self):
        """Buy fill allowed when only partially bearish (not strongly)."""
        filt = MTFFilter(_default_mtf_config())
        partial = MTFAnalysis(
            signal_5m="BEARISH", signal_15m="NEUTRAL", signal_1h="BEARISH",
            alignment="PARTIAL", strength=0.5, recommended_action="REDUCE",
        )
        # Not ALL bearish, so buy should still be allowed
        assert filt.should_allow_fill(partial, "Buy") is True


# ---------------------------------------------------------------------------
# TestAdjustBias
# ---------------------------------------------------------------------------


class TestAdjustBias:

    def test_aligned_boosts_bias(self):
        """ALIGNED alignment boosts bias magnitude by 1.5x."""
        filt = MTFFilter(_default_mtf_config())
        aligned = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BULLISH", signal_1h="BULLISH",
            alignment="ALIGNED", strength=0.9, recommended_action="TRADE",
        )
        adjusted = filt.adjust_bias(0.4, aligned)
        assert adjusted == pytest.approx(0.6, abs=0.01)  # 0.4 * 1.5 = 0.6

    def test_conflicting_reduces_bias(self):
        """CONFLICTING reduces bias toward neutral (0.5x)."""
        filt = MTFFilter(_default_mtf_config())
        conflicting = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BEARISH", signal_1h="NEUTRAL",
            alignment="CONFLICTING", strength=0.2, recommended_action="SKIP",
        )
        adjusted = filt.adjust_bias(0.4, conflicting)
        assert adjusted == pytest.approx(0.2, abs=0.01)  # 0.4 * 0.5 = 0.2

    def test_partial_unchanged(self):
        """PARTIAL keeps bias unchanged (1.0x)."""
        filt = MTFFilter(_default_mtf_config())
        partial = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BULLISH", signal_1h="BEARISH",
            alignment="PARTIAL", strength=0.5, recommended_action="REDUCE",
        )
        adjusted = filt.adjust_bias(0.4, partial)
        assert adjusted == pytest.approx(0.4, abs=0.01)

    def test_adjust_bias_clamped(self):
        """Adjusted bias is clamped to [-1, 1]."""
        filt = MTFFilter(_default_mtf_config())
        aligned = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BULLISH", signal_1h="BULLISH",
            alignment="ALIGNED", strength=0.9, recommended_action="TRADE",
        )
        adjusted = filt.adjust_bias(0.8, aligned)
        assert adjusted <= 1.0  # 0.8 * 1.5 = 1.2 clamped to 1.0

    def test_adjust_bias_negative(self):
        """Negative bias is also adjusted correctly."""
        filt = MTFFilter(_default_mtf_config())
        aligned = MTFAnalysis(
            signal_5m="BEARISH", signal_15m="BEARISH", signal_1h="BEARISH",
            alignment="ALIGNED", strength=0.9, recommended_action="TRADE",
        )
        adjusted = filt.adjust_bias(-0.4, aligned)
        assert adjusted == pytest.approx(-0.6, abs=0.01)

    def test_adjust_bias_when_disabled(self):
        """MTF disabled -> bias unchanged."""
        filt = MTFFilter(_default_mtf_config(enabled=False))
        aligned = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BULLISH", signal_1h="BULLISH",
            alignment="ALIGNED", strength=0.9, recommended_action="TRADE",
        )
        adjusted = filt.adjust_bias(0.4, aligned)
        assert adjusted == pytest.approx(0.4, abs=0.01)


# ---------------------------------------------------------------------------
# TestGridAdjustment
# ---------------------------------------------------------------------------


class TestGridAdjustment:

    def test_strong_trend_wider_spacing(self):
        """Strong aligned trend -> spacing multiplier > 1."""
        filt = MTFFilter(_default_mtf_config())
        aligned_bullish = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BULLISH", signal_1h="BULLISH",
            alignment="ALIGNED", strength=0.9, recommended_action="TRADE",
        )
        adj = filt.get_grid_adjustment(aligned_bullish)
        assert adj["spacing_multiplier"] == pytest.approx(1.3, abs=0.01)
        assert adj["level_count_adjustment"] <= 0  # Fewer levels in trends

    def test_range_bound_tighter_spacing(self):
        """Neutral/range -> spacing multiplier < 1."""
        filt = MTFFilter(_default_mtf_config())
        neutral = MTFAnalysis(
            signal_5m="NEUTRAL", signal_15m="NEUTRAL", signal_1h="NEUTRAL",
            alignment="ALIGNED", strength=0.5, recommended_action="TRADE",
        )
        adj = filt.get_grid_adjustment(neutral)
        assert adj["spacing_multiplier"] == pytest.approx(0.9, abs=0.01)
        assert adj["level_count_adjustment"] >= 0  # More levels in ranges

    def test_conflicting_fewer_levels(self):
        """Conflicting signals -> reduce level count."""
        filt = MTFFilter(_default_mtf_config())
        conflicting = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BEARISH", signal_1h="NEUTRAL",
            alignment="CONFLICTING", strength=0.2, recommended_action="SKIP",
        )
        adj = filt.get_grid_adjustment(conflicting)
        assert adj["level_count_adjustment"] == -2  # Reduce by conflicting_reduce_levels
        assert adj["recenter_urgency"] > 0  # Higher urgency when conflicting

    def test_partial_moderate_adjustment(self):
        """Partial alignment -> moderate adjustments."""
        filt = MTFFilter(_default_mtf_config())
        partial = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BULLISH", signal_1h="BEARISH",
            alignment="PARTIAL", strength=0.5, recommended_action="REDUCE",
        )
        adj = filt.get_grid_adjustment(partial)
        assert adj["spacing_multiplier"] == pytest.approx(1.0, abs=0.01)
        assert adj["level_count_adjustment"] == 0

    def test_adjustment_when_disabled(self):
        """MTF disabled -> neutral adjustments (no effect)."""
        filt = MTFFilter(_default_mtf_config(enabled=False))
        aligned = MTFAnalysis(
            signal_5m="BULLISH", signal_15m="BULLISH", signal_1h="BULLISH",
            alignment="ALIGNED", strength=0.9, recommended_action="TRADE",
        )
        adj = filt.get_grid_adjustment(aligned)
        assert adj["spacing_multiplier"] == pytest.approx(1.0, abs=0.01)
        assert adj["level_count_adjustment"] == 0
        assert adj["recenter_urgency"] == pytest.approx(0.0, abs=0.01)


# ---------------------------------------------------------------------------
# TestGridBiasIntegration — MTF integration with GridBiasStrategy
# ---------------------------------------------------------------------------


class TestGridBiasIntegration:

    def _make_strategy(self, mtf_enabled=True):
        """Create a GridBiasStrategy with MTF config."""
        from src.strategy.position.grid_bias import GridBiasStrategy

        config = {
            "strategies": {
                "grid_bias": {
                    "num_levels": 10,
                    "default_buy_levels": 5,
                    "default_sell_levels": 5,
                    "range_atr_multiplier": 2.5,
                    "min_range_pct": 1.0,
                    "max_range_pct": 8.0,
                    "recenter_threshold_pct": 1.5,
                    "max_open_levels": 6,
                    "center_method": "last_close",
                    "recenter_interval_minutes": 60,
                    "leverage": 5,
                    "qty_per_level_pct": 5.0,
                    "max_drawdown_pct": 20.0,
                    "min_spacing_pct": 0.10,  # Low threshold for testing
                    "stale_fill_revert_seconds": 360,
                    "max_symbols": 0,
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
                        "threshold": 0.15,
                    },
                    "mtf": {
                        "enabled": mtf_enabled,
                        "require_15m_alignment": True,
                        "require_1h_alignment": False,
                        "weight_5m": 0.3,
                        "weight_15m": 0.4,
                        "weight_1h": 0.3,
                        "ema_fast": 20,
                        "ema_slow": 50,
                        "rsi_period": 14,
                        "rsi_bullish_threshold": 55,
                        "rsi_bearish_threshold": 45,
                        "trend_spacing_multiplier": 1.3,
                        "range_spacing_multiplier": 0.9,
                        "conflicting_reduce_levels": 2,
                    },
                },
            },
            "exit": {
                "hard_stop_loss_pct": 5.0,
                "grid_timeout_hours": 24,
            },
        }
        return GridBiasStrategy(config, db=None)

    def _make_1h_df(self):
        """Create a 1h DataFrame with indicators."""
        return pd.DataFrame([{
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
            "volume": 1000.0, "ema_20": 100.0, "ema_50": 100.0,
            "rsi_14": 50.0, "vwap": 100.0, "atr_14": 5.0,
        }])

    def test_evaluate_with_mtf_data(self):
        """evaluate() uses MTF filter when df_5m/df_15m provided."""
        strategy = self._make_strategy(mtf_enabled=True)

        candle = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                  "volume": 1000.0, "timestamp": 1000}
        df_1h = self._make_1h_df()
        df_5m = _make_neutral_df()
        df_15m = _make_neutral_df()

        # Should not raise; MTF is used internally
        signals = strategy.evaluate(
            symbol="TESTUSDT",
            candle_5m=candle,
            df_5m=df_5m,
            df_15m=df_15m,
            df_1h=df_1h,
            btc_trend="mixed",
            eth_trend="mixed",
            current_balance=1000.0,
            initial_balance=1000.0,
            mode="paper",
        )
        assert isinstance(signals, list)

    def test_evaluate_without_mtf_data(self):
        """evaluate() falls back to single-TF when df_5m/df_15m is None."""
        strategy = self._make_strategy(mtf_enabled=True)

        candle = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                  "volume": 1000.0, "timestamp": 1000}
        df_1h = self._make_1h_df()

        # No df_5m or df_15m — should fall back gracefully
        signals = strategy.evaluate(
            symbol="TESTUSDT",
            candle_5m=candle,
            df_5m=None,
            df_15m=None,
            df_1h=df_1h,
            btc_trend="mixed",
            eth_trend="mixed",
            current_balance=1000.0,
            initial_balance=1000.0,
            mode="paper",
        )
        assert isinstance(signals, list)

    def test_evaluate_mtf_disabled(self):
        """evaluate() skips MTF when config disabled."""
        strategy = self._make_strategy(mtf_enabled=False)

        candle = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                  "volume": 1000.0, "timestamp": 1000}
        df_1h = self._make_1h_df()
        df_5m = _make_neutral_df()
        df_15m = _make_neutral_df()

        signals = strategy.evaluate(
            symbol="TESTUSDT",
            candle_5m=candle,
            df_5m=df_5m,
            df_15m=df_15m,
            df_1h=df_1h,
            btc_trend="mixed",
            eth_trend="mixed",
            current_balance=1000.0,
            initial_balance=1000.0,
            mode="paper",
        )
        assert isinstance(signals, list)

    def test_evaluate_backward_compatible(self):
        """evaluate() works with old signature (no df_5m/df_15m args)."""
        strategy = self._make_strategy(mtf_enabled=True)

        candle = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                  "volume": 1000.0, "timestamp": 1000}
        df_1h = self._make_1h_df()

        # Call with old signature (positional args match original order)
        signals = strategy.evaluate(
            symbol="TESTUSDT",
            candle_5m=candle,
            df_1h=df_1h,
            btc_trend="mixed",
            eth_trend="mixed",
            current_balance=1000.0,
            initial_balance=1000.0,
            mode="paper",
        )
        assert isinstance(signals, list)

    def test_fill_filtered_by_mtf(self):
        """Fill signals are filtered by MTF direction check."""
        strategy = self._make_strategy(mtf_enabled=True)

        # First create a grid with neutral data
        candle_create = {
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
            "volume": 1000.0, "timestamp": 1000,
        }
        df_1h = self._make_1h_df()

        # Create the grid first with neutral MTF
        strategy.evaluate(
            symbol="TESTUSDT",
            candle_5m=candle_create,
            df_5m=_make_neutral_df(),
            df_15m=_make_neutral_df(),
            df_1h=df_1h,
            btc_trend="mixed",
            eth_trend="mixed",
            current_balance=1000.0,
            initial_balance=1000.0,
            mode="paper",
        )

        # Verify grid was created
        assert "TESTUSDT" in strategy._grids

        # Now try to fill with strongly bearish MTF — buy fills should be filtered
        grid = strategy._grids["TESTUSDT"]
        buy_levels = [lv for lv in grid.levels if lv.side == "Buy"]
        assert len(buy_levels) > 0

        # Candle that touches the first buy level
        first_buy = min(buy_levels, key=lambda l: abs(l.level_index))
        candle_fill = {
            "open": 100.0, "high": 100.0, "low": first_buy.price - 0.01,
            "close": first_buy.price, "volume": 1000.0, "timestamp": 2000,
        }

        # With all-bearish MTF, buy fills should be blocked
        # All three TFs must be bearish for should_allow_fill to block buys
        df_1h_bearish = _make_bearish_df()
        signals = strategy.evaluate(
            symbol="TESTUSDT",
            candle_5m=candle_fill,
            df_5m=_make_bearish_df(),
            df_15m=_make_bearish_df(),
            df_1h=df_1h_bearish,
            btc_trend="bear",
            eth_trend="bear",
            current_balance=1000.0,
            initial_balance=1000.0,
            mode="paper",
        )
        # Buy fill signals should be filtered out by MTF
        buy_fills = [s for s in signals if s.action.value == "FILL" and s.side == "Buy"]
        assert len(buy_fills) == 0

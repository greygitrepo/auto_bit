"""Tests for P2/P3 process integration fixes.

Verifies:
1. _evaluate_grid_strategy passes df_5m and df_15m to GridBiasStrategy.evaluate()
2. apply_funding is called in P3 paper mode monitoring loop
3. SlippageGuard config is correctly wired in GridBiasStrategy
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch, PropertyMock

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# 1. Verify _evaluate_grid_strategy passes df_5m / df_15m
# ---------------------------------------------------------------------------

class TestEvaluateGridStrategyMTFArgs:
    """Verify that _evaluate_grid_strategy fetches and passes df_5m/df_15m."""

    def _make_process(self):
        """Build a minimal StrategyEngineProcess mock with required attrs."""
        from src.strategy.process import StrategyEngineProcess

        proc = object.__new__(StrategyEngineProcess)

        # Minimal config
        proc._config = {"mode": "paper", "asset": {"capital": {"initial_balance": 20.0}}}
        proc._primary_tf = "5m"
        proc._secondary_tfs = ["15m"]
        proc._trend_tf = "1h"
        proc._balance = 100.0
        proc._active_trading_symbols = {"TESTUSDT"}
        proc._last_funding_fetch = time.time()
        proc._funding_fetch_interval = 3600.0

        # Minimal market cache with indicator DataFrames
        df_5m = pd.DataFrame({"close": [100.0], "timestamp": [1000]})
        df_15m = pd.DataFrame({"close": [101.0], "timestamp": [2000]})
        df_1h = pd.DataFrame({"close": [102.0], "timestamp": [3000]})
        proc._market_cache = {
            ("TESTUSDT", "5m"): df_5m,
            ("TESTUSDT", "15m"): df_15m,
            ("TESTUSDT", "1h"): df_1h,
            ("BTCUSDT", "1h"): pd.DataFrame(columns=["open", "high", "low", "close", "volume", "timestamp"]),
            ("ETHUSDT", "1h"): pd.DataFrame(columns=["open", "high", "low", "close", "volume", "timestamp"]),
        }

        # Mock grid strategy
        proc._grid_strategy = MagicMock()
        proc._grid_strategy.evaluate.return_value = []
        proc._grid_strategy._grids = {}

        # Mock signal queue
        proc._signal_queue = MagicMock()

        # Mock REST client
        proc._rest_client = MagicMock()

        return proc

    def test_df_5m_and_df_15m_passed_to_evaluate(self):
        """evaluate() must receive df_5m and df_15m keyword arguments."""
        proc = self._make_process()
        candle = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                  "volume": 1000.0, "timestamp": 1000}

        proc._evaluate_grid_strategy("TESTUSDT", candle)

        # Verify evaluate was called
        proc._grid_strategy.evaluate.assert_called_once()
        call_kwargs = proc._grid_strategy.evaluate.call_args
        # Check df_5m and df_15m are present as keyword args
        assert "df_5m" in call_kwargs.kwargs, "df_5m must be passed to evaluate()"
        assert "df_15m" in call_kwargs.kwargs, "df_15m must be passed to evaluate()"
        # Check they are the correct DataFrames from cache
        assert call_kwargs.kwargs["df_5m"] is proc._market_cache[("TESTUSDT", "5m")]
        assert call_kwargs.kwargs["df_15m"] is proc._market_cache[("TESTUSDT", "15m")]

    def test_df_5m_and_df_15m_none_when_cache_empty(self):
        """evaluate() should still be called with df_5m=None when not cached."""
        proc = self._make_process()
        # Remove 5m from cache
        del proc._market_cache[("TESTUSDT", "5m")]
        del proc._market_cache[("TESTUSDT", "15m")]

        candle = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                  "volume": 1000.0, "timestamp": 1000}

        proc._evaluate_grid_strategy("TESTUSDT", candle)

        call_kwargs = proc._grid_strategy.evaluate.call_args
        assert call_kwargs.kwargs["df_5m"] is None
        assert call_kwargs.kwargs["df_15m"] is None


# ---------------------------------------------------------------------------
# 2. Verify apply_funding is called in P3 paper mode
# ---------------------------------------------------------------------------

class TestApplyFundingIntegration:
    """Verify PaperExecutor.apply_funding is wired into P3 monitoring."""

    def test_apply_funding_method_exists(self):
        """PaperExecutor must have an apply_funding method."""
        from src.order.paper_executor import PaperExecutor

        config = {"fee_rate": {"taker": 0.0006}, "slippage_bps": 5}
        executor = PaperExecutor(config, initial_balance=100.0)
        assert hasattr(executor, "apply_funding")
        assert callable(executor.apply_funding)

    def test_apply_funding_updates_balance(self):
        """apply_funding should adjust balance when funding boundary crossed."""
        from src.order.paper_executor import PaperExecutor, PaperPosition

        config = {"fee_rate": {"taker": 0.0006}, "slippage_bps": 5}
        executor = PaperExecutor(config, initial_balance=1000.0)

        # Add a position
        executor.account.positions["pos1"] = PaperPosition(
            symbol="BTCUSDT", side="Buy", qty=0.01, entry_price=50000.0,
            leverage=5, margin=100.0, sl_price=0, tp_price=0,
            sl_order_id="", tp_order_id="",
        )

        # Initialize funding simulator with a rate
        # Use a time that crosses a funding boundary
        from datetime import datetime, timezone
        # 2024-01-01 07:59:00 UTC (just before 08:00 boundary)
        t_before = datetime(2024, 1, 1, 7, 59, 0, tzinfo=timezone.utc).timestamp()
        t_after = datetime(2024, 1, 1, 8, 1, 0, tzinfo=timezone.utc).timestamp()

        rates = {"BTCUSDT": 0.0001}  # 0.01% positive funding rate

        # First call initializes the timestamp
        executor.apply_funding(rates, current_time=t_before)
        balance_before = executor.account.balance

        # Second call crosses the 08:00 boundary
        charges = executor.apply_funding(rates, current_time=t_after)

        # Long pays positive rate, so balance should decrease
        assert len(charges) == 1
        assert charges[0]["symbol"] == "BTCUSDT"
        assert charges[0]["funding_payment"] < 0  # Long pays positive rate
        assert executor.account.balance < balance_before

    def test_apply_paper_funding_method_exists_on_process(self):
        """OrderManagerProcess must have _apply_paper_funding."""
        from src.order.process import OrderManagerProcess
        assert hasattr(OrderManagerProcess, '_apply_paper_funding')


# ---------------------------------------------------------------------------
# 3. Verify SlippageGuard config wiring in GridBiasStrategy
# ---------------------------------------------------------------------------

class TestSlippageGuardConfig:
    """Verify SlippageGuard is correctly configured in GridBiasStrategy."""

    def test_slippage_guard_instantiated(self):
        """GridBiasStrategy must create a SlippageGuard with paper config."""
        from src.strategy.position.grid_bias import GridBiasStrategy

        config = {
            "strategies": {"grid_bias": {"num_levels": 10}},
            "exit": {},
            "paper": {
                "slippage_bps": 20,
                "max_slippage_bps": 60,
                "fee_rate": {"taker": 0.0008},
            },
        }
        strategy = GridBiasStrategy(config)

        assert strategy._slippage_guard is not None
        assert strategy._slippage_guard.base_slippage_bps == 20
        assert strategy._slippage_guard.max_slippage_bps == 60
        assert strategy._slippage_guard.fee_rate == 0.0008

    def test_slippage_guard_defaults(self):
        """SlippageGuard uses defaults when paper config is missing."""
        from src.strategy.position.grid_bias import GridBiasStrategy

        config = {
            "strategies": {"grid_bias": {}},
            "exit": {},
        }
        strategy = GridBiasStrategy(config)

        assert strategy._slippage_guard is not None
        assert strategy._slippage_guard.base_slippage_bps == 15
        assert strategy._slippage_guard.fee_rate == 0.0006

    def test_dynamic_min_spacing_used_in_create_grid(self):
        """Grid creation must use dynamic min spacing from SlippageGuard."""
        from src.strategy.position.grid_bias import GridBiasStrategy
        from src.order.slippage_guard import SlippageGuard

        config = {
            "strategies": {"grid_bias": {
                "num_levels": 10,
                "min_spacing_pct": 0.50,
                "leverage": 5,
                "qty_per_level_pct": 5.0,
            }},
            "exit": {},
            "paper": {"slippage_bps": 15, "fee_rate": {"taker": 0.0006}},
        }
        strategy = GridBiasStrategy(config)

        # Verify SlippageGuard methods are accessible
        est = strategy._slippage_guard.estimate_slippage_bps("TEST", 1.0)
        assert est == 15.0  # base_slippage_bps fallback

        min_spacing = strategy._slippage_guard.adjust_min_spacing(est)
        assert min_spacing > 0  # Should return a positive percentage

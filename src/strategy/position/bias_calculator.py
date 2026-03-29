"""Directional bias calculator for grid strategy.

Combines 1h EMA trend, funding rate, and BTC/ETH market trend
to produce a directional bias that tilts the grid asymmetrically.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import pandas as pd
from loguru import logger

from src.strategy.position.base import BiasDirection


class BiasCalculator:
    """Computes directional bias from multiple market signals."""

    def __init__(self, config: dict) -> None:
        bias_cfg = config.get("bias", {})
        self.enabled = bias_cfg.get("enabled", True)
        self.ema_periods = bias_cfg.get("ema_periods", [20, 50])
        self.ema_weight = bias_cfg.get("ema_weight", 0.5)
        self.btc_eth_weight = bias_cfg.get("btc_eth_weight", 0.2)

        fr_cfg = bias_cfg.get("funding_rate", {})
        self.funding_enabled = fr_cfg.get("enabled", True)
        self.funding_threshold = fr_cfg.get("extreme_threshold", 0.01)
        self.funding_weight = fr_cfg.get("weight", 0.3)

        self.max_level_shift = bias_cfg.get("max_level_shift", 3)
        self.threshold = bias_cfg.get("threshold", 0.15)

    def compute(
        self,
        df_1h: Optional[pd.DataFrame],
        funding_rate: Optional[float],
        btc_trend: str,
        eth_trend: str,
    ) -> Tuple[BiasDirection, float, int]:
        """Compute overall bias.

        Args:
            df_1h: 1-hour indicator DataFrame for the symbol.
            funding_rate: Current funding rate (e.g., 0.0001 = 0.01%).
            btc_trend: "bull", "bear", or "mixed".
            eth_trend: "bull", "bear", or "mixed".

        Returns:
            Tuple of (direction, magnitude [-1,1], level_shift).
            level_shift: positive = more buy levels, negative = more sell levels.
        """
        if not self.enabled:
            return BiasDirection.NEUTRAL, 0.0, 0

        ema_bias = self._calc_ema_bias(df_1h)
        funding_bias = self._calc_funding_bias(funding_rate)
        market_bias = self._calc_market_bias(btc_trend, eth_trend)

        # Weighted sum
        total = (
            self.ema_weight * ema_bias
            + self.funding_weight * funding_bias
            + self.btc_eth_weight * market_bias
        )
        # Clamp to [-1, 1]
        total = max(-1.0, min(1.0, total))

        # Determine direction
        if total > self.threshold:
            direction = BiasDirection.BULLISH
        elif total < -self.threshold:
            direction = BiasDirection.BEARISH
        else:
            direction = BiasDirection.NEUTRAL

        # Calculate level shift
        level_shift = int(round(total * self.max_level_shift))
        level_shift = max(-self.max_level_shift, min(self.max_level_shift, level_shift))

        logger.debug(
            "Bias: ema={:.2f} funding={:.2f} market={:.2f} → total={:.2f} "
            "dir={} shift={}",
            ema_bias, funding_bias, market_bias,
            total, direction.value, level_shift,
        )

        return direction, total, level_shift

    def _calc_ema_bias(self, df_1h: Optional[pd.DataFrame]) -> float:
        """EMA trend bias from 1h data. Returns [-1, 1]."""
        if df_1h is None or df_1h.empty:
            return 0.0

        fast_col = f"ema_{self.ema_periods[0]}"
        slow_col = f"ema_{self.ema_periods[1]}"

        if fast_col not in df_1h.columns or slow_col not in df_1h.columns:
            return 0.0

        row = df_1h.iloc[-1]
        fast = row[fast_col]
        slow = row[slow_col]

        if pd.isna(fast) or pd.isna(slow) or slow == 0:
            return 0.0

        # Normalized spread: how far fast EMA is from slow EMA
        spread_pct = (fast - slow) / slow * 100.0

        # Map spread to [-1, 1]: ±1% spread → ±1.0
        bias = max(-1.0, min(1.0, spread_pct))
        return bias

    def _calc_funding_bias(self, funding_rate: Optional[float]) -> float:
        """Funding rate bias. Returns [-1, 1].

        Positive funding → longs pay shorts → bearish bias (favor shorts).
        Negative funding → shorts pay longs → bullish bias (favor longs).
        """
        if not self.funding_enabled or funding_rate is None:
            return 0.0

        # Normalize: funding_threshold maps to ±1.0
        if self.funding_threshold > 0:
            normalized = -funding_rate / self.funding_threshold
        else:
            normalized = 0.0

        return max(-1.0, min(1.0, normalized))

    def _calc_market_bias(self, btc_trend: str, eth_trend: str) -> float:
        """BTC/ETH market trend bias. Returns [-1, 1]."""
        score = 0.0

        if btc_trend == "bull":
            score += 0.5
        elif btc_trend == "bear":
            score -= 0.5

        if eth_trend == "bull":
            score += 0.5
        elif eth_trend == "bear":
            score -= 0.5

        return max(-1.0, min(1.0, score))

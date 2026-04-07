"""Multi-timeframe (MTF) analysis filter for grid trading decisions.

Analyzes 5m, 15m, and 1h timeframes to produce a combined signal that
the grid strategy uses to gate grid creation, fill acceptance, bias
adjustment, and spacing/level tuning.

Pure logic module — no DB, no IPC.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

import pandas as pd
from loguru import logger


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class MTFSignal(Enum):
    """Directional signal for a single timeframe."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    CONFLICTING = "CONFLICTING"  # Timeframes disagree


@dataclass
class MTFAnalysis:
    """Result of multi-timeframe analysis."""

    signal_5m: str    # BULLISH / BEARISH / NEUTRAL
    signal_15m: str
    signal_1h: str
    alignment: str    # ALIGNED / PARTIAL / CONFLICTING
    strength: float   # 0.0 to 1.0 (how strongly aligned)
    recommended_action: str  # TRADE / REDUCE / SKIP


# ---------------------------------------------------------------------------
# MTFFilter
# ---------------------------------------------------------------------------


class MTFFilter:
    """Multi-timeframe analysis filter for grid trading decisions."""

    def __init__(self, config: dict) -> None:
        self.enabled = config.get("enabled", True)
        self.require_15m_alignment = config.get("require_15m_alignment", True)
        self.require_1h_alignment = config.get("require_1h_alignment", False)

        # Timeframe weights for combined signal
        self.weight_5m = config.get("weight_5m", 0.3)
        self.weight_15m = config.get("weight_15m", 0.4)
        self.weight_1h = config.get("weight_1h", 0.3)

        # Indicator parameters
        self.ema_fast_col = f"ema_{config.get('ema_fast', 20)}"
        self.ema_slow_col = f"ema_{config.get('ema_slow', 50)}"
        self.rsi_col = f"rsi_{config.get('rsi_period', 14)}"
        self.rsi_bullish = config.get("rsi_bullish_threshold", 55)
        self.rsi_bearish = config.get("rsi_bearish_threshold", 45)

        # Grid adjustment parameters
        self.trend_spacing_multiplier = config.get("trend_spacing_multiplier", 1.3)
        self.range_spacing_multiplier = config.get("range_spacing_multiplier", 0.9)
        self.conflicting_reduce_levels = config.get("conflicting_reduce_levels", 2)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(
        self,
        df_5m: pd.DataFrame,
        df_15m: pd.DataFrame,
        df_1h: pd.DataFrame,
    ) -> MTFAnalysis:
        """Analyze all timeframes and return combined signal."""
        sig_5m = self._analyze_timeframe(df_5m, "5m")
        sig_15m = self._analyze_timeframe(df_15m, "15m")
        sig_1h = self._analyze_timeframe(df_1h, "1h")

        alignment, strength = self._calc_alignment(sig_5m, sig_15m, sig_1h)
        recommended_action = self._calc_recommended_action(alignment, strength)

        analysis = MTFAnalysis(
            signal_5m=sig_5m,
            signal_15m=sig_15m,
            signal_1h=sig_1h,
            alignment=alignment,
            strength=strength,
            recommended_action=recommended_action,
        )

        logger.debug(
            "MTF: 5m={} 15m={} 1h={} align={} str={:.2f} action={}",
            sig_5m, sig_15m, sig_1h, alignment, strength, recommended_action,
        )
        return analysis

    def should_create_grid(self, analysis: MTFAnalysis) -> bool:
        """Whether MTF conditions allow grid creation."""
        if not self.enabled:
            return True
        # Block creation only when signals conflict
        return analysis.alignment != "CONFLICTING"

    def should_allow_fill(self, analysis: MTFAnalysis, fill_side: str) -> bool:
        """Whether MTF conditions allow a specific grid fill.

        - Buy fills blocked when ALL timeframes are BEARISH (strongly bearish)
        - Sell fills blocked when ALL timeframes are BULLISH (strongly bullish)
        """
        if not self.enabled:
            return True

        signals = [analysis.signal_5m, analysis.signal_15m, analysis.signal_1h]

        if fill_side == "Buy":
            # Block buy only when strongly bearish (all TFs agree bearish)
            if all(s == MTFSignal.BEARISH.value for s in signals):
                return False
        elif fill_side == "Sell":
            # Block sell only when strongly bullish (all TFs agree bullish)
            if all(s == MTFSignal.BULLISH.value for s in signals):
                return False

        return True

    def adjust_bias(self, current_bias: float, analysis: MTFAnalysis) -> float:
        """Adjust bias magnitude based on MTF alignment.

        - ALIGNED: boost bias by 1.5x
        - PARTIAL: keep bias as-is (1.0x)
        - CONFLICTING: reduce bias by 0.5x
        """
        if not self.enabled:
            return current_bias

        if analysis.alignment == "ALIGNED":
            multiplier = 1.5
        elif analysis.alignment == "CONFLICTING":
            multiplier = 0.5
        else:
            # PARTIAL
            multiplier = 1.0

        adjusted = current_bias * multiplier
        # Clamp to [-1, 1]
        return max(-1.0, min(1.0, adjusted))

    def get_grid_adjustment(self, analysis: MTFAnalysis) -> dict:
        """Suggest grid parameter adjustments based on MTF.

        Returns:
            dict with keys:
                spacing_multiplier: float (>1 wider, <1 tighter)
                level_count_adjustment: int (negative = fewer levels)
                recenter_urgency: float (0.0 to 1.0)
        """
        if not self.enabled:
            return {
                "spacing_multiplier": 1.0,
                "level_count_adjustment": 0,
                "recenter_urgency": 0.0,
            }

        if analysis.alignment == "ALIGNED":
            # Check if it's a directional trend or neutral range
            signals = [analysis.signal_5m, analysis.signal_15m, analysis.signal_1h]
            is_directional = all(s != MTFSignal.NEUTRAL.value for s in signals)

            if is_directional:
                # Strong trend: wider spacing, fewer levels
                return {
                    "spacing_multiplier": self.trend_spacing_multiplier,
                    "level_count_adjustment": -1,
                    "recenter_urgency": 0.3,
                }
            else:
                # Range-bound: tighter spacing, more levels
                return {
                    "spacing_multiplier": self.range_spacing_multiplier,
                    "level_count_adjustment": 1,
                    "recenter_urgency": 0.0,
                }

        elif analysis.alignment == "CONFLICTING":
            return {
                "spacing_multiplier": 1.0,
                "level_count_adjustment": -self.conflicting_reduce_levels,
                "recenter_urgency": 0.7,
            }

        else:
            # PARTIAL — moderate, no change
            return {
                "spacing_multiplier": 1.0,
                "level_count_adjustment": 0,
                "recenter_urgency": 0.2,
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _analyze_timeframe(self, df: pd.DataFrame, tf_name: str) -> str:
        """Analyze a single timeframe. Returns BULLISH/BEARISH/NEUTRAL.

        Uses: EMA(fast) vs EMA(slow) crossover + RSI position + price vs VWAP.
        Requires at least 2 out of 3 sub-signals to agree for a directional call.
        """
        if df is None or df.empty:
            return MTFSignal.NEUTRAL.value

        row = df.iloc[-1]

        # --- EMA crossover signal ---
        ema_fast = row.get(self.ema_fast_col)
        ema_slow = row.get(self.ema_slow_col)
        ema_signal = self._classify_ema(ema_fast, ema_slow)

        # --- RSI signal ---
        rsi = row.get(self.rsi_col)
        rsi_signal = self._classify_rsi(rsi)

        # --- Price vs VWAP signal ---
        close = row.get("close")
        vwap = row.get("vwap")
        vwap_signal = self._classify_vwap(close, vwap)

        # Combine: require at least 2 of 3 to agree
        sub_signals = [ema_signal, rsi_signal, vwap_signal]
        bullish_count = sum(1 for s in sub_signals if s == "BULLISH")
        bearish_count = sum(1 for s in sub_signals if s == "BEARISH")

        if bullish_count >= 2:
            return MTFSignal.BULLISH.value
        elif bearish_count >= 2:
            return MTFSignal.BEARISH.value
        else:
            return MTFSignal.NEUTRAL.value

    def _classify_ema(self, fast, slow) -> str:
        """Classify EMA crossover as BULLISH/BEARISH/NEUTRAL."""
        if fast is None or slow is None:
            return "NEUTRAL"
        if pd.isna(fast) or pd.isna(slow) or slow == 0:
            return "NEUTRAL"
        fast = float(fast)
        slow = float(slow)
        spread_pct = (fast - slow) / slow * 100.0
        if spread_pct > 0.1:
            return "BULLISH"
        elif spread_pct < -0.1:
            return "BEARISH"
        return "NEUTRAL"

    def _classify_rsi(self, rsi) -> str:
        """Classify RSI as BULLISH/BEARISH/NEUTRAL."""
        if rsi is None or pd.isna(rsi):
            return "NEUTRAL"
        rsi = float(rsi)
        if rsi > self.rsi_bullish:
            return "BULLISH"
        elif rsi < self.rsi_bearish:
            return "BEARISH"
        return "NEUTRAL"

    def _classify_vwap(self, close, vwap) -> str:
        """Classify price vs VWAP as BULLISH/BEARISH/NEUTRAL."""
        if close is None or vwap is None:
            return "NEUTRAL"
        if pd.isna(close) or pd.isna(vwap) or vwap == 0:
            return "NEUTRAL"
        close = float(close)
        vwap = float(vwap)
        diff_pct = (close - vwap) / vwap * 100.0
        if diff_pct > 0.5:
            return "BULLISH"
        elif diff_pct < -0.5:
            return "BEARISH"
        return "NEUTRAL"

    def _calc_alignment(
        self, sig_5m: str, sig_15m: str, sig_1h: str,
    ) -> tuple:
        """Calculate alignment and strength from three TF signals.

        Returns:
            (alignment: str, strength: float)
        """
        signals = [sig_5m, sig_15m, sig_1h]
        non_neutral = [s for s in signals if s != MTFSignal.NEUTRAL.value]

        # All neutral → ALIGNED (range-bound)
        if len(non_neutral) == 0:
            return "ALIGNED", 0.6

        # All same direction (including all neutral handled above)
        if len(set(signals)) == 1:
            return "ALIGNED", 1.0

        # Check if all non-neutral agree
        directions = set(non_neutral)
        if len(directions) == 1:
            # Some neutral, some directional, all directional agree
            # Strength proportional to how many are directional
            strength = len(non_neutral) / 3.0
            if strength >= 0.66:
                return "ALIGNED", strength
            else:
                return "PARTIAL", strength

        # Non-neutral signals disagree
        if len(directions) == 2:
            # Two directions present among non-neutral signals
            # Check if 5m+15m agree (lower TFs aligned)
            if sig_5m == sig_15m and sig_5m != MTFSignal.NEUTRAL.value:
                return "PARTIAL", 0.5
            # 15m+1h agree
            elif sig_15m == sig_1h and sig_15m != MTFSignal.NEUTRAL.value:
                return "PARTIAL", 0.5
            else:
                return "CONFLICTING", 0.2

        # All three different (shouldn't happen with BULLISH/BEARISH/NEUTRAL,
        # but handle defensively)
        return "CONFLICTING", 0.1

    def _calc_recommended_action(self, alignment: str, strength: float) -> str:
        """Determine recommended action from alignment and strength."""
        if alignment == "ALIGNED":
            return "TRADE"
        elif alignment == "PARTIAL":
            return "REDUCE"
        else:
            return "SKIP"

"""Volatility Breakout — enters when price moves beyond ATR-based range.

"Range breakout" strategy: if the current candle's move exceeds a
threshold based on ATR, enter in the direction of the move.
Requires strong volume, ADX trending confirmation, and higher timeframe
alignment to avoid false breakouts.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd
from loguru import logger

from src.strategy.position.base import BasePositionStrategy, PositionSignal, SignalType
from src.strategy.position.registry import register_position


@register_position("volatility_breakout")
class VolatilityBreakout(BasePositionStrategy):
    """ATR-based volatility breakout with confluence filters.

    Entry: Current candle range (high-low) exceeds ATR * multiplier
           AND close is near the high (bullish) or low (bearish)
           AND volume confirms with strong surge
           AND ADX > 20 (trending market)
           AND 15m EMA alignment supports direction
           AND candle body is strong (not a doji/wick trap)
    Exit: Trailing stop, volume dry-up, or time-based.
    """

    def __init__(self, config: dict | None = None) -> None:
        self.params: Dict[str, Any] = self.get_default_params()
        if config:
            self._merge_config(config)

    def get_default_params(self) -> dict:
        return {
            "atr_breakout_multiplier": 1.8,  # was 1.5 — require bigger move
            "close_position_ratio": 0.75,    # was 0.7 — close must be in top/bottom 25%
            "volume_multiplier": 1.8,        # was 1.2 — require much stronger volume
            "min_body_ratio": 0.5,           # NEW: candle body must be > 50% of range
            "adx_threshold": 20,             # NEW: ADX minimum
            "min_confidence": 0.65,          # was 0.5 — higher bar
            "follow_scanner_direction": False,
            "min_candles_between_trades": 5,  # was 1 — dramatically reduce overtrading
            "exit": {
                "stop_loss": {
                    "atr_period": 14,
                    "atr_multiplier": 2.5,   # was 1.2 — MUCH wider (was getting stopped constantly)
                    "min_pct": 0.5,           # was 0.3
                    "max_pct": 3.0,           # was 1.5
                },
                "take_profit": {
                    "risk_reward_ratio": 2.5, # was 1.5 — much better R:R
                },
                "trailing_stop": {
                    "activation_r": 0.5,      # was 0.4 — slightly later for less whipsaw
                    "callback_atr_multiplier": 0.35,  # was 0.5 — tighter trail once activated
                },
                "strategy_exit": {
                    "ema_cross_exit": False,
                    "rsi_reversal_exit": False,
                    "volume_dry_exit": True,
                    "volume_dry_threshold": 0.4,  # was 0.3 — slightly more lenient
                },
                "time_limit": {
                    "max_holding_minutes": 30, # same — vol breakout should resolve fast
                    "warning_minutes": 22,     # was 25
                },
            },
        }

    def _merge_config(self, config: dict) -> None:
        def _deep_update(base: dict, override: dict) -> dict:
            for key, value in override.items():
                if isinstance(value, dict) and isinstance(base.get(key), dict):
                    _deep_update(base[key], value)
                else:
                    base[key] = value
            return base
        _deep_update(self.params, config)

    def evaluate(
        self,
        symbol: str,
        indicators_5m: pd.DataFrame,
        indicators_15m: pd.DataFrame,
        current_position: dict | None,
        scan_result: dict | None,
    ) -> PositionSignal:
        hold = PositionSignal(symbol=symbol, signal=SignalType.HOLD,
                              strategy="volatility_breakout", timeframe="5m")

        if indicators_5m is None or len(indicators_5m) < 20:
            return hold

        required = {"open", "high", "low", "close", "atr_14", "volume", "vol_ma5"}
        if not required.issubset(indicators_5m.columns):
            return hold

        latest = indicators_5m.iloc[-1]
        close = float(latest["close"])
        open_p = float(latest["open"])
        high = float(latest["high"])
        low = float(latest["low"])
        atr = float(latest["atr_14"])
        volume = float(latest["volume"])
        vol_ma5 = float(latest["vol_ma5"])
        adx = float(latest.get("adx_14", 0)) if "adx_14" in indicators_5m.columns else 0

        if pd.isna(atr) or atr <= 0 or high <= low:
            return hold

        candle_range = high - low
        candle_body = abs(close - open_p)

        # Exit logic: for volatility breakout, mainly rely on trailing stop + time
        if current_position is not None:
            side = current_position.get("side", "")
            # Exit on volume dry-up (momentum fading)
            if vol_ma5 > 0 and volume < vol_ma5 * self.params["exit"]["strategy_exit"].get("volume_dry_threshold", 0.4):
                return PositionSignal(
                    symbol=symbol, signal=SignalType.CLOSE,
                    strategy="volatility_breakout", timeframe="5m",
                    reason=f"Volume dried up ({volume/vol_ma5:.2f}x avg)",
                )
            return hold

        # ── FILTER 1: Candle range must exceed ATR * multiplier ──
        breakout_threshold = atr * self.params["atr_breakout_multiplier"]
        if candle_range < breakout_threshold:
            return hold

        # ── FILTER 2: Volume confirmation ──
        vol_ok = vol_ma5 > 0 and volume >= vol_ma5 * self.params["volume_multiplier"]
        if not vol_ok:
            return hold

        # ── FILTER 3: ADX trending filter ──
        if pd.isna(adx) or adx < self.params["adx_threshold"]:
            return hold

        # ── FILTER 4: Candle body strength — reject doji/wick traps ──
        body_ratio = candle_body / candle_range if candle_range > 0 else 0
        if body_ratio < self.params.get("min_body_ratio", 0.5):
            return hold

        # ── FILTER 5: Higher timeframe trend alignment ──
        htf_bias = self._get_htf_bias(indicators_15m)

        # Determine direction: where did the candle close relative to its range?
        close_ratio = self.params["close_position_ratio"]
        vol_strength = volume / vol_ma5 if vol_ma5 > 0 else 1.0
        strength = candle_range / atr

        # Bullish breakout: close near high
        if close > low + candle_range * close_ratio and htf_bias != "bearish":
            sl = close - atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
            sl = max(sl, close * (1 - self.params["exit"]["stop_loss"]["max_pct"] / 100))
            risk = close - sl
            tp = close + risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]

            confidence = 0.45
            confidence += min(0.15, (strength - 1.5) * 0.10)       # breakout strength
            confidence += min(0.15, (vol_strength - 1) * 0.05)     # volume
            confidence += 0.10 if htf_bias == "bullish" else 0.0   # HTF alignment
            confidence += min(0.10, (adx - 20) / 200)              # ADX
            confidence += 0.05 if body_ratio > 0.7 else 0.0        # strong body

            if confidence >= self.params["min_confidence"]:
                return PositionSignal(
                    symbol=symbol, signal=SignalType.LONG,
                    entry_price=close, stop_loss=sl, take_profit=tp,
                    confidence=min(confidence, 1.0),
                    strategy="volatility_breakout", timeframe="5m",
                    reason=f"Vol breakout UP {strength:.1f}xATR vol={vol_strength:.1f}x ADX={adx:.0f} htf={htf_bias}",
                )

        # Bearish breakout: close near low
        if close < high - candle_range * close_ratio and htf_bias != "bullish":
            sl = close + atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
            sl = min(sl, close * (1 + self.params["exit"]["stop_loss"]["max_pct"] / 100))
            risk = sl - close
            tp = close - risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]

            confidence = 0.45
            confidence += min(0.15, (strength - 1.5) * 0.10)
            confidence += min(0.15, (vol_strength - 1) * 0.05)
            confidence += 0.10 if htf_bias == "bearish" else 0.0
            confidence += min(0.10, (adx - 20) / 200)
            confidence += 0.05 if body_ratio > 0.7 else 0.0

            if confidence >= self.params["min_confidence"]:
                return PositionSignal(
                    symbol=symbol, signal=SignalType.SHORT,
                    entry_price=close, stop_loss=sl, take_profit=tp,
                    confidence=min(confidence, 1.0),
                    strategy="volatility_breakout", timeframe="5m",
                    reason=f"Vol breakout DOWN {strength:.1f}xATR vol={vol_strength:.1f}x ADX={adx:.0f} htf={htf_bias}",
                )

        return hold

    def _get_htf_bias(self, indicators_15m: pd.DataFrame | None) -> str:
        """Determine higher-timeframe trend bias from 15m EMA alignment.

        Returns 'bullish', 'bearish', or 'neutral'.
        """
        if indicators_15m is None or len(indicators_15m) < 20:
            return "neutral"

        htf_cols = {"ema_5", "ema_20", "close"}
        if not htf_cols.issubset(indicators_15m.columns):
            return "neutral"

        htf = indicators_15m.iloc[-1]
        htf_close = float(htf["close"])
        htf_ema5 = float(htf["ema_5"])
        htf_ema20 = float(htf["ema_20"])

        if pd.isna(htf_ema5) or pd.isna(htf_ema20):
            return "neutral"

        if htf_close > htf_ema5 > htf_ema20:
            return "bullish"
        elif htf_close < htf_ema5 < htf_ema20:
            return "bearish"
        return "neutral"

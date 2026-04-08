"""Volatility Breakout — enters when price moves beyond ATR-based range.

"Range breakout" strategy: if the current candle's move exceeds a
threshold based on ATR, enter in the direction of the move.
Best suited for volatile altcoins with sudden directional moves.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd
from loguru import logger

from src.strategy.position.base import BasePositionStrategy, PositionSignal, SignalType
from src.strategy.position.registry import register_position


@register_position("volatility_breakout")
class VolatilityBreakout(BasePositionStrategy):
    """ATR-based volatility breakout.

    Entry: Current candle range (high-low) exceeds ATR * multiplier
           AND close is near the high (bullish) or low (bearish)
           AND volume confirms
    Exit: Trailing stop or time-based. Quick in, quick out.
    """

    def __init__(self, config: dict | None = None) -> None:
        self.params: Dict[str, Any] = self.get_default_params()
        if config:
            self._merge_config(config)

    def get_default_params(self) -> dict:
        return {
            "atr_breakout_multiplier": 1.5,  # Candle range > ATR * this = breakout
            "close_position_ratio": 0.7,      # Close must be in top/bottom 30% of candle
            "volume_multiplier": 1.2,
            "min_confidence": 0.5,
            "follow_scanner_direction": False,
            "min_candles_between_trades": 1,
            "exit": {
                "stop_loss": {
                    "atr_period": 14,
                    "atr_multiplier": 1.2,
                    "min_pct": 0.3,
                    "max_pct": 1.5,
                },
                "take_profit": {
                    "risk_reward_ratio": 1.5,
                },
                "trailing_stop": {
                    "activation_r": 0.4,
                    "callback_atr_multiplier": 0.5,
                },
                "strategy_exit": {
                    "ema_cross_exit": False,
                    "rsi_reversal_exit": False,
                    "volume_dry_exit": True,
                    "volume_dry_threshold": 0.3,
                },
                "time_limit": {
                    "max_holding_minutes": 30,
                    "warning_minutes": 25,
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

        if pd.isna(atr) or atr <= 0 or high <= low:
            return hold

        candle_range = high - low
        candle_body = abs(close - open_p)

        # Exit logic: for volatility breakout, mainly rely on trailing stop + time
        if current_position is not None:
            side = current_position.get("side", "")
            # Exit on volume dry-up (momentum fading)
            if vol_ma5 > 0 and volume < vol_ma5 * self.params["exit"]["strategy_exit"].get("volume_dry_threshold", 0.3):
                return PositionSignal(
                    symbol=symbol, signal=SignalType.CLOSE,
                    strategy="volatility_breakout", timeframe="5m",
                    reason=f"Volume dried up ({volume/vol_ma5:.2f}x avg)",
                )
            return hold

        # Entry: candle range > ATR * multiplier
        breakout_threshold = atr * self.params["atr_breakout_multiplier"]
        if candle_range < breakout_threshold:
            return hold

        # Volume confirmation
        vol_ok = vol_ma5 > 0 and volume >= vol_ma5 * self.params["volume_multiplier"]
        if not vol_ok:
            return hold

        # Determine direction: where did the candle close relative to its range?
        close_ratio = self.params["close_position_ratio"]

        # Bullish breakout: close near high
        if close > low + candle_range * close_ratio:
            sl = close - atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
            sl = max(sl, close * (1 - self.params["exit"]["stop_loss"]["max_pct"] / 100))
            risk = close - sl
            tp = close + risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]

            strength = candle_range / atr
            vol_strength = volume / vol_ma5 if vol_ma5 > 0 else 1.0
            confidence = 0.5 + min(0.3, (strength - 1) * 0.15) + min(0.2, (vol_strength - 1) * 0.1)

            if confidence >= self.params["min_confidence"]:
                return PositionSignal(
                    symbol=symbol, signal=SignalType.LONG,
                    entry_price=close, stop_loss=sl, take_profit=tp,
                    confidence=min(confidence, 1.0),
                    strategy="volatility_breakout", timeframe="5m",
                    reason=f"Vol breakout UP range={candle_range/atr:.1f}xATR vol={vol_strength:.1f}x",
                )

        # Bearish breakout: close near low
        if close < high - candle_range * close_ratio:
            sl = close + atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
            sl = min(sl, close * (1 + self.params["exit"]["stop_loss"]["max_pct"] / 100))
            risk = sl - close
            tp = close - risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]

            strength = candle_range / atr
            vol_strength = volume / vol_ma5 if vol_ma5 > 0 else 1.0
            confidence = 0.5 + min(0.3, (strength - 1) * 0.15) + min(0.2, (vol_strength - 1) * 0.1)

            if confidence >= self.params["min_confidence"]:
                return PositionSignal(
                    symbol=symbol, signal=SignalType.SHORT,
                    entry_price=close, stop_loss=sl, take_profit=tp,
                    confidence=min(confidence, 1.0),
                    strategy="volatility_breakout", timeframe="5m",
                    reason=f"Vol breakout DOWN range={candle_range/atr:.1f}xATR vol={vol_strength:.1f}x",
                )

        return hold

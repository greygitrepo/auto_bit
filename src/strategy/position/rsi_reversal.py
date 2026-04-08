"""RSI Mean Reversion — enters on extreme RSI readings expecting reversal.

Buys oversold, sells overbought. Targets quick mean-reversion profits
on volatile altcoins that tend to snap back.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd
from loguru import logger

from src.strategy.position.base import BasePositionStrategy, PositionSignal, SignalType
from src.strategy.position.registry import register_position


@register_position("rsi_reversal")
class RSIReversal(BasePositionStrategy):
    """RSI extreme → reversal entry.

    Entry: RSI crosses back from extreme zone + price shows reversal candle.
    Exit: RSI reaches neutral zone or opposite extreme.
    """

    def __init__(self, config: dict | None = None) -> None:
        self.params: Dict[str, Any] = self.get_default_params()
        if config:
            self._merge_config(config)

    def get_default_params(self) -> dict:
        return {
            "rsi_period": 14,
            "rsi_oversold": 25,        # Enter long when RSI was below this
            "rsi_overbought": 75,      # Enter short when RSI was above this
            "rsi_exit_neutral_low": 45, # Exit long when RSI reaches this
            "rsi_exit_neutral_high": 55, # Exit short when RSI reaches this
            "require_reversal_candle": True,  # Require bullish/bearish candle
            "volume_multiplier": 1.0,
            "min_confidence": 0.5,
            "follow_scanner_direction": False,
            "min_candles_between_trades": 2,
            "exit": {
                "stop_loss": {
                    "atr_period": 14,
                    "atr_multiplier": 2.0,
                    "min_pct": 0.3,
                    "max_pct": 2.5,
                },
                "take_profit": {
                    "risk_reward_ratio": 1.8,
                },
                "trailing_stop": {
                    "activation_r": 0.8,
                    "callback_atr_multiplier": 0.6,
                },
                "strategy_exit": {
                    "ema_cross_exit": False,
                    "rsi_reversal_exit": True,
                    "volume_dry_exit": False,
                },
                "time_limit": {
                    "max_holding_minutes": 45,
                    "warning_minutes": 35,
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
                              strategy="rsi_reversal", timeframe="5m")

        if indicators_5m is None or len(indicators_5m) < 20:
            return hold

        required = {"close", "open", "rsi_14", "atr_14", "volume", "vol_ma5"}
        if not required.issubset(indicators_5m.columns):
            return hold

        latest = indicators_5m.iloc[-1]
        prev = indicators_5m.iloc[-2] if len(indicators_5m) > 1 else latest
        prev2 = indicators_5m.iloc[-3] if len(indicators_5m) > 2 else prev

        close = float(latest["close"])
        open_price = float(latest["open"])
        rsi = float(latest["rsi_14"])
        prev_rsi = float(prev["rsi_14"]) if not pd.isna(prev["rsi_14"]) else 50
        prev2_rsi = float(prev2["rsi_14"]) if not pd.isna(prev2["rsi_14"]) else 50
        atr = float(latest["atr_14"])

        if pd.isna(rsi) or pd.isna(atr) or atr <= 0:
            return hold

        # Exit logic
        if current_position is not None:
            side = current_position.get("side", "")
            should_close = False
            reason = ""

            if side == "Buy":
                if rsi >= self.params["rsi_exit_neutral_high"]:
                    should_close = True
                    reason = f"RSI reached neutral ({rsi:.1f})"
                elif rsi >= self.params["rsi_overbought"]:
                    should_close = True
                    reason = f"RSI overbought ({rsi:.1f})"
            elif side == "Sell":
                if rsi <= self.params["rsi_exit_neutral_low"]:
                    should_close = True
                    reason = f"RSI reached neutral ({rsi:.1f})"
                elif rsi <= self.params["rsi_oversold"]:
                    should_close = True
                    reason = f"RSI oversold ({rsi:.1f})"

            return PositionSignal(
                symbol=symbol,
                signal=SignalType.CLOSE if should_close else SignalType.HOLD,
                strategy="rsi_reversal", timeframe="5m", reason=reason,
            )

        # Entry logic: RSI reversal from extreme
        oversold = self.params["rsi_oversold"]
        overbought = self.params["rsi_overbought"]

        # Bullish reversal candle (close > open)
        bullish_candle = close > open_price
        bearish_candle = close < open_price

        # Long: RSI was oversold and is now recovering
        rsi_was_oversold = prev_rsi < oversold or prev2_rsi < oversold
        rsi_recovering = rsi > oversold and rsi > prev_rsi

        if rsi_was_oversold and rsi_recovering:
            if not self.params["require_reversal_candle"] or bullish_candle:
                sl = close - atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
                sl = max(sl, close * (1 - self.params["exit"]["stop_loss"]["max_pct"] / 100))
                risk = close - sl
                tp = close + risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]
                # Confidence: deeper the oversold dip, higher confidence
                depth = max(0, oversold - min(prev_rsi, prev2_rsi)) / oversold
                confidence = 0.55 + min(0.35, depth)

                if confidence >= self.params["min_confidence"]:
                    return PositionSignal(
                        symbol=symbol, signal=SignalType.LONG,
                        entry_price=close, stop_loss=sl, take_profit=tp,
                        confidence=min(confidence, 1.0),
                        strategy="rsi_reversal", timeframe="5m",
                        reason=f"RSI reversal from oversold ({min(prev_rsi,prev2_rsi):.1f}→{rsi:.1f})",
                    )

        # Short: RSI was overbought and is now declining
        rsi_was_overbought = prev_rsi > overbought or prev2_rsi > overbought
        rsi_declining = rsi < overbought and rsi < prev_rsi

        if rsi_was_overbought and rsi_declining:
            if not self.params["require_reversal_candle"] or bearish_candle:
                sl = close + atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
                sl = min(sl, close * (1 + self.params["exit"]["stop_loss"]["max_pct"] / 100))
                risk = sl - close
                tp = close - risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]
                depth = max(0, max(prev_rsi, prev2_rsi) - overbought) / (100 - overbought)
                confidence = 0.55 + min(0.35, depth)

                if confidence >= self.params["min_confidence"]:
                    return PositionSignal(
                        symbol=symbol, signal=SignalType.SHORT,
                        entry_price=close, stop_loss=sl, take_profit=tp,
                        confidence=min(confidence, 1.0),
                        strategy="rsi_reversal", timeframe="5m",
                        reason=f"RSI reversal from overbought ({max(prev_rsi,prev2_rsi):.1f}→{rsi:.1f})",
                    )

        return hold

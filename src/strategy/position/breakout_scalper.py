"""Breakout Scalper — enters on Bollinger Band breakouts with volume confirmation.

Targets quick profits when price breaks out of a consolidation range.
Uses BB width contraction → expansion pattern with volume surge.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd
from loguru import logger

from src.strategy.position.base import BasePositionStrategy, PositionSignal, SignalType
from src.strategy.position.registry import register_position


@register_position("breakout_scalper")
class BreakoutScalper(BasePositionStrategy):
    """Bollinger Band breakout with volume confirmation.

    Entry: Price closes above upper BB (long) or below lower BB (short)
           + volume > MA(5) * multiplier
           + BB width was contracting (squeeze)
    Exit: RSI reversal or price returns inside BB.
    """

    def __init__(self, config: dict | None = None) -> None:
        self.params: Dict[str, Any] = self.get_default_params()
        if config:
            self._merge_config(config)

    def get_default_params(self) -> dict:
        return {
            "bb_period": 20,
            "bb_std": 2.0,
            "volume_lookback": 5,
            "volume_multiplier": 1.3,
            "bb_squeeze_threshold": 0.02,  # BB width below this = squeeze
            "rsi_overbought": 80,
            "rsi_oversold": 20,
            "min_confidence": 0.5,
            "higher_tf": {
                "enabled": False,
            },
            "follow_scanner_direction": False,
            "min_candles_between_trades": 2,
            "exit": {
                "stop_loss": {
                    "atr_period": 14,
                    "atr_multiplier": 1.5,
                    "min_pct": 0.3,
                    "max_pct": 2.0,
                },
                "take_profit": {
                    "risk_reward_ratio": 1.5,
                },
                "trailing_stop": {
                    "activation_r": 0.6,
                    "callback_atr_multiplier": 0.5,
                },
                "strategy_exit": {
                    "ema_cross_exit": False,
                    "rsi_reversal_exit": True,
                    "volume_dry_exit": False,
                },
                "time_limit": {
                    "max_holding_minutes": 60,
                    "warning_minutes": 50,
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
                              strategy="breakout_scalper", timeframe="5m")

        if indicators_5m is None or len(indicators_5m) < 25:
            return hold

        required = {"close", "bb_upper", "bb_lower", "bb_width", "rsi_14",
                     "atr_14", "volume", "vol_ma5"}
        if not required.issubset(indicators_5m.columns):
            return hold

        latest = indicators_5m.iloc[-1]
        prev = indicators_5m.iloc[-2] if len(indicators_5m) > 1 else latest

        close = float(latest["close"])
        bb_upper = float(latest["bb_upper"])
        bb_lower = float(latest["bb_lower"])
        bb_width = float(latest["bb_width"])
        rsi = float(latest["rsi_14"])
        atr = float(latest["atr_14"])
        volume = float(latest["volume"])
        vol_ma5 = float(latest["vol_ma5"])

        if pd.isna(bb_upper) or pd.isna(atr) or atr <= 0:
            return hold

        # Exit logic
        if current_position is not None:
            side = current_position.get("side", "")
            should_close = False
            reason = ""

            if side == "Buy" and rsi > self.params["rsi_overbought"]:
                should_close = True
                reason = f"RSI overbought ({rsi:.1f})"
            elif side == "Sell" and rsi < self.params["rsi_oversold"]:
                should_close = True
                reason = f"RSI oversold ({rsi:.1f})"
            elif side == "Buy" and close < float(latest.get("bb_mid", close)):
                should_close = True
                reason = "Price returned below BB mid"
            elif side == "Sell" and close > float(latest.get("bb_mid", close)):
                should_close = True
                reason = "Price returned above BB mid"

            return PositionSignal(
                symbol=symbol,
                signal=SignalType.CLOSE if should_close else SignalType.HOLD,
                strategy="breakout_scalper", timeframe="5m", reason=reason,
            )

        # Entry logic: BB breakout + volume surge
        vol_mult = self.params["volume_multiplier"]
        volume_ok = vol_ma5 > 0 and volume > vol_ma5 * vol_mult

        # Check for BB squeeze (width was narrow, now expanding)
        prev_width = float(prev["bb_width"]) if not pd.isna(prev["bb_width"]) else bb_width
        squeeze_release = prev_width < self.params["bb_squeeze_threshold"] and bb_width >= prev_width

        # Long: breakout above upper BB
        if close > bb_upper and volume_ok and rsi < self.params["rsi_overbought"]:
            sl = close - atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
            sl = max(sl, close * (1 - self.params["exit"]["stop_loss"]["max_pct"] / 100))
            risk = close - sl
            tp = close + risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]
            confidence = 0.6 + (0.2 if squeeze_release else 0.0) + min(0.2, (volume / vol_ma5 - 1) * 0.1)

            if confidence >= self.params["min_confidence"]:
                return PositionSignal(
                    symbol=symbol, signal=SignalType.LONG,
                    entry_price=close, stop_loss=sl, take_profit=tp,
                    confidence=min(confidence, 1.0),
                    strategy="breakout_scalper", timeframe="5m",
                    reason=f"BB breakout UP vol={volume/vol_ma5:.1f}x width={bb_width:.4f}",
                )

        # Short: breakout below lower BB
        if close < bb_lower and volume_ok and rsi > self.params["rsi_oversold"]:
            sl = close + atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
            sl = min(sl, close * (1 + self.params["exit"]["stop_loss"]["max_pct"] / 100))
            risk = sl - close
            tp = close - risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]
            confidence = 0.6 + (0.2 if squeeze_release else 0.0) + min(0.2, (volume / vol_ma5 - 1) * 0.1)

            if confidence >= self.params["min_confidence"]:
                return PositionSignal(
                    symbol=symbol, signal=SignalType.SHORT,
                    entry_price=close, stop_loss=sl, take_profit=tp,
                    confidence=min(confidence, 1.0),
                    strategy="breakout_scalper", timeframe="5m",
                    reason=f"BB breakout DOWN vol={volume/vol_ma5:.1f}x width={bb_width:.4f}",
                )

        return hold

"""EMA Crossover — fast trend-following strategy on EMA crosses.

Enters when fast EMA crosses slow EMA with volume confirmation.
Quick entries and exits for capturing short trend bursts.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd
from loguru import logger

from src.strategy.position.base import BasePositionStrategy, PositionSignal, SignalType
from src.strategy.position.registry import register_position


@register_position("ema_crossover")
class EMACrossover(BasePositionStrategy):
    """Fast/slow EMA crossover with volume filter.

    Entry: Fast EMA crosses above slow (long) or below (short)
           + volume above average
           + ADX > threshold (trending market)
    Exit: Reverse crossover or trailing stop.
    """

    def __init__(self, config: dict | None = None) -> None:
        self.params: Dict[str, Any] = self.get_default_params()
        if config:
            self._merge_config(config)

    def get_default_params(self) -> dict:
        return {
            "ema_fast": 5,
            "ema_slow": 20,
            "adx_threshold": 18,       # Min ADX for trending confirmation
            "volume_multiplier": 1.0,
            "require_adx": True,
            "min_confidence": 0.5,
            "follow_scanner_direction": False,
            "min_candles_between_trades": 1,
            "exit": {
                "stop_loss": {
                    "atr_period": 14,
                    "atr_multiplier": 1.8,
                    "min_pct": 0.3,
                    "max_pct": 2.0,
                },
                "take_profit": {
                    "risk_reward_ratio": 2.0,
                },
                "trailing_stop": {
                    "activation_r": 0.5,
                    "callback_atr_multiplier": 0.7,
                },
                "strategy_exit": {
                    "ema_cross_exit": True,
                    "rsi_reversal_exit": False,
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
                              strategy="ema_crossover", timeframe="5m")

        if indicators_5m is None or len(indicators_5m) < 25:
            return hold

        required = {"close", "ema_5", "ema_20", "atr_14", "volume", "vol_ma5"}
        if not required.issubset(indicators_5m.columns):
            return hold

        latest = indicators_5m.iloc[-1]
        prev = indicators_5m.iloc[-2] if len(indicators_5m) > 1 else latest

        close = float(latest["close"])
        ema_fast = float(latest["ema_5"])
        ema_slow = float(latest["ema_20"])
        prev_ema_fast = float(prev["ema_5"]) if not pd.isna(prev["ema_5"]) else ema_fast
        prev_ema_slow = float(prev["ema_20"]) if not pd.isna(prev["ema_20"]) else ema_slow
        atr = float(latest["atr_14"])
        volume = float(latest["volume"])
        vol_ma5 = float(latest["vol_ma5"])
        adx = float(latest.get("adx_14", 25)) if "adx_14" in indicators_5m.columns else 25

        if pd.isna(ema_fast) or pd.isna(ema_slow) or pd.isna(atr) or atr <= 0:
            return hold

        # Detect crossover
        golden_cross = prev_ema_fast <= prev_ema_slow and ema_fast > ema_slow
        death_cross = prev_ema_fast >= prev_ema_slow and ema_fast < ema_slow

        # Exit logic
        if current_position is not None:
            side = current_position.get("side", "")
            should_close = False
            reason = ""

            if side == "Buy" and death_cross:
                should_close = True
                reason = "EMA death cross (exit long)"
            elif side == "Sell" and golden_cross:
                should_close = True
                reason = "EMA golden cross (exit short)"

            return PositionSignal(
                symbol=symbol,
                signal=SignalType.CLOSE if should_close else SignalType.HOLD,
                strategy="ema_crossover", timeframe="5m", reason=reason,
            )

        # Volume filter
        vol_ok = vol_ma5 > 0 and volume >= vol_ma5 * self.params["volume_multiplier"]

        # ADX filter (trending market)
        adx_ok = not self.params["require_adx"] or (not pd.isna(adx) and adx >= self.params["adx_threshold"])

        # Long: golden cross
        if golden_cross and vol_ok and adx_ok:
            sl = close - atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
            sl = max(sl, close * (1 - self.params["exit"]["stop_loss"]["max_pct"] / 100))
            risk = close - sl
            tp = close + risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]

            # Confidence based on cross strength + ADX
            cross_strength = abs(ema_fast - ema_slow) / close * 1000
            confidence = 0.5 + min(0.25, cross_strength * 0.05)
            if not pd.isna(adx):
                confidence += min(0.25, (adx - 15) / 100)

            if confidence >= self.params["min_confidence"]:
                return PositionSignal(
                    symbol=symbol, signal=SignalType.LONG,
                    entry_price=close, stop_loss=sl, take_profit=tp,
                    confidence=min(confidence, 1.0),
                    strategy="ema_crossover", timeframe="5m",
                    reason=f"Golden cross EMA{self.params['ema_fast']}/{self.params['ema_slow']} ADX={adx:.1f}",
                )

        # Short: death cross
        if death_cross and vol_ok and adx_ok:
            sl = close + atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
            sl = min(sl, close * (1 + self.params["exit"]["stop_loss"]["max_pct"] / 100))
            risk = sl - close
            tp = close - risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]

            cross_strength = abs(ema_fast - ema_slow) / close * 1000
            confidence = 0.5 + min(0.25, cross_strength * 0.05)
            if not pd.isna(adx):
                confidence += min(0.25, (adx - 15) / 100)

            if confidence >= self.params["min_confidence"]:
                return PositionSignal(
                    symbol=symbol, signal=SignalType.SHORT,
                    entry_price=close, stop_loss=sl, take_profit=tp,
                    confidence=min(confidence, 1.0),
                    strategy="ema_crossover", timeframe="5m",
                    reason=f"Death cross EMA{self.params['ema_fast']}/{self.params['ema_slow']} ADX={adx:.1f}",
                )

        return hold

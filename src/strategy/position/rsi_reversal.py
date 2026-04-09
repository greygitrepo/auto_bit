"""RSI Mean Reversion — enters on extreme RSI readings expecting reversal.

Buys oversold, sells overbought. Targets quick mean-reversion profits
on volatile altcoins that tend to snap back.
Requires deep RSI extremes, volume confirmation, ADX filter,
and higher timeframe trend alignment.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd
from loguru import logger

from src.strategy.position.base import BasePositionStrategy, PositionSignal, SignalType
from src.strategy.position.registry import register_position


@register_position("rsi_reversal")
class RSIReversal(BasePositionStrategy):
    """RSI extreme → reversal entry with confluence filters.

    Entry: RSI crosses back from deep extreme zone
           + reversal candle pattern (bullish/bearish engulfing or strong body)
           + volume spike confirms participation
           + ADX > 20 (avoid flat/choppy markets where mean-reversion fails)
           + 15m trend not strongly opposing the trade direction
    Exit: RSI reaches neutral zone or opposite extreme.
    """

    def __init__(self, config: dict | None = None) -> None:
        self.params: Dict[str, Any] = self.get_default_params()
        if config:
            self._merge_config(config)

    def get_default_params(self) -> dict:
        return {
            "rsi_period": 14,
            "rsi_oversold": 20,            # was 25 — require deeper extremes
            "rsi_overbought": 80,          # was 75 — require deeper extremes
            "rsi_exit_neutral_low": 45,
            "rsi_exit_neutral_high": 55,
            "require_reversal_candle": True,
            "volume_multiplier": 1.3,      # was 1.0 — require volume confirmation
            "adx_threshold": 20,           # NEW: ADX minimum
            "min_confidence": 0.65,        # was 0.5 — higher bar
            "follow_scanner_direction": False,
            "min_candles_between_trades": 5, # was 2 — reduce overtrading
            "exit": {
                "stop_loss": {
                    "atr_period": 14,
                    "atr_multiplier": 2.5,   # was 2.0 — wider to survive noise
                    "min_pct": 0.5,           # was 0.3
                    "max_pct": 3.5,           # was 2.5
                },
                "take_profit": {
                    "risk_reward_ratio": 2.5, # was 1.8 — better R:R
                },
                "trailing_stop": {
                    "activation_r": 0.5,      # was 0.8 — activate much earlier
                    "callback_atr_multiplier": 0.4,  # was 0.6 — tighter trail
                },
                "strategy_exit": {
                    "ema_cross_exit": False,
                    "rsi_reversal_exit": True,
                    "volume_dry_exit": False,
                },
                "time_limit": {
                    "max_holding_minutes": 40, # was 45
                    "warning_minutes": 30,     # was 35
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

        required = {"close", "open", "high", "low", "rsi_14", "atr_14", "volume", "vol_ma5"}
        if not required.issubset(indicators_5m.columns):
            return hold

        latest = indicators_5m.iloc[-1]
        prev = indicators_5m.iloc[-2] if len(indicators_5m) > 1 else latest
        prev2 = indicators_5m.iloc[-3] if len(indicators_5m) > 2 else prev

        close = float(latest["close"])
        open_price = float(latest["open"])
        high = float(latest["high"])
        low = float(latest["low"])
        rsi = float(latest["rsi_14"])
        prev_rsi = float(prev["rsi_14"]) if not pd.isna(prev["rsi_14"]) else 50
        prev2_rsi = float(prev2["rsi_14"]) if not pd.isna(prev2["rsi_14"]) else 50
        atr = float(latest["atr_14"])
        volume = float(latest["volume"])
        vol_ma5 = float(latest["vol_ma5"])
        adx = float(latest.get("adx_14", 0)) if "adx_14" in indicators_5m.columns else 0

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

        # ── FILTER 1: ADX trending filter ──
        if pd.isna(adx) or adx < self.params["adx_threshold"]:
            return hold

        # ── FILTER 2: Volume confirmation ──
        vol_ok = vol_ma5 > 0 and volume >= vol_ma5 * self.params["volume_multiplier"]
        if not vol_ok:
            return hold

        # ── FILTER 3: Higher timeframe trend bias ──
        htf_bias = self._get_htf_bias(indicators_15m)

        # Entry logic: RSI reversal from deep extreme
        oversold = self.params["rsi_oversold"]
        overbought = self.params["rsi_overbought"]

        # Reversal candle analysis
        candle_body = abs(close - open_price)
        candle_range = high - low if high > low else atr * 0.1
        body_ratio = candle_body / candle_range  # strong body = conviction
        bullish_candle = close > open_price and body_ratio > 0.5
        bearish_candle = close < open_price and body_ratio > 0.5

        # Long: RSI was deeply oversold and is now recovering
        rsi_was_oversold = prev_rsi < oversold or prev2_rsi < oversold
        rsi_recovering = rsi > oversold and rsi > prev_rsi

        if rsi_was_oversold and rsi_recovering and htf_bias != "bearish":
            if not self.params["require_reversal_candle"] or bullish_candle:
                sl = close - atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
                sl = max(sl, close * (1 - self.params["exit"]["stop_loss"]["max_pct"] / 100))
                risk = close - sl
                tp = close + risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]

                # Confidence: deeper the dip + volume strength + HTF alignment
                depth = max(0, oversold - min(prev_rsi, prev2_rsi)) / oversold
                confidence = 0.45
                confidence += min(0.20, depth * 0.8)
                confidence += min(0.10, (volume / vol_ma5 - 1) * 0.05) if vol_ma5 > 0 else 0.0
                confidence += 0.10 if htf_bias == "bullish" else 0.0
                confidence += min(0.10, (adx - 20) / 200)
                confidence += 0.05 if body_ratio > 0.6 else 0.0  # strong reversal candle

                if confidence >= self.params["min_confidence"]:
                    return PositionSignal(
                        symbol=symbol, signal=SignalType.LONG,
                        entry_price=close, stop_loss=sl, take_profit=tp,
                        confidence=min(confidence, 1.0),
                        strategy="rsi_reversal", timeframe="5m",
                        reason=f"RSI reversal oversold ({min(prev_rsi,prev2_rsi):.1f}->{rsi:.1f}) ADX={adx:.0f} htf={htf_bias}",
                    )

        # Short: RSI was deeply overbought and is now declining
        rsi_was_overbought = prev_rsi > overbought or prev2_rsi > overbought
        rsi_declining = rsi < overbought and rsi < prev_rsi

        if rsi_was_overbought and rsi_declining and htf_bias != "bullish":
            if not self.params["require_reversal_candle"] or bearish_candle:
                sl = close + atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
                sl = min(sl, close * (1 + self.params["exit"]["stop_loss"]["max_pct"] / 100))
                risk = sl - close
                tp = close - risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]

                depth = max(0, max(prev_rsi, prev2_rsi) - overbought) / (100 - overbought)
                confidence = 0.45
                confidence += min(0.20, depth * 0.8)
                confidence += min(0.10, (volume / vol_ma5 - 1) * 0.05) if vol_ma5 > 0 else 0.0
                confidence += 0.10 if htf_bias == "bearish" else 0.0
                confidence += min(0.10, (adx - 20) / 200)
                confidence += 0.05 if body_ratio > 0.6 else 0.0

                if confidence >= self.params["min_confidence"]:
                    return PositionSignal(
                        symbol=symbol, signal=SignalType.SHORT,
                        entry_price=close, stop_loss=sl, take_profit=tp,
                        confidence=min(confidence, 1.0),
                        strategy="rsi_reversal", timeframe="5m",
                        reason=f"RSI reversal overbought ({max(prev_rsi,prev2_rsi):.1f}->{rsi:.1f}) ADX={adx:.0f} htf={htf_bias}",
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

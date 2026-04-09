"""EMA Crossover — trend-following strategy on EMA crosses.

Enters when fast EMA crosses slow EMA with volume confirmation.
Requires ADX trending confirmation + higher timeframe alignment
to avoid whipsaw trades in choppy markets.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd
from loguru import logger

from src.strategy.position.base import BasePositionStrategy, PositionSignal, SignalType
from src.strategy.position.registry import register_position


@register_position("ema_crossover")
class EMACrossover(BasePositionStrategy):
    """Fast/slow EMA crossover with multi-layer confluence.

    Entry: Fast EMA crosses above slow (long) or below (short)
           + volume above average * multiplier
           + ADX > 25 (strong trending market)
           + 15m EMA alignment confirms direction
           + RSI not in extreme zone (avoid exhaustion)
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
            "adx_threshold": 25,           # was 18 — stronger trend required
            "volume_multiplier": 1.5,      # was 1.0 — require volume confirmation
            "require_adx": True,
            "rsi_long_max": 70,            # NEW: don't buy when RSI already high
            "rsi_short_min": 30,           # NEW: don't sell when RSI already low
            "min_cross_separation_pct": 0.05,  # NEW: min % gap between EMAs at cross
            "min_confidence": 0.65,        # was 0.5 — higher bar
            "follow_scanner_direction": False,
            "min_candles_between_trades": 5, # was 1 — dramatically reduce overtrading
            "exit": {
                "stop_loss": {
                    "atr_period": 14,
                    "atr_multiplier": 2.5,   # was 1.8 — wider to survive noise
                    "min_pct": 0.5,           # was 0.3
                    "max_pct": 3.0,           # was 2.0
                },
                "take_profit": {
                    "risk_reward_ratio": 2.5, # was 2.0 — better R:R
                },
                "trailing_stop": {
                    "activation_r": 0.5,      # same — activate at 0.5R profit
                    "callback_atr_multiplier": 0.4,  # was 0.7 — much tighter trail
                },
                "strategy_exit": {
                    "ema_cross_exit": True,
                    "rsi_reversal_exit": False,
                    "volume_dry_exit": False,
                },
                "time_limit": {
                    "max_holding_minutes": 45, # was 60
                    "warning_minutes": 35,     # was 50
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
        adx = float(latest.get("adx_14", 0)) if "adx_14" in indicators_5m.columns else 0
        rsi = float(latest.get("rsi_14", 50)) if "rsi_14" in indicators_5m.columns else 50

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

        # ── FILTER 1: ADX trending filter (mandatory) ──
        adx_ok = not pd.isna(adx) and adx >= self.params["adx_threshold"]
        if self.params["require_adx"] and not adx_ok:
            return hold

        # ── FILTER 2: Volume confirmation ──
        vol_ok = vol_ma5 > 0 and volume >= vol_ma5 * self.params["volume_multiplier"]
        if not vol_ok:
            return hold

        # ── FILTER 3: Higher timeframe trend alignment ──
        htf_bias = self._get_htf_bias(indicators_15m)

        # ── FILTER 4: Cross separation — reject weak crosses ──
        cross_gap_pct = abs(ema_fast - ema_slow) / close * 100
        min_sep = self.params.get("min_cross_separation_pct", 0.05)

        # Long: golden cross
        if golden_cross and cross_gap_pct >= min_sep:
            # Don't long when RSI already high (exhaustion)
            if not pd.isna(rsi) and rsi > self.params.get("rsi_long_max", 70):
                return hold
            # Don't long against 15m downtrend
            if htf_bias == "bearish":
                return hold

            sl = close - atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
            sl = max(sl, close * (1 - self.params["exit"]["stop_loss"]["max_pct"] / 100))
            risk = close - sl
            tp = close + risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]

            # Confidence based on cross strength + ADX + HTF alignment
            confidence = 0.45
            confidence += min(0.15, cross_gap_pct * 0.5)           # cross strength
            confidence += min(0.15, (adx - 20) / 100) if not pd.isna(adx) else 0.0  # ADX
            confidence += 0.10 if htf_bias == "bullish" else 0.0   # HTF alignment
            confidence += min(0.10, (volume / vol_ma5 - 1) * 0.05) if vol_ma5 > 0 else 0.0

            if confidence >= self.params["min_confidence"]:
                return PositionSignal(
                    symbol=symbol, signal=SignalType.LONG,
                    entry_price=close, stop_loss=sl, take_profit=tp,
                    confidence=min(confidence, 1.0),
                    strategy="ema_crossover", timeframe="5m",
                    reason=f"Golden cross EMA{self.params['ema_fast']}/{self.params['ema_slow']} ADX={adx:.1f} htf={htf_bias}",
                )

        # Short: death cross
        if death_cross and cross_gap_pct >= min_sep:
            # Don't short when RSI already low (oversold bounce risk)
            if not pd.isna(rsi) and rsi < self.params.get("rsi_short_min", 30):
                return hold
            # Don't short against 15m uptrend
            if htf_bias == "bullish":
                return hold

            sl = close + atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
            sl = min(sl, close * (1 + self.params["exit"]["stop_loss"]["max_pct"] / 100))
            risk = sl - close
            tp = close - risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]

            confidence = 0.45
            confidence += min(0.15, cross_gap_pct * 0.5)
            confidence += min(0.15, (adx - 20) / 100) if not pd.isna(adx) else 0.0
            confidence += 0.10 if htf_bias == "bearish" else 0.0
            confidence += min(0.10, (volume / vol_ma5 - 1) * 0.05) if vol_ma5 > 0 else 0.0

            if confidence >= self.params["min_confidence"]:
                return PositionSignal(
                    symbol=symbol, signal=SignalType.SHORT,
                    entry_price=close, stop_loss=sl, take_profit=tp,
                    confidence=min(confidence, 1.0),
                    strategy="ema_crossover", timeframe="5m",
                    reason=f"Death cross EMA{self.params['ema_fast']}/{self.params['ema_slow']} ADX={adx:.1f} htf={htf_bias}",
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

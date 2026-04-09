"""Breakout Scalper — enters on Bollinger Band breakouts with volume confirmation.

Targets quick profits when price breaks out of a consolidation range.
Uses BB width contraction → expansion pattern with volume surge.
Requires ADX trending filter + higher timeframe trend alignment.
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
           + BB width was contracting (squeeze) and now expanding
           + ADX > 20 (trending market — avoid chop)
           + 15m EMA alignment confirms direction
           + RSI not extreme (avoid chasing)
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
            "volume_multiplier": 1.8,       # was 1.3 — require stronger volume surge
            "bb_squeeze_threshold": 0.02,    # BB width below this = squeeze
            "rsi_overbought": 75,            # was 80 — tighter to avoid chasing
            "rsi_oversold": 25,              # was 20 — tighter to avoid chasing
            "adx_threshold": 20,             # ADX minimum for trending confirmation
            "min_confidence": 0.65,          # was 0.5 — higher bar for entry
            "higher_tf": {
                "enabled": True,             # was False — now mandatory
            },
            "follow_scanner_direction": False,
            "min_candles_between_trades": 5,  # was 2 — reduce overtrading
            "exit": {
                "stop_loss": {
                    "atr_period": 14,
                    "atr_multiplier": 2.5,   # was 1.5 — wider to avoid noise
                    "min_pct": 0.5,           # was 0.3
                    "max_pct": 3.0,           # was 2.0
                },
                "take_profit": {
                    "risk_reward_ratio": 2.5, # was 1.5 — better R:R
                },
                "trailing_stop": {
                    "activation_r": 0.5,      # was 0.6 — activate earlier
                    "callback_atr_multiplier": 0.4,  # was 0.5 — tighter trail
                },
                "strategy_exit": {
                    "ema_cross_exit": False,
                    "rsi_reversal_exit": True,
                    "volume_dry_exit": False,
                },
                "time_limit": {
                    "max_holding_minutes": 45, # was 60 — tighter time limit
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
        adx = float(latest.get("adx_14", 0)) if "adx_14" in indicators_5m.columns else 0

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

        # ── FILTER 1: ADX trending filter (mandatory) ──
        if pd.isna(adx) or adx < self.params["adx_threshold"]:
            return hold

        # ── FILTER 2: Higher timeframe trend alignment ──
        htf_bias = self._get_htf_bias(indicators_15m)

        # ── FILTER 3: Volume surge ──
        vol_mult = self.params["volume_multiplier"]
        volume_ok = vol_ma5 > 0 and volume > vol_ma5 * vol_mult

        # ── FILTER 4: BB squeeze release ──
        prev_width = float(prev["bb_width"]) if not pd.isna(prev["bb_width"]) else bb_width
        squeeze_release = prev_width < self.params["bb_squeeze_threshold"] and bb_width >= prev_width

        # Long: breakout above upper BB
        if (close > bb_upper
                and volume_ok
                and rsi < self.params["rsi_overbought"]
                and htf_bias != "bearish"):       # don't long against 15m downtrend
            sl = close - atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
            sl = max(sl, close * (1 - self.params["exit"]["stop_loss"]["max_pct"] / 100))
            risk = close - sl
            tp = close + risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]

            # Confidence: squeeze release + volume strength + HTF alignment
            confidence = 0.50
            confidence += 0.15 if squeeze_release else 0.0
            confidence += min(0.15, (volume / vol_ma5 - 1) * 0.05) if vol_ma5 > 0 else 0.0
            confidence += 0.10 if htf_bias == "bullish" else 0.0
            confidence += min(0.10, (adx - 20) / 200)  # stronger trend = more confidence

            if confidence >= self.params["min_confidence"]:
                return PositionSignal(
                    symbol=symbol, signal=SignalType.LONG,
                    entry_price=close, stop_loss=sl, take_profit=tp,
                    confidence=min(confidence, 1.0),
                    strategy="breakout_scalper", timeframe="5m",
                    reason=f"BB breakout UP vol={volume/vol_ma5:.1f}x w={bb_width:.4f} ADX={adx:.0f} htf={htf_bias}",
                )

        # Short: breakout below lower BB
        if (close < bb_lower
                and volume_ok
                and rsi > self.params["rsi_oversold"]
                and htf_bias != "bullish"):       # don't short against 15m uptrend
            sl = close + atr * self.params["exit"]["stop_loss"]["atr_multiplier"]
            sl = min(sl, close * (1 + self.params["exit"]["stop_loss"]["max_pct"] / 100))
            risk = sl - close
            tp = close - risk * self.params["exit"]["take_profit"]["risk_reward_ratio"]

            confidence = 0.50
            confidence += 0.15 if squeeze_release else 0.0
            confidence += min(0.15, (volume / vol_ma5 - 1) * 0.05) if vol_ma5 > 0 else 0.0
            confidence += 0.10 if htf_bias == "bearish" else 0.0
            confidence += min(0.10, (adx - 20) / 200)

            if confidence >= self.params["min_confidence"]:
                return PositionSignal(
                    symbol=symbol, signal=SignalType.SHORT,
                    entry_price=close, stop_loss=sl, take_profit=tp,
                    confidence=min(confidence, 1.0),
                    strategy="breakout_scalper", timeframe="5m",
                    reason=f"BB breakout DOWN vol={volume/vol_ma5:.1f}x w={bb_width:.4f} ADX={adx:.0f} htf={htf_bias}",
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

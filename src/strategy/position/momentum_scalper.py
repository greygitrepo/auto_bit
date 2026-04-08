"""Momentum Scalper position strategy.

A short-term scalping strategy that combines EMA alignment, RSI momentum,
volume confirmation, and VWAP context to produce LONG/SHORT entry signals
on the 5-minute timeframe with a 15-minute higher-timeframe filter.

Tasks S-07 (entry), S-08 (exit), S-09 (SL/TP), S-10 (trailing stop),
S-11 (time limit).
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, Tuple

import pandas as pd
from loguru import logger

from src.strategy.position.base import (
    BasePositionStrategy,
    PositionSignal,
    SignalType,
    TrailingStopState,
)
from src.strategy.position.registry import register_position


# ======================================================================
# S-10  Trailing-stop manager
# ======================================================================


class TrailingStopManager:
    """Manages trailing-stop activation and updates for an open position.

    The trailing stop activates once the unrealised profit reaches 1 R
    (i.e. the original stop-loss distance).  After activation the stop
    follows the price at ``ATR * callback_atr_multiplier`` distance.
    """

    @staticmethod
    def update(
        state: TrailingStopState,
        current_price: float,
        side: str,
        atr: float,
        config: dict,
    ) -> Tuple[TrailingStopState, bool]:
        """Advance the trailing-stop state and decide whether to close.

        Args:
            state: Current trailing-stop state (mutated in-place and returned).
            current_price: Latest market price.
            side: ``"LONG"`` or ``"SHORT"``.
            atr: Current ATR value used for callback distance.
            config: Must contain ``activation_r`` (float) and
                ``callback_atr_multiplier`` (float).

        Returns:
            Tuple of (updated state, should_close).
        """
        callback_distance = atr * config.get("callback_atr_multiplier", 0.8)
        should_close = False

        if side == "LONG":
            # --- activation check ---
            if not state.active:
                if current_price >= state.activation_price:
                    state.active = True
                    state.highest_price = current_price
                    state.callback_distance = callback_distance
                    state.trailing_sl = max(current_price - callback_distance, state.entry_price)
                    logger.info(
                        "Trailing stop activated (LONG) at {:.4f}, "
                        "trailing SL={:.4f} (entry_price floor={:.4f})",
                        current_price,
                        state.trailing_sl,
                        state.entry_price,
                    )
            else:
                # update high-water mark
                if current_price > state.highest_price:
                    state.highest_price = current_price
                    state.callback_distance = callback_distance
                    state.trailing_sl = max(current_price - callback_distance, state.entry_price)
                # check trigger
                if current_price <= state.trailing_sl:
                    should_close = True
                    logger.info(
                        "Trailing stop triggered (LONG): price={:.4f} <= "
                        "trailing_sl={:.4f}",
                        current_price,
                        state.trailing_sl,
                    )
        else:
            # SHORT
            if not state.active:
                if current_price <= state.activation_price:
                    state.active = True
                    state.lowest_price = current_price
                    state.callback_distance = callback_distance
                    state.trailing_sl = min(current_price + callback_distance, state.entry_price)
                    logger.info(
                        "Trailing stop activated (SHORT) at {:.4f}, "
                        "trailing SL={:.4f} (entry_price ceiling={:.4f})",
                        current_price,
                        state.trailing_sl,
                        state.entry_price,
                    )
            else:
                if current_price < state.lowest_price:
                    state.lowest_price = current_price
                    state.callback_distance = callback_distance
                    state.trailing_sl = min(current_price + callback_distance, state.entry_price)
                if current_price >= state.trailing_sl:
                    should_close = True
                    logger.info(
                        "Trailing stop triggered (SHORT): price={:.4f} >= "
                        "trailing_sl={:.4f}",
                        current_price,
                        state.trailing_sl,
                    )

        return state, should_close

    @staticmethod
    def create_initial_state(
        entry_price: float,
        sl_distance: float,
        side: str,
        activation_r: float = 1.0,
    ) -> TrailingStopState:
        """Build the initial trailing-stop state when a position is opened.

        Args:
            entry_price: Position entry price.
            sl_distance: Absolute stop-loss distance (1 R).
            side: ``"LONG"`` or ``"SHORT"``.
            activation_r: Multiples of R required before trailing activates.

        Returns:
            A fresh :class:`TrailingStopState`.
        """
        activation_distance = sl_distance * activation_r
        if side == "LONG":
            activation_price = entry_price + activation_distance
        else:
            activation_price = entry_price - activation_distance

        return TrailingStopState(
            active=False,
            activation_price=activation_price,
            entry_price=entry_price,
        )


# ======================================================================
# S-11  Time-limit manager
# ======================================================================


class TimeLimitManager:
    """Checks whether a position has exceeded its maximum holding time."""

    @staticmethod
    def check(
        entered_at: float,
        max_minutes: int = 90,
        warning_minutes: int = 75,
    ) -> Tuple[str, int]:
        """Evaluate the elapsed holding time against configured limits.

        Args:
            entered_at: Unix timestamp when the position was opened.
            max_minutes: Maximum allowed holding time in minutes.
            warning_minutes: Elapsed minutes at which a warning is issued.

        Returns:
            Tuple of (status, elapsed_minutes) where *status* is one of
            ``"normal"``, ``"warning"``, or ``"expired"``.
        """
        elapsed_seconds = time.time() - entered_at
        elapsed_minutes = int(elapsed_seconds / 60)

        if elapsed_minutes >= max_minutes:
            return "expired", elapsed_minutes
        if elapsed_minutes >= warning_minutes:
            return "warning", elapsed_minutes
        return "normal", elapsed_minutes


# ======================================================================
# S-07 / S-08 / S-09  Momentum Scalper strategy
# ======================================================================


@register_position("momentum_scalper")
class MomentumScalper(BasePositionStrategy):
    """Short-term momentum scalping strategy.

    **Entry (S-07)** requires all five conditions on the 5-minute chart plus
    a higher-timeframe EMA filter on the 15-minute chart.

    **Exit (S-08)** triggers on EMA cross, RSI reversal, or volume dry-up.

    **SL/TP (S-09)** are ATR-based with configurable risk-reward ratio.
    """

    def __init__(self, config: dict | None = None) -> None:
        self.params: Dict[str, Any] = self.get_default_params()
        if config:
            self._merge_config(config)

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def get_default_params(self) -> dict:
        """Return default strategy parameters matching ``position.yaml``."""
        return {
            "ema_fast": 5,
            "ema_mid": 10,
            "ema_slow": 20,
            "rsi_period": 14,
            "rsi_long_range": [50, 75],
            "rsi_short_range": [25, 50],
            "volume_lookback": 5,
            "volume_multiplier": 1.5,
            "vwap_enabled": True,
            "higher_tf": {
                "enabled": True,
                "timeframe": "15m",
                "ema_fast": 5,
                "ema_slow": 10,
            },
            "follow_scanner_direction": True,
            "min_candles_between_trades": 3,
            "exit": {
                "stop_loss": {
                    "atr_period": 14,
                    "atr_multiplier": 1.5,
                    "min_pct": 0.3,
                    "max_pct": 2.0,
                },
                "take_profit": {
                    "risk_reward_ratio": 2.0,
                },
                "trailing_stop": {
                    "activation_r": 1.0,
                    "callback_atr_multiplier": 0.8,
                },
                "strategy_exit": {
                    "ema_cross_exit": True,
                    "rsi_reversal_exit": True,
                    "volume_dry_exit": True,
                    "volume_dry_threshold": 0.3,
                },
                "time_limit": {
                    "max_holding_minutes": 90,
                    "warning_minutes": 75,
                },
            },
        }

    def _merge_config(self, config: dict) -> None:
        """Recursively merge *config* into ``self.params``."""
        def _deep_update(base: dict, override: dict) -> dict:
            for key, value in override.items():
                if isinstance(value, dict) and isinstance(base.get(key), dict):
                    _deep_update(base[key], value)
                else:
                    base[key] = value
            return base

        _deep_update(self.params, config)

    # ------------------------------------------------------------------
    # S-07: Entry logic
    # ------------------------------------------------------------------

    def _check_long_entry(
        self, df_5m: pd.DataFrame, df_15m: pd.DataFrame
    ) -> Tuple[bool, str]:
        """Evaluate all LONG entry conditions.

        Returns:
            Tuple of (conditions_met, reason_string).
        """
        row = df_5m.iloc[-1]
        reasons: list[str] = []

        # 1. EMA alignment (mode-dependent)
        ema_mode = self.params.get("ema_alignment_mode", "strict")
        if ema_mode == "minimal":
            # Only require EMA5 > EMA20 (price trending up)
            if not (row["ema_5"] > row["ema_20"]):
                return False, "EMA5 not above EMA20"
        elif ema_mode == "relaxed":
            # Require EMA5 > EMA10 (2-EMA alignment)
            if not (row["ema_5"] > row["ema_10"]):
                return False, "EMA5 not above EMA10"
        else:
            # Strict: EMA5 > EMA10 > EMA20 (5m)
            if not (row["ema_5"] > row["ema_10"] > row["ema_20"]):
                return False, "EMA alignment not bullish"

        # 2. RSI in range [50, 75]
        rsi_lo, rsi_hi = self.params["rsi_long_range"]
        if not (rsi_lo <= row["rsi_14"] <= rsi_hi):
            return False, f"RSI {row['rsi_14']:.1f} outside [{rsi_lo}, {rsi_hi}]"

        # 3. Volume > avg(last 5) * multiplier
        vol_lookback = self.params["volume_lookback"]
        multiplier = self.params["volume_multiplier"]
        if len(df_5m) >= vol_lookback:
            avg_vol = df_5m["volume"].iloc[-vol_lookback:].mean()
            if row["volume"] <= avg_vol * multiplier:
                return False, (
                    f"Volume {row['volume']:.0f} <= "
                    f"{avg_vol * multiplier:.0f} threshold"
                )
        else:
            return False, "Insufficient candles for volume check"

        # 4. Price > VWAP
        if self.params["vwap_enabled"]:
            if not (row["close"] > row["vwap"]):
                return False, "Price below VWAP"

        # 5. 15m higher-TF filter: EMA5 > EMA10
        htf = self.params["higher_tf"]
        if htf.get("enabled", True) and not df_15m.empty:
            row_15m = df_15m.iloc[-1]
            ema_f_col = f"ema_{htf['ema_fast']}"
            ema_s_col = f"ema_{htf['ema_slow']}"
            if ema_f_col in df_15m.columns and ema_s_col in df_15m.columns:
                if not (row_15m[ema_f_col] > row_15m[ema_s_col]):
                    return False, "15m EMA filter bearish"

        reasons = [
            f"EMA bullish ({row['ema_5']:.2f}>{row['ema_10']:.2f}>{row['ema_20']:.2f})",
            f"RSI={row['rsi_14']:.1f}",
            f"vol_ratio={row['volume'] / (avg_vol or 1):.2f}",
        ]
        return True, " | ".join(reasons)

    def _check_short_entry(
        self, df_5m: pd.DataFrame, df_15m: pd.DataFrame
    ) -> Tuple[bool, str]:
        """Evaluate all SHORT entry conditions (mirror of LONG).

        Returns:
            Tuple of (conditions_met, reason_string).
        """
        row = df_5m.iloc[-1]

        # 1. EMA alignment (mode-dependent)
        ema_mode = self.params.get("ema_alignment_mode", "strict")
        if ema_mode == "minimal":
            if not (row["ema_5"] < row["ema_20"]):
                return False, "EMA5 not below EMA20"
        elif ema_mode == "relaxed":
            if not (row["ema_5"] < row["ema_10"]):
                return False, "EMA5 not below EMA10"
        else:
            if not (row["ema_5"] < row["ema_10"] < row["ema_20"]):
                return False, "EMA alignment not bearish"

        # 2. RSI in range [25, 50]
        rsi_lo, rsi_hi = self.params["rsi_short_range"]
        if not (rsi_lo <= row["rsi_14"] <= rsi_hi):
            return False, f"RSI {row['rsi_14']:.1f} outside [{rsi_lo}, {rsi_hi}]"

        # 3. Volume > avg(last 5) * multiplier
        vol_lookback = self.params["volume_lookback"]
        multiplier = self.params["volume_multiplier"]
        if len(df_5m) >= vol_lookback:
            avg_vol = df_5m["volume"].iloc[-vol_lookback:].mean()
            if row["volume"] <= avg_vol * multiplier:
                return False, (
                    f"Volume {row['volume']:.0f} <= "
                    f"{avg_vol * multiplier:.0f} threshold"
                )
        else:
            return False, "Insufficient candles for volume check"

        # 4. Price < VWAP
        if self.params["vwap_enabled"]:
            if not (row["close"] < row["vwap"]):
                return False, "Price above VWAP"

        # 5. 15m higher-TF filter: EMA5 < EMA10
        htf = self.params["higher_tf"]
        if htf.get("enabled", True) and not df_15m.empty:
            row_15m = df_15m.iloc[-1]
            ema_f_col = f"ema_{htf['ema_fast']}"
            ema_s_col = f"ema_{htf['ema_slow']}"
            if ema_f_col in df_15m.columns and ema_s_col in df_15m.columns:
                if not (row_15m[ema_f_col] < row_15m[ema_s_col]):
                    return False, "15m EMA filter bullish"

        reasons = [
            f"EMA bearish ({row['ema_5']:.2f}<{row['ema_10']:.2f}<{row['ema_20']:.2f})",
            f"RSI={row['rsi_14']:.1f}",
            f"vol_ratio={row['volume'] / (avg_vol or 1):.2f}",
        ]
        return True, " | ".join(reasons)

    # ------------------------------------------------------------------
    # S-08: Exit logic (strategy-based close signals)
    # ------------------------------------------------------------------

    def _check_exit(
        self, df_5m: pd.DataFrame, side: str
    ) -> Tuple[bool, str]:
        """Check whether a strategy-based exit condition is met.

        These are *strategy* exits (EMA cross, RSI reversal, volume dry-up).
        SL/TP and time-limit exits are handled by the order manager.

        Args:
            df_5m: 5-minute indicator DataFrame (at least 2 rows).
            side: ``"LONG"`` or ``"SHORT"``.

        Returns:
            Tuple of (should_close, reason).
        """
        exit_cfg = self.params["exit"]["strategy_exit"]

        if len(df_5m) < 2:
            return False, ""

        curr = df_5m.iloc[-1]
        prev = df_5m.iloc[-2]

        if side == "LONG":
            # EMA5 crosses below EMA10
            if exit_cfg.get("ema_cross_exit", True):
                crossed = prev["ema_5"] >= prev["ema_10"] and curr["ema_5"] < curr["ema_10"]
                if crossed:
                    return True, "EMA5 crossed below EMA10"

            # RSI was > 75 and drops below 70
            if exit_cfg.get("rsi_reversal_exit", True):
                if prev["rsi_14"] > 75 and curr["rsi_14"] < 70:
                    return True, f"RSI reversal: {prev['rsi_14']:.1f} -> {curr['rsi_14']:.1f}"

            # Volume drops below threshold of 5-candle average
            if exit_cfg.get("volume_dry_exit", True):
                threshold = exit_cfg.get("volume_dry_threshold", 0.3)
                vol_lookback = self.params["volume_lookback"]
                if len(df_5m) >= vol_lookback:
                    avg_vol = df_5m["volume"].iloc[-vol_lookback:].mean()
                    if avg_vol > 0 and curr["volume"] < avg_vol * threshold:
                        return True, (
                            f"Volume dry-up: {curr['volume']:.0f} < "
                            f"{avg_vol * threshold:.0f}"
                        )
        else:
            # SHORT mirror
            if exit_cfg.get("ema_cross_exit", True):
                crossed = prev["ema_5"] <= prev["ema_10"] and curr["ema_5"] > curr["ema_10"]
                if crossed:
                    return True, "EMA5 crossed above EMA10"

            if exit_cfg.get("rsi_reversal_exit", True):
                if prev["rsi_14"] < 25 and curr["rsi_14"] > 30:
                    return True, f"RSI reversal: {prev['rsi_14']:.1f} -> {curr['rsi_14']:.1f}"

            if exit_cfg.get("volume_dry_exit", True):
                threshold = exit_cfg.get("volume_dry_threshold", 0.3)
                vol_lookback = self.params["volume_lookback"]
                if len(df_5m) >= vol_lookback:
                    avg_vol = df_5m["volume"].iloc[-vol_lookback:].mean()
                    if avg_vol > 0 and curr["volume"] < avg_vol * threshold:
                        return True, (
                            f"Volume dry-up: {curr['volume']:.0f} < "
                            f"{avg_vol * threshold:.0f}"
                        )

        return False, ""

    # ------------------------------------------------------------------
    # S-09: SL / TP calculation
    # ------------------------------------------------------------------

    def calculate_sl_tp(
        self,
        signal: SignalType,
        entry_price: float,
        atr: float,
        config: dict | None = None,
    ) -> Tuple[float, float]:
        """Calculate stop-loss and take-profit prices.

        The stop-loss distance is derived from ATR, clamped between
        ``min_pct`` and ``max_pct`` of the entry price.  Take-profit is
        placed at ``risk_reward_ratio`` multiples of the SL distance.

        Args:
            signal: ``LONG`` or ``SHORT``.
            entry_price: Position entry price.
            atr: Current ATR(14) value.
            config: Override for SL/TP config; defaults to ``self.params``.

        Returns:
            Tuple of (stop_loss_price, take_profit_price).
        """
        if config is None:
            sl_cfg = self.params["exit"]["stop_loss"]
            tp_cfg = self.params["exit"]["take_profit"]
        else:
            sl_cfg = config.get("stop_loss", self.params["exit"]["stop_loss"])
            tp_cfg = config.get("take_profit", self.params["exit"]["take_profit"])

        atr_mult = sl_cfg.get("atr_multiplier", 1.5)
        min_pct = sl_cfg.get("min_pct", 0.3) / 100.0
        max_pct = sl_cfg.get("max_pct", 2.0) / 100.0
        rr_ratio = tp_cfg.get("risk_reward_ratio", 2.0)

        # ATR-based SL distance, clamped by percentage bounds
        sl_distance = atr * atr_mult
        sl_pct = sl_distance / entry_price if entry_price > 0 else min_pct
        sl_pct = max(min_pct, min(max_pct, sl_pct))
        sl_distance = entry_price * sl_pct

        tp_distance = sl_distance * rr_ratio

        if signal == SignalType.LONG:
            sl = entry_price - sl_distance
            tp = entry_price + tp_distance
        else:
            sl = entry_price + sl_distance
            tp = entry_price - tp_distance

        return sl, tp

    # ------------------------------------------------------------------
    # Main evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        symbol: str,
        indicators_5m: pd.DataFrame,
        indicators_15m: pd.DataFrame,
        current_position: dict | None,
        scan_result: dict | None,
    ) -> PositionSignal:
        """Evaluate market conditions and produce a signal.

        When flat (no position): check entry conditions and return
        ``LONG``, ``SHORT``, or ``HOLD``.

        When in a position: check strategy-based exit conditions and
        return ``CLOSE`` or ``HOLD``.  SL/TP enforcement and time-limit
        closes are left to the order manager.

        Args:
            symbol: Trading pair.
            indicators_5m: 5m OHLCV DataFrame with indicators.
            indicators_15m: 15m OHLCV DataFrame with indicators.
            current_position: Open position dict or ``None``.
            scan_result: Scanner result dict or ``None``.

        Returns:
            :class:`PositionSignal` with the recommended action.
        """
        hold = PositionSignal(
            symbol=symbol,
            signal=SignalType.HOLD,
            strategy="momentum_scalper",
            timeframe="5m",
        )

        # Guard: need enough data
        required_cols = {"ema_5", "ema_10", "ema_20", "rsi_14", "vwap", "atr_14", "adx_14"}
        if indicators_5m.empty or not required_cols.issubset(indicators_5m.columns):
            logger.debug("{}: insufficient 5m indicator data, holding", symbol)
            return hold

        last = indicators_5m.iloc[-1]
        if any(pd.isna(last[c]) for c in required_cols):
            logger.debug("{}: NaN in required indicators, holding", symbol)
            return hold

        # ADX trend strength filter — reject entries in ranging markets
        adx_threshold = self.params.get("adx_threshold", 20)
        if last["adx_14"] < adx_threshold:
            hold.reason = f"ADX {last['adx_14']:.1f} < {adx_threshold} (no trend)"
            return hold

        # ----- In position: check exit -----
        if current_position is not None:
            side = current_position.get("side", "LONG")
            should_close, reason = self._check_exit(indicators_5m, side)
            if should_close:
                logger.info("{}: strategy exit triggered – {}", symbol, reason)
                return PositionSignal(
                    symbol=symbol,
                    signal=SignalType.CLOSE,
                    strategy="momentum_scalper",
                    timeframe="5m",
                    reason=reason,
                )
            return hold

        # ----- Flat: check entry -----
        suggested_side = ""
        if scan_result is not None:
            suggested_side = scan_result.get("suggested_side", "NEUTRAL")

        follow = self.params.get("follow_scanner_direction", True)

        # Determine which directions to evaluate
        check_long = True
        check_short = True
        if follow and suggested_side:
            if suggested_side == "LONG":
                check_short = False
            elif suggested_side == "SHORT":
                check_long = False
            # NEUTRAL: evaluate both

        entry_price = float(last["close"])
        atr = float(last["atr_14"])

        # Try LONG
        long_reason = ""
        short_reason = ""
        if check_long:
            ok, reason = self._check_long_entry(indicators_5m, indicators_15m)
            long_reason = reason
            if ok:
                sl, tp = self.calculate_sl_tp(SignalType.LONG, entry_price, atr)
                sl_dist = entry_price - sl
                confidence = self._compute_confidence(indicators_5m, "LONG")
                logger.info(
                    "{}: LONG signal | entry={:.4f} sl={:.4f} tp={:.4f} | {}",
                    symbol, entry_price, sl, tp, reason,
                )
                return PositionSignal(
                    symbol=symbol,
                    signal=SignalType.LONG,
                    entry_price=entry_price,
                    stop_loss=sl,
                    take_profit=tp,
                    confidence=confidence,
                    strategy="momentum_scalper",
                    timeframe="5m",
                    suggested_side=suggested_side,
                    reason=reason,
                )

        # Try SHORT
        if check_short:
            ok, reason = self._check_short_entry(indicators_5m, indicators_15m)
            short_reason = reason
            if ok:
                sl, tp = self.calculate_sl_tp(SignalType.SHORT, entry_price, atr)
                confidence = self._compute_confidence(indicators_5m, "SHORT")
                logger.info(
                    "{}: SHORT signal | entry={:.4f} sl={:.4f} tp={:.4f} | {}",
                    symbol, entry_price, sl, tp, reason,
                )
                return PositionSignal(
                    symbol=symbol,
                    signal=SignalType.SHORT,
                    entry_price=entry_price,
                    stop_loss=sl,
                    take_profit=tp,
                    confidence=confidence,
                    strategy="momentum_scalper",
                    timeframe="5m",
                    suggested_side=suggested_side,
                    reason=reason,
                )

        # Log rejection reasons for debugging
        reject_parts = []
        if long_reason:
            reject_parts.append(f"L:{long_reason}")
        if short_reason:
            reject_parts.append(f"S:{short_reason}")
        hold.reason = " | ".join(reject_parts) if reject_parts else ""

        return hold

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_confidence(self, df_5m: pd.DataFrame, side: str) -> float:
        """Compute a confidence score in ``[0, 1]``.

        Factors:
        - Volume ratio (higher volume = more confident)
        - RSI distance from neutral (stronger momentum = more confident)
        - ADX trend strength (stronger trend = more confident)
        - Candle body ratio (strong directional candle = more confident)

        Args:
            df_5m: Indicator DataFrame.
            side: ``"LONG"`` or ``"SHORT"``.

        Returns:
            Confidence float between 0.0 and 1.0.
        """
        row = df_5m.iloc[-1]
        score = 0.4  # base (lowered to make room for ADX/candle factors)

        # Volume component (max +0.15)
        vol_lookback = self.params["volume_lookback"]
        if len(df_5m) >= vol_lookback:
            avg_vol = df_5m["volume"].iloc[-vol_lookback:].mean()
            if avg_vol > 0:
                vol_ratio = row["volume"] / avg_vol
                score += min(0.15, (vol_ratio - 1.0) * 0.08)

        # RSI component (max +0.15)
        rsi = row["rsi_14"]
        if side == "LONG":
            rsi_strength = (rsi - 50) / 25.0
        else:
            rsi_strength = (50 - rsi) / 25.0
        score += min(0.15, max(0.0, rsi_strength * 0.15))

        # ADX trend strength component (max +0.20)
        adx = row.get("adx_14", 20)
        if not pd.isna(adx):
            adx_contrib = min(0.20, max(0.0, (adx - 20) / 40.0 * 0.20))
            score += adx_contrib

        # Candle body ratio component (max +0.10)
        candle_range = row["high"] - row["low"]
        if candle_range > 0:
            body = abs(row["close"] - row["open"])
            body_ratio = body / candle_range
            is_directional = (
                (side == "LONG" and row["close"] > row["open"]) or
                (side == "SHORT" and row["close"] < row["open"])
            )
            if is_directional:
                score += min(0.10, body_ratio * 0.10)

        return round(max(0.0, min(1.0, score)), 3)

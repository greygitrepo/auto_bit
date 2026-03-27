"""Base classes for position strategies.

Defines the abstract interface that all position strategies must implement,
along with shared data structures for signals, trailing stops, and time limits.

Task S-06.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd


# ---------------------------------------------------------------------------
# Signal types and data
# ---------------------------------------------------------------------------


class SignalType(Enum):
    """Direction or action for a position signal."""

    LONG = "LONG"
    SHORT = "SHORT"
    CLOSE = "CLOSE"
    HOLD = "HOLD"


@dataclass
class PositionSignal:
    """Output produced by a position strategy's ``evaluate`` method.

    Attributes:
        symbol: Trading pair (e.g. ``"BTCUSDT"``).
        signal: The recommended action.
        entry_price: Intended entry price (mid-market at signal time).
        stop_loss: Calculated stop-loss price.
        take_profit: Calculated take-profit price.
        confidence: Strategy confidence score in ``[0, 1]``.
        strategy: Name of the strategy that produced the signal.
        timeframe: Primary timeframe used for evaluation.
        suggested_side: Scanner-suggested direction (``"LONG"`` / ``"SHORT"`` / ``"NEUTRAL"``).
        reason: Human-readable explanation of why the signal was produced.
    """

    symbol: str
    signal: SignalType
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    confidence: float = 0.0
    strategy: str = ""
    timeframe: str = ""
    suggested_side: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# Trailing stop state
# ---------------------------------------------------------------------------


@dataclass
class TrailingStopState:
    """Mutable state for a trailing stop attached to an open position.

    Attributes:
        active: Whether the trailing stop has been activated.
        activation_price: Price at which trailing became active.
        highest_price: Highest observed price since activation (LONG).
        lowest_price: Lowest observed price since activation (SHORT).
        trailing_sl: Current trailing stop-loss level.
        callback_distance: Distance the price may pull back before the stop triggers.
    """

    active: bool = False
    activation_price: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    trailing_sl: float = 0.0
    callback_distance: float = 0.0
    entry_price: float = 0.0


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class BasePositionStrategy(ABC):
    """Interface that every position strategy must implement."""

    @abstractmethod
    def evaluate(
        self,
        symbol: str,
        indicators_5m: pd.DataFrame,
        indicators_15m: pd.DataFrame,
        current_position: dict | None,
        scan_result: dict | None,
    ) -> PositionSignal:
        """Evaluate market data and return a trading signal.

        Args:
            symbol: Trading pair.
            indicators_5m: 5-minute OHLCV DataFrame with indicator columns
                appended by :class:`IndicatorEngine.calculate_all`.
            indicators_15m: 15-minute OHLCV DataFrame with indicator columns.
            current_position: Dict describing the current open position for
                *symbol*, or ``None`` when flat.  Expected keys when present:
                ``side``, ``entry_price``, ``entry_time``, ``sl``, ``tp``.
            scan_result: Scanner output dict for *symbol*, or ``None``.
                Expected keys: ``suggested_side``, ``score``, ``direction``.

        Returns:
            A :class:`PositionSignal` indicating the recommended action.
        """

    @abstractmethod
    def get_default_params(self) -> dict:
        """Return the default parameter dictionary for this strategy."""

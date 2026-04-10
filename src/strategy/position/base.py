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
from typing import Any, Dict, List, Optional

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


# ---------------------------------------------------------------------------
# Grid trading data structures
# ---------------------------------------------------------------------------


class GridAction(Enum):
    """Actions emitted by the grid engine."""

    FILL = "FILL"            # Price crossed a pending level → open position
    TP_HIT = "TP_HIT"        # TP reached on a filled level → close position
    RECENTER = "RECENTER"    # Grid recentered → cancel/close out-of-range levels
    CLOSE_ALL = "CLOSE_ALL"  # Emergency close all grid positions


class BiasDirection(Enum):
    """Directional bias for the grid."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class GridLevelStatus(Enum):
    """State machine status of an individual grid level."""

    PENDING = "PENDING"      # Waiting for price to reach this level
    FILLED = "FILLED"        # Price crossed level, awaiting P3 confirmation
    TP_SET = "TP_SET"        # Position open, TP price set, awaiting TP hit
    COMPLETED = "COMPLETED"  # TP hit, position closed — ready for recycle
    CANCELLED = "CANCELLED"  # Removed by recenter or close_all


@dataclass
class GridLevel:
    """State of a single grid level."""

    id: int = 0
    grid_state_id: int = 0
    level_index: int = 0        # Signed: negative = below center, positive = above
    price: float = 0.0
    side: str = ""              # "Buy" or "Sell"
    status: GridLevelStatus = GridLevelStatus.PENDING
    tp_price: float = 0.0
    sl_price: float = 0.0
    fill_price: float = 0.0
    fill_time: int = 0
    tp_fill_price: float = 0.0
    tp_fill_time: int = 0
    pnl: float = 0.0
    fee: float = 0.0
    position_id: int = 0
    created_at: int = 0
    updated_at: int = 0


@dataclass
class GridState:
    """Overall grid configuration for a symbol."""

    id: int = 0
    mode: str = "paper"
    symbol: str = ""
    status: str = "active"      # active | paused | stopped
    center_price: float = 0.0
    grid_range: float = 0.0
    grid_spacing: float = 0.0
    num_buy_levels: int = 5
    num_sell_levels: int = 5
    bias: str = "NEUTRAL"
    bias_magnitude: float = 0.0
    leverage: int = 5
    qty_per_level: float = 0.0
    total_margin: float = 0.0
    realized_pnl: float = 0.0
    created_at: int = 0
    updated_at: int = 0
    levels: List[GridLevel] = field(default_factory=list)


@dataclass
class GridSignal:
    """A grid action to be sent from P2 to P3."""

    symbol: str
    action: GridAction
    level_id: int = 0
    level_index: int = 0
    level_price: float = 0.0
    side: str = ""              # "Buy" or "Sell"
    tp_price: float = 0.0
    sl_price: float = 0.0
    grid_state_id: int = 0
    reason: str = ""

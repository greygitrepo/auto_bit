"""
Base classes for asset-level trading strategy.

Defines the abstract interface that every asset strategy must implement,
along with shared data structures for order requests and daily statistics.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class OrderRequest:
    """Outcome of strategy evaluation -- either an approved order or a rejection."""

    approved: bool
    symbol: str = ""
    side: str = ""  # "Buy" | "Sell"
    size: float = 0.0  # position size in USDT
    qty: float = 0.0  # quantity in base currency
    leverage: int = 1
    order_type: str = "Market"
    stop_loss: float = 0.0
    take_profit: float = 0.0
    risk_amount: float = 0.0
    reject_reason: str = ""


@dataclass
class DailyStats:
    """Aggregated statistics for the current trading day (resets at UTC midnight)."""

    date: str = ""
    pnl: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    consecutive_losses: int = 0
    cooldown_until: float | None = None  # unix timestamp


class BaseAssetStrategy(ABC):
    """Abstract base for asset-level strategies.

    Each implementation decides whether a signal should be traded and, if so,
    how large the position should be, what leverage to use, and where to place
    stop-loss / take-profit orders.
    """

    @abstractmethod
    def evaluate(
        self,
        signal,
        initial_balance: float,
        current_balance: float,
        open_positions: list,
        daily_stats: DailyStats,
    ) -> OrderRequest:
        """Evaluate a :class:`SignalMessage` and return an :class:`OrderRequest`.

        Parameters
        ----------
        signal:
            A ``SignalMessage`` from the signal pipeline.
        initial_balance:
            Account balance at the start of the session / day.
        current_balance:
            Real-time account balance.
        open_positions:
            List of currently open position dicts.
        daily_stats:
            Aggregated stats for the current UTC day.

        Returns
        -------
        OrderRequest
            Approved order with sizing details, or a rejection with reason.
        """

    @abstractmethod
    def get_default_params(self) -> dict:
        """Return the default configuration parameters for this strategy."""

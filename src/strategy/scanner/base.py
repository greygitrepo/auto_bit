"""Base classes and data structures for scanner strategies.

Defines the abstract interface that all scanner strategies must implement,
along with the ScanResult dataclass used to communicate scan outcomes to
downstream strategy and execution components.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ScanResult:
    """Outcome of scoring a single symbol during a market scan.

    Attributes:
        symbol: Trading pair symbol (e.g. ``"XYZUSDT"``).
        score: Composite score from 0 (worst) to 100 (best).
        market_direction: Overall market regime -- ``"bull"``, ``"bear"``,
            or ``"mixed"``.
        suggested_side: Recommended trade direction based on market
            environment -- ``"LONG"``, ``"SHORT"``, or ``"NEUTRAL"``.
        scores_detail: Per-category breakdown for debugging, e.g.
            ``{"volume": 80.0, "volatility": 65.0, ...}``.
        reason: Human-readable summary explaining why this symbol scored
            the way it did.
        metadata: Arbitrary extra data (ticker snapshot, indicator values,
            etc.) that downstream consumers may find useful.
    """

    symbol: str
    score: float
    market_direction: str
    suggested_side: str
    scores_detail: dict
    reason: str
    metadata: dict = field(default_factory=dict)


class BaseScannerStrategy(ABC):
    """Abstract base class for all scanner strategies.

    A scanner strategy is responsible for:

    1. Selecting an initial pool of candidate symbols.
    2. Scoring each candidate across multiple dimensions.
    3. Applying entry filters (open positions, cooldowns, etc.).
    4. Returning a ranked list of :class:`ScanResult` objects.
    """

    @abstractmethod
    def scan(
        self,
        market_data: dict,
        btc_trend: str,
        eth_trend: str,
        open_positions: list,
        recent_sl_symbols: dict,
    ) -> list[ScanResult]:
        """Run a full scan cycle and return ranked results.

        Args:
            market_data: Per-symbol data bundle keyed by symbol string.
                Each value is a dict with ``"tickers"`` (raw ticker dict)
                and ``"indicators"`` (a :class:`pd.DataFrame` or ``None``).
            btc_trend: Current BTC trend from
                :meth:`IndicatorEngine.get_ema_alignment` -- one of
                ``"bullish"``, ``"bearish"``, ``"neutral"``.
            eth_trend: Current ETH trend (same vocabulary as *btc_trend*).
            open_positions: List of symbol strings currently held.
            recent_sl_symbols: Mapping of ``{symbol: sl_timestamp}`` for
                symbols that recently hit stop-loss, used for cooldown
                enforcement.

        Returns:
            List of :class:`ScanResult` sorted by score descending.
        """

    @abstractmethod
    def get_default_params(self) -> dict:
        """Return default configuration parameters for this strategy.

        Useful for documentation, testing, and as a fallback when no
        external config is provided.

        Returns:
            Dictionary of parameter names to default values.
        """

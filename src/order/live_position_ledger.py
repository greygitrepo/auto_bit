"""Live position ledger for net-position exchange translation.

Tracks individual micro-positions (one per grid level) internally, while the
exchange (Bybit) uses a NET POSITION model (one position per symbol per side).

This ledger bridges the gap: when paper mode tracks 5 independent Buy
micro-positions, this ledger knows they map to a single net Buy position
on the exchange, and calculates partial close quantities accordingly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from loguru import logger

# Type alias matching grid_manager.py
LevelKey = Tuple[str, int]  # (symbol, level_index)


@dataclass
class MicroPosition:
    """A single grid-level micro-position tracked internally."""

    level_key: LevelKey  # (symbol, level_index)
    symbol: str
    side: str  # "Buy" | "Sell"
    qty: float
    entry_price: float
    leverage: int
    margin: float
    opened_at: float = field(default_factory=time.time)


class LivePositionLedger:
    """Internal ledger that tracks micro-positions and translates to net-position ops.

    Key responsibilities:
    - Track individual grid level positions internally (entry_price, qty, side per level)
    - Calculate what the NET position should be on exchange
    - Calculate PARTIAL close qty for a single micro-position
    - Reconcile internal ledger with exchange actual position after each operation
    """

    def __init__(self) -> None:
        self._positions: Dict[LevelKey, MicroPosition] = {}

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def add_position(
        self,
        level_key: LevelKey,
        symbol: str,
        side: str,
        qty: float,
        entry_price: float,
        leverage: int,
        margin: float,
    ) -> None:
        """Add or replace a micro-position for a grid level.

        Parameters
        ----------
        level_key:
            Composite key ``(symbol, level_index)``.
        symbol:
            Trading pair.
        side:
            ``"Buy"`` or ``"Sell"``.
        qty:
            Position quantity in base currency.
        entry_price:
            Fill price for this micro-position.
        leverage:
            Leverage multiplier.
        margin:
            Isolated margin amount.
        """
        pos = MicroPosition(
            level_key=level_key,
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=entry_price,
            leverage=leverage,
            margin=margin,
        )
        self._positions[level_key] = pos
        logger.debug(
            "Ledger ADD: {} {} {:.6f} @ {:.4f} key={}",
            side, symbol, qty, entry_price, level_key,
        )

    def remove_position(self, level_key: LevelKey) -> Optional[MicroPosition]:
        """Remove and return a micro-position by level key.

        Returns ``None`` if the key does not exist.
        """
        pos = self._positions.pop(level_key, None)
        if pos is not None:
            logger.debug("Ledger REMOVE: {} key={}", pos.symbol, level_key)
        return pos

    def get_position(self, level_key: LevelKey) -> Optional[MicroPosition]:
        """Get a micro-position by level key without removing it."""
        return self._positions.get(level_key)

    # ------------------------------------------------------------------
    # Net position calculation
    # ------------------------------------------------------------------

    def get_net_position(self, symbol: str) -> dict:
        """Calculate the net position for a symbol across all micro-positions.

        Returns
        -------
        Dict with keys:
            - ``side``: ``"Buy"``, ``"Sell"``, or ``""`` (flat)
            - ``total_qty``: absolute net quantity
            - ``avg_entry``: weighted average entry price for the dominant side
        """
        buy_qty = 0.0
        buy_notional = 0.0
        sell_qty = 0.0
        sell_notional = 0.0

        for pos in self._positions.values():
            if pos.symbol != symbol:
                continue
            if pos.side == "Buy":
                buy_qty += pos.qty
                buy_notional += pos.qty * pos.entry_price
            elif pos.side == "Sell":
                sell_qty += pos.qty
                sell_notional += pos.qty * pos.entry_price

        net_qty = buy_qty - sell_qty

        if abs(net_qty) < 1e-12:
            return {"side": "", "total_qty": 0.0, "avg_entry": 0.0}

        if net_qty > 0:
            side = "Buy"
            total_qty = net_qty
            avg_entry = buy_notional / buy_qty if buy_qty > 0 else 0.0
        else:
            side = "Sell"
            total_qty = abs(net_qty)
            avg_entry = sell_notional / sell_qty if sell_qty > 0 else 0.0

        return {"side": side, "total_qty": total_qty, "avg_entry": avg_entry}

    # ------------------------------------------------------------------
    # Partial close
    # ------------------------------------------------------------------

    def get_partial_close_qty(self, level_key: LevelKey) -> float:
        """Return the qty for a specific micro-position (for partial close).

        This is the qty that should be sent to the exchange to close
        just this one grid level, NOT the entire net position.

        Returns 0.0 if the key does not exist.
        """
        pos = self._positions.get(level_key)
        if pos is None:
            return 0.0
        return pos.qty

    # ------------------------------------------------------------------
    # Symbol queries
    # ------------------------------------------------------------------

    def get_positions_by_symbol(self, symbol: str) -> List[MicroPosition]:
        """Return all micro-positions for a given symbol."""
        return [p for p in self._positions.values() if p.symbol == symbol]

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def reconcile(
        self,
        symbol: str,
        exchange_qty: float,
        exchange_avg_entry: float,
    ) -> None:
        """Reconcile internal ledger with exchange's actual net position.

        If the exchange shows a different total qty than our ledger
        (due to rounding, partial fills, etc.), adjust each micro-position's
        qty proportionally to match the exchange total.

        If the exchange shows zero qty, clear all micro-positions for this symbol.

        Parameters
        ----------
        symbol:
            Trading pair.
        exchange_qty:
            The exchange's reported net position size.
        exchange_avg_entry:
            The exchange's reported average entry price.
        """
        positions = self.get_positions_by_symbol(symbol)
        if not positions:
            return

        # If exchange has zero qty, the position was fully closed externally
        if exchange_qty < 1e-12:
            logger.warning(
                "Ledger RECONCILE: exchange shows 0 qty for {} — clearing {} micro-positions",
                symbol, len(positions),
            )
            for pos in positions:
                self._positions.pop(pos.level_key, None)
            return

        # Calculate our internal total (same-side only for simplicity)
        net = self.get_net_position(symbol)
        internal_qty = net["total_qty"]

        if internal_qty < 1e-12:
            logger.warning(
                "Ledger RECONCILE: internal qty is 0 but exchange shows {:.6f} for {}",
                exchange_qty, symbol,
            )
            return

        # Proportional adjustment if there's a discrepancy
        ratio = exchange_qty / internal_qty

        if abs(ratio - 1.0) < 1e-6:
            # Close enough, no adjustment needed
            return

        logger.info(
            "Ledger RECONCILE {}: internal={:.6f} exchange={:.6f} ratio={:.6f}",
            symbol, internal_qty, exchange_qty, ratio,
        )

        # Adjust each micro-position proportionally
        dominant_side = net["side"]
        for pos in positions:
            if pos.side == dominant_side:
                pos.qty *= ratio

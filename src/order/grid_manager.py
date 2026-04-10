"""Grid Position Manager for P3 (Order Manager Process).

Handles grid signal processing: opening micro-positions on grid fills,
closing them on TP hits, and managing recenter/close_all operations.

Uses (symbol, level_index) as the unique key for level-position mapping
instead of level_id (which may be 0 before DB persist).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from src.order.live_position_ledger import LivePositionLedger
from src.strategy.asset.base import DailyStats
from src.strategy.asset.grid_sizing import GridSizingStrategy
from src.tracker.position_tracker import PositionTracker
from src.utils.messages import GridSignalMessage, GridUpdateMessage


# Type alias for the composite key
LevelKey = Tuple[str, int]  # (symbol, level_index)


class GridPositionManager:
    """Manages grid micro-positions in P3.

    Uses (symbol, level_index) as the stable unique key for tracking,
    since level_id (DB auto-increment) may be 0 before first DB persist.
    """

    def __init__(
        self,
        executor: Any,
        position_tracker: PositionTracker,
        sizing: GridSizingStrategy,
        mode: str = "paper",
        initial_balance: float = 20.0,
    ) -> None:
        self._executor = executor
        self._tracker = position_tracker
        self._sizing = sizing
        self._mode = mode
        self._initial_balance = initial_balance

        self._order_delay = 0.15 if mode == "live" else 0.0

        # All mappings use LevelKey = (symbol, level_index)
        self._level_positions: Dict[LevelKey, int] = {}    # → position_id in DB
        self._level_order_ids: Dict[LevelKey, str] = {}    # → orderId in paper executor
        self._level_entry_fees: Dict[LevelKey, float] = {} # → entry fee

        # Live mode: internal ledger to track micro-positions for net-position translation
        self._ledger: Optional[LivePositionLedger] = (
            LivePositionLedger() if mode == "live" else None
        )

    def _key(self, msg: GridSignalMessage) -> LevelKey:
        """Create composite key from signal message."""
        return (msg.symbol, msg.level_index)

    def restore_from_positions(self, open_positions: list) -> int:
        """Restore _level_positions from DB open positions after restart.

        Matches open positions with strategy='grid_bias' back to their
        (symbol, level_index) keys so that TP_HIT signals can find them.

        For grid positions that don't have level_index info, we assign
        synthetic negative indices to ensure they can still be closed.

        Returns number of restored mappings.
        """
        restored = 0
        # Group by symbol for synthetic index assignment
        symbol_counters: Dict[str, int] = {}

        for pos in open_positions:
            strategy = pos.get("strategy", "")
            if strategy not in ("grid_bias", "recovered"):
                continue

            symbol = pos.get("symbol", "")
            position_id = pos.get("id", 0)
            if not symbol or not position_id:
                continue

            # Try to get level_index from position metadata
            # If not available, assign synthetic index
            if symbol not in symbol_counters:
                symbol_counters[symbol] = -100  # Start from -100 to avoid clash
            idx = symbol_counters[symbol]
            symbol_counters[symbol] -= 1

            key = (symbol, idx)
            self._level_positions[key] = position_id

            # Restore ledger entry for live mode
            if self._ledger is not None:
                side = pos.get("side", "Buy")
                qty = float(pos.get("size", 0))
                entry_price = float(pos.get("entry_price", 0))
                leverage = int(pos.get("leverage", 1))
                margin = float(pos.get("margin", 0))
                self._ledger.add_position(
                    level_key=key,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    entry_price=entry_price,
                    leverage=leverage,
                    margin=margin,
                )

            restored += 1
            logger.info(
                "GridManager restored: {} pos_id={} key={}",
                symbol, position_id, key,
            )

        return restored

    async def handle_grid_signal(
        self,
        msg: GridSignalMessage,
        current_balance: float,
        open_positions: List[Dict[str, Any]],
        daily_stats: DailyStats,
    ) -> Optional[GridUpdateMessage]:
        action = msg.action
        if action == "FILL":
            return await self._handle_fill(msg, current_balance, open_positions, daily_stats)
        elif action == "TP_HIT":
            return await self._handle_tp_hit(msg)
        elif action == "RECENTER":
            return await self._handle_close(msg, "grid_recenter")
        elif action == "CLOSE_ALL":
            return await self._handle_close(msg, "grid_close_all")
        else:
            logger.warning("Unknown grid action: {}", action)
            return None

    async def _handle_fill(
        self,
        msg: GridSignalMessage,
        current_balance: float,
        open_positions: List[Dict[str, Any]],
        daily_stats: DailyStats,
    ) -> Optional[GridUpdateMessage]:
        symbol = msg.symbol
        key = self._key(msg)

        # Skip if this level already has an open position
        if key in self._level_positions:
            logger.debug("Grid fill skipped {} idx={}: already open", symbol, msg.level_index)
            return None

        side = msg.side
        level_price = msg.level_price
        qty = msg.qty_per_level
        leverage = msg.leverage

        order_req = self._sizing.evaluate_grid_fill(
            symbol=symbol, side=side, level_price=level_price,
            qty_per_level=qty, leverage=leverage,
            initial_balance=self._initial_balance,
            current_balance=current_balance,
            open_positions=open_positions,
            daily_stats=daily_stats,
        )

        if not order_req.approved:
            logger.info("Grid fill REJECTED {}: {}", symbol, order_req.reject_reason)
            return GridUpdateMessage(
                symbol=symbol, level_id=msg.level_id, action="FAILED",
                reason=order_req.reject_reason,
            )

        try:
            await self._executor.set_margin_and_leverage(symbol, leverage)
        except Exception as exc:
            logger.error("Failed to set leverage for {}: {}", symbol, exc)

        if self._order_delay > 0:
            await asyncio.sleep(self._order_delay)

        try:
            result = await self._executor.place_market_order(
                symbol=symbol, side=side, qty=order_req.qty,
                current_price=level_price,
            )
        except Exception as exc:
            logger.error("Grid fill order failed {}: {}", symbol, exc)
            return GridUpdateMessage(
                symbol=symbol, level_id=msg.level_id, action="FAILED",
                reason=str(exc),
            )

        # Check if order was rejected (min notional, insufficient balance, etc.)
        if result.get("rejected"):
            logger.info("Grid fill REJECTED {}: {}", symbol, result.get("reason", "unknown"))
            return GridUpdateMessage(
                symbol=symbol, level_id=msg.level_id, action="FAILED",
                reason=result.get("reason", "order_rejected"),
            )

        fill_price = result.get("fillPrice", level_price)
        fee = result.get("fee", 0.0)
        order_id = result.get("orderId", "")

        if order_id:
            self._level_order_ids[key] = order_id
        self._level_entry_fees[key] = fee

        # Live mode: record micro-position in ledger for net-position tracking
        if self._ledger is not None:
            self._ledger.add_position(
                level_key=key,
                symbol=symbol,
                side=side,
                qty=order_req.qty,
                entry_price=fill_price,
                leverage=leverage,
                margin=order_req.risk_amount,
            )

        position_id = self._tracker.add_position({
            "mode": self._mode,
            "symbol": symbol,
            "side": side,
            "size": order_req.qty,
            "entry_price": fill_price,
            "leverage": leverage,
            "stop_loss": getattr(msg, 'sl_price', 0.0),
            "take_profit": msg.tp_price,
            "margin": order_req.risk_amount,
            "unrealized_pnl": 0.0,
            "strategy": "grid_bias",
            "scanner_direction": "",
            "entered_at": int(time.time()),
            "max_hold_minutes": 1440,
        })

        self._level_positions[key] = position_id

        logger.info(
            "Grid FILL executed: {} {} idx={} price={:.6f} qty={:.6f} pos_id={}",
            symbol, side, msg.level_index, fill_price, order_req.qty, position_id,
        )

        return GridUpdateMessage(
            symbol=symbol, level_id=msg.level_id, action="CONFIRMED",
            position_id=position_id, fill_price=fill_price, fee=fee,
        )

    async def _handle_tp_hit(self, msg: GridSignalMessage) -> Optional[GridUpdateMessage]:
        symbol = msg.symbol
        key = self._key(msg)
        position_id = self._level_positions.get(key)

        if position_id is None:
            logger.warning(
                "Grid TP_HIT: no position for {} idx={}", symbol, msg.level_index,
            )
            return None

        positions = self._tracker.get_open_positions()
        position = None
        for p in positions:
            if p.get("id") == position_id:
                position = p
                break

        if position is None:
            logger.warning("Grid TP_HIT: position {} not found in tracker", position_id)
            self._level_positions.pop(key, None)
            return None

        tp_price = msg.tp_price
        try:
            if self._ledger is not None and hasattr(self._executor, 'close_partial'):
                # Live mode: partial close using ledger qty
                close_qty = self._ledger.get_partial_close_qty(key)
                pos_side = position["side"]
                if close_qty > 0:
                    result = await self._executor.close_partial(
                        symbol=symbol, side=pos_side,
                        qty=close_qty, current_price=tp_price,
                    )
                else:
                    logger.warning("Grid TP: ledger has no qty for key={}", key)
                    close_side = "Sell" if pos_side == "Buy" else "Buy"
                    result = await self._executor.close_position(
                        symbol=symbol, side=close_side,
                        qty=float(position["size"]), current_price=tp_price,
                    )
            else:
                # Paper mode: close by key or full position
                order_key = self._level_order_ids.get(key, "")
                if order_key and hasattr(self._executor, 'close_position_by_key'):
                    result = await self._executor.close_position_by_key(order_key, tp_price)
                else:
                    close_side = "Sell" if position["side"] == "Buy" else "Buy"
                    result = await self._executor.close_position(
                        symbol=symbol, side=close_side,
                        qty=float(position["size"]), current_price=tp_price,
                    )
        except Exception as exc:
            logger.error("Grid TP close failed {}: {}", symbol, exc)
            return None

        pnl = result.get("pnl", 0.0)
        exit_fee = result.get("fee", 0.0)
        fill_price = result.get("fillPrice", tp_price)
        entry_fee = self._level_entry_fees.get(key, 0.0)
        total_fee = entry_fee + exit_fee

        self._tracker.close_position(
            position_id=position_id,
            exit_price=fill_price,
            exit_reason="grid_tp",
            exit_type="take_profit",
            fee=total_fee,
        )

        # Live mode: remove from ledger
        if self._ledger is not None:
            self._ledger.remove_position(key)

        self._level_positions.pop(key, None)
        self._level_order_ids.pop(key, None)
        self._level_entry_fees.pop(key, None)

        logger.info(
            "Grid TP executed: {} idx={} pnl={:.6f} fee={:.6f}",
            symbol, msg.level_index, pnl, total_fee,
        )

        return GridUpdateMessage(
            symbol=symbol, level_id=msg.level_id, action="CLOSED",
            position_id=position_id, pnl=pnl, fee=total_fee, reason="grid_tp",
        )

    async def _handle_close(
        self, msg: GridSignalMessage, reason: str,
    ) -> Optional[GridUpdateMessage]:
        symbol = msg.symbol
        key = self._key(msg)
        position_id = self._level_positions.get(key)

        if position_id is None:
            return None

        positions = self._tracker.get_open_positions()
        position = None
        for p in positions:
            if p.get("id") == position_id:
                position = p
                break

        if position is None:
            self._level_positions.pop(key, None)
            return None

        current_price = msg.level_price

        try:
            if self._ledger is not None and hasattr(self._executor, 'close_partial'):
                # Live mode: partial close using ledger qty
                close_qty = self._ledger.get_partial_close_qty(key)
                pos_side = position["side"]
                if close_qty > 0:
                    result = await self._executor.close_partial(
                        symbol=symbol, side=pos_side,
                        qty=close_qty, current_price=current_price,
                    )
                else:
                    logger.warning("Grid close: ledger has no qty for key={}", key)
                    close_side = "Sell" if pos_side == "Buy" else "Buy"
                    result = await self._executor.close_position(
                        symbol=symbol, side=close_side,
                        qty=float(position["size"]), current_price=current_price,
                    )
            else:
                # Paper mode: close by key or full position
                order_key = self._level_order_ids.get(key, "")
                if order_key and hasattr(self._executor, 'close_position_by_key'):
                    result = await self._executor.close_position_by_key(order_key, current_price)
                else:
                    close_side = "Sell" if position["side"] == "Buy" else "Buy"
                    result = await self._executor.close_position(
                        symbol=symbol, side=close_side,
                        qty=float(position["size"]), current_price=current_price,
                    )
        except Exception as exc:
            logger.error("Grid close failed {}: {}", symbol, exc)
            return None

        pnl = result.get("pnl", 0.0)
        exit_fee = result.get("fee", 0.0)
        fill_price = result.get("fillPrice", current_price)
        entry_fee = self._level_entry_fees.get(key, 0.0)
        total_fee = entry_fee + exit_fee

        self._tracker.close_position(
            position_id=position_id,
            exit_price=fill_price,
            exit_reason=reason,
            exit_type="market",
            fee=total_fee,
        )

        # Live mode: remove from ledger
        if self._ledger is not None:
            self._ledger.remove_position(key)

        self._level_positions.pop(key, None)
        self._level_order_ids.pop(key, None)
        self._level_entry_fees.pop(key, None)

        logger.info(
            "Grid close executed: {} idx={} reason={} pnl={:.6f}",
            symbol, msg.level_index, reason, pnl,
        )

        return GridUpdateMessage(
            symbol=symbol, level_id=msg.level_id, action="CLOSED",
            position_id=position_id, pnl=pnl, fee=total_fee, reason=reason,
        )

    def get_grid_positions_count(self) -> int:
        return len(self._level_positions)

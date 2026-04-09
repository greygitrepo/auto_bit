"""Order manager: routes orders to the appropriate executor and manages lifecycle.

Serves as the central coordinator between strategy signals and order
execution, handling position state, SL/TP monitoring, P&L calculation,
and database persistence.

Task E-01.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Union

from loguru import logger

from src.strategy.asset.base import OrderRequest
from src.strategy.position.base import PositionSignal, SignalType
from src.utils.db import DatabaseManager


class OrderManager:
    """Routes orders to the appropriate executor (paper/live) based on mode.

    Manages the full order lifecycle: entry, SL/TP placement, position
    monitoring, exit, P&L recording, and crash recovery synchronisation.

    Parameters
    ----------
    mode:
        ``"paper"`` or ``"live"``.
    executor:
        A :class:`PaperExecutor` or :class:`LiveExecutor` instance.
    db:
        A :class:`DatabaseManager` for persisting positions and trades.
    """

    def __init__(
        self,
        mode: str,
        executor: Any,
        db: DatabaseManager,
    ) -> None:
        if mode not in ("paper", "live"):
            raise ValueError(f"Invalid mode: {mode!r}. Must be 'paper' or 'live'.")

        self.mode = mode
        self.executor = executor
        self.db = db

        logger.info("OrderManager initialised in {} mode", self.mode)

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    async def execute_order(
        self, order_request: OrderRequest, signal: PositionSignal
    ) -> dict:
        """Execute an approved order request.

        Steps:
        1. Set margin mode (isolated) and leverage via executor.
        2. Place a market order via executor.
        3. Place SL/TP conditional orders via executor.
        4. Record the position in the database.

        Parameters
        ----------
        order_request:
            An approved :class:`OrderRequest` with sizing details.
        signal:
            The :class:`PositionSignal` that triggered this order.

        Returns
        -------
        Dict with keys: ``success``, ``position_id``, ``order_info``, ``error``.
        """
        symbol = order_request.symbol
        side = order_request.side
        qty = order_request.qty
        leverage = order_request.leverage
        sl_price = order_request.stop_loss
        tp_price = order_request.take_profit

        logger.info(
            "Executing order: {} {} {:.6f} {} @ leverage={}x SL={:.4f} TP={:.4f}",
            side,
            qty,
            qty,
            symbol,
            leverage,
            sl_price,
            tp_price,
        )

        try:
            # 1. Set margin mode and leverage.
            await self.executor.set_margin_and_leverage(symbol, leverage)

            # 2. Place market order.
            if self.mode == "paper":
                order_info = await self.executor.place_market_order(
                    symbol, side, qty, signal.entry_price
                )
            else:
                order_info = await self.executor.place_market_order(
                    symbol, side, qty
                )

            # Check if order was rejected (min notional, insufficient balance)
            if order_info.get("rejected"):
                logger.info("Order REJECTED {}: {}", symbol, order_info.get("reason"))
                return None

            fill_price = order_info.get("fillPrice", signal.entry_price)

            # Try to get actual fee from executions
            exec_fee = 0.0
            if self.mode == "live":
                try:
                    executions = self.executor.client.get_executions(
                        symbol, order_id=order_info.get("orderId", "")
                    )
                    if executions:
                        exec_fee = sum(float(e.get("execFee", 0)) for e in executions)
                except Exception:
                    exec_fee = abs(fill_price * qty) * 0.0006  # estimate

            # 3. Place SL/TP conditional orders.
            sl_tp_info = await self.executor.place_sl_tp(
                symbol, side, qty, sl_price, tp_price
            )
            order_info["slOrderId"] = sl_tp_info.get("slOrderId", "")
            order_info["tpOrderId"] = sl_tp_info.get("tpOrderId", "")

            # 4. Record position in database.
            margin = order_request.size / leverage
            position_id = self.db.insert_position(
                mode=self.mode,
                symbol=symbol,
                side=side,
                size=qty,
                entry_price=fill_price,
                leverage=leverage,
                stop_loss=sl_price,
                take_profit=tp_price,
                margin=margin,
                unrealized_pnl=0.0,
                strategy=signal.strategy,
                scanner_direction=signal.suggested_side,
                entered_at=int(time.time()),
                sl_order_id=order_info.get("slOrderId", ""),
                tp_order_id=order_info.get("tpOrderId", ""),
            )

            logger.info(
                "Position opened: id={} {} {} {:.6f} {} @ {:.4f}",
                position_id,
                self.mode,
                side,
                qty,
                symbol,
                fill_price,
            )

            return {
                "success": True,
                "position_id": position_id,
                "order_info": order_info,
                "error": "",
            }

        except Exception as exc:
            logger.error(
                "Order execution failed for {} {}: {}",
                side,
                symbol,
                exc,
            )
            return {
                "success": False,
                "position_id": 0,
                "order_info": {},
                "error": str(exc),
            }

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    async def close_position(
        self,
        position: dict,
        reason: str,
        current_price: float = None,
    ) -> dict:
        """Close an open position.

        Steps:
        1. Place a market close order via executor.
        2. Cancel associated SL/TP conditional orders.
        3. Calculate P&L.
        4. Record trade in DB and delete position.

        Parameters
        ----------
        position:
            A position dict (from DB row or executor). Expected keys:
            ``id``, ``symbol``, ``side``, ``size``, ``entry_price``,
            ``leverage``, ``stop_loss``, ``take_profit``, ``strategy``,
            ``entered_at``.
        reason:
            Human-readable reason for closing (e.g. ``"signal_close"``,
            ``"time_limit"``, ``"manual"``).
        current_price:
            Current market price. Required for paper mode.

        Returns
        -------
        Dict with keys: ``success``, ``pnl``, ``fee``.
        """
        symbol = position["symbol"]
        side = position["side"]
        qty = float(position["size"])
        entry_price = float(position["entry_price"])
        leverage = int(position.get("leverage", 1))
        position_id = position.get("id")

        logger.info(
            "Closing position: {} {} {:.6f} {} (reason={})",
            side,
            qty,
            qty,
            symbol,
            reason,
        )

        try:
            # 1. Place market close order.
            if self.mode == "paper":
                if current_price is None:
                    raise ValueError(
                        "current_price is required for paper mode closes"
                    )
                close_info = await self.executor.close_position(
                    symbol, side, qty, current_price
                )
            else:
                close_info = await self.executor.close_position(
                    symbol, side, qty, current_price=current_price or 0.0
                )

            # 2. Cancel associated SL/TP orders.
            sl_tp_ids = self._collect_sl_tp_ids(position)
            if sl_tp_ids:
                await self.executor.cancel_orders(symbol, sl_tp_ids)

            # 3. Calculate P&L.
            exit_price = close_info.get("fillPrice", current_price or 0.0)
            fee = close_info.get("fee", 0.0)

            if self.mode == "paper":
                pnl = close_info.get("pnl", 0.0)
            else:
                # For live mode, calculate P&L from prices.
                if side == "Buy":
                    pnl = (exit_price - entry_price) * qty - fee
                else:
                    pnl = (entry_price - exit_price) * qty - fee

            # 4. Record trade and delete position from DB.
            exit_type = reason
            self.db.insert_trade(
                mode=self.mode,
                symbol=symbol,
                side=side,
                size=qty,
                entry_price=entry_price,
                exit_price=exit_price,
                pnl=pnl,
                fee=fee,
                leverage=leverage,
                strategy=position.get("strategy", ""),
                entry_time=int(position.get("entered_at", 0)),
                exit_time=int(time.time()),
                entry_reason=position.get("scanner_direction", ""),
                exit_reason=reason,
                exit_type=exit_type,
            )

            if position_id is not None:
                self.db.delete_position(position_id)

            logger.info(
                "Position closed: {} {} @ {:.4f} (entry {:.4f}) "
                "pnl={:.4f} fee={:.4f} reason={}",
                side,
                symbol,
                exit_price,
                entry_price,
                pnl,
                fee,
                reason,
            )

            return {"success": True, "pnl": pnl, "fee": fee}

        except Exception as exc:
            logger.error(
                "Failed to close position {} {}: {}",
                symbol,
                side,
                exc,
            )
            return {"success": False, "pnl": 0.0, "fee": 0.0}

    # ------------------------------------------------------------------
    # SL/TP monitoring
    # ------------------------------------------------------------------

    async def check_sl_tp_fills(self, positions: list) -> list:
        """Check whether any SL/TP orders have been filled.

        For **paper** mode, checks candle prices against SL/TP levels
        using :meth:`PaperExecutor.check_sl_tp`.

        For **live** mode, queries the exchange for recently filled
        conditional orders and matches them against tracked positions.

        Parameters
        ----------
        positions:
            List of open position dicts (from DB).

        Returns
        -------
        List of fill dicts with keys: ``symbol``, ``side``, ``fill_price``,
        ``fill_type``, ``pnl``, ``fee``, ``position`` (original position dict).
        """
        filled: List[dict] = []

        if self.mode == "paper":
            # Paper mode: SL/TP fills are handled inside check_sl_tp,
            # which is called by the main loop with candle data.
            # Here we just check if any positions have disappeared from
            # the paper executor (meaning they were filled by check_sl_tp).
            for pos in positions:
                symbol = pos["symbol"]
                paper_pos = await self.executor.get_position(symbol)
                if paper_pos is None:
                    # Position was closed by SL/TP fill.
                    # The PaperExecutor already recorded the trade internally,
                    # but we need to find the fill details from recent trades.
                    recent_trades = self.executor.get_trade_history()
                    for trade in reversed(recent_trades):
                        if trade["symbol"] == symbol:
                            filled.append(
                                {
                                    "symbol": symbol,
                                    "side": trade["side"],
                                    "fill_price": trade["exit_price"],
                                    "fill_type": trade["exit_type"],
                                    "pnl": trade["pnl"],
                                    "fee": trade["fee"],
                                    "position": pos,
                                }
                            )
                            break
        else:
            # Live mode: check exchange for filled conditional orders.
            for pos in positions:
                symbol = pos["symbol"]
                filled_orders = await self.executor.get_filled_orders(symbol)

                for order in filled_orders:
                    order_id = order.get("orderId", "")
                    trigger_price = float(order.get("triggerPrice", "0"))
                    avg_price = float(order.get("avgPrice", "0"))

                    # Determine if this is an SL or TP fill.
                    sl_price = float(pos.get("stop_loss", 0))
                    tp_price = float(pos.get("take_profit", 0))

                    fill_type = "unknown"
                    if trigger_price > 0:
                        sl_diff = abs(trigger_price - sl_price) if sl_price else float("inf")
                        tp_diff = abs(trigger_price - tp_price) if tp_price else float("inf")
                        fill_type = "stop_loss" if sl_diff < tp_diff else "take_profit"

                    entry_price = float(pos.get("entry_price", 0))
                    side = pos["side"]
                    qty = float(pos["size"])

                    if side == "Buy":
                        pnl = (avg_price - entry_price) * qty
                    else:
                        pnl = (entry_price - avg_price) * qty

                    # Estimate fee from fill price (taker rate 0.06%)
                    fee = abs(avg_price * qty) * 0.0006

                    filled.append(
                        {
                            "symbol": symbol,
                            "side": side,
                            "fill_price": avg_price,
                            "fill_type": fill_type,
                            "pnl": pnl - fee,
                            "fee": fee,
                            "position": pos,
                            "order_id": order_id,
                        }
                    )

        return filled

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------

    async def sync_with_exchange(self) -> dict:
        """Synchronise local DB state with the exchange.

        Compares positions stored in the database with those reported by
        the exchange (live mode) or the paper executor (paper mode).
        Detects positions closed during downtime and orphaned entries.

        Returns
        -------
        Dict with keys: ``synced``, ``closed_during_downtime``, ``orphaned``.
        """
        db_positions = self.db.get_open_positions(self.mode)
        synced = 0
        closed_during_downtime = 0
        orphaned = 0

        db_symbols = {dict(p)["symbol"] for p in db_positions}

        for db_row in db_positions:
            pos = dict(db_row)
            symbol = pos["symbol"]
            position_id = pos["id"]

            exchange_pos = await self.executor.get_position(symbol)

            if exchange_pos is not None:
                # Position exists on both sides -- synced.
                synced += 1
                logger.debug("Position {} synced for {}", position_id, symbol)
            else:
                # Position in DB but not on exchange -- closed during downtime.
                closed_during_downtime += 1
                logger.warning(
                    "Position {} ({}) closed during downtime -- cleaning up",
                    position_id,
                    symbol,
                )

                # Record as a trade with unknown exit.
                self.db.insert_trade(
                    mode=self.mode,
                    symbol=symbol,
                    side=pos["side"],
                    size=float(pos["size"]),
                    entry_price=float(pos["entry_price"]),
                    exit_price=0.0,
                    pnl=0.0,
                    fee=0.0,
                    leverage=int(pos.get("leverage", 1)),
                    strategy=pos.get("strategy", ""),
                    entry_time=int(pos.get("entered_at", 0)),
                    exit_time=int(time.time()),
                    entry_reason=pos.get("scanner_direction", ""),
                    exit_reason="closed_during_downtime",
                    exit_type="unknown",
                )
                self.db.delete_position(position_id)

        # Check for orphaned exchange positions not in our DB (live only).
        if self.mode == "live":
            try:
                from src.collector.bybit_client import BybitClient

                all_exchange = await self.executor._run_sync(
                    self.executor.client.get_positions
                )
                for ex_pos in all_exchange:
                    ex_symbol = ex_pos.get("symbol", "")
                    if ex_symbol and ex_symbol not in db_symbols:
                        orphaned += 1
                        logger.warning(
                            "Orphaned exchange position found: {} {} size={}",
                            ex_symbol,
                            ex_pos.get("side"),
                            ex_pos.get("size"),
                        )
            except Exception as exc:
                logger.error("Failed to check for orphaned positions: {}", exc)

        logger.info(
            "Sync complete: synced={}, closed_during_downtime={}, orphaned={}",
            synced,
            closed_during_downtime,
            orphaned,
        )

        return {
            "synced": synced,
            "closed_during_downtime": closed_during_downtime,
            "orphaned": orphaned,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_sl_tp_ids(position: dict) -> list:
        """Extract SL/TP order IDs from a position dict.

        Looks for ``sl_order_id`` / ``tp_order_id`` keys (paper) or
        ``slOrderId`` / ``tpOrderId`` keys.
        """
        ids = []
        for key in ("sl_order_id", "slOrderId"):
            val = position.get(key, "")
            if val:
                ids.append(val)
        for key in ("tp_order_id", "tpOrderId"):
            val = position.get(key, "")
            if val:
                ids.append(val)
        return ids

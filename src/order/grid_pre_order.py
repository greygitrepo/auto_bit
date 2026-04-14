"""Grid Pre-Order Manager — place limit orders on Bybit ahead of time.

Instead of waiting for P2 to detect grid fills after candle completion
and then having P3 place limit orders (by which time the price has moved),
this manager places limit orders on Bybit AHEAD of time so that fills
happen automatically when price touches the grid level.

Lifecycle:
  1. On grid creation  → place_grid_orders() for all pending levels
  2. Every ~5 seconds  → check_fills() + check_tp_fills()
  3. On grid recenter  → cancel_symbol_orders() then place_grid_orders()
  4. On shutdown        → cancel_all()
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from src.collector.bybit_client import BybitAPIError, BybitClient
from src.strategy.asset.grid_sizing import GridSizingStrategy
from src.tracker.position_tracker import PositionTracker


# (symbol, level_index) composite key
LevelKey = Tuple[str, int]


class GridPreOrderManager:
    """Places grid limit orders on Bybit ahead of price movement.

    Works alongside :class:`GridPositionManager` — this class handles
    order placement and fill detection, while GridPositionManager
    handles position tracking and P&L.

    Parameters
    ----------
    executor:
        A :class:`LiveExecutor` instance (used for rounding helpers and
        margin/leverage setup).
    bybit_client:
        An authenticated :class:`BybitClient` for direct API calls.
    position_tracker:
        Shared position tracker for recording fills.
    sizing:
        Grid sizing strategy for risk checks.
    mode:
        ``"live"`` or ``"paper"``.
    initial_balance:
        Account starting balance in USDT.
    leverage:
        Default leverage for grid orders.
    """

    def __init__(
        self,
        executor: Any,
        bybit_client: BybitClient,
        position_tracker: PositionTracker,
        sizing: GridSizingStrategy,
        mode: str,
        initial_balance: float,
        leverage: int,
        qty_per_level_pct: float = 5.0,
    ) -> None:
        self._executor = executor
        self._client = bybit_client
        self._tracker = position_tracker
        self._sizing = sizing
        self._mode = mode
        self._initial_balance = initial_balance
        self._leverage = leverage
        self._qty_per_level_pct = qty_per_level_pct

        # symbol -> {level_index: order_id}
        self._pending_orders: Dict[str, Dict[int, str]] = {}
        # symbol -> {level_index: {price, side, tp_price, sl_price, qty}}
        self._level_info: Dict[str, Dict[int, dict]] = {}
        # symbol -> {level_index: tp_order_id}
        self._tp_orders: Dict[str, Dict[int, str]] = {}
        # symbol -> {level_index: fill_info_dict}
        self._filled_levels: Dict[str, Dict[int, dict]] = {}

        # Bybit limit is 50 active orders per symbol; stay well within it.
        self._max_orders_per_symbol = 20

        # Per-symbol loss tracking for auto-ban
        self._symbol_losses: Dict[str, list] = {}  # symbol -> [pnl, pnl, ...]
        self._banned_symbols: set = set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _run_sync(fn, *args, **kwargs):
        """Run a blocking call in the default thread-pool executor."""
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    def _round_qty(self, symbol: str, qty: float) -> float:
        return self._executor._round_qty(symbol, qty)

    def _round_price(self, symbol: str, price: float) -> float:
        return self._executor._round_price(symbol, price)

    # ------------------------------------------------------------------
    # Place grid orders
    # ------------------------------------------------------------------

    async def place_grid_orders(
        self,
        symbol: str,
        levels: list = None,
        current_balance: float = 0.0,
        qty_per_level: float = 0.0,
        leverage: int = 0,
        grid_state: dict = None,
    ) -> int:
        """Place limit orders for all PENDING levels of a grid.

        Can be called with either:
        - levels, current_balance, qty_per_level, leverage (from P3 SETUP signal)
        - grid_state dict (legacy)
        """
        if grid_state is not None:
            levels = grid_state.get("levels", [])
            qty_per_level = grid_state.get("qty_per_level", 0)
            leverage = grid_state.get("leverage", self._leverage)

        if not levels:
            return 0
        if symbol in self._banned_symbols:
            logger.info("GridPreOrder: {} is banned, skipping", symbol)
            return 0
        if leverage <= 0:
            leverage = self._leverage
        if current_balance <= 0:
            current_balance = self._initial_balance

        qty_pct = self._qty_per_level_pct

        # Check BTC trend — skip Buy orders in bearish market
        btc_bearish = False
        try:
            tickers = await self._run_sync(self._client.get_tickers)
            btc = next((t for t in tickers if t.get("symbol") == "BTCUSDT"), None)
            if btc:
                price_24h_pct = float(btc.get("price24hPcnt", 0)) * 100
                if price_24h_pct < -2.0:  # BTC down > 2% in 24h
                    btc_bearish = True
                    logger.info("GridPreOrder: BTC bearish ({:+.1f}%), filtering Buy orders", price_24h_pct)
        except Exception:
            pass

        # Filter levels based on market direction
        if btc_bearish:
            pending = [lv for lv in levels if lv.get("side") == "Sell"]
            logger.info("GridPreOrder: {} levels after BTC filter (Sell only)", len(pending))
        else:
            pending = levels
        if not pending:
            logger.debug("GridPreOrder: no pending levels for {}", symbol)
            return 0

        # Ensure margin/leverage are set
        try:
            await self._executor.set_margin_and_leverage(symbol, leverage)
        except Exception as exc:
            logger.error("GridPreOrder: failed to set leverage for {}: {}", symbol, exc)

        # Calculate per-level notional and margin
        margin_per_level = current_balance * qty_pct / 100.0
        notional_per_level = margin_per_level * leverage

        # Account for margin already used by existing positions + pending orders
        existing_margin = 0.0
        try:
            open_positions = self._tracker.get_open_positions()
            existing_margin = sum(float(p.get("margin", 0)) for p in open_positions)
        except Exception:
            pass
        # Also count margin from pending pre-orders on other symbols
        for other_sym, other_levels in self._level_info.items():
            if other_sym != symbol:
                existing_margin += sum(
                    lv.get("qty", 0) * lv.get("price", 0) / leverage
                    for lv in other_levels.values()
                )

        total_margin_needed = margin_per_level * len(pending)
        available_margin = max(0, current_balance * 0.6 - existing_margin)

        # If we'd exceed margin budget, keep only levels closest to current price
        if total_margin_needed > available_margin and margin_per_level > 0:
            max_levels = int(available_margin / margin_per_level)
            max_levels = max(max_levels, 1)
        else:
            max_levels = len(pending)

        # Also respect Bybit order limit
        max_levels = min(max_levels, self._max_orders_per_symbol)

        if max_levels < len(pending):
            # Sort by distance from current mid-price (closest first)
            try:
                tickers = await self._run_sync(self._client.get_tickers)
                current_price = float(
                    next((t["lastPrice"] for t in tickers if t["symbol"] == symbol), 0)
                )
            except Exception:
                current_price = 0.0

            if current_price > 0:
                pending.sort(key=lambda lv: abs(lv["price"] - current_price))
            pending = pending[:max_levels]
            logger.info(
                "GridPreOrder: reduced {} orders to {} for {} (margin/limit)",
                len(levels), max_levels, symbol,
            )

        # Initialize tracking dicts for this symbol
        if symbol not in self._pending_orders:
            self._pending_orders[symbol] = {}
        if symbol not in self._level_info:
            self._level_info[symbol] = {}

        placed = 0
        for lv in pending:
            level_index = lv.get("index", lv.get("level_index", 0))
            level_price = float(lv["price"])
            side = lv.get("side", "Buy")

            # Calculate quantity
            qty = notional_per_level / level_price if level_price > 0 else 0
            if qty <= 0:
                continue

            qty = self._round_qty(symbol, qty)
            price = self._round_price(symbol, level_price)

            # Check minimum notional
            info = self._executor._get_instrument_info(symbol)
            lot_filter = info.get("lotSizeFilter", {})
            min_notional = float(lot_filter.get("minNotionalValue", "5"))
            if qty * price < min_notional:
                logger.debug(
                    "GridPreOrder: skip {} idx={} notional {:.2f} < min {:.1f}",
                    symbol, level_index, qty * price, min_notional,
                )
                continue

            try:
                result = await self._run_sync(
                    self._client.place_order,
                    symbol=symbol,
                    side=side,
                    qty=str(qty),
                    order_type="Limit",
                    price=str(price),
                )
                order_id = result.get("orderId", "")
                if order_id:
                    self._pending_orders[symbol][level_index] = order_id
                    self._level_info[symbol][level_index] = {
                        "price": level_price,
                        "side": side,
                        "tp_price": float(lv.get("tp_price", 0)),
                        "sl_price": float(lv.get("sl_price", 0)),
                        "qty": qty,
                    }
                    placed += 1
                    logger.info(
                        "GridPreOrder: placed {} {} idx={} price={} qty={} -> {}",
                        side, symbol, level_index, price, qty, order_id,
                    )
                else:
                    logger.warning(
                        "GridPreOrder: no orderId for {} idx={}", symbol, level_index,
                    )
            except BybitAPIError as exc:
                # PostOnly rejected (would match as taker) — adjust price 1 tick away
                if "140024" in str(exc) or "post only" in str(exc).lower():
                    try:
                        tick = float(self._executor._get_instrument_info(symbol)
                                     .get("priceFilter", {}).get("tickSize", "0.01"))
                        adjusted = price - tick if side == "Buy" else price + tick
                        result2 = await self._run_sync(
                            self._client.place_order,
                            symbol=symbol, side=side, qty=str(qty),
                            order_type="Limit", price=str(adjusted),
                        )
                        oid2 = result2.get("orderId", "")
                        if oid2:
                            self._pending_orders[symbol][level_index] = oid2
                            self._level_info[symbol][level_index] = {
                                "price": adjusted, "side": side,
                                "tp_price": float(lv.get("tp_price", 0)),
                                "sl_price": float(lv.get("sl_price", 0)),
                                "qty": qty,
                            }
                            placed += 1
                            logger.info("GridPreOrder: PostOnly retry {} idx={} @ {}", symbol, level_index, adjusted)
                    except Exception:
                        pass
                else:
                    logger.error("GridPreOrder: API error {} idx={}: {}", symbol, level_index, exc)
            except Exception as exc:
                exc_str = str(exc)
                if "140024" in exc_str or "post only" in exc_str.lower():
                    logger.debug("GridPreOrder: PostOnly skip {} idx={}", symbol, level_index)
                else:
                    logger.error("GridPreOrder: error {} idx={}: {}", symbol, level_index, exc)

            # Small delay between orders to respect rate limits
            await asyncio.sleep(0.1)

        logger.info(
            "GridPreOrder: placed {}/{} orders for {}",
            placed, len(pending), symbol,
        )
        return placed

    # ------------------------------------------------------------------
    # Fill detection
    # ------------------------------------------------------------------

    async def check_fills(self) -> List[dict]:
        """Check all pending orders for fills.

        Compares tracked order IDs against Bybit's open orders list.
        Any order NOT in the open list is assumed filled.

        Returns
        -------
        List of dicts describing filled levels::

            [{"symbol": ..., "level_index": ..., "side": ...,
              "fill_price": ..., "qty": ..., "fee": ..., "order_id": ...}, ...]
        """
        filled_results: List[dict] = []

        # Snapshot symbols to iterate (avoid dict-changed-during-iteration)
        symbols = list(self._pending_orders.keys())

        for symbol in symbols:
            level_orders = self._pending_orders.get(symbol, {})
            if not level_orders:
                continue

            # Fetch currently open orders for this symbol
            try:
                open_orders = await self._run_sync(
                    self._client.get_open_orders, symbol,
                )
            except Exception as exc:
                logger.error("GridPreOrder: failed to get open orders for {}: {}", symbol, exc)
                continue

            open_ids = {o.get("orderId") for o in open_orders}

            # Find which tracked orders are no longer open (= filled or cancelled)
            filled_indices = [
                (idx, oid) for idx, oid in level_orders.items()
                if oid not in open_ids
            ]

            for level_index, order_id in filled_indices:
                # Query executions to confirm fill and get details
                try:
                    executions = await self._run_sync(
                        self._client.get_executions, symbol, order_id=order_id,
                    )
                except Exception as exc:
                    logger.error(
                        "GridPreOrder: failed to get executions for {} order {}: {}",
                        symbol, order_id, exc,
                    )
                    executions = []

                if not executions:
                    # Order disappeared but no executions
                    # Could be: (a) cancelled externally, (b) execution data not yet available
                    # Keep in tracking for one more cycle to retry, then remove
                    retry_key = f"{symbol}_{level_index}_retry"
                    if not hasattr(self, '_retry_counts'):
                        self._retry_counts = {}
                    self._retry_counts[retry_key] = self._retry_counts.get(retry_key, 0) + 1
                    if self._retry_counts[retry_key] >= 3:
                        # After 3 retries, assume cancelled
                        logger.warning(
                            "GridPreOrder: order {} for {} idx={} gone after 3 retries, removing",
                            order_id, symbol, level_index,
                        )
                        level_orders.pop(level_index, None)
                        self._retry_counts.pop(retry_key, None)
                    else:
                        logger.debug(
                            "GridPreOrder: order {} for {} idx={} gone, retry {}/3",
                            order_id, symbol, level_index, self._retry_counts[retry_key],
                        )
                    continue

                # Aggregate fill info
                fill_price = float(executions[0].get("execPrice", 0))
                total_qty = sum(float(e.get("execQty", 0)) for e in executions)
                total_fee = sum(abs(float(e.get("execFee", 0))) for e in executions)
                side = executions[0].get("side", "Buy")

                # Get tp/sl from cached level info
                lv_info = self._level_info.get(symbol, {}).get(level_index, {})

                fill_info = {
                    "symbol": symbol,
                    "level_index": level_index,
                    "side": side,
                    "fill_price": fill_price,
                    "qty": total_qty,
                    "fee": total_fee,
                    "order_id": order_id,
                    "tp_price": lv_info.get("tp_price", 0),
                    "sl_price": lv_info.get("sl_price", 0),
                }

                # Move from pending to filled
                level_orders.pop(level_index, None)
                if symbol not in self._filled_levels:
                    self._filled_levels[symbol] = {}
                self._filled_levels[symbol][level_index] = fill_info

                filled_results.append(fill_info)

                logger.info(
                    "GridPreOrder: FILL detected {} {} idx={} @ {:.6f} qty={:.6f} fee={:.6f}",
                    side, symbol, level_index, fill_price, total_qty, total_fee,
                )

        return filled_results

    # ------------------------------------------------------------------
    # Take-profit order placement
    # ------------------------------------------------------------------

    async def place_tp_order(
        self,
        symbol: str,
        level_index: int,
        side: str,
        qty: float,
        tp_price: float,
    ) -> Optional[str]:
        """Place a take-profit limit order for a filled level.

        Parameters
        ----------
        symbol:
            Trading pair.
        level_index:
            Grid level index that was filled.
        side:
            The POSITION side (``"Buy"`` or ``"Sell"``). The TP order is
            placed on the opposite side.
        qty:
            Position quantity to close.
        tp_price:
            Take-profit price.

        Returns
        -------
        Order ID string, or ``None`` on failure.
        """
        close_side = "Sell" if side == "Buy" else "Buy"
        qty = self._round_qty(symbol, qty)
        tp_price = self._round_price(symbol, tp_price)

        try:
            result = await self._run_sync(
                self._client.place_order,
                symbol=symbol,
                side=close_side,
                qty=str(qty),
                order_type="Limit",
                price=str(tp_price),
            )
            order_id = result.get("orderId", "")
            if order_id:
                if symbol not in self._tp_orders:
                    self._tp_orders[symbol] = {}
                self._tp_orders[symbol][level_index] = order_id

                logger.info(
                    "GridPreOrder: TP placed {} {} idx={} @ {} qty={} -> {}",
                    close_side, symbol, level_index, tp_price, qty, order_id,
                )
                return order_id
            else:
                logger.warning(
                    "GridPreOrder: TP no orderId for {} idx={}", symbol, level_index,
                )
                return None
        except BybitAPIError as exc:
            logger.error(
                "GridPreOrder: TP API error {} idx={}: {}", symbol, level_index, exc,
            )
            return None
        except Exception as exc:
            logger.error(
                "GridPreOrder: TP unexpected error {} idx={}: {}", symbol, level_index, exc,
            )
            return None

    # ------------------------------------------------------------------
    # TP fill detection
    # ------------------------------------------------------------------

    async def check_tp_fills(self) -> List[dict]:
        """Check if any take-profit orders have been filled.

        Returns
        -------
        List of dicts describing completed TP levels::

            [{"symbol": ..., "level_index": ..., "fill_price": ...,
              "qty": ..., "fee": ..., "order_id": ...}, ...]
        """
        tp_results: List[dict] = []

        symbols = list(self._tp_orders.keys())

        for symbol in symbols:
            tp_orders = self._tp_orders.get(symbol, {})
            if not tp_orders:
                continue

            try:
                open_orders = await self._run_sync(
                    self._client.get_open_orders, symbol,
                )
            except Exception as exc:
                logger.error("GridPreOrder: failed to get open orders for {} (TP check): {}", symbol, exc)
                continue

            open_ids = {o.get("orderId") for o in open_orders}

            filled_tp = [
                (idx, oid) for idx, oid in tp_orders.items()
                if oid not in open_ids
            ]

            for level_index, order_id in filled_tp:
                try:
                    executions = await self._run_sync(
                        self._client.get_executions, symbol, order_id=order_id,
                    )
                except Exception as exc:
                    logger.error(
                        "GridPreOrder: failed to get TP executions {} order {}: {}",
                        symbol, order_id, exc,
                    )
                    executions = []

                if not executions:
                    logger.warning(
                        "GridPreOrder: TP order {} for {} idx={} gone but no executions",
                        order_id, symbol, level_index,
                    )
                    tp_orders.pop(level_index, None)
                    continue

                fill_price = float(executions[0].get("execPrice", 0))
                total_qty = sum(float(e.get("execQty", 0)) for e in executions)
                total_fee = sum(abs(float(e.get("execFee", 0))) for e in executions)

                # Calculate PnL from entry info
                entry_info = self._filled_levels.get(symbol, {}).get(level_index, {})
                entry_price = entry_info.get("fill_price", 0)
                entry_side = entry_info.get("side", "Buy")
                entry_fee = entry_info.get("fee", 0)
                if entry_price > 0 and total_qty > 0:
                    if entry_side == "Buy":
                        gross_pnl = (fill_price - entry_price) * total_qty
                    else:
                        gross_pnl = (entry_price - fill_price) * total_qty
                    net_pnl = gross_pnl - total_fee - entry_fee
                else:
                    net_pnl = 0

                tp_info = {
                    "symbol": symbol,
                    "level_index": level_index,
                    "fill_price": fill_price,
                    "qty": total_qty,
                    "fee": total_fee + entry_fee,
                    "pnl": net_pnl,
                    "order_id": order_id,
                }

                tp_orders.pop(level_index, None)
                # Also clear from filled_levels since the cycle is complete
                if symbol in self._filled_levels:
                    self._filled_levels[symbol].pop(level_index, None)

                tp_results.append(tp_info)

                logger.info(
                    "GridPreOrder: TP FILL {} idx={} @ {:.6f} qty={:.6f} fee={:.6f}",
                    symbol, level_index, fill_price, total_qty, total_fee,
                )

        return tp_results

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    async def cancel_symbol_orders(self, symbol: str) -> int:
        """Cancel all unfilled orders for a symbol (recenter / close).

        Cancels both pending entry orders and any outstanding TP orders
        for levels that have not yet been TP-filled.

        Returns
        -------
        Number of orders successfully cancelled.
        """
        cancelled = 0

        # Cancel pending entry orders
        pending = self._pending_orders.pop(symbol, {})
        for level_index, order_id in pending.items():
            try:
                await self._run_sync(
                    self._client.cancel_order, symbol, order_id,
                )
                cancelled += 1
                logger.debug("GridPreOrder: cancelled pending {} idx={} -> {}", symbol, level_index, order_id)
            except BybitAPIError as exc:
                # Order may already be filled or cancelled
                logger.warning(
                    "GridPreOrder: cancel pending failed {} idx={} ({}): {}",
                    symbol, level_index, order_id, exc,
                )
            except Exception as exc:
                logger.error(
                    "GridPreOrder: cancel pending error {} idx={}: {}",
                    symbol, level_index, exc,
                )

        # Cancel outstanding TP orders
        tp_orders = self._tp_orders.pop(symbol, {})
        for level_index, order_id in tp_orders.items():
            try:
                await self._run_sync(
                    self._client.cancel_order, symbol, order_id,
                )
                cancelled += 1
                logger.debug("GridPreOrder: cancelled TP {} idx={} -> {}", symbol, level_index, order_id)
            except BybitAPIError as exc:
                logger.warning(
                    "GridPreOrder: cancel TP failed {} idx={} ({}): {}",
                    symbol, level_index, order_id, exc,
                )
            except Exception as exc:
                logger.error(
                    "GridPreOrder: cancel TP error {} idx={}: {}",
                    symbol, level_index, exc,
                )

        # Clear all tracking for this symbol
        self._filled_levels.pop(symbol, {})
        self._level_info.pop(symbol, {})

        if cancelled > 0:
            logger.info("GridPreOrder: cancelled {} orders for {}", cancelled, symbol)

        return cancelled

    def record_trade_result(self, symbol: str, pnl: float) -> None:
        """Record a trade result for symbol ban tracking."""
        if symbol not in self._symbol_losses:
            self._symbol_losses[symbol] = []
        self._symbol_losses[symbol].append(pnl)
        self._symbol_losses[symbol] = self._symbol_losses[symbol][-5:]

        recent = self._symbol_losses[symbol]
        if len(recent) >= 3 and all(p < 0 for p in recent[-3:]):
            logger.warning(
                "GridPreOrder: {} banned after 3 consecutive losses ({})",
                symbol, [round(p, 4) for p in recent[-3:]],
            )
            self._banned_symbols.add(symbol)
            # Cancel existing orders for this symbol
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(self.cancel_symbol_orders(symbol))
            except Exception:
                pass

    async def cancel_all(self) -> int:
        """Cancel all orders across all symbols (shutdown).

        Returns
        -------
        Total number of orders cancelled.
        """
        total = 0
        all_symbols = set(
            list(self._pending_orders.keys())
            + list(self._tp_orders.keys())
        )

        for symbol in all_symbols:
            try:
                count = await self.cancel_symbol_orders(symbol)
                total += count
            except Exception as exc:
                logger.error("GridPreOrder: cancel_all error for {}: {}", symbol, exc)

        logger.info("GridPreOrder: cancel_all complete — {} orders cancelled", total)
        return total

    # ------------------------------------------------------------------
    # Margin / status queries
    # ------------------------------------------------------------------

    def get_margin_usage(self) -> float:
        """Calculate total margin locked by pending entry orders.

        This is an estimate based on tracked orders; actual margin usage
        on the exchange may differ slightly due to funding and mark-price
        changes.

        Returns
        -------
        Estimated margin in USDT.
        """
        total_margin = 0.0

        for symbol, level_orders in self._pending_orders.items():
            if not level_orders:
                continue
            # We don't store notional per order, so re-derive from
            # instrument info and sizing config.  This is approximate.
            try:
                info = self._executor._get_instrument_info(symbol)
                lot_filter = info.get("lotSizeFilter", {})
                qty_step = float(lot_filter.get("qtyStep", "0.01"))
            except Exception:
                qty_step = 0.01

            # Each pending order locks roughly:
            #   margin = (balance * qty_pct / 100) per level
            # But we don't have per-order price cached, so use the sizing pct.
            margin_per_level = self._initial_balance * self._sizing.qty_per_level_pct / 100.0
            total_margin += margin_per_level * len(level_orders)

        return total_margin

    def get_pending_count(self, symbol: Optional[str] = None) -> int:
        """Return count of pending entry orders.

        Parameters
        ----------
        symbol:
            If given, count only for this symbol. Otherwise total across all.
        """
        if symbol:
            return len(self._pending_orders.get(symbol, {}))
        return sum(len(v) for v in self._pending_orders.values())

    def get_tp_count(self, symbol: Optional[str] = None) -> int:
        """Return count of outstanding TP orders.

        Parameters
        ----------
        symbol:
            If given, count only for this symbol. Otherwise total across all.
        """
        if symbol:
            return len(self._tp_orders.get(symbol, {}))
        return sum(len(v) for v in self._tp_orders.values())

    def get_filled_levels(self, symbol: str) -> Dict[int, dict]:
        """Return filled levels awaiting TP for a symbol."""
        return dict(self._filled_levels.get(symbol, {}))

    def get_status_summary(self) -> dict:
        """Return a summary of pre-order state for diagnostics."""
        return {
            "pending_orders": {
                sym: len(orders) for sym, orders in self._pending_orders.items()
            },
            "tp_orders": {
                sym: len(orders) for sym, orders in self._tp_orders.items()
            },
            "filled_awaiting_tp": {
                sym: len(fills) for sym, fills in self._filled_levels.items()
            },
            "estimated_margin": self.get_margin_usage(),
        }

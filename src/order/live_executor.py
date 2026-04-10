"""Live order executor for Bybit exchange.

Wraps :class:`BybitClient` to provide the executor interface expected by
:class:`OrderManager`. All methods are async for interface compatibility
with :class:`PaperExecutor`.

Task E-02.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from loguru import logger

from src.collector.bybit_client import BybitAPIError, BybitClient


class LiveExecutor:
    """Executes real orders on Bybit via the V5 REST API.

    Parameters
    ----------
    bybit_client:
        An authenticated :class:`BybitClient` instance.
    """

    def __init__(self, bybit_client: BybitClient) -> None:
        self.client = bybit_client
        self._instrument_cache: Dict[str, dict] = {}
        self._leverage_cache: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_instrument_info(self, symbol: str) -> dict:
        """Get and cache instrument info for qty/price precision."""
        if symbol not in self._instrument_cache:
            try:
                info = self.client.get_instrument_info(symbol)
                self._instrument_cache[symbol] = info
            except Exception:
                self._instrument_cache[symbol] = {}
        return self._instrument_cache[symbol]

    def _round_qty(self, symbol: str, qty: float) -> float:
        """Round quantity to instrument's lot size step."""
        info = self._get_instrument_info(symbol)
        lot_filter = info.get("lotSizeFilter", {})
        qty_step = float(lot_filter.get("qtyStep", "0.01"))
        min_qty = float(lot_filter.get("minOrderQty", "0.01"))
        # Floor to step size
        rounded = int(qty / qty_step) * qty_step
        return max(rounded, min_qty)

    def _round_price(self, symbol: str, price: float) -> float:
        """Round price to instrument's tick size."""
        info = self._get_instrument_info(symbol)
        price_filter = info.get("priceFilter", {})
        tick_size = float(price_filter.get("tickSize", "0.01"))
        # Round to nearest tick
        return round(round(price / tick_size) * tick_size, 10)

    @staticmethod
    def _run_sync(fn, *args, **kwargs):
        """Run a synchronous BybitClient method in the default executor.

        This keeps the async event loop responsive while the blocking
        HTTP call (with potential retries) executes.
        """
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    # ------------------------------------------------------------------
    # Margin / leverage
    # ------------------------------------------------------------------

    async def set_margin_and_leverage(self, symbol: str, leverage: int) -> None:
        """Set isolated margin mode and leverage for a symbol.

        Silently handles the case where the margin mode or leverage is
        already set (Bybit returns an error for no-op changes).

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTCUSDT"``.
        leverage:
            Desired leverage multiplier.
        """
        # Set isolated margin mode.
        # Skip for Unified Trading Accounts (ErrCode 100028).
        try:
            await self._run_sync(
                self.client.set_margin_mode, symbol, "ISOLATED"
            )
            logger.info("Set margin mode to ISOLATED for {}", symbol)
        except BybitAPIError as exc:
            if exc.ret_code in (110026, 100028):
                # 110026 = already set, 100028 = UTA (not applicable)
                logger.debug(
                    "Margin mode skip for {} (code={})", symbol, exc.ret_code
                )
            else:
                raise

        # Set leverage.
        try:
            await self._run_sync(
                self.client.set_leverage, symbol, str(leverage)
            )
            logger.info("Set leverage to {}x for {}", leverage, symbol)
        except BybitAPIError as exc:
            # ret_code 110043 = "leverage not modified" (already set).
            if exc.ret_code == 110043:
                logger.debug(
                    "Leverage already {}x for {}", leverage, symbol
                )
            else:
                raise
        self._leverage_cache[symbol] = leverage

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def place_market_order(
        self, symbol: str, side: str, qty: float, current_price: float = 0.0
    ) -> dict:
        """Place a market order and return fill info.

        Parameters
        ----------
        symbol:
            Trading pair.
        side:
            ``"Buy"`` or ``"Sell"``.
        qty:
            Order quantity in base currency.

        Returns
        -------
        Dict with ``orderId``, ``orderLinkId``, and additional metadata.
        """
        qty = self._round_qty(symbol, qty)

        # Get current price as fallback for fill price
        try:
            tickers = await self._run_sync(self.client.get_tickers)
            current_price = float(
                next((t["lastPrice"] for t in tickers if t["symbol"] == symbol), 0)
            )
        except Exception:
            current_price = 0.0

        # Check minimum notional value (5 USDT)
        if current_price > 0:
            info = self._get_instrument_info(symbol)
            lot_filter = info.get("lotSizeFilter", {})
            min_notional = float(lot_filter.get("minNotionalValue", "5"))
            notional = qty * current_price
            if notional < min_notional:
                logger.info("Live REJECT {}: notional {:.2f} < min {:.1f}", symbol, notional, min_notional)
                return {"rejected": True, "reason": "below_min_notional", "orderId": "", "fillPrice": 0.0, "fee": 0.0}

        # Check available margin before placing order
        try:
            wallet = await self._run_sync(self.client.get_wallet_balance)
            usdt = wallet.get("usdt", {})
            available = float(usdt.get("availableToWithdraw", 0) or usdt.get("walletBalance", 0) or 0)
            # Estimate required margin: notional / leverage
            leverage = self._leverage_cache.get(symbol, 1)
            est_notional = qty * current_price if current_price > 0 else 0
            required_margin = est_notional / leverage if leverage > 0 else est_notional
            # Keep 20% reserve
            if available > 0 and required_margin > available * 0.8:
                logger.info(
                    "Live REJECT {}: margin {:.2f} > available {:.2f} (80%)",
                    symbol, required_margin, available * 0.8,
                )
                return {"rejected": True, "reason": "insufficient_margin", "orderId": "", "fillPrice": 0.0, "fee": 0.0}
        except Exception as exc:
            logger.debug("Margin check skipped for {}: {}", symbol, exc)

        result = await self._run_sync(
            self.client.place_order,
            symbol=symbol,
            side=side,
            qty=str(qty),
            order_type="Market",
        )

        # Get actual fill price
        order_id = result.get("orderId", "")
        fill_price = float(result.get("avgPrice", 0))
        if fill_price == 0 and order_id:
            import time as _time
            _time.sleep(0.5)  # Brief wait for fill
            try:
                executions = await self._run_sync(
                    self.client.get_executions, symbol, order_id=order_id
                )
                if executions:
                    fill_price = float(executions[0].get("execPrice", current_price))
            except Exception:
                fill_price = current_price
        result["fillPrice"] = fill_price if fill_price > 0 else current_price

        # Check for partial fill
        if order_id:
            try:
                executions = await self._run_sync(
                    self.client.get_executions, symbol, order_id=order_id
                ) if not executions else executions  # reuse if already fetched
                filled_qty = sum(float(e.get("execQty", 0)) for e in (executions or []))
                if 0 < filled_qty < qty * 0.99:
                    logger.warning(
                        "PARTIAL FILL detected for {} {}: requested={:.6f} filled={:.6f}",
                        symbol, side, qty, filled_qty,
                    )
                    result["partialFill"] = True
                    result["filledQty"] = filled_qty
            except Exception:
                pass

        logger.info(
            "Live MARKET ORDER: {} {} {:.6f} {} @ {:.4f} -> orderId={}",
            side,
            qty,
            qty,
            symbol,
            result["fillPrice"],
            result.get("orderId"),
        )
        return result

    async def place_sl_tp(
        self,
        symbol: str,
        side: str,
        qty: float,
        sl_price: float,
        tp_price: float,
    ) -> dict:
        """Place stop-loss and take-profit conditional orders.

        Parameters
        ----------
        symbol:
            Trading pair.
        side:
            Position side (``"Buy"`` or ``"Sell"``). The conditional orders
            are placed on the opposite side to close the position.
        qty:
            Order quantity.
        sl_price:
            Stop-loss trigger price.
        tp_price:
            Take-profit trigger price.

        Returns
        -------
        Dict with ``slOrderId`` and ``tpOrderId``.
        """
        close_side = "Sell" if side == "Buy" else "Buy"
        qty = self._round_qty(symbol, qty)
        sl_price = self._round_price(symbol, sl_price)
        tp_price = self._round_price(symbol, tp_price)

        # For SL:
        #   LONG position -> SL sells when price falls below trigger -> triggerDirection=2
        #   SHORT position -> SL buys when price rises above trigger -> triggerDirection=1
        # place_conditional_order already sets direction based on side.
        sl_result = await self._run_sync(
            self.client.place_conditional_order,
            symbol=symbol,
            side=close_side,
            qty=str(qty),
            trigger_price=str(sl_price),
            order_type="Market",
        )
        sl_order_id = sl_result.get("orderId", "")
        logger.info(
            "Live SL order placed: {} {} @ {:.4f} -> {}",
            close_side,
            symbol,
            sl_price,
            sl_order_id,
        )

        # For TP:
        #   LONG position -> TP sells when price rises above trigger -> triggerDirection=2
        #   SHORT position -> TP buys when price falls below trigger -> triggerDirection=1
        # Note: TP close_side is same as SL close_side, but trigger direction
        # is opposite. We need to handle this since BybitClient infers direction
        # from side. For a LONG TP (Sell when price rises), triggerDirection
        # should be 1 (rise), but BybitClient sets it to 2 for Sell.
        # We call the raw HTTP client directly for TP.
        tp_trigger_direction = "1" if close_side == "Sell" else "2"
        tp_order_id = ""
        for attempt in range(3):
            try:
                tp_raw = await self._run_sync(
                    self.client._http.place_order,
                    category=self.client.CATEGORY,
                    symbol=symbol,
                    side=close_side,
                    qty=str(qty),
                    orderType="Market",
                    triggerPrice=str(tp_price),
                    triggerBy="MarkPrice",
                    triggerDirection=tp_trigger_direction,
                )
                tp_result = self.client._parse_response(tp_raw, "place_conditional_order_tp")
                tp_order_id = tp_result.get("orderId", "")
                break
            except Exception as exc:
                if attempt == 2:
                    logger.error("Failed to place TP order for {} after 3 attempts: {}", symbol, exc)
                    tp_order_id = ""
                else:
                    logger.warning("TP order attempt {} failed for {}: {}, retrying...", attempt + 1, symbol, exc)
                    import time as _time
                    _time.sleep(1)
        logger.info(
            "Live TP order placed: {} {} @ {:.4f} -> {}",
            close_side,
            symbol,
            tp_price,
            tp_order_id,
        )

        return {"slOrderId": sl_order_id, "tpOrderId": tp_order_id}

    async def close_position(
        self, symbol: str, side: str, qty: float, current_price: float = 0.0
    ) -> dict:
        """Close a position with a market order on the opposite side.

        Parameters
        ----------
        symbol:
            Trading pair.
        side:
            Current position side. The close order uses the opposite side.
        qty:
            Quantity to close.
        current_price:
            Fallback price if fill price cannot be determined.

        Returns
        -------
        Dict with order result from exchange, including ``fillPrice``.
        """
        close_side = "Sell" if side == "Buy" else "Buy"
        qty = self._round_qty(symbol, qty)

        # Get current price as fallback if not provided
        if current_price == 0.0:
            try:
                tickers = await self._run_sync(self.client.get_tickers)
                current_price = float(
                    next((t["lastPrice"] for t in tickers if t["symbol"] == symbol), 0)
                )
            except Exception:
                pass

        result = await self._run_sync(
            self.client.place_order,
            symbol=symbol,
            side=close_side,
            qty=str(qty),
            order_type="Market",
        )

        # Get actual fill price
        order_id = result.get("orderId", "")
        fill_price = float(result.get("avgPrice", 0))
        if fill_price == 0 and order_id:
            import time as _time
            _time.sleep(0.5)  # Brief wait for fill
            try:
                executions = await self._run_sync(
                    self.client.get_executions, symbol, order_id=order_id
                )
                if executions:
                    fill_price = float(executions[0].get("execPrice", current_price))
            except Exception:
                fill_price = current_price
        result["fillPrice"] = fill_price if fill_price > 0 else current_price

        # Fetch actual PnL and fee from executions
        pnl = 0.0
        fee = 0.0
        if order_id:
            try:
                import time as _time2
                _time2.sleep(1.0)  # Wait for Bybit to process
                executions = await self._run_sync(
                    self.client.get_executions, symbol, order_id=order_id
                )
                for ex in (executions or []):
                    fee += abs(float(ex.get("execFee", 0)))
                # Get closed PnL from API
                closed = await self._run_sync(
                    self.client.get_closed_pnl, symbol, limit=10
                )
                for cp in (closed or []):
                    if cp.get("orderId") == order_id:
                        pnl = float(cp.get("closedPnl", 0))
                        break
            except Exception as exc:
                logger.debug("Failed to fetch close PnL for {}: {}", symbol, exc)
        result["pnl"] = pnl
        result["fee"] = fee

        logger.info(
            "Live CLOSE: {} {:.6f} {} @ {:.4f} pnl={:.6f} fee={:.6f} orderId={}",
            close_side, qty, symbol, result["fillPrice"], pnl, fee,
            result.get("orderId"),
        )
        return result

    async def close_partial(
        self, symbol: str, side: str, qty: float, current_price: float = 0.0
    ) -> dict:
        """Close a partial quantity from the net position.

        Places a market order on the opposite side for the specified qty,
        effectively reducing the net position by that amount. Used by the
        LivePositionLedger flow to close individual micro-positions.

        Parameters
        ----------
        symbol:
            Trading pair.
        side:
            Current position side (``"Buy"`` or ``"Sell"``). The close order
            is placed on the opposite side.
        qty:
            Quantity to close (just this micro-position, not the full net).
        current_price:
            Fallback price if fill price cannot be determined.

        Returns
        -------
        Dict with ``orderId``, ``fillPrice``, and additional metadata.
        """
        close_side = "Sell" if side == "Buy" else "Buy"
        qty = self._round_qty(symbol, qty)

        # Get current price as fallback if not provided
        if current_price == 0.0:
            try:
                tickers = await self._run_sync(self.client.get_tickers)
                current_price = float(
                    next((t["lastPrice"] for t in tickers if t["symbol"] == symbol), 0)
                )
            except Exception:
                pass

        result = await self._run_sync(
            self.client.place_order,
            symbol=symbol,
            side=close_side,
            qty=str(qty),
            order_type="Market",
        )

        # Get actual fill price
        order_id = result.get("orderId", "")
        fill_price = float(result.get("avgPrice", 0))
        if fill_price == 0 and order_id:
            import time as _time
            _time.sleep(0.5)
            try:
                executions = await self._run_sync(
                    self.client.get_executions, symbol, order_id=order_id
                )
                if executions:
                    fill_price = float(executions[0].get("execPrice", current_price))
            except Exception:
                fill_price = current_price
        result["fillPrice"] = fill_price if fill_price > 0 else current_price

        # Check for partial fill
        if order_id:
            try:
                executions = await self._run_sync(
                    self.client.get_executions, symbol, order_id=order_id
                )
                filled_qty = sum(float(e.get("execQty", 0)) for e in (executions or []))
                if 0 < filled_qty < qty * 0.99:
                    logger.warning(
                        "PARTIAL FILL on close_partial for {} {}: requested={:.6f} filled={:.6f}",
                        symbol, close_side, qty, filled_qty,
                    )
                    result["partialFill"] = True
                    result["filledQty"] = filled_qty
            except Exception:
                pass

        # Fetch actual PnL and fee
        pnl = 0.0
        fee = 0.0
        if order_id:
            try:
                import time as _time3
                _time3.sleep(1.0)  # Wait for Bybit to process
                executions = await self._run_sync(
                    self.client.get_executions, symbol, order_id=order_id
                )
                for ex in (executions or []):
                    fee += abs(float(ex.get("execFee", 0)))
                closed = await self._run_sync(
                    self.client.get_closed_pnl, symbol, limit=5
                )
                for cp in (closed or []):
                    if cp.get("orderId") == order_id:
                        pnl = float(cp.get("closedPnl", 0))
                        break
            except Exception as exc:
                logger.debug("Failed to fetch partial close PnL for {}: {}", symbol, exc)
        result["pnl"] = pnl
        result["fee"] = fee

        logger.info(
            "Live CLOSE_PARTIAL: {} {:.6f} {} @ {:.4f} pnl={:.6f} fee={:.6f} orderId={}",
            close_side, qty, symbol, result["fillPrice"], pnl, fee, result.get("orderId"),
        )
        return result

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def cancel_orders(self, symbol: str, order_ids: list) -> None:
        """Cancel a list of conditional orders.

        Parameters
        ----------
        symbol:
            Trading pair.
        order_ids:
            List of order ID strings to cancel.
        """
        for oid in order_ids:
            try:
                await self._run_sync(
                    self.client.cancel_order, symbol, oid
                )
                logger.info("Cancelled order {} for {}", oid, symbol)
            except BybitAPIError as exc:
                # Order may already be filled or cancelled.
                logger.warning(
                    "Failed to cancel order {} for {}: {}",
                    oid,
                    symbol,
                    exc,
                )

    # ------------------------------------------------------------------
    # Position / order queries
    # ------------------------------------------------------------------

    async def get_position(self, symbol: str) -> Optional[dict]:
        """Get the current exchange position for a symbol.

        Returns
        -------
        Position dict from Bybit, or ``None`` if no open position.
        """
        positions = await self._run_sync(
            self.client.get_positions, symbol
        )
        if positions:
            return positions[0]
        return None

    async def get_filled_orders(self, symbol: str) -> list:
        """Check for recently filled conditional orders.

        Returns
        -------
        List of filled order dicts from order history.
        """
        history = await self._run_sync(
            self.client.get_order_history, symbol, 20
        )
        filled = [
            o
            for o in history
            if o.get("orderStatus") == "Filled"
            and o.get("triggerPrice", "0") != "0"
        ]
        return filled

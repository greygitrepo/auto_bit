"""Paper trading executor for simulated order execution.

Provides a complete paper trading environment with realistic simulation
of market orders, slippage, fees, isolated margin, SL/TP monitoring,
and funding rate simulation.

Tasks E-03, E-04, E-05, Gap-E (funding simulation).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger

from src.order.funding_simulator import FundingSimulator


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PaperPosition:
    """A simulated open position with isolated margin."""

    symbol: str
    side: str  # "Buy" | "Sell"
    qty: float
    entry_price: float
    leverage: int
    margin: float  # isolated margin = position_value / leverage
    sl_price: float
    tp_price: float
    sl_order_id: str
    tp_order_id: str
    entered_at: float = field(default_factory=time.time)


@dataclass
class PaperOrder:
    """A pending conditional order (SL or TP)."""

    order_id: str
    symbol: str
    side: str  # "Buy" | "Sell" -- the closing side
    qty: float
    trigger_price: float
    order_type: str  # "stop_loss" | "take_profit"
    created_at: float = field(default_factory=time.time)


@dataclass
class PaperTrade:
    """A completed trade record."""

    symbol: str
    side: str
    qty: float
    entry_price: float
    exit_price: float
    pnl: float
    fee: float
    leverage: int
    exit_type: str  # "market" | "stop_loss" | "take_profit"
    entered_at: float
    exited_at: float


@dataclass
class PaperAccount:
    """Virtual trading account for paper mode."""

    initial_balance: float
    balance: float
    positions: Dict[str, PaperPosition] = field(default_factory=dict)
    pending_orders: Dict[str, PaperOrder] = field(default_factory=dict)
    trades: List[PaperTrade] = field(default_factory=list)
    fees_paid: float = 0.0
    leverage_settings: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Paper Executor
# ---------------------------------------------------------------------------


class PaperExecutor:
    """Simulates order execution for paper trading.

    Provides the same async interface as :class:`LiveExecutor` so the
    :class:`OrderManager` can use either transparently.

    Parameters
    ----------
    config:
        The ``paper`` section from ``config/strategy/asset.yaml``.
        Expected keys: ``fee_rate`` (dict with ``maker``, ``taker``),
        ``slippage_bps`` (int).
    initial_balance:
        Starting account balance in USDT.
    """

    def __init__(self, config: dict, initial_balance: float = 10_000.0,
                 bybit_client=None) -> None:
        self.taker_fee_rate: float = config.get("fee_rate", {}).get("taker", 0.0006)
        self.maker_fee_rate: float = config.get("fee_rate", {}).get("maker", 0.0001)
        self.slippage_bps: int = config.get("slippage_bps", 5)

        self.account = PaperAccount(
            initial_balance=initial_balance,
            balance=initial_balance,
        )
        # Funding rate simulator
        self.funding_simulator = FundingSimulator(config)

        # Track last known prices per symbol for unrealized P&L calculation
        self.last_prices: dict[str, float] = {}

        # Instrument info cache for min qty / qty step / min notional validation
        self._bybit_client = bybit_client
        self._instrument_cache: dict[str, dict] = {}

        logger.info(
            "PaperExecutor initialised: balance={:.2f} USDT, "
            "taker_fee={:.4f}, slippage={}bps",
            initial_balance,
            self.taker_fee_rate,
            self.slippage_bps,
        )

    # ------------------------------------------------------------------
    # Instrument validation (mirrors LiveExecutor)
    # ------------------------------------------------------------------

    def _get_instrument_info(self, symbol: str) -> dict:
        """Fetch and cache instrument info from Bybit API."""
        if symbol not in self._instrument_cache:
            if self._bybit_client is not None:
                try:
                    info = self._bybit_client.get_instrument_info(symbol)
                    self._instrument_cache[symbol] = info
                except Exception:
                    self._instrument_cache[symbol] = {}
            else:
                self._instrument_cache[symbol] = {}
        return self._instrument_cache[symbol]

    def _round_qty(self, symbol: str, qty: float) -> float:
        """Round quantity to instrument's lot size step, enforce minimum."""
        info = self._get_instrument_info(symbol)
        lot_filter = info.get("lotSizeFilter", {})
        qty_step = float(lot_filter.get("qtyStep", "0.01"))
        min_qty = float(lot_filter.get("minOrderQty", "0.01"))
        rounded = int(qty / qty_step) * qty_step
        return max(rounded, min_qty)

    def _check_min_notional(self, symbol: str, qty: float, price: float) -> bool:
        """Check if order meets minimum notional value (typically 5 USDT)."""
        notional = qty * price
        info = self._get_instrument_info(symbol)
        # Bybit linear contracts: minNotionalValue is in lotSizeFilter
        lot_filter = info.get("lotSizeFilter", {})
        min_notional = float(lot_filter.get("minNotionalValue", "5"))
        if notional < min_notional:
            logger.info(
                "Paper REJECT {}: notional {:.4f} < min {:.1f} USDT",
                symbol, notional, min_notional,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _apply_slippage(self, price: float, side: str) -> float:
        """Apply slippage to a fill price.

        Buy orders fill higher (worse); sell orders fill lower (worse).
        """
        factor = self.slippage_bps / 10_000
        if side == "Buy":
            return price * (1 + factor)
        return price * (1 - factor)

    def _calculate_fee(self, notional: float) -> float:
        """Calculate taker fee on a notional value."""
        return notional * self.taker_fee_rate

    @staticmethod
    def _gen_order_id() -> str:
        """Generate a unique paper order ID."""
        return f"paper-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _calculate_pnl(
        side: str, entry_price: float, exit_price: float, qty: float
    ) -> float:
        """Calculate raw P&L (before fees).

        LONG:  (exit - entry) * qty
        SHORT: (entry - exit) * qty
        """
        if side == "Buy":
            return (exit_price - entry_price) * qty
        return (entry_price - exit_price) * qty

    # ------------------------------------------------------------------
    # Public interface (mirrors LiveExecutor)
    # ------------------------------------------------------------------

    async def set_margin_and_leverage(self, symbol: str, leverage: int) -> None:
        """Record leverage setting. No real action needed for paper mode."""
        self.account.leverage_settings[symbol] = leverage
        logger.debug(
            "Paper: set leverage for {} to {}x (isolated)", symbol, leverage
        )

    async def place_market_order(
        self, symbol: str, side: str, qty: float, current_price: float
    ) -> dict:
        """Simulate a market order fill with slippage and fees.

        Parameters
        ----------
        symbol:
            Trading pair.
        side:
            ``"Buy"`` or ``"Sell"``.
        qty:
            Position quantity in base currency.
        current_price:
            Current market price to apply slippage against.

        Returns
        -------
        Dict with ``orderId``, ``fillPrice``, ``fee``, ``side``, ``qty``.
        """
        # Round qty to instrument step size and enforce minimum
        qty = self._round_qty(symbol, qty)

        fill_price = self._apply_slippage(current_price, side)

        # Check minimum notional value (e.g. 5 USDT)
        if not self._check_min_notional(symbol, qty, fill_price):
            return {
                "orderId": "",
                "fillPrice": 0.0,
                "fee": 0.0,
                "side": side,
                "qty": 0.0,
                "rejected": True,
                "reason": "below_min_notional",
            }

        notional = fill_price * qty
        fee = self._calculate_fee(notional)

        leverage = self.account.leverage_settings.get(symbol, 1)
        margin = notional / leverage

        # Check sufficient balance
        if self.account.balance < margin + fee:
            logger.info("Paper REJECT {}: insufficient balance {:.4f} < {:.4f}",
                        symbol, self.account.balance, margin + fee)
            return {
                "orderId": "",
                "fillPrice": 0.0,
                "fee": 0.0,
                "side": side,
                "qty": 0.0,
                "rejected": True,
                "reason": "insufficient_balance",
            }

        # Deduct margin and fee from balance.
        self.account.balance -= margin + fee
        self.account.fees_paid += fee

        # Create position.
        # Use composite key if a position already exists for this symbol
        # (supports multiple grid micro-positions per symbol).
        order_id = self._gen_order_id()
        position = PaperPosition(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=fill_price,
            leverage=leverage,
            margin=margin,
            sl_price=0.0,
            tp_price=0.0,
            sl_order_id="",
            tp_order_id="",
        )
        # Always use order_id as key to support multiple positions per symbol
        pos_key = order_id
        self.account.positions[pos_key] = position

        logger.info(
            "Paper FILL: {} {} {:.6f} {} @ {:.4f} (slippage from {:.4f}) | "
            "fee={:.4f} margin={:.4f} balance={:.2f}",
            order_id,
            side,
            qty,
            symbol,
            fill_price,
            current_price,
            fee,
            margin,
            self.account.balance,
        )

        return {
            "orderId": order_id,
            "fillPrice": fill_price,
            "fee": fee,
            "side": side,
            "qty": qty,
            "symbol": symbol,
            "margin": margin,
        }

    async def place_sl_tp(
        self,
        symbol: str,
        side: str,
        qty: float,
        sl_price: float,
        tp_price: float,
    ) -> dict:
        """Record SL/TP levels for monitoring.

        Parameters
        ----------
        symbol:
            Trading pair.
        side:
            Position side (``"Buy"`` or ``"Sell"``).
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
        # The closing side is the opposite of the position side.
        close_side = "Sell" if side == "Buy" else "Buy"

        sl_order_id = self._gen_order_id()
        tp_order_id = self._gen_order_id()

        sl_order = PaperOrder(
            order_id=sl_order_id,
            symbol=symbol,
            side=close_side,
            qty=qty,
            trigger_price=sl_price,
            order_type="stop_loss",
        )
        tp_order = PaperOrder(
            order_id=tp_order_id,
            symbol=symbol,
            side=close_side,
            qty=qty,
            trigger_price=tp_price,
            order_type="take_profit",
        )

        self.account.pending_orders[sl_order_id] = sl_order
        self.account.pending_orders[tp_order_id] = tp_order

        # Attach to position if it exists.
        pos = self.account.positions.get(symbol)
        if pos is not None:
            pos.sl_price = sl_price
            pos.tp_price = tp_price
            pos.sl_order_id = sl_order_id
            pos.tp_order_id = tp_order_id

        logger.info(
            "Paper SL/TP set for {}: SL={:.4f} ({}), TP={:.4f} ({})",
            symbol,
            sl_price,
            sl_order_id,
            tp_price,
            tp_order_id,
        )

        return {"slOrderId": sl_order_id, "tpOrderId": tp_order_id}

    async def check_sl_tp(self, candle: dict) -> list:
        """Check if any SL/TP was triggered by a candle's high/low.

        Logic:
        - LONG: low <= SL -> SL fill at sl_price; high >= TP -> TP fill at tp_price
        - SHORT: high >= SL -> SL fill at sl_price; low <= TP -> TP fill at tp_price
        - If both triggered in same candle, SL takes priority (conservative).

        Parameters
        ----------
        candle:
            Dict with at minimum ``high``, ``low``, ``symbol`` keys.
            Prices should be floats.

        Returns
        -------
        List of fill dicts: ``{symbol, side, fill_price, fill_type, order_id, pnl, fee}``.
        """
        fills: list[dict] = []
        high = float(candle.get("high", 0))
        low = float(candle.get("low", 0))
        close = float(candle.get("close", 0))
        candle_symbol = candle.get("symbol", "")

        # Track last known price for unrealized P&L
        if candle_symbol and close > 0:
            self.last_prices[candle_symbol] = close

        # Iterate over a snapshot since we may mutate during iteration.
        # Keys may be order_ids (grid) or symbols (legacy).
        for pos_key, pos in list(self.account.positions.items()):
            symbol = pos.symbol  # Always use pos.symbol for matching
            if pos.sl_price <= 0 and pos.tp_price <= 0:
                continue

            sl_triggered = False
            tp_triggered = False

            if pos.side == "Buy":  # LONG
                if pos.sl_price > 0 and low <= pos.sl_price:
                    sl_triggered = True
                if pos.tp_price > 0 and high >= pos.tp_price:
                    tp_triggered = True
            else:  # SHORT
                if pos.sl_price > 0 and high >= pos.sl_price:
                    sl_triggered = True
                if pos.tp_price > 0 and low <= pos.tp_price:
                    tp_triggered = True

            if not sl_triggered and not tp_triggered:
                continue

            # SL takes priority when both trigger on the same candle.
            if sl_triggered:
                fill_price = pos.sl_price
                fill_type = "stop_loss"
                order_id = pos.sl_order_id
            else:
                fill_price = pos.tp_price
                fill_type = "take_profit"
                order_id = pos.tp_order_id

            raw_pnl = self._calculate_pnl(
                pos.side, pos.entry_price, fill_price, pos.qty
            )
            notional = fill_price * pos.qty
            fee = self._calculate_fee(notional)
            net_pnl = raw_pnl - fee

            # Update account balance: return margin + net P&L.
            self.account.balance += pos.margin + net_pnl
            self.account.fees_paid += fee

            # Record trade.
            trade = PaperTrade(
                symbol=symbol,
                side=pos.side,
                qty=pos.qty,
                entry_price=pos.entry_price,
                exit_price=fill_price,
                pnl=net_pnl,
                fee=fee,
                leverage=pos.leverage,
                exit_type=fill_type,
                entered_at=pos.entered_at,
                exited_at=time.time(),
            )
            self.account.trades.append(trade)

            logger.info(
                "Paper {} TRIGGERED for {} {}: entry={:.4f} exit={:.4f} "
                "pnl={:.4f} fee={:.4f} balance={:.2f}",
                fill_type.upper(),
                pos.side,
                symbol,
                pos.entry_price,
                fill_price,
                net_pnl,
                fee,
                self.account.balance,
            )

            fill_info = {
                "symbol": symbol,
                "side": pos.side,
                "fill_price": fill_price,
                "fill_type": fill_type,
                "order_id": order_id,
                "pnl": net_pnl,
                "fee": fee,
                "qty": pos.qty,
                "entry_price": pos.entry_price,
                "leverage": pos.leverage,
                "margin": pos.margin,
            }
            fills.append(fill_info)

            # Clean up: remove position and its pending orders.
            self.account.positions.pop(pos_key, None)
            self.account.pending_orders.pop(pos.sl_order_id, None)
            self.account.pending_orders.pop(pos.tp_order_id, None)

        return fills

    async def close_position(
        self, symbol: str, side: str, qty: float, current_price: float
    ) -> dict:
        """Simulate closing a position with a market order.

        Parameters
        ----------
        symbol:
            Trading pair.
        side:
            Original position side (``"Buy"`` or ``"Sell"``).
        qty:
            Quantity to close.
        current_price:
            Current market price for fill simulation.

        Returns
        -------
        Dict with ``fillPrice``, ``pnl``, ``fee``, ``orderId``.
        """
        close_side = "Sell" if side == "Buy" else "Buy"
        fill_price = self._apply_slippage(current_price, close_side)

        raw_pnl = self._calculate_pnl(side, 0.0, fill_price, qty)
        notional = fill_price * qty
        fee = self._calculate_fee(notional)

        # Find position by key: try exact symbol, then order_id keys
        pos = self.account.positions.get(symbol)
        pos_key = symbol
        if pos is None:
            # Search by symbol match in position objects (order_id keyed)
            for k, v in list(self.account.positions.items()):
                if v.symbol == symbol:
                    pos = v
                    pos_key = k
                    break
        if pos is not None:
            raw_pnl = self._calculate_pnl(
                pos.side, pos.entry_price, fill_price, pos.qty
            )
            net_pnl = raw_pnl - fee

            # Return margin + net P&L to balance.
            self.account.balance += pos.margin + net_pnl
            self.account.fees_paid += fee

            # Record trade.
            trade = PaperTrade(
                symbol=symbol,
                side=pos.side,
                qty=pos.qty,
                entry_price=pos.entry_price,
                exit_price=fill_price,
                pnl=net_pnl,
                fee=fee,
                leverage=pos.leverage,
                exit_type="market",
                entered_at=pos.entered_at,
                exited_at=time.time(),
            )
            self.account.trades.append(trade)

            logger.info(
                "Paper CLOSE: {} {} @ {:.4f} (from {:.4f}) | "
                "pnl={:.4f} fee={:.4f} balance={:.2f}",
                pos.side,
                symbol,
                fill_price,
                current_price,
                net_pnl,
                fee,
                self.account.balance,
            )

            # Remove position and pending SL/TP orders.
            self.account.pending_orders.pop(pos.sl_order_id, None)
            self.account.pending_orders.pop(pos.tp_order_id, None)
            self.account.positions.pop(pos_key, None)

            return {
                "orderId": self._gen_order_id(),
                "fillPrice": fill_price,
                "pnl": net_pnl,
                "fee": fee,
                "side": close_side,
                "qty": qty,
            }

        # No tracked position -- just simulate the fill.
        net_pnl = -fee
        self.account.fees_paid += fee

        logger.warning(
            "Paper CLOSE: no tracked position for {} -- fill simulated only",
            symbol,
        )
        return {
            "orderId": self._gen_order_id(),
            "fillPrice": fill_price,
            "pnl": net_pnl,
            "fee": fee,
            "side": close_side,
            "qty": qty,
        }

    async def close_position_by_key(
        self, pos_key: str, current_price: float
    ) -> dict:
        """Close a specific position by its executor key (orderId).

        Used by grid_manager to close the exact micro-position.
        """
        pos = self.account.positions.get(pos_key)
        if pos is None:
            logger.warning("Paper CLOSE by key: no position for key={}", pos_key)
            return {"orderId": "", "fillPrice": current_price, "pnl": 0.0, "fee": 0.0, "side": "", "qty": 0}

        close_side = "Sell" if pos.side == "Buy" else "Buy"
        fill_price = self._apply_slippage(current_price, close_side)
        raw_pnl = self._calculate_pnl(pos.side, pos.entry_price, fill_price, pos.qty)
        notional = fill_price * pos.qty
        fee = self._calculate_fee(notional)
        net_pnl = raw_pnl - fee

        self.account.balance += pos.margin + net_pnl
        self.account.fees_paid += fee

        trade = PaperTrade(
            symbol=pos.symbol, side=pos.side, qty=pos.qty,
            entry_price=pos.entry_price, exit_price=fill_price,
            pnl=net_pnl, fee=fee, leverage=pos.leverage,
            exit_type="market", entered_at=pos.entered_at, exited_at=time.time(),
        )
        self.account.trades.append(trade)

        logger.info(
            "Paper CLOSE by key: {} {} @ {:.4f} | pnl={:.4f} fee={:.4f} balance={:.2f}",
            pos.side, pos.symbol, fill_price, net_pnl, fee, self.account.balance,
        )

        self.account.pending_orders.pop(pos.sl_order_id, None)
        self.account.pending_orders.pop(pos.tp_order_id, None)
        self.account.positions.pop(pos_key, None)

        return {
            "orderId": self._gen_order_id(),
            "fillPrice": fill_price,
            "pnl": net_pnl,
            "fee": fee,
            "side": close_side,
            "qty": pos.qty,
        }

    async def cancel_orders(self, symbol: str, order_ids: list) -> None:
        """Remove specified orders from the pending orders map.

        Parameters
        ----------
        symbol:
            Trading pair (for logging).
        order_ids:
            List of order ID strings to cancel.
        """
        for oid in order_ids:
            removed = self.account.pending_orders.pop(oid, None)
            if removed:
                logger.debug("Paper: cancelled order {} for {}", oid, symbol)
            else:
                logger.debug(
                    "Paper: order {} not found (already filled/cancelled)", oid
                )

    async def get_position(self, symbol: str) -> Optional[dict]:
        """Get current paper position for a symbol.

        Returns
        -------
        Dict representation of the position, or ``None`` if no position.
        """
        pos = self.account.positions.get(symbol)
        if pos is None:
            # Search by symbol match in position objects (order_id keyed)
            for k, v in self.account.positions.items():
                if v.symbol == symbol:
                    pos = v
                    break
        if pos is None:
            return None

        return {
            "symbol": pos.symbol,
            "side": pos.side,
            "size": str(pos.qty),
            "entryPrice": str(pos.entry_price),
            "leverage": str(pos.leverage),
            "positionMargin": str(pos.margin),
            "stopLoss": str(pos.sl_price),
            "takeProfit": str(pos.tp_price),
        }

    async def get_filled_orders(self, symbol: str) -> list:
        """Return recently filled orders for a symbol.

        For paper mode this always returns an empty list; SL/TP fills are
        detected via :meth:`check_sl_tp` instead.
        """
        return []

    # ------------------------------------------------------------------
    # Account query helpers
    # ------------------------------------------------------------------

    def get_balance(self) -> float:
        """Return the current paper account balance (free balance)."""
        return self.account.balance

    def get_equity(self) -> float:
        """Return total equity including unrealised P&L on open positions.

        Note: this requires current market prices which are not available
        here, so we return balance + sum of margins as a proxy.
        """
        total_margin = sum(p.margin for p in self.account.positions.values())
        return self.account.balance + total_margin

    def get_all_positions(self) -> list:
        """Return all open paper positions as a list of dicts."""
        result = []
        for pos in self.account.positions.values():
            result.append(
                {
                    "symbol": pos.symbol,
                    "side": pos.side,
                    "size": str(pos.qty),
                    "entryPrice": str(pos.entry_price),
                    "leverage": str(pos.leverage),
                    "positionMargin": str(pos.margin),
                    "stopLoss": str(pos.sl_price),
                    "takeProfit": str(pos.tp_price),
                    "sl_order_id": pos.sl_order_id,
                    "tp_order_id": pos.tp_order_id,
                    "entered_at": pos.entered_at,
                }
            )
        return result

    def apply_funding(
        self,
        funding_rates: dict[str, float],
        current_time: float | None = None,
    ) -> list[dict]:
        """Apply funding charges/credits to all open paper positions.

        Called by the order process on each cycle to check if funding should
        be applied. Updates the funding simulator's rates and checks all
        open positions for funding charges.

        Parameters
        ----------
        funding_rates:
            Dict mapping symbol -> current funding rate.
        current_time:
            Override for testing (unix timestamp).

        Returns
        -------
        List of funding charge dicts from FundingSimulator.check_and_apply.
        """
        # Update rates
        for symbol, rate in funding_rates.items():
            self.funding_simulator.update_rate(symbol, rate)

        # Build position list from open positions
        positions = []
        for pos in self.account.positions.values():
            positions.append({
                "symbol": pos.symbol,
                "side": pos.side,
                "size": pos.qty,
                "entry_price": pos.entry_price,
                "leverage": pos.leverage,
            })

        if not positions:
            return []

        charges = self.funding_simulator.check_and_apply(positions, current_time)

        # Apply charges to account balance
        for charge in charges:
            payment = charge["funding_payment"]
            self.account.balance += payment
            logger.info(
                "Paper FUNDING: {} {} rate={:.6f} payment={:.4f} balance={:.2f}",
                charge["symbol"],
                charge["side"],
                charge["rate"],
                payment,
                self.account.balance,
            )

        return charges

    def get_trade_history(self) -> List[dict]:
        """Return all completed paper trades as dicts."""
        return [
            {
                "symbol": t.symbol,
                "side": t.side,
                "qty": t.qty,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "fee": t.fee,
                "leverage": t.leverage,
                "exit_type": t.exit_type,
                "entered_at": t.entered_at,
                "exited_at": t.exited_at,
            }
            for t in self.account.trades
        ]

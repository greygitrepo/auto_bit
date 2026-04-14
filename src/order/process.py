"""P3: Order Manager Process.

Receives trade signals from P2 (Strategy Engine), evaluates them through
the asset strategy layer, executes approved orders, monitors open positions
for SL/TP fills, trailing stops, and time limits, and reports position
state back to P2 and the Orchestrator.

Task E-09.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import queue
import time
from typing import Any, Dict, List, Optional

from loguru import logger

from src.collector.bybit_client import BybitClient
from src.order.grid_manager import GridPositionManager
from src.order.live_executor import LiveExecutor
from src.order.order_manager import OrderManager
from src.order.paper_executor import PaperExecutor
from src.strategy.asset.base import DailyStats
from src.strategy.asset.fixed_ratio import (
    ConsecutiveLossTracker,
    DrawdownManager,
    FixedRatioStrategy,
)
from src.strategy.asset.grid_sizing import GridSizingStrategy
from src.strategy.position.base import PositionSignal, SignalType, TrailingStopState
from src.strategy.position.momentum_scalper import TimeLimitManager, TrailingStopManager
from src.tracker.position_tracker import PositionTracker
from src.utils.db import DatabaseManager
from src.utils.logger import setup_logger
from src.utils.messages import (
    ControlMessage,
    GridSignalMessage,
    PositionUpdateMessage,
    SignalMessage,
    SlotAvailableMessage,
)


# Interval constants (seconds)
_POSITION_MONITOR_INTERVAL = 3.0
_POSITION_UPDATE_INTERVAL = 10.0
_QUEUE_POLL_INTERVAL = 0.1


class OrderManagerProcess(multiprocessing.Process):
    """P3: Order Manager Process.

    Receives: SignalMessages from P2 (trade signals)
              ControlMessages from Orchestrator (stop, check positions)
    Sends:    PositionUpdateMessages to P2 (position state feedback)
              SlotAvailableMessages to Orchestrator (when a position closes)
    """

    def __init__(
        self,
        config: dict,
        credentials: dict,
        signal_queue: multiprocessing.Queue,
        control_queue: multiprocessing.Queue,
        position_update_queue: multiprocessing.Queue,
        event_queue: multiprocessing.Queue,
    ) -> None:
        super().__init__(name="P3-OrderManager", daemon=True)
        self.config = config
        self.credentials = credentials

        # Queues
        self.signal_queue = signal_queue
        self.control_queue = control_queue
        self.position_update_queue = position_update_queue
        self.event_queue = event_queue

        # Runtime state (initialised in run)
        self._running = False

    def run(self) -> None:
        """Process entry point -- sets up logger and runs the async main loop."""
        setup_logger("p3_order")
        logger.info("P3 OrderManagerProcess starting")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except KeyboardInterrupt:
            logger.info("P3 received KeyboardInterrupt, shutting down")
        except Exception as exc:
            logger.exception("P3 fatal error: {}", exc)
        finally:
            loop.close()
            logger.info("P3 OrderManagerProcess stopped")

    # ------------------------------------------------------------------
    # Async main
    # ------------------------------------------------------------------

    async def _main(self) -> None:
        """Initialise components and run the main event loop."""
        mode = self.config.get("mode", "paper")
        asset_config = self.config.get("asset", {})
        position_config = self.config.get("position", {})
        paper_config = asset_config.get("paper", {})
        capital_config = asset_config.get("capital", {})
        initial_balance = capital_config.get("initial_balance", 10_000.0)

        # 1. Create executor
        if mode == "live":
            bybit_client = BybitClient(
                api_key=self.credentials.get("api_key"),
                api_secret=self.credentials.get("api_secret"),
            )
            executor = LiveExecutor(bybit_client)
            logger.info("P3 using LiveExecutor")
        else:
            # Create a read-only Bybit client for instrument info (min qty, etc.)
            paper_bybit = None
            try:
                api_key = self.credentials.get("api_key")
                api_secret = self.credentials.get("api_secret")
                if api_key:
                    paper_bybit = BybitClient(api_key=api_key, api_secret=api_secret)
            except Exception:
                pass
            executor = PaperExecutor(paper_config, initial_balance=initial_balance,
                                     bybit_client=paper_bybit)
            bybit_client = paper_bybit
            logger.info("P3 using PaperExecutor (balance={:.2f}, instrument_check={})",
                        initial_balance, paper_bybit is not None)

        # 2. Create DB, OrderManager, PositionTracker
        db = DatabaseManager()
        self._order_manager = OrderManager(mode=mode, executor=executor, db=db)
        self._position_tracker = PositionTracker(db=db, mode=mode)

        # 3. Create asset strategy and risk managers
        self._asset_strategy = FixedRatioStrategy(asset_config)

        # 3b. Create grid position manager if grid strategy is active
        grid_config = self.config.get("grid", {})
        self._strategy_mode = grid_config.get("active", "") if grid_config else ""
        self._grid_manager: Optional[GridPositionManager] = None
        if self._strategy_mode == "grid_bias":
            grid_sizing = GridSizingStrategy(grid_config)
            self._grid_manager = GridPositionManager(
                executor=executor,
                position_tracker=self._position_tracker,
                sizing=grid_sizing,
                mode=mode,
                initial_balance=initial_balance,
            )
            logger.info("P3 grid position manager initialized")

            # Restore grid position mappings from DB
            open_positions = self._position_tracker.get_open_positions()
            restored = self._grid_manager.restore_from_positions(open_positions)
            if restored:
                logger.info("P3 restored {} grid position mappings from DB", restored)

            # Pre-order manager: places limit orders ahead of time on Bybit
            self._pre_order_manager = None
            if mode == "live" and bybit_client is not None:
                from src.order.grid_pre_order import GridPreOrderManager
                grid_gb = grid_config.get("strategies", {}).get("grid_bias", {})
                self._pre_order_manager = GridPreOrderManager(
                    executor=executor,
                    bybit_client=bybit_client,
                    position_tracker=self._position_tracker,
                    sizing=grid_sizing,
                    mode=mode,
                    initial_balance=initial_balance,
                    leverage=grid_gb.get("leverage", 6),
                    qty_per_level_pct=grid_gb.get("qty_per_level_pct", 5.0),
                )
                logger.info("P3 grid pre-order manager initialized (live mode)")

        # 4. State
        self._mode = mode
        self._executor = executor
        self._db = db
        self._bybit_client = bybit_client
        self._initial_balance = initial_balance
        self._trailing_stops: Dict[str, TrailingStopState] = {}
        self._position_config = position_config
        self._running = True
        self._processed_fill_ids: set = set()

        # REST client for fetching current prices (paper mode)
        if mode == "paper":
            api_key = self.credentials.get("api_key") or None
            api_secret = self.credentials.get("api_secret") or None
            self._rest_client = BybitClient(api_key=api_key, api_secret=api_secret)
            self._ticker_cache: Dict[str, float] = {}
            self._last_ticker_fetch: float = 0.0
            self._funding_rate_cache: Dict[str, float] = {}
            self._last_funding_rate_fetch: float = 0.0
        else:
            self._rest_client = bybit_client
            self._ticker_cache = {}
            self._last_ticker_fetch = 0.0
            self._funding_rate_cache = {}
            self._last_funding_rate_fetch = 0.0

        # Determine current balance
        if mode == "live" and bybit_client is not None:
            try:
                wallet = await asyncio.get_event_loop().run_in_executor(
                    None, bybit_client.get_wallet_balance
                )
                self._initial_balance = float(wallet.get("totalWalletBalance", initial_balance))
            except Exception as exc:
                logger.warning("Failed to fetch live wallet balance: {}", exc)

        logger.info("P3 initialised: mode={}, initial_balance={:.2f}", mode, self._initial_balance)

        # 4b. Reconstruct trailing stops for existing open positions
        trailing_config = position_config.get("exit", {}).get("trailing_stop", {})
        activation_r = trailing_config.get("activation_r", 1.0)
        open_positions = self._position_tracker.get_open_positions()
        for pos in open_positions:
            symbol = pos.get("symbol", "")
            entry_price = float(pos.get("entry_price", 0))
            sl_price = float(pos.get("stop_loss", 0))
            side = pos.get("side", "Buy")
            side_str = "LONG" if side == "Buy" else "SHORT"
            sl_distance = abs(entry_price - sl_price) if sl_price > 0 else entry_price * 0.01
            if symbol and entry_price > 0:
                self._trailing_stops[symbol] = TrailingStopManager.create_initial_state(
                    entry_price=entry_price,
                    sl_distance=sl_distance,
                    side=side_str,
                    activation_r=activation_r,
                )
                logger.info(
                    "P3 reconstructed trailing stop for {} ({}): entry={:.4f} activation={:.4f}",
                    symbol, side_str, entry_price, self._trailing_stops[symbol].activation_price,
                )
        logger.info("P3 reconstructed trailing stops for {} existing positions", len(open_positions))

        # 5. Main loop
        last_monitor_time = 0.0
        last_update_time = 0.0

        while self._running:
            now = time.time()

            # a. Check control queue
            self._drain_control_queue()
            if not self._running:
                break

            # b. Check signal queue for new signals
            await self._drain_signal_queue()

            # c. Monitor positions periodically (skip for grid strategy —
            #    grid TP/SL is handled by GridEngine in P2, not by P3 monitor)
            if self._grid_manager is None and now - last_monitor_time >= _POSITION_MONITOR_INTERVAL:
                await self._monitor_positions()
                last_monitor_time = now

            # c2. For grid mode: monitor positions + sync + funding + pre-order fills
            if self._grid_manager is not None and now - last_monitor_time >= _POSITION_MONITOR_INTERVAL:
                # Check pre-order fills (limit orders placed ahead of time)
                if self._pre_order_manager is not None:
                    try:
                        fills = await self._pre_order_manager.check_fills()
                        for fill in fills:
                            sym = fill["symbol"]
                            idx = fill["level_index"]
                            fill_price = fill["fill_price"]
                            qty = fill["qty"]
                            fee = fill["fee"]
                            side = fill["side"]
                            tp_price = fill.get("tp_price", 0)

                            # Record position in tracker
                            position_id = self._position_tracker.add_position({
                                "mode": self._mode,
                                "symbol": sym,
                                "side": side,
                                "size": qty,
                                "entry_price": fill_price,
                                "leverage": self._pre_order_manager._leverage,
                                "stop_loss": fill.get("sl_price", 0),
                                "take_profit": tp_price,
                                "margin": fill_price * qty / self._pre_order_manager._leverage,
                                "unrealized_pnl": 0.0,
                                "strategy": "grid_bias",
                                "scanner_direction": "",
                                "entered_at": int(time.time()),
                            })

                            # Place TP limit order
                            if tp_price > 0:
                                await self._pre_order_manager.place_tp_order(
                                    sym, idx, side, qty, tp_price,
                                )

                            logger.info(
                                "Pre-order FILL: {} {} idx={} @ {:.6f} qty={:.6f} pos_id={}",
                                sym, side, idx, fill_price, qty, position_id,
                            )

                        # Check TP fills
                        tp_fills = await self._pre_order_manager.check_tp_fills()
                        for tp in tp_fills:
                            sym = tp["symbol"]
                            tp_pnl = tp.get("pnl", 0)
                            tp_fee = tp.get("fee", 0)
                            tp_price = tp.get("fill_price", 0)
                            level_idx = tp.get("level_index", 0)

                            # Find and close the matching position
                            for pos in self._position_tracker.get_open_positions():
                                if pos.get("symbol") == sym and pos.get("strategy") == "grid_bias":
                                    self._position_tracker.close_position(
                                        position_id=pos["id"],
                                        exit_price=tp_price,
                                        exit_reason="pre_order_tp",
                                        exit_type="take_profit",
                                        fee=tp_fee,
                                        pnl_override=tp_pnl if tp_pnl != 0 else None,
                                    )
                                    self._notify_slot_available()
                                    # Track for auto-ban
                                    self._pre_order_manager.record_trade_result(sym, tp_pnl)
                                    logger.info(
                                        "Pre-order TP: {} idx={} pnl={:+.6f} fee={:.6f}",
                                        sym, level_idx, tp_pnl, tp_fee,
                                    )
                                    break

                    except Exception as exc:
                        logger.error("Pre-order check failed: {}", exc)

                # Monitor SL/TP for grid positions (P3-side, no server-side SL)
                await self._monitor_positions()

                if self._mode == "paper" and isinstance(self._executor, PaperExecutor):
                    self._apply_paper_funding()
                    self._db.set_state("current_balance_paper", str(round(self._executor.account.balance, 4)))
                    self._db.set_state("initial_balance_paper", str(round(self._executor.account.initial_balance, 4)))

                if self._mode == "live":
                    await self._sync_exchange_positions()

                last_monitor_time = now

            # d. Send position updates to P2 periodically
            if now - last_update_time >= _POSITION_UPDATE_INTERVAL:
                self._send_position_update()
                last_update_time = now

            # Brief sleep to avoid busy-spinning
            await asyncio.sleep(_QUEUE_POLL_INTERVAL)

        # Cancel all pre-orders on shutdown
        if self._pre_order_manager is not None:
            try:
                await self._pre_order_manager.cancel_all()
                logger.info("P3 cancelled all pre-orders on shutdown")
            except Exception as exc:
                logger.error("P3 pre-order cancel on shutdown failed: {}", exc)

        logger.info("P3 main loop exited")

    # ------------------------------------------------------------------
    # Queue draining
    # ------------------------------------------------------------------

    def _drain_control_queue(self) -> None:
        """Process all pending control messages."""
        while True:
            try:
                msg = self.control_queue.get_nowait()
            except queue.Empty:
                break

            if not isinstance(msg, ControlMessage):
                logger.warning("P3 ignoring non-ControlMessage: {}", type(msg).__name__)
                continue

            command = msg.command
            logger.info("P3 received control command: {}", command)

            if command == "stop":
                self._running = False
            elif command == "health_check":
                logger.info("P3 health check: OK, positions={}",
                            len(self._position_tracker.get_open_positions()))
            else:
                logger.debug("P3 ignoring unknown control command: {}", command)

    async def _drain_signal_queue(self) -> None:
        """Process all pending signal messages.

        Handles both legacy SignalMessages and GridSignalMessages.
        CLOSE signals are processed immediately (time-sensitive).
        LONG/SHORT signals are batched and sorted by confidence descending
        so that the highest-quality signals fill limited position slots first.
        """
        entry_signals: list[SignalMessage] = []
        grid_signals: list[GridSignalMessage] = []

        while True:
            try:
                msg = self.signal_queue.get_nowait()
            except queue.Empty:
                break

            if isinstance(msg, GridSignalMessage):
                grid_signals.append(msg)
                continue

            if not isinstance(msg, SignalMessage):
                logger.warning("P3 ignoring non-SignalMessage: {}", type(msg).__name__)
                continue

            # CLOSE and HOLD signals: process immediately
            if msg.signal in ("CLOSE", "HOLD"):
                await self._handle_signal(msg)
            else:
                entry_signals.append(msg)

        # Process grid signals
        if grid_signals and self._grid_manager is not None:
            current_balance = self._get_current_balance()
            open_positions = self._position_tracker.get_open_positions()
            daily_stats = self._position_tracker.get_daily_stats()

            for gmsg in grid_signals:
                # Handle SETUP: place pre-orders on Bybit
                if gmsg.action == "SETUP" and self._pre_order_manager is not None:
                    try:
                        await self._pre_order_manager.place_grid_orders(
                            symbol=gmsg.symbol,
                            levels=gmsg.levels,
                            current_balance=current_balance,
                            qty_per_level=gmsg.qty_per_level,
                            leverage=gmsg.leverage,
                        )
                        logger.info("Pre-orders placed for {}: {} levels",
                                    gmsg.symbol, len(gmsg.levels))
                    except Exception as exc:
                        logger.error("Pre-order setup failed for {}: {}", gmsg.symbol, exc)
                    continue

                # Handle RECENTER: cancel existing pre-orders first
                if gmsg.action == "RECENTER" and self._pre_order_manager is not None:
                    try:
                        await self._pre_order_manager.cancel_symbol_orders(gmsg.symbol)
                    except Exception:
                        pass

                # In pre-order mode, skip FILL/TP_HIT from P2 — pre-order handles fills
                if self._pre_order_manager is not None and gmsg.action in ("FILL", "TP_HIT"):
                    continue

                try:
                    update = await self._grid_manager.handle_grid_signal(
                        gmsg, current_balance, open_positions, daily_stats,
                    )
                    if update is not None:
                        logger.info(
                            "P3 grid {} result: {} {} level={} pnl={:.6f}",
                            gmsg.action, update.action, gmsg.symbol,
                            gmsg.level_index, update.pnl,
                        )
                        # Notify slot available on closes
                        if update.action == "CLOSED":
                            self._notify_slot_available()
                except Exception as exc:
                    logger.error("P3 grid signal error: {}", exc)

        # Sort entry signals by confidence (highest first)
        if entry_signals:
            entry_signals.sort(key=lambda s: s.confidence, reverse=True)
            logger.info(
                "P3 processing {} entry signals (top conf={:.3f}, bottom conf={:.3f})",
                len(entry_signals),
                entry_signals[0].confidence,
                entry_signals[-1].confidence,
            )
            for msg in entry_signals:
                await self._handle_signal(msg)

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------

    async def _handle_signal(self, msg: SignalMessage) -> None:
        """Process a trade signal through asset strategy and execute if approved."""
        symbol = msg.symbol
        signal_type = msg.signal  # "LONG", "SHORT", "CLOSE", "HOLD"

        if signal_type == "HOLD":
            return

        logger.info(
            "P3 processing signal: {} {} confidence={:.3f}",
            signal_type, symbol, msg.confidence,
        )

        # Handle CLOSE signals
        if signal_type == "CLOSE":
            await self._handle_close_signal(msg)
            return

        # Short confidence threshold: Short 승률이 현저히 낮으므로
        # 높은 confidence가 아니면 Short 진입을 거부한다.
        active_strategy = self._position_config.get("active", "momentum_scalper")
        strategy_cfg = self._position_config.get("strategies", {}).get(active_strategy, {})
        short_min_confidence = strategy_cfg.get("short_min_confidence", 0.75)
        if signal_type == "SHORT" and msg.confidence < short_min_confidence:
            logger.info(
                "P3 short filtered for {}: confidence {:.3f} < {:.3f} threshold",
                symbol, msg.confidence, short_min_confidence,
            )
            return

        # Dual mode: skip if grid already has positions on this symbol
        if self._grid_manager is not None:
            grid_symbols = set(
                k[0] for k in self._grid_manager._level_positions.keys()
            )
            if symbol in grid_symbols:
                logger.debug("P3 position signal skipped {}: grid has open levels", symbol)
                return

        # For LONG / SHORT: evaluate through asset strategy
        current_balance = self._get_current_balance()
        open_positions = self._position_tracker.get_open_positions()
        daily_stats = self._position_tracker.get_daily_stats()

        order_request = self._asset_strategy.evaluate(
            signal=msg,
            initial_balance=self._initial_balance,
            current_balance=current_balance,
            open_positions=open_positions,
            daily_stats=daily_stats,
        )

        if not order_request.approved:
            logger.info(
                "P3 order rejected for {}: {}", symbol, order_request.reject_reason
            )
            return

        # Build PositionSignal for OrderManager
        pos_signal = PositionSignal(
            symbol=symbol,
            signal=SignalType.LONG if signal_type == "LONG" else SignalType.SHORT,
            entry_price=msg.entry_price,
            stop_loss=msg.stop_loss,
            take_profit=msg.take_profit,
            confidence=msg.confidence,
            strategy=msg.strategy,
            timeframe="5m",
            suggested_side=msg.suggested_side,
            reason=msg.reason,
        )

        # Execute order
        result = await self._order_manager.execute_order(order_request, pos_signal)

        if result["success"]:
            logger.info(
                "P3 order executed: {} {} position_id={}",
                signal_type, symbol, result["position_id"],
            )

            # Initialise trailing stop state for this position
            entry_price = msg.entry_price
            sl_distance = abs(entry_price - msg.stop_loss)
            side_str = "LONG" if signal_type == "LONG" else "SHORT"

            trailing_config = self._position_config.get("exit", {}).get("trailing_stop", {})
            activation_r = trailing_config.get("activation_r", 1.0)

            self._trailing_stops[symbol] = TrailingStopManager.create_initial_state(
                entry_price=entry_price,
                sl_distance=sl_distance,
                side=side_str,
                activation_r=activation_r,
            )
        else:
            logger.error(
                "P3 order execution failed for {}: {}", symbol, result["error"]
            )

    async def _handle_close_signal(self, msg: SignalMessage) -> None:
        """Handle a strategy-generated CLOSE signal."""
        symbol = msg.symbol
        position = self._position_tracker.get_position_by_symbol(symbol)

        if position is None:
            logger.warning("P3 received CLOSE for {} but no open position found", symbol)
            return

        # For live mode, verify position exists on exchange before closing
        if self._mode == "live":
            try:
                exchange_pos = await asyncio.get_event_loop().run_in_executor(
                    None, self._rest_client.get_positions, symbol
                )
                if not exchange_pos:
                    logger.warning("P3 CLOSE signal for {} but no exchange position found, skipping", symbol)
                    cleanup_price = self._get_current_price(symbol, position)
                    self._position_tracker.close_position(
                        position_id=position["id"], exit_price=cleanup_price,
                        exit_reason="orphan_cleanup", exit_type="orphan_cleanup", fee=0.0,
                    )
                    self._trailing_stops.pop(symbol, None)
                    self._notify_slot_available()
                    return
            except Exception as exc:
                logger.warning("P3 failed to verify exchange position for {}: {}", symbol, exc)

        # Use ticker price for exit, not msg.entry_price (which is 0 for CLOSE signals)
        current_price = self._get_current_price(symbol, position)
        if current_price <= 0:
            # Fallback: refresh ticker and try again
            await asyncio.get_event_loop().run_in_executor(
                None, self._refresh_ticker_prices, [symbol],
            )
            current_price = self._get_current_price(symbol, position)

        if current_price <= 0:
            logger.error("P3 cannot close {} - no current price available", symbol)
            return

        result = await self._order_manager.close_position(
            position=position,
            reason=f"strategy_exit: {msg.reason}",
            current_price=current_price,
        )

        if result["success"]:
            pnl = result["pnl"]
            self._asset_strategy.loss_tracker.record_trade(is_win=pnl > 0)
            self._trailing_stops.pop(symbol, None)
            self._notify_slot_available()
            logger.info("P3 closed {} via strategy signal: pnl={:.4f}", symbol, pnl)

    # ------------------------------------------------------------------
    # Live exchange position sync
    # ------------------------------------------------------------------

    async def _sync_exchange_positions(self) -> None:
        """Detect positions closed by Bybit server-side SL/TP.

        Compares DB open positions against actual exchange positions.
        If a DB position no longer exists on exchange, it was closed by
        a server-side SL/TP trigger. Clean up: close DB record, cancel
        remaining conditional orders, notify slot available.
        """
        if self._bybit_client is None:
            return

        db_positions = self._position_tracker.get_open_positions()
        if not db_positions:
            return

        try:
            exchange_positions = await asyncio.get_event_loop().run_in_executor(
                None, self._bybit_client.get_positions, None
            )
        except Exception as exc:
            logger.debug("Exchange position sync failed: {}", exc)
            return

        # Build set of symbols with open exchange positions
        exchange_symbols = set()
        for ep in exchange_positions:
            if float(ep.get("size", 0)) > 0:
                exchange_symbols.add(ep["symbol"])

        # Find DB positions not on exchange (server-side closed)
        for pos in db_positions:
            symbol = pos.get("symbol", "")
            if symbol in exchange_symbols:
                continue

            # This position was closed by server-side SL/TP
            position_id = pos.get("id")
            logger.warning(
                "Server-side close detected: {} {} (pos_id={}). Cleaning up.",
                symbol, pos.get("side"), position_id,
            )

            # Cancel remaining conditional orders
            sl_oid = pos.get("sl_order_id", "")
            tp_oid = pos.get("tp_order_id", "")
            cancel_ids = [oid for oid in [sl_oid, tp_oid] if oid]
            if cancel_ids and hasattr(self._executor, 'cancel_orders'):
                try:
                    await self._executor.cancel_orders(symbol, cancel_ids)
                    logger.info("Cancelled {} orphan orders for {}", len(cancel_ids), symbol)
                except Exception:
                    pass

            # Also cancel ALL open orders for this symbol as safety net
            try:
                raw = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._bybit_client._http.get_open_orders(
                        category="linear", symbol=symbol
                    )
                )
                remaining = raw.get("result", {}).get("list", [])
                for order in remaining:
                    oid = order.get("orderId", "")
                    if oid:
                        try:
                            await asyncio.get_event_loop().run_in_executor(
                                None, lambda o=oid: self._bybit_client.cancel_order(symbol, o)
                            )
                            logger.info("Cancelled remaining order {} for {}", oid, symbol)
                        except Exception:
                            pass
            except Exception:
                pass

            # Get closed PnL from exchange — match by symbol + entry price
            pnl = 0.0
            fee = 0.0
            exit_price = float(pos.get("entry_price", 0))
            pos_entry = float(pos.get("entry_price", 0))
            pos_side = pos.get("side", "")
            pos_size = float(pos.get("size", 0))
            try:
                closed = await asyncio.get_event_loop().run_in_executor(
                    None, self._bybit_client.get_closed_pnl, symbol, 20
                )
                # Find the matching close: same side, similar qty, most recent
                best_match = None
                for cp in (closed or []):
                    cp_side = cp.get("side", "")
                    cp_qty = float(cp.get("qty", 0))
                    cp_entry = float(cp.get("avgEntryPrice", 0))
                    # Match: same side, similar entry price (within 1%)
                    if cp_side == pos_side and abs(cp_entry - pos_entry) / max(pos_entry, 0.0001) < 0.01:
                        best_match = cp
                        break
                if best_match:
                    pnl = float(best_match.get("closedPnl", 0))
                    fee = abs(float(best_match.get("totalFee", 0) or 0))
                    exit_price = float(best_match.get("avgExitPrice", pos_entry))
                    logger.info(
                        "Server-side PnL matched: {} pnl={:+.6f} fee={:.6f} exit={:.6f}",
                        symbol, pnl, fee, exit_price,
                    )
            except Exception as exc:
                logger.debug("Failed to fetch server-side PnL for {}: {}", symbol, exc)

            # Close in DB with actual PnL
            self._position_tracker.close_position(
                position_id=position_id,
                exit_price=exit_price,
                exit_reason="server_side_sl_tp",
                exit_type="server_side",
                fee=fee,
                pnl_override=pnl if pnl != 0 else None,
            )

            # Clean up grid manager mapping if applicable
            if self._grid_manager is not None:
                keys_to_remove = [
                    k for k, v in self._grid_manager._level_positions.items()
                    if v == position_id
                ]
                for k in keys_to_remove:
                    self._grid_manager._level_positions.pop(k, None)
                    self._grid_manager._level_order_ids.pop(k, None)
                    self._grid_manager._level_entry_fees.pop(k, None)

            self._trailing_stops.pop(symbol, None)
            self._asset_strategy.loss_tracker.record_trade(is_win=pnl > 0)
            self._notify_slot_available()

            logger.info(
                "Server-side close cleanup done: {} pnl={:+.6f}", symbol, pnl,
            )

    # ------------------------------------------------------------------
    # Position monitoring
    # ------------------------------------------------------------------

    async def _monitor_positions(self) -> None:
        """Periodic check: SL/TP fills, time limits, trailing stops, P&L updates."""
        closed_ids = set()
        positions = self._position_tracker.get_open_positions()
        if not positions:
            return

        # 0. Refresh current prices from exchange
        symbols = [p["symbol"] for p in positions]
        await asyncio.get_event_loop().run_in_executor(
            None, self._refresh_ticker_prices, symbols,
        )

        # 1. Check SL/TP fills using current ticker prices
        fills = await self._order_manager.check_sl_tp_fills(positions)

        # Also check SL/TP directly with ticker prices (paper mode)
        # SL/TP monitoring — works in both paper and live modes
        # In live mode with limit orders, server-side SL is not set,
        # so P3 must monitor prices and execute SL via market order.
        for pos in list(positions):
            symbol = pos["symbol"]
            current_price = self._get_current_price(symbol, pos)
            if current_price <= 0:
                continue

            sl = float(pos.get("stop_loss", 0))
            tp = float(pos.get("take_profit", 0))
            side = pos["side"]
            entry_price = float(pos.get("entry_price", 0))
            size = float(pos.get("size", 0))

            sl_hit = False
            tp_hit = False

            if side == "Buy":  # LONG
                if sl > 0 and current_price <= sl:
                    sl_hit = True
                if tp > 0 and current_price >= tp:
                    tp_hit = True
            else:  # SHORT
                if sl > 0 and current_price >= sl:
                    sl_hit = True
                if tp > 0 and current_price <= tp:
                    tp_hit = True

            if sl_hit or tp_hit:
                if pos.get("id") in closed_ids:
                    continue
                fill_type = "stop_loss" if sl_hit else "take_profit"
                fill_price = sl if sl_hit else tp

                # Live mode: execute market close immediately
                if self._mode == "live" and sl_hit:
                    try:
                        close_side = "Sell" if side == "Buy" else "Buy"
                        await self._executor.close_position(
                            symbol=symbol, side=close_side,
                            qty=size, current_price=current_price,
                        )
                        logger.info("P3 SL executed: {} {} @ {:.6f} (SL={:.6f})",
                                    symbol, side, current_price, sl)
                    except Exception as exc:
                        logger.error("P3 SL execution failed {}: {}", symbol, exc)
                        continue

                if side == "Buy":
                    pnl = (fill_price - entry_price) * size
                else:
                    pnl = (entry_price - fill_price) * size

                fee_rate = self._executor.taker_fee_rate if isinstance(self._executor, PaperExecutor) else 0.0006
                fee = abs(fill_price * size) * fee_rate

                fills.append({
                    "position": pos,
                    "symbol": symbol,
                    "side": side,
                    "fill_price": fill_price,
                    "fill_type": fill_type,
                    "pnl": pnl - fee,
                    "fee": fee,
                })

        for fill in fills:
            fill_order_id = fill.get("order_id", fill.get("orderId", ""))
            if fill_order_id and fill_order_id in self._processed_fill_ids:
                logger.debug("P3 skipping already-processed fill: {}", fill_order_id)
                continue

            pos = fill["position"]
            symbol = pos["symbol"]
            pnl = fill["pnl"]
            fill_type = fill["fill_type"]

            # Cancel remaining SL/TP conditional orders on exchange
            sl_oid = pos.get("sl_order_id", "")
            tp_oid = pos.get("tp_order_id", "")
            cancel_ids = [oid for oid in [sl_oid, tp_oid] if oid]
            if cancel_ids and self._mode == "live" and hasattr(self._executor, 'cancel_orders'):
                try:
                    await self._executor.cancel_orders(symbol, cancel_ids)
                except Exception:
                    pass

            # Record in position tracker
            if pos.get("id"):
                self._position_tracker.close_position(
                    position_id=pos["id"],
                    exit_price=fill["fill_price"],
                    exit_reason=fill_type,
                    exit_type=fill_type,
                    fee=fill.get("fee", 0.0),
                )

            # Close in paper executor too
            if self._mode == "paper" and isinstance(self._executor, PaperExecutor):
                paper_pos = self._executor.account.positions.pop(symbol, None)
                if paper_pos is not None:
                    # Return margin to balance
                    self._executor.account.balance += paper_pos.margin

            # Update loss tracker
            self._asset_strategy.loss_tracker.record_trade(is_win=pnl > 0)
            self._trailing_stops.pop(symbol, None)
            self._notify_slot_available()

            # Track closed position to prevent double-close
            if pos.get("id"):
                closed_ids.add(pos["id"])
            if fill_order_id:
                self._processed_fill_ids.add(fill_order_id)

            logger.info(
                "P3 {} hit for {}: exit={:.4f} pnl={:.4f} fee={:.4f}",
                fill_type.upper(), symbol, fill["fill_price"], pnl, fill.get("fee", 0),
            )

        # Refresh positions after processing fills
        positions = self._position_tracker.get_open_positions()

        time_limit_config = self._position_config.get("exit", {}).get("time_limit", {})
        max_hold_minutes = time_limit_config.get("max_holding_minutes", 90)
        warning_minutes = time_limit_config.get("warning_minutes", 75)

        trailing_config = self._position_config.get("exit", {}).get("trailing_stop", {})

        for pos in positions:
            symbol = pos["symbol"]
            position_id = pos["id"]
            entered_at = pos.get("entered_at", 0)
            entry_price = float(pos.get("entry_price", 0))
            side = pos["side"]

            # 2. Check time limits
            status, elapsed = TimeLimitManager.check(
                entered_at=entered_at,
                max_minutes=max_hold_minutes,
                warning_minutes=warning_minutes,
            )

            if status == "expired":
                logger.warning(
                    "P3 time limit expired for {} ({}min), closing",
                    symbol, elapsed,
                )
                current_price = self._get_current_price(symbol, pos)
                result = await self._order_manager.close_position(
                    position=pos,
                    reason=f"time_limit ({elapsed}min)",
                    current_price=current_price,
                )
                if result["success"]:
                    pnl = result["pnl"]
                    self._asset_strategy.loss_tracker.record_trade(is_win=pnl > 0)
                    self._trailing_stops.pop(symbol, None)
                    self._notify_slot_available()
                continue

            if status == "warning":
                logger.info(
                    "P3 time limit warning for {} ({}min / {}min max)",
                    symbol, elapsed, max_hold_minutes,
                )

            # 3. Update trailing stops
            trailing_state = self._trailing_stops.get(symbol)
            if trailing_state is not None:
                current_price = self._get_current_price(symbol, pos)
                if current_price > 0:
                    side_str = "LONG" if side == "Buy" else "SHORT"

                    # Use a default ATR if we don't have the live value
                    atr = abs(entry_price - float(pos.get("stop_loss", 0))) or entry_price * 0.01

                    trailing_state, should_close = TrailingStopManager.update(
                        state=trailing_state,
                        current_price=current_price,
                        side=side_str,
                        atr=atr,
                        config=trailing_config,
                    )
                    self._trailing_stops[symbol] = trailing_state

                    if should_close:
                        logger.info("P3 trailing stop triggered for {}", symbol)
                        result = await self._order_manager.close_position(
                            position=pos,
                            reason="trailing_stop",
                            current_price=current_price,
                        )
                        if result["success"]:
                            pnl = result["pnl"]
                            self._asset_strategy.loss_tracker.record_trade(is_win=pnl > 0)
                            self._trailing_stops.pop(symbol, None)
                            self._notify_slot_available()
                        continue

            # 4. Update unrealised P&L
            current_price = self._get_current_price(symbol, pos)
            if current_price > 0:
                self._position_tracker.update_position_pnl(position_id, current_price)

        # 5. Persist paper balance to DB
        if self._mode == "paper" and isinstance(self._executor, PaperExecutor):
            self._db.set_state("current_balance_paper", str(round(self._executor.account.balance, 4)))
            self._db.set_state("initial_balance_paper", str(round(self._executor.account.initial_balance, 4)))

        # Apply funding rate simulation for paper mode
        if self._mode == "paper" and isinstance(self._executor, PaperExecutor):
            self._apply_paper_funding()

        # Sync live wallet balance every monitor cycle
        if self._mode == "live" and self._rest_client is not None:
            try:
                wallet = await asyncio.get_event_loop().run_in_executor(
                    None, self._rest_client.get_wallet_balance
                )
                new_balance = float(wallet.get("totalWalletBalance", 0))
                if new_balance > 0:
                    self._db.set_state("current_balance_live", str(new_balance))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Position update broadcasting
    # ------------------------------------------------------------------

    def _send_position_update(self) -> None:
        """Send current position state to P2 for strategy awareness."""
        positions = self._position_tracker.get_open_positions()
        daily_stats = self._position_tracker.get_daily_stats()
        current_balance = self._get_current_balance()

        msg = PositionUpdateMessage(
            positions=positions,
            daily_pnl=daily_stats.pnl,
            balance=current_balance,
            trade_count=daily_stats.trade_count,
            consecutive_losses=daily_stats.consecutive_losses,
        )

        try:
            self.position_update_queue.put_nowait(msg)
        except queue.Full:
            # Discard oldest and put new one
            try:
                self.position_update_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.position_update_queue.put_nowait(msg)
            except queue.Full:
                logger.warning("P3 position_update_queue full, dropping update")

    def _notify_slot_available(self) -> None:
        """Notify the Orchestrator that a trading slot has opened up."""
        positions = self._position_tracker.get_open_positions()
        max_positions = self._asset_strategy.max_concurrent_positions
        available = max(0, max_positions - len(positions))

        if available > 0:
            current_symbols = [p["symbol"] for p in positions]
            msg = SlotAvailableMessage(
                available_slots=available,
                current_positions=current_symbols,
            )
            try:
                self.event_queue.put_nowait(msg)
            except queue.Full:
                logger.warning("P3 event_queue full, dropping slot_available")

    # ------------------------------------------------------------------
    # Funding rate simulation
    # ------------------------------------------------------------------

    def _apply_paper_funding(self) -> None:
        """Fetch funding rates periodically and apply to paper positions.

        Funding rates are fetched every hour from the exchange; the
        FundingSimulator inside PaperExecutor handles the 8h schedule
        internally and only charges when a Bybit funding boundary is crossed.
        """
        if not isinstance(self._executor, PaperExecutor):
            return
        if not self._executor.account.positions:
            return

        now = time.time()
        # Refresh funding rates from exchange every hour
        if now - self._last_funding_rate_fetch >= 3600.0:
            symbols = set()
            for pos in self._executor.account.positions.values():
                symbols.add(pos.symbol)
            for symbol in symbols:
                try:
                    rates = self._rest_client.get_funding_rate(symbol)
                    if rates:
                        rate = float(rates[0].get("fundingRate", 0))
                        self._funding_rate_cache[symbol] = rate
                except Exception:
                    pass  # Non-critical
            self._last_funding_rate_fetch = now

        if self._funding_rate_cache:
            charges = self._executor.apply_funding(self._funding_rate_cache)
            if charges:
                logger.info("P3 applied {} funding charges", len(charges))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_current_balance(self) -> float:
        """Return the current account balance."""
        if self._mode == "paper" and isinstance(self._executor, PaperExecutor):
            return self._executor.account.balance + sum(
                p.margin for p in self._executor.account.positions.values()
            )
        # Live mode: use synced wallet balance from DB, fallback to initial
        try:
            balance_raw = self._db.get_state("current_balance_live")
            if balance_raw:
                return float(balance_raw)
        except Exception:
            pass
        return self._initial_balance

    def _get_current_price(self, symbol: str, position: dict) -> float:
        """Get the current price for a symbol.

        Uses cached ticker data refreshed every monitor cycle.
        Falls back to entry_price if no price is available.
        """
        cached = self._ticker_cache.get(symbol, 0)
        if cached > 0:
            return cached
        entry = float(position.get("entry_price", 0))
        if entry > 0:
            logger.warning("P3 using stale entry_price for {} (no ticker data)", symbol)
        return entry

    def _refresh_ticker_prices(self, symbols: list[str]) -> None:
        """Fetch latest prices via REST API and update the cache.

        Called once per monitor cycle (~2s) to avoid excessive API calls.
        """
        now = time.time()
        # Only refresh every 5 seconds
        if now - self._last_ticker_fetch < 5.0:
            return

        try:
            tickers = self._rest_client.get_tickers()
            for t in tickers:
                sym = t.get("symbol", "")
                if sym in symbols:
                    last = float(t.get("lastPrice", 0))
                    if last > 0:
                        self._ticker_cache[sym] = last
                        # Also update paper executor's last_prices
                        if isinstance(self._executor, PaperExecutor):
                            self._executor.last_prices[sym] = last
            self._last_ticker_fetch = now
        except Exception as exc:
            logger.warning("P3 failed to refresh ticker prices: {}", exc)

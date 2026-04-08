"""P2: Strategy Engine Process.

Runs as a separate :class:`multiprocessing.Process`.  Consumes completed
candles from P1, maintains rolling DataFrames with technical indicators,
runs the scanner on demand, and evaluates the position strategy on every
primary-timeframe candle for active trading symbols.

Task S-17.
"""

from __future__ import annotations

import multiprocessing
import queue
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger

from src.collector.bybit_client import BybitClient
from src.collector.symbol_manager import SymbolManager
from src.indicators.technical import IndicatorEngine
from src.strategy.position.base import GridAction, SignalType
from src.strategy.scanner.new_listing import NewListingScanner
from src.strategy.tuner import StrategyTuner
from src.utils.logger import setup_logger
from src.utils.messages import (
    ControlMessage,
    GridSignalMessage,
    MarketDataMessage,
    PositionUpdateMessage,
    ScanResultMessage,
    SignalMessage,
)


# How long (seconds) the main loop sleeps when no messages are available.
_POLL_INTERVAL_S = 0.05

# Maximum number of messages to drain per queue per loop iteration.
_MAX_DRAIN = 50


class StrategyEngineProcess(multiprocessing.Process):
    """P2: Strategy Engine Process.

    Receives
    --------
    MarketDataMessages from P1 via *market_data_queue*:
        Completed candle updates for all subscribed symbols.
    PositionUpdateMessages from P3 via *position_update_queue*:
        Current open positions and daily P&L snapshots.
    ControlMessages from the Orchestrator via *control_queue*:
        - ``scan``  -- trigger a new-listing scan cycle.
        - ``stop``  -- gracefully shut down.

    Sends
    -----
    SignalMessages to P3 via *signal_queue*:
        Entry / exit signals produced by :class:`MomentumScalper`.
    ScanResultMessages to the Orchestrator via *scan_result_queue*:
        Results of each scanner cycle so the orchestrator can decide
        which symbols to track.
    ControlMessages to P1 via *p1_control_queue*:
        ``subscribe`` commands to tell the data collector to stream
        newly discovered symbols.

    Parameters
    ----------
    config:
        Application configuration dict.  Expected sub-keys:

        - ``base_symbols``   -- e.g. ``["BTCUSDT", "ETHUSDT"]``
        - ``timeframes``     -- ``{"primary": "5m", "secondary": ["15m"],
          "btc_eth_trend": "1h"}``
        - ``candle_history`` -- rolling window size (default 100)
        - ``strategy``       -- ``{"scanner": {...}, "position": {...}}``
    credentials:
        Dict with ``api_key`` and ``api_secret``.
    market_data_queue:
        Queue from which candle messages (from P1) are read.
    position_update_queue:
        Queue from which position snapshots (from P3) are read.
    control_queue:
        Queue from which orchestrator commands are read.
    signal_queue:
        Queue into which trading signals are written (consumed by P3).
    scan_result_queue:
        Queue into which scan results are written (consumed by orchestrator).
    p1_control_queue:
        Queue into which subscribe/unsubscribe commands for P1 are written.
    """

    def __init__(
        self,
        config: dict,
        credentials: dict,
        market_data_queue: multiprocessing.Queue,
        position_update_queue: multiprocessing.Queue,
        control_queue: multiprocessing.Queue,
        signal_queue: multiprocessing.Queue,
        scan_result_queue: multiprocessing.Queue,
        p1_control_queue: multiprocessing.Queue,
    ) -> None:
        super().__init__(name="P2-StrategyEngine", daemon=True)
        self._config = config
        self._credentials = credentials

        # Queues
        self._market_data_queue = market_data_queue
        self._position_update_queue = position_update_queue
        self._control_queue = control_queue
        self._signal_queue = signal_queue
        self._scan_result_queue = scan_result_queue
        self._p1_control_queue = p1_control_queue

        # Populated in run() -- not picklable across fork.
        self._running = False

    # ------------------------------------------------------------------
    # Process entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Process entry point -- sets up logging, builds components, loops."""
        setup_logger("p2_strategy")
        logger.info("P2 StrategyEngineProcess starting (pid={})", self.pid)

        try:
            self._init_components()
            self._main_loop()
        except KeyboardInterrupt:
            logger.info("P2 interrupted by KeyboardInterrupt")
        except Exception as exc:
            logger.exception("P2 fatal error: {}", exc)
        finally:
            logger.info("P2 StrategyEngineProcess exiting")

    # ------------------------------------------------------------------
    # Initialisation (runs inside child process)
    # ------------------------------------------------------------------

    def _init_components(self) -> None:
        """Create strategy objects and internal caches."""
        # --- Config unpacking ---
        # Config may have timeframes/base_symbols at root (legacy) or
        # under "symbols" key (from Orchestrator _process_config).
        symbols_cfg = self._config.get("symbols", {})
        tf_cfg = (
            self._config.get("timeframes")
            or symbols_cfg.get("timeframes", {})
        )
        self._primary_tf: str = tf_cfg.get("primary", "5m")
        self._secondary_tfs: list[str] = tf_cfg.get("secondary", ["15m"])
        self._trend_tf: str = tf_cfg.get("btc_eth_trend", "1h")
        self._candle_history: int = (
            tf_cfg.get("candle_history")
            or self._config.get("candle_history", 100)
        )
        self._base_symbols: list[str] = (
            self._config.get("base_symbols")
            or symbols_cfg.get("base_symbols", ["BTCUSDT", "ETHUSDT"])
        )

        # --- Rolling DataFrame cache: {(symbol, timeframe): pd.DataFrame} ---
        self._market_cache: Dict[Tuple[str, str], pd.DataFrame] = {}

        # --- Position / P&L state received from P3 ---
        self._current_positions: List[Dict[str, Any]] = []
        self._daily_pnl: float = 0.0
        self._balance: float = 0.0
        self._trade_count: int = 0
        self._consecutive_losses: int = 0

        # --- Scanner bookkeeping ---
        self._scan_results: Dict[str, Dict[str, Any]] = {}
        self._recent_sl_symbols: Dict[str, float] = {}

        # --- Active trading symbols (those that P2 evaluates for signals) ---
        self._active_trading_symbols: set[str] = set()

        # --- REST client for scanner (its own instance, no WS needed) ---
        api_key = self._credentials.get("api_key") or None
        api_secret = self._credentials.get("api_secret") or None
        self._rest_client = BybitClient(api_key=api_key, api_secret=api_secret)

        # --- Scanner ---
        scanner_cfg = self._config.get("scanner", {})
        nl_cfg = scanner_cfg.get("strategies", {}).get("new_listing", {})
        self._symbol_manager = SymbolManager(self._rest_client)
        self._scanner = NewListingScanner(self._symbol_manager, nl_cfg)

        # --- Database ---
        from src.utils.db import DatabaseManager
        self._db = DatabaseManager()

        # --- Determine active strategy via registry ---
        from src.strategy.position.registry import get_strategy_class, ensure_loaded, GRID_STRATEGIES
        ensure_loaded()

        # Check grid config first (backward compat)
        grid_cfg = self._config.get("grid", {})
        position_cfg = self._config.get("position", {})

        # Strategy name: check grid.active first, then position.active, fallback
        strategy_name = grid_cfg.get("active", "")
        if not strategy_name or strategy_name not in GRID_STRATEGIES:
            strategy_name = position_cfg.get("active", "momentum_scalper")

        strategy_cls, is_grid = get_strategy_class(strategy_name)

        if strategy_cls is None:
            logger.warning("Unknown strategy '{}', falling back to momentum_scalper", strategy_name)
            from src.strategy.position.momentum_scalper import MomentumScalper as _FallbackScalper
            strategy_cls = _FallbackScalper
            is_grid = False

        if is_grid:
            self._grid_strategy = strategy_cls(grid_cfg, db=self._db)
            self._grid_strategy.restore_from_db(self._config.get("mode", "paper"))
            self._scalper = None
            self._tuner = None
            self._last_funding_fetch = 0.0
            self._funding_fetch_interval = 3600.0
            logger.info("P2 using strategy: {} (grid type)", strategy_name)
        else:
            self._grid_strategy = None
            ms_cfg = position_cfg.get("strategies", {}).get(strategy_name, {})
            exit_cfg = position_cfg.get("exit", {})
            if exit_cfg and "exit" not in ms_cfg:
                ms_cfg["exit"] = exit_cfg
            self._scalper = strategy_cls(config=ms_cfg if ms_cfg else None)

            tuner_cfg = ms_cfg.get("tuner", {})
            self._tuner = StrategyTuner(
                config=tuner_cfg,
                initial_params=self._scalper.params,
                db=self._db,
            )
            self._tuner.restore_from_db(self._scalper.params)
            logger.info("P2 using strategy: {} (position type)", strategy_name)

        # --- Load base symbol history via API ---
        for sym in self._base_symbols:
            for tf in [self._primary_tf, self._trend_tf] + self._secondary_tfs:
                self._load_history_from_api(sym, tf)

        logger.info(
            "P2 components initialised: primary_tf={} secondary_tfs={} "
            "trend_tf={} candle_history={}",
            self._primary_tf,
            self._secondary_tfs,
            self._trend_tf,
            self._candle_history,
        )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _main_loop(self) -> None:
        """Synchronous main loop.

        On each iteration:
        1. Drain control messages (scan trigger, stop).
        2. Drain market-data messages (candle updates).
        3. Drain position-update messages.
        4. Sleep briefly if no work was done.
        """
        self._running = True
        logger.info("P2 entering main loop")

        while self._running:
            did_work = False

            # 1. Control messages
            did_work |= self._drain_control_queue()

            if not self._running:
                break

            # 2. Market data
            did_work |= self._drain_market_data_queue()

            # 3. Position updates
            did_work |= self._drain_position_update_queue()

            if not did_work:
                time.sleep(_POLL_INTERVAL_S)

    # ------------------------------------------------------------------
    # Queue draining
    # ------------------------------------------------------------------

    def _drain_control_queue(self) -> bool:
        """Read and handle up to *_MAX_DRAIN* control messages.

        Returns ``True`` if at least one message was processed.
        """
        processed = False
        for _ in range(_MAX_DRAIN):
            try:
                msg: ControlMessage = self._control_queue.get_nowait()
            except queue.Empty:
                break

            if not isinstance(msg, ControlMessage):
                logger.warning(
                    "P2 ignoring non-ControlMessage on control queue: {}",
                    type(msg).__name__,
                )
                continue

            processed = True
            command = msg.command

            if command == "stop":
                logger.info("P2 received stop command")
                self._running = False
                return True

            if command == "scan":
                available_slots = msg.data.get("available_slots", 3)
                current_positions = msg.data.get("current_positions", [])
                logger.info(
                    "P2 scan triggered: available_slots={} current_positions={}",
                    available_slots,
                    current_positions,
                )
                self._run_scanner(available_slots, current_positions)

            else:
                logger.warning("P2 ignoring unknown control command: {}", command)

        return processed

    def _drain_market_data_queue(self) -> bool:
        """Read and handle up to *_MAX_DRAIN* market-data messages.

        Returns ``True`` if at least one message was processed.
        """
        processed = False
        for _ in range(_MAX_DRAIN):
            try:
                msg: MarketDataMessage = self._market_data_queue.get_nowait()
            except queue.Empty:
                break

            if not isinstance(msg, MarketDataMessage):
                logger.warning(
                    "P2 ignoring non-MarketDataMessage: {}", type(msg).__name__
                )
                continue

            processed = True
            logger.info("P2 ← Queue: {} {} C={}", msg.symbol, msg.timeframe, msg.candle.get("close"))
            self._handle_candle(msg)

        return processed

    def _drain_position_update_queue(self) -> bool:
        """Read and handle up to *_MAX_DRAIN* position-update messages.

        Returns ``True`` if at least one message was processed.
        """
        processed = False
        for _ in range(_MAX_DRAIN):
            try:
                msg: PositionUpdateMessage = (
                    self._position_update_queue.get_nowait()
                )
            except queue.Empty:
                break

            if not isinstance(msg, PositionUpdateMessage):
                logger.warning(
                    "P2 ignoring non-PositionUpdateMessage: {}",
                    type(msg).__name__,
                )
                continue

            processed = True
            self._current_positions = msg.positions
            self._daily_pnl = msg.daily_pnl
            self._balance = msg.balance
            self._trade_count = msg.trade_count
            self._consecutive_losses = msg.consecutive_losses

        return processed

    # ------------------------------------------------------------------
    # Candle handling
    # ------------------------------------------------------------------

    def _handle_candle(self, msg: MarketDataMessage) -> None:
        """Process a single completed candle.

        1. Append to the rolling DataFrame cache.
        2. Recalculate technical indicators.
        3. If the symbol is an active trading symbol and the timeframe is
           the primary timeframe, evaluate the position strategy.

        Parameters
        ----------
        msg:
            A :class:`MarketDataMessage` with ``symbol``, ``timeframe``,
            and ``candle`` fields.
        """
        symbol = msg.symbol
        timeframe = msg.timeframe
        candle = msg.candle

        # 1. Update cache
        self._update_market_data_cache(symbol, timeframe, candle)

        # 2. Only evaluate strategy on primary-timeframe candles for
        #    active trading symbols.
        if (
            timeframe == self._primary_tf
            and symbol in self._active_trading_symbols
        ):
            if self._grid_strategy is not None:
                self._evaluate_grid_strategy(symbol, candle)
            else:
                self._evaluate_strategy(symbol)

    def _evaluate_strategy(self, symbol: str) -> None:
        """Run MomentumScalper on the latest data for *symbol*.

        If the signal is not HOLD, package it as a :class:`SignalMessage`
        and place it on the *signal_queue* for P3.
        """
        # Fetch indicator DataFrames
        df_5m = self._get_indicators(symbol, self._primary_tf)
        if df_5m is None or df_5m.empty:
            return

        # Secondary (higher) timeframe -- use first secondary tf
        htf = self._secondary_tfs[0] if self._secondary_tfs else self._primary_tf
        df_15m = self._get_indicators(symbol, htf)
        if df_15m is None:
            df_15m = pd.DataFrame()

        # Current position for this symbol (if any)
        current_position = self._get_position_for_symbol(symbol)

        # Scanner result for this symbol (if any)
        scan_result = self._scan_results.get(symbol)

        # Evaluate
        signal = self._scalper.evaluate(
            symbol=symbol,
            indicators_5m=df_5m,
            indicators_15m=df_15m,
            current_position=current_position,
            scan_result=scan_result,
        )

        logger.info(
            "P2 evaluate {}: signal={} reason={}",
            symbol, signal.signal.value, signal.reason,
        )

        # Record evaluation for auto-tuner
        is_signal = signal.signal != SignalType.HOLD
        self._tuner.record_evaluation(is_signal)

        # Check if tuning is needed
        if self._tuner.should_tune():
            self._tuner.tune(self._scalper.params)

        if signal.signal == SignalType.HOLD:
            return

        # Determine market direction from cached scan result
        scanner_direction = "mixed"
        if scan_result:
            scanner_direction = scan_result.get("market_direction", "mixed")

        # Send signal to P3
        sig_msg = SignalMessage(
            symbol=signal.symbol,
            signal=signal.signal.value,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            strategy=signal.strategy,
            confidence=signal.confidence,
            scanner_direction=scanner_direction,
            suggested_side=signal.suggested_side,
            reason=signal.reason,
        )

        try:
            self._signal_queue.put_nowait(sig_msg)
            logger.info(
                "P2 signal emitted: {} {} entry={:.4f} sl={:.4f} tp={:.4f}",
                symbol,
                signal.signal.value,
                signal.entry_price,
                signal.stop_loss,
                signal.take_profit,
            )
        except queue.Full:
            logger.warning("P2 signal_queue full -- dropping signal for {}", symbol)

    # ------------------------------------------------------------------
    # Grid strategy evaluation
    # ------------------------------------------------------------------

    def _evaluate_grid_strategy(self, symbol: str, candle: Dict[str, Any]) -> None:
        """Run GridBiasStrategy on the latest candle for *symbol*.

        Emits GridSignalMessage objects to P3 for each grid action.
        """
        if self._grid_strategy is None:
            return

        # Fetch 1h indicators for this symbol
        df_1h = self._get_indicators(symbol, "1h")
        if df_1h is None:
            # Try trend_tf as fallback
            df_1h = self._get_indicators(symbol, self._trend_tf)

        # Get 5m and 15m indicator DataFrames for MTF filtering
        df_5m = self._get_indicators(symbol, self._primary_tf)
        htf = self._secondary_tfs[0] if self._secondary_tfs else self._primary_tf
        df_15m = self._get_indicators(symbol, htf)

        # Get BTC/ETH trends
        btc_trend, eth_trend = self._get_market_trends()

        # Periodically fetch funding rates
        self._maybe_fetch_funding_rates()

        # Get balance info
        mode = self._config.get("mode", "paper")
        asset_cfg = self._config.get("asset", {})
        initial_balance = asset_cfg.get("capital", {}).get("initial_balance", 20.0)
        current_balance = self._balance if self._balance > 0 else initial_balance

        # Evaluate grid
        signals = self._grid_strategy.evaluate(
            symbol=symbol,
            candle_5m=candle,
            df_1h=df_1h,
            btc_trend=btc_trend,
            eth_trend=eth_trend,
            current_balance=current_balance,
            initial_balance=initial_balance,
            mode=mode,
            df_5m=df_5m,
            df_15m=df_15m,
        )

        if not signals:
            return

        # Send each grid signal to P3
        for sig in signals:
            grid_state = self._grid_strategy._grids.get(symbol)
            msg = GridSignalMessage(
                symbol=sig.symbol,
                action=sig.action.value,
                level_id=sig.level_id,
                level_index=sig.level_index,
                level_price=sig.level_price,
                side=sig.side,
                tp_price=sig.tp_price,
                grid_state_id=sig.grid_state_id,
                qty_per_level=grid_state.qty_per_level if grid_state else 0,
                leverage=grid_state.leverage if grid_state else 5,
                reason=sig.reason,
            )
            try:
                self._signal_queue.put_nowait(msg)
                logger.info(
                    "P2 grid signal: {} {} idx={} side={} price={:.6f}",
                    symbol, sig.action.value, sig.level_index,
                    sig.side, sig.level_price,
                )
            except queue.Full:
                logger.warning("P2 signal_queue full -- dropping grid signal")

    def _maybe_fetch_funding_rates(self) -> None:
        """Periodically fetch funding rates for active symbols."""
        if self._grid_strategy is None:
            return
        now = time.time()
        if now - self._last_funding_fetch < self._funding_fetch_interval:
            return
        self._last_funding_fetch = now

        for symbol in self._active_trading_symbols:
            try:
                rates = self._rest_client.get_funding_rate(symbol)
                if rates:
                    rate = float(rates[0].get("fundingRate", 0))
                    self._grid_strategy.update_funding_rate(symbol, rate)
            except Exception:
                pass  # Non-critical

    # ------------------------------------------------------------------
    # Scanner
    # ------------------------------------------------------------------

    def _run_scanner(
        self, available_slots: int, current_positions: list
    ) -> None:
        """Execute a full new-listing scan cycle.

        1. Fetch tickers via BybitClient (REST).
        2. Determine BTC/ETH trend from cached indicator data.
        3. Run :meth:`NewListingScanner.scan`.
        4. Send :class:`ScanResultMessage` with top candidates.
        5. Send :class:`ControlMessage` to P1 to subscribe new symbols.

        Parameters
        ----------
        available_slots:
            Number of position slots available for new trades.
        current_positions:
            List of symbol strings currently held.
        """
        # 1. Fetch tickers
        try:
            tickers = self._rest_client.get_tickers()
        except Exception as exc:
            logger.error("P2 scanner failed to fetch tickers: {}", exc)
            return

        # 2. Determine BTC / ETH trend from cached 1h data
        btc_trend, eth_trend = self._get_market_trends()

        # 3. Build market_data dict expected by the scanner
        market_data = self._build_scanner_market_data(tickers)

        # 4. Run scanner
        try:
            results = self._scanner.scan(
                market_data=market_data,
                btc_trend=btc_trend,
                eth_trend=eth_trend,
                open_positions=current_positions,
                recent_sl_symbols=self._recent_sl_symbols,
            )
        except Exception as exc:
            logger.error("P2 scanner error: {}", exc)
            return

        logger.info("P2 scanner returned {} results", len(results))

        # Determine overall market direction
        market_direction = IndicatorEngine.get_market_trend(
            self._get_cached_df("BTCUSDT", self._trend_tf),
            self._get_cached_df("ETHUSDT", self._trend_tf),
        )

        # Store ALL scan results for monitoring (not limited by available_slots).
        # We monitor all candidates but only generate trading signals when
        # slots are available (enforced by the asset strategy / order manager).
        top_symbols: list[str] = []
        result_dicts: list[dict] = []
        new_symbols: list[str] = []
        for r in results:
            self._scan_results[r.symbol] = {
                "symbol": r.symbol,
                "score": r.score,
                "market_direction": r.market_direction,
                "suggested_side": r.suggested_side,
                "scores_detail": r.scores_detail,
                "reason": r.reason,
                "metadata": r.metadata,
            }
            if r.symbol not in self._active_trading_symbols:
                new_symbols.append(r.symbol)
            self._active_trading_symbols.add(r.symbol)
            top_symbols.append(r.symbol)
            result_dicts.append(asdict(r))

        # Pre-load historical candle data from DB for newly added symbols
        # so that indicators are available from the first evaluation.
        # For grid strategy, also load 1h data for bias calculation.
        tfs_to_load = [self._primary_tf] + self._secondary_tfs
        if self._grid_strategy is not None and "1h" not in tfs_to_load:
            tfs_to_load.append("1h")
        for sym in new_symbols:
            for tf in tfs_to_load:
                self._load_history_from_api(sym, tf)

        logger.info(
            "P2 monitoring {} symbol(s) (available_slots={})",
            len(top_symbols), available_slots,
        )

        # 5a. Send scan results to orchestrator
        scan_msg = ScanResultMessage(
            results=result_dicts,
            market_direction=market_direction,
        )
        try:
            self._scan_result_queue.put_nowait(scan_msg)
        except queue.Full:
            logger.warning("P2 scan_result_queue full -- dropping scan results")

        # 5b. Tell P1 to subscribe the new symbols
        if top_symbols:
            ctrl_msg = ControlMessage(
                command="subscribe",
                data={"symbols": top_symbols},
            )
            try:
                self._p1_control_queue.put_nowait(ctrl_msg)
                logger.info(
                    "P2 requested P1 to subscribe {} symbol(s): {}",
                    len(top_symbols),
                    top_symbols,
                )
            except queue.Full:
                logger.warning(
                    "P2 p1_control_queue full -- could not send subscribe"
                )

    # ------------------------------------------------------------------
    # Market data cache
    # ------------------------------------------------------------------

    def _update_market_data_cache(
        self, symbol: str, timeframe: str, candle: Dict[str, Any]
    ) -> None:
        """Append a candle to the rolling DataFrame for *(symbol, timeframe)*.

        Maintains a maximum of *candle_history* rows per key.  After
        appending, recalculates indicators on the updated DataFrame.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTCUSDT"``.
        timeframe:
            Human-friendly timeframe, e.g. ``"5m"``.
        candle:
            Dict with ``timestamp``, ``open``, ``high``, ``low``,
            ``close``, ``volume``.
        """
        key = (symbol, timeframe)
        new_row = {
            "open": float(candle.get("open", 0)),
            "high": float(candle.get("high", 0)),
            "low": float(candle.get("low", 0)),
            "close": float(candle.get("close", 0)),
            "volume": float(candle.get("volume", 0)),
            "timestamp": int(candle.get("timestamp", 0)),
        }

        if key in self._market_cache:
            df = self._market_cache[key]
            new_df = pd.DataFrame([new_row])
            df = pd.concat([df, new_df], ignore_index=True)

            # Deduplicate on timestamp (keep latest)
            df = df.drop_duplicates(subset="timestamp", keep="last")
            df = df.sort_values("timestamp").reset_index(drop=True)

            # Trim to rolling window
            if len(df) > self._candle_history:
                df = df.iloc[-self._candle_history :].reset_index(drop=True)
        else:
            df = pd.DataFrame([new_row])

        # Recalculate indicators
        # For grid strategy, all 1h data needs trend indicators (EMA 20/50)
        is_trend = (
            (symbol in self._base_symbols and timeframe == self._trend_tf)
            or (self._grid_strategy is not None and timeframe in ("1h", self._trend_tf))
        )
        try:
            df = IndicatorEngine.calculate_all(df, include_trend=is_trend)
        except Exception as exc:
            logger.error(
                "P2 indicator calculation failed for {} {}: {}",
                symbol,
                timeframe,
                exc,
            )

        self._market_cache[key] = df

    def _get_indicators(
        self, symbol: str, timeframe: str
    ) -> Optional[pd.DataFrame]:
        """Return the cached indicator DataFrame, or ``None``."""
        return self._market_cache.get((symbol, timeframe))

    def _get_cached_df(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """Return cached DataFrame or an empty one (never None)."""
        df = self._market_cache.get((symbol, timeframe))
        if df is None:
            return pd.DataFrame(
                columns=[
                    "open", "high", "low", "close", "volume", "timestamp"
                ]
            )
        return df

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_market_trends(self) -> Tuple[str, str]:
        """Derive BTC and ETH EMA alignment from cached trend-timeframe data.

        Returns
        -------
        Tuple of ``(btc_trend, eth_trend)`` -- each one of
        ``"bullish"``, ``"bearish"``, or ``"neutral"``.
        """
        btc_df = self._get_cached_df("BTCUSDT", self._trend_tf)
        eth_df = self._get_cached_df("ETHUSDT", self._trend_tf)

        btc_trend = IndicatorEngine.get_ema_alignment(btc_df)
        eth_trend = IndicatorEngine.get_ema_alignment(eth_df)
        return btc_trend, eth_trend

    def _get_position_for_symbol(
        self, symbol: str
    ) -> Optional[Dict[str, Any]]:
        """Find and return the open position dict for *symbol*, if any."""
        for pos in self._current_positions:
            if pos.get("symbol") == symbol:
                return pos
        return None

    def _build_scanner_market_data(
        self, tickers: List[Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        """Build the ``market_data`` dict expected by :meth:`NewListingScanner.scan`.

        Combines the raw ticker list with any cached indicator DataFrames.

        Parameters
        ----------
        tickers:
            List of ticker dicts from :meth:`BybitClient.get_tickers`.

        Returns
        -------
        Dict keyed by symbol, each value containing ``"tickers"`` (raw dict)
        and ``"indicators"`` (DataFrame or ``None``).
        """
        market_data: Dict[str, Dict[str, Any]] = {}
        for ticker in tickers:
            symbol = ticker.get("symbol", "")
            if not symbol:
                continue
            indicators = self._get_indicators(symbol, self._primary_tf)
            market_data[symbol] = {
                "tickers": ticker,
                "indicators": indicators,
            }
        return market_data

    # Timeframe string to Bybit interval mapping
    _TF_TO_INTERVAL = {
        "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
        "1h": "60", "2h": "120", "4h": "240", "1d": "D",
    }

    def _load_history_from_api(self, symbol: str, timeframe: str) -> None:
        """Fetch historical candles via REST API into the market cache.

        Pre-populates the rolling DataFrame so that indicators (EMA, RSI,
        etc.) have enough data from the first evaluation.
        """
        key = (symbol, timeframe)
        if key in self._market_cache and len(self._market_cache[key]) >= 20:
            return  # Already have enough data

        interval = self._TF_TO_INTERVAL.get(timeframe, "5")
        try:
            raw = self._rest_client.get_klines(
                symbol=symbol, interval=interval, limit=self._candle_history,
            )
        except Exception as exc:
            logger.warning("P2 failed to fetch history for {} {}: {}", symbol, timeframe, exc)
            return

        if not raw:
            return

        records = []
        for k in raw:
            records.append({
                "open": float(k.get("open", k.get("o", 0))),
                "high": float(k.get("high", k.get("h", 0))),
                "low": float(k.get("low", k.get("l", 0))),
                "close": float(k.get("close", k.get("c", 0))),
                "volume": float(k.get("volume", k.get("v", 0))),
                "timestamp": int(k.get("timestamp", k.get("t", k.get("startTime", 0)))),
            })

        df = pd.DataFrame(records)
        df = df.drop_duplicates(subset="timestamp", keep="last")
        df = df.sort_values("timestamp").reset_index(drop=True)

        # Calculate indicators
        is_trend = symbol in self._base_symbols and timeframe == self._trend_tf
        try:
            df = IndicatorEngine.calculate_all(df, include_trend=is_trend)
        except Exception as exc:
            logger.warning("P2 indicator calc failed for {} {}: {}", symbol, timeframe, exc)

        self._market_cache[key] = df
        logger.info(
            "P2 loaded {} candles for {} {} via API",
            len(df), symbol, timeframe,
        )

        # Throttle API calls to avoid rate limiting during bulk history load
        time.sleep(0.2)

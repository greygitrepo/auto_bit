"""P1: Data Collector Process.

Runs as a separate :class:`multiprocessing.Process`.  Manages the full
lifecycle of market data collection -- WebSocket streaming, historical
candle loading, and dynamic symbol subscription -- and forwards completed
candles to the Strategy Engine (P2) via a multiprocessing Queue.

Task D-08.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import queue
from typing import Any, Dict

from loguru import logger

from src.collector.bybit_client import BybitClient
from src.collector.data_collector import DataCollector
from src.collector.ws_manager import WebSocketManager
from src.utils.db import DatabaseManager
from src.utils.logger import setup_logger
from src.utils.messages import ControlMessage, MarketDataMessage


# How often (seconds) the control-message poller checks the queue.
_CONTROL_POLL_INTERVAL_S = 0.1


class DataCollectorProcess(multiprocessing.Process):
    """P1: Data Collector Process.

    Receives
    --------
    ControlMessages from the Orchestrator via *control_queue*:
        - ``subscribe``  -- ``data["symbols"]`` is a list of symbols to add.
        - ``unsubscribe`` -- ``data["symbols"]`` is a list of symbols to remove.
        - ``stop``       -- gracefully shuts down the process.

    Sends
    -----
    MarketDataMessages to P2 via *market_data_queue*:
        One message per completed candle (any symbol / any timeframe).

    Parameters
    ----------
    config:
        Application configuration dict.  Expected keys:

        - ``base_symbols``  -- e.g. ``["BTCUSDT", "ETHUSDT"]``
        - ``timeframes``    -- ``{"primary": "5m", "secondary": ["15m"],
          "btc_eth_trend": "1h"}``
        - ``candle_history`` -- number of historical candles to bootstrap
        - ``database``      -- ``{"path": "data/auto_bit.db"}``
    credentials:
        Dict with ``api_key`` and ``api_secret`` (may be empty for
        unauthenticated / market-data-only mode).
    control_queue:
        Queue from which this process reads :class:`ControlMessage` objects.
    market_data_queue:
        Queue into which this process writes :class:`MarketDataMessage`
        objects for downstream consumption by P2.
    """

    def __init__(
        self,
        config: dict,
        credentials: dict,
        control_queue: multiprocessing.Queue,
        market_data_queue: multiprocessing.Queue,
    ) -> None:
        super().__init__(name="P1-DataCollector", daemon=True)
        self._config = config
        self._credentials = credentials
        self._control_queue = control_queue
        self._market_data_queue = market_data_queue

        # Populated inside the child process (not picklable across fork).
        self._collector: DataCollector | None = None
        self._stop_event: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # Process entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Process entry point -- sets up loguru and runs the async main."""
        setup_logger("p1_collector")
        logger.info("P1 DataCollectorProcess starting (pid={})", self.pid)

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._main())
        except KeyboardInterrupt:
            logger.info("P1 interrupted by KeyboardInterrupt")
        except Exception as exc:
            logger.exception("P1 fatal error: {}", exc)
        finally:
            logger.info("P1 DataCollectorProcess exiting")

    # ------------------------------------------------------------------
    # Async main
    # ------------------------------------------------------------------

    async def _main(self) -> None:
        """Async entry point executed inside the child process.

        1. Create infrastructure objects (BybitClient, WS, DB, DataCollector).
        2. Start the DataCollector (connects WS, loads BTC/ETH history).
        3. Run the control-message listener until a ``stop`` command arrives.
        4. Tear down gracefully.
        """
        self._stop_event = asyncio.Event()

        # --- Build infrastructure ---
        api_key = self._credentials.get("api_key") or None
        api_secret = self._credentials.get("api_secret") or None
        client = BybitClient(api_key=api_key, api_secret=api_secret)

        db_path = self._config.get("database", {}).get("path")
        db = DatabaseManager(db_path=db_path)

        self._collector = DataCollector(
            bybit_client=client,
            ws_manager=None,  # set below after wiring callback
            db=db,
            config=self._config,
            on_candle_ready=self._on_candle_ready,
        )

        # Wire WebSocket callback to DataCollector._on_candle
        ws = WebSocketManager(on_candle_callback=self._collector._on_candle)
        self._collector._ws = ws

        # --- Start collection ---
        try:
            await self._collector.start()
        except Exception as exc:
            logger.error("P1 failed to start DataCollector: {}", exc)
            return

        logger.info("P1 DataCollector started -- entering control loop")

        # --- Control loop ---
        try:
            await self._process_control_messages()
        finally:
            await self._collector.stop()
            db.close()
            logger.info("P1 DataCollector stopped cleanly")

    # ------------------------------------------------------------------
    # Control-message processing
    # ------------------------------------------------------------------

    async def _process_control_messages(self) -> None:
        """Poll *control_queue* in a loop and dispatch commands.

        Runs until a ``stop`` command is received or ``_stop_event`` is set.
        """
        assert self._stop_event is not None

        while not self._stop_event.is_set():
            try:
                msg: ControlMessage = self._control_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(_CONTROL_POLL_INTERVAL_S)
                continue

            if not isinstance(msg, ControlMessage):
                logger.warning(
                    "P1 ignoring unexpected message type: {}", type(msg).__name__
                )
                continue

            command = msg.command
            data = msg.data

            if command == "stop":
                logger.info("P1 received stop command")
                self._stop_event.set()

            elif command == "subscribe":
                symbols = data.get("symbols", [])
                logger.info("P1 subscribing symbols: {}", symbols)
                for symbol in symbols:
                    await self._subscribe_symbol(symbol)

            elif command == "unsubscribe":
                symbols = data.get("symbols", [])
                logger.info("P1 unsubscribing symbols: {}", symbols)
                for symbol in symbols:
                    await self._unsubscribe_symbol(symbol)

            else:
                logger.warning("P1 ignoring unknown command: {}", command)

    # ------------------------------------------------------------------
    # Symbol management helpers
    # ------------------------------------------------------------------

    async def _subscribe_symbol(self, symbol: str) -> None:
        """Add a trading symbol via the DataCollector."""
        if self._collector is None:
            return
        try:
            await self._collector.add_symbol(symbol)
        except Exception as exc:
            logger.error("P1 failed to subscribe {}: {}", symbol, exc)

    async def _unsubscribe_symbol(self, symbol: str) -> None:
        """Remove a trading symbol via the DataCollector."""
        if self._collector is None:
            return
        try:
            await self._collector.remove_symbol(symbol)
        except Exception as exc:
            logger.error("P1 failed to unsubscribe {}: {}", symbol, exc)

    # ------------------------------------------------------------------
    # Candle callbacks
    # ------------------------------------------------------------------

    async def _on_ws_candle(
        self, symbol: str, interval: str, candle: Dict[str, Any]
    ) -> None:
        """Raw WebSocket candle callback.

        This is invoked by :class:`WebSocketManager` for every confirmed
        (closed) candle.  The :class:`DataCollector` already wires its own
        ``_on_candle`` through here, so we do not duplicate persistence
        logic -- the DB write happens inside DataCollector.  This callback
        exists only so the WS manager has a valid callable at construction
        time; the real forwarding happens in :meth:`_on_candle_ready`.
        """

    async def _on_candle_ready(
        self,
        symbol: str,
        timeframe: str,
        candle: Dict[str, Any],
    ) -> None:
        """Callback invoked by DataCollector after a candle is persisted.

        Packages the candle into a :class:`MarketDataMessage` and places it
        on the *market_data_queue* for consumption by P2.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTCUSDT"``.
        timeframe:
            Human-friendly timeframe, e.g. ``"5m"``, ``"1h"``.
        candle:
            Dict with ``timestamp``, ``open``, ``high``, ``low``,
            ``close``, ``volume``, ``is_closed``.
        """
        msg = MarketDataMessage(
            symbol=symbol,
            timeframe=timeframe,
            candle=candle,
        )

        try:
            self._market_data_queue.put_nowait(msg)
            logger.info("P1 → Queue: {} {} C={}", symbol, timeframe, candle.get("close"))
        except queue.Full:
            logger.warning(
                "P1 market_data_queue full -- dropping candle {} {}",
                symbol,
                timeframe,
            )

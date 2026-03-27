"""Market data collection orchestrator for auto_bit.

Combines REST historical loading with WebSocket real-time streaming and
SQLite persistence.  BTC/ETH candles are always collected (for trend
analysis); additional trading symbols can be added/removed dynamically.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, Optional

import pandas as pd
from loguru import logger

from src.collector.bybit_client import BybitClient
from src.collector.ws_manager import WebSocketManager
from src.utils.db import DatabaseManager


# ---------------------------------------------------------------------------
# Timeframe <-> Bybit interval mapping
# ---------------------------------------------------------------------------
# Bybit kline intervals: "1", "3", "5", "15", "30", "60", "120", "240",
#                         "360", "720", "D", "W", "M"
# We use human-friendly timeframe strings internally (e.g. "5m", "1h") and
# convert to Bybit format for API calls.

_TF_TO_INTERVAL: dict[str, str] = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",
    "1d": "D",
    "1w": "W",
    "1M": "M",
}

_INTERVAL_TO_TF: dict[str, str] = {v: k for k, v in _TF_TO_INTERVAL.items()}


def _tf(timeframe: str) -> str:
    """Convert a human-friendly timeframe to a Bybit interval string.

    Raises ``ValueError`` if the timeframe is unknown.
    """
    interval = _TF_TO_INTERVAL.get(timeframe)
    if interval is None:
        # Maybe the caller already passed a Bybit interval string.
        if timeframe in _INTERVAL_TO_TF:
            return timeframe
        raise ValueError(
            f"Unknown timeframe {timeframe!r}. "
            f"Valid values: {list(_TF_TO_INTERVAL.keys())}"
        )
    return interval


def _to_tf(interval: str) -> str:
    """Convert a Bybit interval string back to a human-friendly timeframe."""
    return _INTERVAL_TO_TF.get(interval, interval)


# ---------------------------------------------------------------------------
# Rate-limit helper
# ---------------------------------------------------------------------------

_REST_DELAY_S = 0.15  # ~6-7 requests/second; well within Bybit limits


# ---------------------------------------------------------------------------
# DataCollector
# ---------------------------------------------------------------------------

class DataCollector:
    """Manages market data collection from Bybit.

    - Always collects BTC/ETH candles (for trend analysis)
    - Dynamically subscribes/unsubscribes to selected trading symbols
    - Stores all candles in SQLite via :class:`DatabaseManager`
    - Loads historical candles on first subscription via REST

    Parameters
    ----------
    bybit_client:
        REST API client for fetching historical klines.
    ws_manager:
        WebSocket manager for real-time kline streaming.
    db:
        SQLite database manager for candle persistence.
    config:
        Configuration dict with keys:

        - ``base_symbols`` -- list of always-on symbols, e.g. ``["BTCUSDT", "ETHUSDT"]``
        - ``timeframes`` -- dict with ``primary`` (e.g. ``"5m"``),
          ``secondary`` (e.g. ``["15m"]``), and ``btc_eth_trend`` (e.g. ``"1h"``)
        - ``candle_history`` -- number of historical candles to load (default 100)
    on_candle_ready:
        Optional async callback invoked after every completed candle is
        persisted.  Signature: ``(symbol, timeframe, candle_dict) -> None``.
    """

    def __init__(
        self,
        bybit_client: BybitClient,
        ws_manager: WebSocketManager,
        db: DatabaseManager,
        config: dict[str, Any],
        on_candle_ready: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._client = bybit_client
        self._ws = ws_manager
        self._db = db

        # Config
        self._base_symbols: list[str] = config.get("base_symbols", ["BTCUSDT", "ETHUSDT"])
        tf_cfg = config.get("timeframes", {})
        self._primary_tf: str = tf_cfg.get("primary", "5m")
        self._secondary_tfs: list[str] = tf_cfg.get("secondary", ["15m"])
        self._trend_tf: str = tf_cfg.get("btc_eth_trend", "1h")
        self._candle_history: int = config.get("candle_history", 100)

        # External callback
        self._on_candle_ready = on_candle_ready

        # Active trading symbols (excludes base symbols).
        self._active_symbols: set[str] = set()

        # Pre-compute the full set of timeframes for base and trading symbols.
        self._base_timeframes: list[str] = sorted(
            {self._primary_tf, *self._secondary_tfs, self._trend_tf}
        )
        self._trading_timeframes: list[str] = sorted(
            {self._primary_tf, *self._secondary_tfs}
        )

        logger.info(
            "DataCollector initialised: base_symbols={} primary={} secondary={} "
            "trend={} history={}",
            self._base_symbols,
            self._primary_tf,
            self._secondary_tfs,
            self._trend_tf,
            self._candle_history,
        )

    # ==================================================================
    # Public lifecycle
    # ==================================================================

    async def start(self) -> None:
        """Start data collection.

        1. Connect the WebSocket.
        2. Load historical candles for BTC/ETH via REST.
        3. Subscribe BTC/ETH to WebSocket on all configured timeframes.
        """
        logger.info("DataCollector starting ...")

        # 1. Connect WebSocket (launches background task, returns when connected).
        await self._ws.connect()

        # 2 & 3. Bootstrap base symbols.
        for symbol in self._base_symbols:
            for tf in self._base_timeframes:
                await self._load_history(symbol, _tf(tf), self._candle_history)
                await asyncio.sleep(_REST_DELAY_S)

            # Subscribe to WebSocket streams.
            subs = [(symbol, _tf(tf)) for tf in self._base_timeframes]
            await self._ws.subscribe_many(subs)

        logger.info(
            "DataCollector started: {} base symbol(s) streaming on {} timeframe(s)",
            len(self._base_symbols),
            len(self._base_timeframes),
        )

    async def stop(self) -> None:
        """Disconnect WebSocket and clean up resources."""
        logger.info("DataCollector stopping ...")
        await self._ws.disconnect()
        self._active_symbols.clear()
        logger.info("DataCollector stopped")

    # ==================================================================
    # Dynamic symbol management
    # ==================================================================

    async def add_symbol(self, symbol: str) -> None:
        """Add a trading symbol for real-time collection.

        1. Load historical candles via REST for primary and secondary
           timeframes.
        2. Subscribe to WebSocket for real-time updates.

        If the symbol is already active (or is a base symbol), this is a
        no-op.
        """
        if symbol in self._active_symbols:
            logger.debug("Symbol {} is already active -- skipping", symbol)
            return

        if symbol in self._base_symbols:
            logger.debug(
                "Symbol {} is a base symbol (always collected) -- skipping add",
                symbol,
            )
            return

        logger.info("Adding trading symbol: {}", symbol)

        # Load historical candles for each trading timeframe.
        for tf in self._trading_timeframes:
            await self._load_history(symbol, _tf(tf), self._candle_history)
            await asyncio.sleep(_REST_DELAY_S)

        # Subscribe to WebSocket streams.
        subs = [(symbol, _tf(tf)) for tf in self._trading_timeframes]
        await self._ws.subscribe_many(subs)

        self._active_symbols.add(symbol)
        logger.info(
            "Symbol {} added: {} timeframe(s) active",
            symbol,
            len(self._trading_timeframes),
        )

    async def remove_symbol(self, symbol: str) -> None:
        """Remove a trading symbol from real-time collection.

        Unsubscribes from WebSocket streams.  Historical data is kept in
        the database for reference.
        """
        if symbol not in self._active_symbols:
            logger.debug("Symbol {} is not active -- nothing to remove", symbol)
            return

        if symbol in self._base_symbols:
            logger.warning(
                "Cannot remove base symbol {} -- it is always collected",
                symbol,
            )
            return

        logger.info("Removing trading symbol: {}", symbol)

        for tf in self._trading_timeframes:
            await self._ws.unsubscribe(symbol, _tf(tf))

        self._active_symbols.discard(symbol)
        logger.info("Symbol {} removed from real-time collection", symbol)

    def get_active_symbols(self) -> list[str]:
        """Return the list of currently subscribed trading symbols.

        Excludes base symbols (BTC/ETH) which are always collected.
        """
        return sorted(self._active_symbols)

    # ==================================================================
    # Data access
    # ==================================================================

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 100,
    ) -> pd.DataFrame:
        """Retrieve candles from the database as a pandas DataFrame.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTCUSDT"``.
        timeframe:
            Human-friendly timeframe, e.g. ``"5m"``, ``"1h"``.
        limit:
            Maximum number of candles to return.

        Returns
        -------
        DataFrame with columns ``open``, ``high``, ``low``, ``close``,
        ``volume``, ``timestamp``, sorted by ``timestamp`` ascending.
        Returns an empty DataFrame with the correct columns if no data is
        available.
        """
        # The DB stores the Bybit interval string as the timeframe key.
        interval = _tf(timeframe)
        rows = self._db.get_candles(symbol, interval, limit)

        if not rows:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "timestamp"]
            )

        df = pd.DataFrame(
            [dict(row) for row in rows],
            columns=["symbol", "timeframe", "timestamp", "open", "high", "low", "close", "volume"],
        )
        # Keep only the columns needed for indicator calculation.
        df = df[["open", "high", "low", "close", "volume", "timestamp"]]
        # Ensure ascending timestamp order (DB query already does this, but
        # be explicit).
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df

    # ==================================================================
    # Internal: WebSocket candle callback
    # ==================================================================

    async def _on_candle(
        self,
        symbol: str,
        interval: str,
        candle: dict[str, Any],
    ) -> None:
        """Handle a completed candle from the WebSocket.

        Persists the candle to SQLite and optionally invokes the
        ``on_candle_ready`` callback.

        Parameters
        ----------
        symbol:
            Trading pair.
        interval:
            Bybit interval string (e.g. ``"5"`` for 5-minute).
        candle:
            Dict with keys ``timestamp``, ``open``, ``high``, ``low``,
            ``close``, ``volume``, ``is_closed``.
        """
        timeframe = interval  # Store the Bybit interval as-is in the DB.

        self._db.insert_candle(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=int(candle["timestamp"]),
            open_=float(candle["open"]),
            high=float(candle["high"]),
            low=float(candle["low"]),
            close=float(candle["close"]),
            volume=float(candle["volume"]),
        )

        human_tf = _to_tf(interval)
        logger.info(
            "Candle saved: {} {} ts={} C={}",
            symbol,
            human_tf,
            candle["timestamp"],
            candle["close"],
        )

        # Invoke the external callback if provided.
        if self._on_candle_ready is not None:
            try:
                result = self._on_candle_ready(symbol, human_tf, candle)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.error(
                    "on_candle_ready callback error for {} {}: {}",
                    symbol,
                    human_tf,
                    exc,
                )

    # ==================================================================
    # Internal: historical candle loading
    # ==================================================================

    async def _load_history(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ) -> None:
        """Load historical candles via the REST API and persist to SQLite.

        Parameters
        ----------
        symbol:
            Trading pair.
        interval:
            Bybit interval string (e.g. ``"5"``, ``"60"``).
        limit:
            Number of historical candles to fetch.
        """
        human_tf = _to_tf(interval)
        logger.info(
            "Loading {} historical {} candles for {} ...",
            limit,
            human_tf,
            symbol,
        )

        try:
            # REST call runs in the default executor to avoid blocking the
            # event loop (pybit's HTTP client is synchronous).
            loop = asyncio.get_running_loop()
            klines = await loop.run_in_executor(
                None,
                lambda: self._client.get_klines(
                    symbol=symbol,
                    interval=interval,
                    limit=limit,
                ),
            )
        except Exception as exc:
            logger.error(
                "Failed to load history for {} {}: {}", symbol, human_tf, exc
            )
            return

        if not klines:
            logger.warning(
                "No historical candles returned for {} {}", symbol, human_tf
            )
            return

        # Build bulk-insert rows.
        rows: list[tuple[str, str, int, float, float, float, float, float]] = []
        for k in klines:
            rows.append((
                symbol,
                interval,
                int(k["startTime"]),
                float(k["open"]),
                float(k["high"]),
                float(k["low"]),
                float(k["close"]),
                float(k["volume"]),
            ))

        inserted = self._db.insert_candles_bulk(rows)
        logger.info(
            "Loaded {} {} candles for {} ({} persisted)",
            len(klines),
            human_tf,
            symbol,
            inserted,
        )

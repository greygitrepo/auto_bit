"""Bybit V5 public WebSocket manager for real-time kline (candle) streaming.

Connects to the Bybit V5 public linear WebSocket, subscribes to kline
topics, and emits completed candles via an async callback.  Handles
auto-reconnect with exponential backoff and Bybit's 20-second ping
requirement.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidStatusCode,
)
from loguru import logger


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WS_URL = "wss://stream.bybit.com/v5/public/linear"

# Bybit requires a ping every 20 seconds; we send one every 18s for safety.
PING_INTERVAL_S = 18.0

# Bybit allows a maximum of 10 args per subscribe/unsubscribe message.
MAX_ARGS_PER_MESSAGE = 10

# Reconnect backoff parameters.
RECONNECT_BASE_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 60.0
RECONNECT_BACKOFF_FACTOR = 2.0


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_topic(symbol: str, interval: str) -> str:
    """Build the Bybit kline topic string for a symbol and interval.

    Example: ``kline.5.BTCUSDT``
    """
    return f"kline.{interval}.{symbol}"


def _parse_topic(topic: str) -> Tuple[str, str]:
    """Extract ``(symbol, interval)`` from a topic like ``kline.5.BTCUSDT``."""
    parts = topic.split(".")
    # topic format: kline.<interval>.<symbol>
    return parts[2], parts[1]


# ---------------------------------------------------------------------------
# WebSocket Manager
# ---------------------------------------------------------------------------

class WebSocketManager:
    """Async WebSocket manager for Bybit V5 public kline streams.

    Parameters
    ----------
    on_candle_callback:
        Async or sync callable invoked when a *closed* candle arrives.
        Signature: ``(symbol: str, interval: str, candle: dict) -> None``.
        The *candle* dict contains: ``open``, ``high``, ``low``, ``close``,
        ``volume``, ``timestamp``, ``is_closed``.
    on_disconnect_callback:
        Optional async or sync callable invoked whenever the WebSocket
        disconnects unexpectedly.  Signature: ``() -> None``.
    """

    def __init__(
        self,
        on_candle_callback: Callable[..., Any],
        on_disconnect_callback: Callable[..., Any] | None = None,
        on_reconnect_callback: Callable[..., Any] | None = None,
    ) -> None:
        self._on_candle = on_candle_callback
        self._on_disconnect = on_disconnect_callback
        self._on_reconnect_callback = on_reconnect_callback

        # Active subscriptions as a set of (symbol, interval) tuples.
        self._subscriptions: Set[Tuple[str, str]] = set()

        # Internal state
        self._ws: Any = None  # websockets connection
        self._connected: bool = False
        self._running: bool = False
        self._reconnect_attempt: int = 0
        self._disconnect_time: float = 0.0  # tracks when disconnection occurred

        # Background tasks
        self._recv_task: asyncio.Task | None = None
        self._ping_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Return ``True`` if the WebSocket connection is currently open."""
        return self._connected and self._ws is not None

    def get_subscriptions(self) -> List[Tuple[str, str]]:
        """Return a copy of the current ``(symbol, interval)`` subscriptions."""
        return list(self._subscriptions)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Establish the WebSocket connection and start background loops.

        If subscriptions were registered before calling ``connect``, they
        are automatically sent after the connection is established.

        This method launches the connection loop as a background task and
        returns once the initial connection is established.
        """
        if self._running:
            logger.warning("WebSocketManager.connect called while already running")
            return

        self._running = True
        self._reconnect_attempt = 0
        self._connect_task = asyncio.ensure_future(self._connect_and_run())

        # Wait for the initial connection to be established (up to 10s).
        for _ in range(100):
            if self._connected:
                return
            await asyncio.sleep(0.1)
        logger.warning("WebSocketManager: connect() timed out waiting for connection")

    async def disconnect(self) -> None:
        """Gracefully close the WebSocket and cancel background tasks."""
        logger.info("WebSocketManager: disconnect requested")
        self._running = False
        if hasattr(self, '_connect_task') and self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
        await self._cleanup()
        logger.info("WebSocketManager: disconnected")

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------

    async def subscribe(self, symbol: str, interval: str) -> None:
        """Subscribe to a single kline stream.

        If already connected the subscribe message is sent immediately;
        otherwise the subscription is queued for the next connection.
        """
        pair = (symbol, interval)
        if pair in self._subscriptions:
            logger.debug("Already subscribed to {}.{}", symbol, interval)
            return

        self._subscriptions.add(pair)
        logger.info("Subscription added: {}.{}", symbol, interval)

        if self.is_connected:
            await self._send_subscribe([pair])

    async def unsubscribe(self, symbol: str, interval: str) -> None:
        """Unsubscribe from a single kline stream."""
        pair = (symbol, interval)
        if pair not in self._subscriptions:
            logger.debug("Not subscribed to {}.{} -- nothing to do", symbol, interval)
            return

        self._subscriptions.discard(pair)
        logger.info("Subscription removed: {}.{}", symbol, interval)

        if self.is_connected:
            await self._send_unsubscribe([pair])

    async def subscribe_many(self, subscriptions: List[Tuple[str, str]]) -> None:
        """Subscribe to multiple kline streams at once.

        Parameters
        ----------
        subscriptions:
            List of ``(symbol, interval)`` tuples.
        """
        new_pairs: List[Tuple[str, str]] = []
        for pair in subscriptions:
            if pair not in self._subscriptions:
                self._subscriptions.add(pair)
                new_pairs.append(pair)

        if not new_pairs:
            logger.debug("subscribe_many: all pairs already subscribed")
            return

        logger.info("subscribe_many: adding {} new subscription(s)", len(new_pairs))

        if self.is_connected:
            await self._send_subscribe(new_pairs)

    # ------------------------------------------------------------------
    # Internal: connection helpers
    # ------------------------------------------------------------------

    async def _connect_and_run(self) -> None:
        """Main connection loop with auto-reconnect."""
        while self._running:
            try:
                await self._establish_connection()
                self._reconnect_attempt = 0

                # Re-subscribe to all active topics after (re)connect.
                if self._subscriptions:
                    await self._send_subscribe(list(self._subscriptions))

                # Notify gap detection after reconnect
                if self._disconnect_time > 0:
                    gap_seconds = int(time.time() - self._disconnect_time)
                    if gap_seconds > 60:
                        logger.warning(
                            "WebSocket reconnected after {}s gap - candles may have been missed",
                            gap_seconds,
                        )
                    if self._on_reconnect_callback:
                        try:
                            result = self._on_reconnect_callback(gap_seconds)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as exc:
                            logger.error("on_reconnect callback error: {}", exc)
                    self._disconnect_time = 0.0

                # Start background tasks.
                self._ping_task = asyncio.ensure_future(self._ping_loop())
                await self._recv_loop()

            except (ConnectionClosed, ConnectionClosedError, OSError) as exc:
                self._disconnect_time = time.time()
                logger.warning("WebSocket connection lost: {}", exc)
            except asyncio.CancelledError:
                logger.info("WebSocket tasks cancelled")
                break
            except Exception as exc:
                self._disconnect_time = time.time()
                logger.error("Unexpected WebSocket error: {}", exc)
            finally:
                was_connected = self._connected
                await self._cleanup()

                if was_connected and self._on_disconnect:
                    try:
                        result = self._on_disconnect()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:
                        logger.error("on_disconnect callback error: {}", exc)

            if not self._running:
                break

            # Exponential backoff before reconnect.
            delay = min(
                RECONNECT_BASE_DELAY_S * (RECONNECT_BACKOFF_FACTOR ** self._reconnect_attempt),
                RECONNECT_MAX_DELAY_S,
            )
            self._reconnect_attempt += 1
            logger.info(
                "Reconnecting in {:.1f}s (attempt {}) ...",
                delay,
                self._reconnect_attempt,
            )
            await asyncio.sleep(delay)

    async def _establish_connection(self) -> None:
        """Open the WebSocket connection."""
        logger.info("Connecting to {}", WS_URL)
        self._ws = await websockets.connect(
            WS_URL,
            ping_interval=None,   # We handle pings manually.
            ping_timeout=None,
            close_timeout=5,
        )
        self._connected = True
        logger.info("WebSocket connected")

    async def _cleanup(self) -> None:
        """Cancel background tasks and close the socket."""
        self._connected = False

        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        self._ping_task = None

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None

    # ------------------------------------------------------------------
    # Internal: send subscribe / unsubscribe
    # ------------------------------------------------------------------

    async def _send_subscribe(self, pairs: List[Tuple[str, str]]) -> None:
        """Send subscribe message(s), batching to respect the 10-arg limit."""
        topics = [_make_topic(sym, ivl) for sym, ivl in pairs]
        await self._send_topic_message("subscribe", topics)

    async def _send_unsubscribe(self, pairs: List[Tuple[str, str]]) -> None:
        """Send unsubscribe message(s), batching to respect the 10-arg limit."""
        topics = [_make_topic(sym, ivl) for sym, ivl in pairs]
        await self._send_topic_message("unsubscribe", topics)

    async def _send_topic_message(self, op: str, topics: List[str]) -> None:
        """Send a subscribe or unsubscribe message, batching if necessary."""
        for i in range(0, len(topics), MAX_ARGS_PER_MESSAGE):
            batch = topics[i : i + MAX_ARGS_PER_MESSAGE]
            msg = {"op": op, "args": batch}
            await self._send_json(msg)
            logger.debug("Sent {} for {} topic(s): {}", op, len(batch), batch)

    async def _send_json(self, data: dict) -> None:
        """Serialize and send a JSON message over the WebSocket."""
        if self._ws is None:
            logger.warning("Cannot send message -- WebSocket is not connected")
            return
        try:
            await self._ws.send(json.dumps(data))
        except ConnectionClosed:
            logger.warning("Send failed -- connection closed")
            raise

    # ------------------------------------------------------------------
    # Internal: ping loop
    # ------------------------------------------------------------------

    async def _ping_loop(self) -> None:
        """Periodically send Bybit-style ping frames.

        Bybit expects a JSON ``{"op": "ping"}`` message (not a WebSocket
        protocol-level ping) at least every 20 seconds.
        """
        try:
            while self._connected and self._ws is not None:
                await asyncio.sleep(PING_INTERVAL_S)
                if self._connected and self._ws is not None:
                    await self._send_json({"op": "ping"})
                    logger.trace("Ping sent")
        except asyncio.CancelledError:
            pass
        except ConnectionClosed:
            logger.debug("Ping loop ended -- connection closed")
        except Exception as exc:
            logger.error("Ping loop error: {}", exc)

    # ------------------------------------------------------------------
    # Internal: receive loop
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        """Read messages from the WebSocket until disconnected."""
        if self._ws is None:
            return

        async for raw_msg in self._ws:
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                logger.warning("Non-JSON message received: {}", raw_msg[:200])
                continue

            await self._handle_message(msg)

    async def _handle_message(self, msg: Dict[str, Any]) -> None:
        """Route an incoming parsed JSON message."""

        # Bybit responses to subscribe/unsubscribe/ping
        if "op" in msg:
            op = msg["op"]
            success = msg.get("success", False)
            if op == "pong":
                logger.trace("Pong received")
            elif op == "subscribe":
                if success:
                    logger.debug("Subscribe confirmed: {}", msg.get("conn_id", ""))
                else:
                    logger.error("Subscribe failed: {}", msg.get("ret_msg", ""))
            elif op == "unsubscribe":
                if success:
                    logger.debug("Unsubscribe confirmed")
                else:
                    logger.error("Unsubscribe failed: {}", msg.get("ret_msg", ""))
            return

        # Data messages have a "topic" field.
        topic = msg.get("topic")
        if topic is None:
            logger.trace("Ignored message without topic: {}", str(msg)[:200])
            return

        if not topic.startswith("kline."):
            logger.trace("Ignored non-kline topic: {}", topic)
            return

        await self._handle_kline(topic, msg)

    async def _handle_kline(self, topic: str, msg: Dict[str, Any]) -> None:
        """Process a kline data message.

        Only emits the callback when the candle is *closed*
        (``confirm == true`` in Bybit's payload).
        """
        symbol, interval = _parse_topic(topic)
        data_list = msg.get("data", [])

        for candle_raw in data_list:
            confirm = candle_raw.get("confirm", False)
            if not confirm:
                continue

            candle = {
                "timestamp": int(candle_raw.get("start", 0)),
                "open": candle_raw.get("open", "0"),
                "high": candle_raw.get("high", "0"),
                "low": candle_raw.get("low", "0"),
                "close": candle_raw.get("close", "0"),
                "volume": candle_raw.get("volume", "0"),
                "is_closed": True,
            }

            logger.info(
                "Closed candle: {}.{} ts={} C={} V={}",
                symbol,
                interval,
                candle["timestamp"],
                candle["close"],
                candle["volume"],
            )

            try:
                result = self._on_candle(symbol, interval, candle)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.error(
                    "on_candle callback error for {}.{}: {}", symbol, interval, exc
                )

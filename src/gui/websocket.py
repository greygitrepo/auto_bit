"""WebSocket support for real-time GUI updates.

Provides a ``ConnectionManager`` for tracking active WebSocket clients,
a ``GUIUpdater`` background task that periodically pushes position and
statistics data, and the ``/ws`` endpoint handler.

Task: G-04 (WebSocket real-time updates).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

from src.utils.db import DatabaseManager


# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Manages active WebSocket connections.

    Provides connect/disconnect lifecycle management and broadcast
    capability for pushing updates to all connected clients.
    """

    def __init__(self) -> None:
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(
            "WebSocket client connected (total={})",
            len(self.active_connections),
        )

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove a WebSocket connection from the active list."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        logger.info(
            "WebSocket client disconnected (total={})",
            len(self.active_connections),
        )

    async def broadcast(self, data: Dict[str, Any]) -> None:
        """Send a JSON message to all connected clients.

        Silently removes clients that have disconnected or error during send.
        """
        if not self.active_connections:
            return

        message = json.dumps(data, default=str)
        disconnected: List[WebSocket] = []

        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                disconnected.append(connection)

        for conn in disconnected:
            self.disconnect(conn)

    @property
    def client_count(self) -> int:
        """Return the number of active connections."""
        return len(self.active_connections)


# Module-level singleton
manager = ConnectionManager()


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint handler for ``/ws``.

    Keeps the connection alive and listens for client messages (pings,
    keepalives).  Actual data push is handled by ``GUIUpdater``.
    """
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; handle pings / client messages
            data = await websocket.receive_text()

            # Respond to explicit ping messages
            if data == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as exc:
        logger.debug("WebSocket connection error: {}", exc)
        manager.disconnect(websocket)


# ---------------------------------------------------------------------------
# GUI Updater (background push task)
# ---------------------------------------------------------------------------


class GUIUpdater:
    """Background task that reads the DB periodically and pushes updates
    to all connected WebSocket clients.

    Parameters
    ----------
    db:
        A ``DatabaseManager`` instance for querying position and trade data.
    mode:
        Trading mode (``"paper"`` or ``"live"``).
    manager:
        The ``ConnectionManager`` singleton for broadcasting messages.
    """

    # Update intervals (seconds)
    POSITION_INTERVAL = 2.0
    STATS_INTERVAL = 30.0

    def __init__(
        self,
        db: DatabaseManager,
        mode: str,
        manager: ConnectionManager,
    ) -> None:
        self._db = db
        self._mode = mode
        self._manager = manager
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._last_trade_count = 0
        self._start_time = time.time()

    async def start(self) -> None:
        """Launch the update loop tasks."""
        self._running = True
        self._tasks = [
            asyncio.create_task(self._position_update_loop()),
            asyncio.create_task(self._stats_update_loop()),
        ]
        logger.info("GUIUpdater started (mode={})", self._mode)

        # Wait for tasks to complete (they run until stopped)
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            logger.info("GUIUpdater tasks cancelled")

    def stop(self) -> None:
        """Signal update loops to stop."""
        self._running = False
        for task in self._tasks:
            if not task.done():
                task.cancel()
        logger.info("GUIUpdater stop requested")

    # ------------------------------------------------------------------
    # Position updates (every 2 seconds)
    # ------------------------------------------------------------------

    async def _position_update_loop(self) -> None:
        """Push position data to clients every ``POSITION_INTERVAL`` seconds.

        Payload::

            {
                "type": "position_update",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                        "entry_price": 65000.0,
                        "unrealized_pnl": 12.50,
                        "elapsed_min": 15,
                        "remaining_min": 75,
                        ...
                    }
                ]
            }
        """
        while self._running:
            try:
                if self._manager.client_count > 0:
                    positions = await asyncio.to_thread(
                        self._fetch_positions,
                    )
                    await self._manager.broadcast({
                        "type": "positions",
                        "data": positions,
                    })
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Position update error: {}", exc)

            await asyncio.sleep(self.POSITION_INTERVAL)

    def _fetch_positions(self) -> List[Dict[str, Any]]:
        """Query open positions from the database (runs in thread).

        Fetches live ticker prices so that current_price and P&L % are accurate.
        """
        try:
            rows = self._db.get_open_positions(self._mode)
        except Exception:
            return []

        if not rows:
            return []

        # Fetch latest ticker prices
        ticker_prices: Dict[str, float] = {}
        try:
            from src.collector.bybit_client import BybitClient
            client = BybitClient()
            tickers = client.get_tickers()
            for t in tickers:
                sym = t.get("symbol", "")
                last = float(t.get("lastPrice", 0))
                if sym and last > 0:
                    ticker_prices[sym] = last
        except Exception:
            pass

        now_ts = int(time.time())
        result = []

        for row in rows:
            pos = dict(row)
            entered_at = pos.get("entered_at", now_ts)
            max_hold = pos.get("max_hold_minutes", 90)
            elapsed_min = max(0, (now_ts - entered_at) // 60)
            remaining_min = max(0, max_hold - elapsed_min)

            entry_price = float(pos.get("entry_price", 0))
            size = float(pos.get("size", 0))
            margin = float(pos.get("margin", 0))
            symbol = pos.get("symbol", "")
            side = pos.get("side", "")

            current_price = ticker_prices.get(symbol, entry_price)

            # Calculate unrealized P&L from current price
            if current_price > 0 and entry_price > 0 and size > 0:
                if side == "Buy":
                    unrealized_pnl = (current_price - entry_price) * size
                else:
                    unrealized_pnl = (entry_price - current_price) * size
            else:
                unrealized_pnl = float(pos.get("unrealized_pnl") or 0.0)

            # P&L % based on margin (ROI)
            unrealized_pnl_pct = (unrealized_pnl / margin * 100) if margin > 0 else 0.0

            result.append({
                "id": pos.get("id"),
                "symbol": symbol,
                "side": side,
                "size": size,
                "entry_price": entry_price,
                "current_price": current_price,
                "leverage": pos.get("leverage", 1),
                "stop_loss": pos.get("stop_loss"),
                "take_profit": pos.get("take_profit"),
                "unrealized_pnl": round(unrealized_pnl, 6),
                "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
                "strategy": pos.get("strategy"),
                "entry_reason": pos.get("entry_reason", ""),
                "trailing_stop_active": pos.get("trailing_stop_active", False),
                "elapsed_min": elapsed_min,
                "elapsed_minutes": elapsed_min,
                "remaining_min": remaining_min,
                "max_hold_minutes": max_hold,
            })

        return result

    # ------------------------------------------------------------------
    # Stats updates (every 30 seconds)
    # ------------------------------------------------------------------

    async def _stats_update_loop(self) -> None:
        """Push balance and daily stats to clients every ``STATS_INTERVAL`` seconds.

        Payload::

            {
                "type": "stats_update",
                "data": {
                    "balance": 10250.00,
                    "daily_pnl": 125.50,
                    "trade_count": 5,
                    "win_count": 3,
                    "consecutive_losses": 0,
                    "drawdown_pct": 1.2,
                    "drawdown_stage": 0,
                    "cooldown_active": false,
                    "position_count": 2,
                    "timestamp": 1700000000
                }
            }
        """
        while self._running:
            try:
                if self._manager.client_count > 0:
                    stats = await asyncio.to_thread(self._fetch_stats)
                    await self._manager.broadcast({
                        "type": "stats",
                        "data": stats,
                    })
                    # Also broadcast as 'summary' for the dashboard
                    await self._manager.broadcast({
                        "type": "summary",
                        "data": stats,
                    })
                    # Broadcast trade_closed if trade count changed
                    trade_count = stats.get("trade_count", 0)
                    if trade_count != self._last_trade_count:
                        if self._last_trade_count > 0:
                            await self._manager.broadcast({
                                "type": "trade_closed",
                                "data": {"trade_count": trade_count},
                            })
                        self._last_trade_count = trade_count
                    # Broadcast trading status
                    await self._manager.broadcast({
                        "type": "status",
                        "data": {
                            "mode": self._mode,
                            "running": True,
                        },
                    })
                    # Broadcast equity update for live chart
                    await self._manager.broadcast({
                        "type": "equity_update",
                        "data": {
                            "time": int(time.time()),
                            "value": stats.get("current_balance", stats.get("balance", 0)),
                        },
                    })
                    # Broadcast system info
                    import os
                    db_path = getattr(self._db, "db_path", "data/auto_bit.db")
                    try:
                        db_size = os.path.getsize(db_path)
                    except OSError:
                        db_size = 0
                    await self._manager.broadcast({
                        "type": "system_info",
                        "data": {
                            "db_size": db_size,
                            "uptime": int(time.time() - self._start_time),
                            "last_scan_time": None,
                        },
                    })
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Stats update error: {}", exc)

            await asyncio.sleep(self.STATS_INTERVAL)

    def _fetch_stats(self) -> Dict[str, Any]:
        """Query balance and daily stats from the database (runs in thread)."""
        now_ts = int(time.time())

        # Initial balance
        init_raw = self._db.get_state(f"initial_balance_{self._mode}")
        initial_balance = float(init_raw) if init_raw else 20.0

        # Balance = initial + realized P&L (same logic as /api/summary)
        realized_pnl = 0.0
        try:
            conn = self._db._get_connection()
            cur = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) as p FROM trades WHERE mode = ?",
                (self._mode,),
            )
            row = cur.fetchone()
            if row:
                realized_pnl = row["p"]
        except Exception:
            pass

        balance = round(initial_balance + realized_pnl, 4)

        # Daily stats from trades
        from datetime import datetime, timezone

        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        daily_pnl = 0.0
        trade_count = 0
        win_count = 0

        try:
            conn = self._db._get_connection()
            cur = conn.execute(
                """
                SELECT pnl FROM trades
                WHERE mode = ? AND date(exit_time, 'unixepoch') = ?
                ORDER BY exit_time ASC
                """,
                (self._mode, today),
            )
            day_trades = [dict(r) for r in cur.fetchall()]
            daily_pnl = sum(t["pnl"] for t in day_trades if t["pnl"] is not None)
            trade_count = len(day_trades)
            win_count = sum(1 for t in day_trades if (t["pnl"] or 0) > 0)
        except Exception:
            pass

        # Consecutive losses
        consecutive_losses = 0
        try:
            conn = self._db._get_connection()
            cur = conn.execute(
                """
                SELECT pnl FROM trades
                WHERE mode = ? AND exit_time IS NOT NULL
                ORDER BY exit_time DESC
                """,
                (self._mode,),
            )
            for row in cur:
                if (row["pnl"] or 0) > 0:
                    break
                consecutive_losses += 1
        except Exception:
            pass

        # Drawdown
        drawdown_pct_raw = self._db.get_state(f"drawdown_pct_{self._mode}")
        drawdown_pct = float(drawdown_pct_raw) if drawdown_pct_raw else 0.0

        drawdown_stage_raw = self._db.get_state(f"drawdown_stage_{self._mode}")
        drawdown_stage = int(drawdown_stage_raw) if drawdown_stage_raw else 0

        # Cooldown
        cooldown_raw = self._db.get_state(f"cooldown_until_{self._mode}")
        cooldown_active = False
        if cooldown_raw:
            cooldown_active = float(cooldown_raw) > now_ts

        # Position count
        try:
            positions = self._db.get_open_positions(self._mode)
            position_count = len(positions)
        except Exception:
            position_count = 0

        # Compute alias values for dashboard compatibility
        total_pnl = round(balance - initial_balance, 4)
        total_pnl_pct = round(total_pnl / initial_balance * 100, 2) if initial_balance > 0 else 0.0
        win_rate = round(win_count / trade_count * 100, 1) if trade_count > 0 else 0.0

        # Daily loss = sum of negative pnl today
        daily_loss = 0.0
        try:
            conn2 = self._db._get_connection()
            cur2 = conn2.execute(
                """
                SELECT COALESCE(SUM(pnl), 0) as neg_sum FROM trades
                WHERE mode = ? AND date(exit_time, 'unixepoch') = ? AND pnl < 0
                """,
                (self._mode, today),
            )
            row2 = cur2.fetchone()
            if row2:
                daily_loss = abs(row2["neg_sum"])
        except Exception:
            pass

        return {
            "balance": round(balance, 2),
            "daily_pnl": round(daily_pnl, 4),
            "trade_count": trade_count,
            "win_count": win_count,
            "consecutive_losses": consecutive_losses,
            "drawdown_pct": round(drawdown_pct, 4),
            "drawdown_stage": drawdown_stage,
            "cooldown_active": cooldown_active,
            "position_count": position_count,
            "timestamp": now_ts,
            # Aliases for dashboard updateSummary
            "current_balance": round(balance, 2),
            "initial_balance": round(initial_balance, 2),
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "today_pnl": round(daily_pnl, 4),
            "today_trades": trade_count,
            # Aliases for dashboard updateStats
            "win_rate": win_rate,
            "daily_loss": round(daily_loss, 4),
            "daily_loss_limit": 10.0,
            "max_trades": 100,
            "btc_trend": "Mixed",
            "eth_trend": "Mixed",
            "cooldown": str(cooldown_active) if cooldown_active else None,
        }

    # ------------------------------------------------------------------
    # Event push (one-time events)
    # ------------------------------------------------------------------

    async def push_event(self, event_type: str, message: str) -> None:
        """Push a one-time event notification to all connected clients.

        Parameters
        ----------
        event_type:
            Classification of the event (e.g. ``trade_filled``, ``sl_hit``,
            ``tp_hit``, ``timeout``, ``error``).
        message:
            Human-readable event description.

        Payload::

            {
                "type": "event",
                "data": {
                    "event_type": "trade_filled",
                    "message": "BTCUSDT Buy filled at 65000.0",
                    "timestamp": 1700000000
                }
            }
        """
        await self._manager.broadcast({
            "type": "event",
            "data": {
                "event_type": event_type,
                "message": message,
                "timestamp": int(time.time()),
            },
        })
        logger.info("GUI event pushed: {} - {}", event_type, message)


# Module-level updater reference (set during app startup)
gui_updater: Optional[GUIUpdater] = None

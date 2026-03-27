"""Position tracking, P&L calculation, and performance metrics.

Provides the ``PositionTracker`` class that manages the full lifecycle of
trading positions -- from opening through real-time P&L updates to closing
with realized P&L recording -- and exposes rich performance analytics over
historical trades.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from src.strategy.asset.base import DailyStats
from src.utils.db import DatabaseManager


class PositionTracker:
    """Tracks positions, calculates P&L, and records performance metrics.

    All database queries are scoped to ``self.mode`` so paper and live
    trading data remain fully isolated.

    Parameters
    ----------
    db:
        A ``DatabaseManager`` instance for persistence.
    mode:
        ``'paper'`` or ``'live'`` -- determines which data partition to use.
    """

    def __init__(self, db: DatabaseManager, mode: str) -> None:
        if mode not in ("paper", "live"):
            raise ValueError(f"mode must be 'paper' or 'live', got {mode!r}")
        self.db = db
        self.mode = mode
        logger.info("PositionTracker initialised in '{}' mode", mode)

    # ===================================================================
    # E-06: Position P&L
    # ===================================================================

    def add_position(self, position_data: dict) -> int:
        """Record a new open position in the database.

        Parameters
        ----------
        position_data:
            Dictionary whose keys match the ``positions`` table columns
            (excluding ``id`` and ``mode``, which are set automatically).

        Returns
        -------
        int
            The newly created position ID.
        """
        data = {**position_data, "mode": self.mode}
        # Ensure entered_at is present.
        if "entered_at" not in data:
            data["entered_at"] = int(time.time())
        position_id = self.db.insert_position(**data)
        logger.info(
            "Added position id={} symbol={} side={} size={}",
            position_id,
            data.get("symbol"),
            data.get("side"),
            data.get("size"),
        )
        return position_id

    def update_position_pnl(self, position_id: int, current_price: float) -> None:
        """Update the unrealized P&L for an open position.

        Parameters
        ----------
        position_id:
            ID of the position to update.
        current_price:
            The latest market price used to calculate unrealized P&L.
        """
        position = self._get_position_row(position_id)
        if position is None:
            logger.warning("Cannot update P&L -- position {} not found", position_id)
            return

        unrealized_pnl = self._calculate_pnl(
            side=position["side"],
            entry_price=position["entry_price"],
            exit_price=current_price,
            size=position["size"],
        )
        self.db.update_position(position_id, unrealized_pnl=unrealized_pnl)

    def close_position(
        self,
        position_id: int,
        exit_price: float,
        exit_reason: str,
        exit_type: str,
        fee: float = 0.0,
    ) -> dict:
        """Close a position, record the trade, and update daily performance.

        Steps:
        1. Calculate realized P&L.
        2. Insert a completed trade record into the ``trades`` table.
        3. Remove the position from the ``positions`` table.
        4. Recalculate daily performance for today.

        Parameters
        ----------
        position_id:
            ID of the position to close.
        exit_price:
            Price at which the position was exited.
        exit_reason:
            Human-readable reason for the exit.
        exit_type:
            Exit classification (``SL``, ``TP``, ``trailing``, ``strategy``, ``timeout``).
        fee:
            Total fees incurred for this trade (entry + exit).

        Returns
        -------
        dict
            ``{pnl: float, fee: float, holding_minutes: int}``
        """
        position = self._get_position_row(position_id)
        if position is None:
            logger.error("Cannot close position {} -- not found", position_id)
            return {"pnl": 0.0, "fee": fee, "holding_minutes": 0}

        gross_pnl = self._calculate_pnl(
            side=position["side"],
            entry_price=position["entry_price"],
            exit_price=exit_price,
            size=position["size"],
        )
        net_pnl = gross_pnl - fee

        now_ts = int(time.time())
        holding_minutes = max(0, (now_ts - position["entered_at"]) // 60)

        # Record trade.
        self.db.insert_trade(
            mode=self.mode,
            symbol=position["symbol"],
            side=position["side"],
            size=position["size"],
            entry_price=position["entry_price"],
            exit_price=exit_price,
            pnl=net_pnl,
            fee=fee,
            leverage=position["leverage"],
            strategy=position["strategy"],
            entry_time=position["entered_at"],
            exit_time=now_ts,
            entry_reason=None,
            exit_reason=exit_reason,
            exit_type=exit_type,
        )

        # Remove position.
        self.db.delete_position(position_id)

        # Update daily stats.
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        self.update_daily_performance(date=today)

        logger.info(
            "Closed position {} | pnl={:.4f} fee={:.4f} held={}min exit_type={}",
            position_id,
            net_pnl,
            fee,
            holding_minutes,
            exit_type,
        )

        return {"pnl": net_pnl, "fee": fee, "holding_minutes": holding_minutes}

    def get_open_positions(self) -> list[dict]:
        """Return all open positions for the current mode as dicts."""
        rows = self.db.get_open_positions(self.mode)
        return [dict(row) for row in rows]

    def get_position_by_symbol(self, symbol: str) -> Optional[dict]:
        """Return the open position for *symbol*, or ``None`` if none exists."""
        conn = self.db._get_connection()
        cur = conn.execute(
            "SELECT * FROM positions WHERE mode = ? AND symbol = ? LIMIT 1",
            (self.mode, symbol),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    # ===================================================================
    # E-07: Performance Metrics
    # ===================================================================

    def get_performance_stats(self, days: int = 30) -> dict:
        """Calculate aggregate performance statistics over the last *days* days.

        Returns
        -------
        dict
            Keys: ``total_trades``, ``win_count``, ``loss_count``, ``win_rate``,
            ``avg_pnl``, ``total_pnl``, ``profit_factor``, ``avg_win``,
            ``avg_loss``, ``win_loss_ratio``, ``max_consecutive_wins``,
            ``max_consecutive_losses``, ``avg_holding_minutes``,
            ``exit_type_breakdown``.
        """
        trades = self._fetch_trades_since_days(days)
        return self._compute_stats(trades)

    def get_symbol_stats(self, symbol: str) -> dict:
        """Performance statistics filtered to a single *symbol*."""
        conn = self.db._get_connection()
        cur = conn.execute(
            """
            SELECT * FROM trades
            WHERE mode = ? AND symbol = ? AND exit_time IS NOT NULL
            ORDER BY exit_time ASC
            """,
            (self.mode, symbol),
        )
        trades = [dict(r) for r in cur.fetchall()]
        stats = self._compute_stats(trades)
        stats["symbol"] = symbol
        return stats

    def get_equity_curve(self, days: int = 30) -> list[dict]:
        """Build a daily equity curve for charting over the last *days* days.

        Returns
        -------
        list[dict]
            Each entry: ``{date, balance, pnl, cumulative_pnl, trade_count, win_rate}``.
        """
        conn = self.db._get_connection()
        cutoff = self._days_ago_date_str(days)
        cur = conn.execute(
            """
            SELECT * FROM daily_performance
            WHERE mode = ? AND date >= ?
            ORDER BY date ASC
            """,
            (self.mode, cutoff),
        )
        rows = [dict(r) for r in cur.fetchall()]

        curve: list[dict] = []
        cumulative_pnl = 0.0
        for row in rows:
            cumulative_pnl += row.get("pnl", 0.0)
            trade_count = row.get("trade_count", 0)
            win_count = row.get("win_count", 0)
            win_rate = (win_count / trade_count) if trade_count > 0 else 0.0
            curve.append(
                {
                    "date": row["date"],
                    "balance": row.get("ending_balance", 0.0),
                    "pnl": row.get("pnl", 0.0),
                    "cumulative_pnl": cumulative_pnl,
                    "trade_count": trade_count,
                    "win_rate": round(win_rate, 4),
                }
            )
        return curve

    # ===================================================================
    # E-08: Daily Performance
    # ===================================================================

    def get_daily_stats(self, date: Optional[str] = None) -> DailyStats:
        """Return a ``DailyStats`` for *date* (defaults to today UTC).

        Queries the trades table to build the stats, including the current
        streak of consecutive losses needed by the asset strategy layer.
        """
        if date is None:
            date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

        conn = self.db._get_connection()

        # Trades closed on the target date.
        cur = conn.execute(
            """
            SELECT pnl FROM trades
            WHERE mode = ? AND date(exit_time, 'unixepoch') = ?
            ORDER BY exit_time ASC
            """,
            (self.mode, date),
        )
        day_trades = [dict(r) for r in cur.fetchall()]

        pnl = sum(t["pnl"] for t in day_trades if t["pnl"] is not None)
        trade_count = len(day_trades)
        win_count = sum(1 for t in day_trades if (t["pnl"] or 0) > 0)

        # Consecutive losses -- look at today's trades in chronological order
        # and also extend backwards into prior days if the day started on a
        # losing streak.
        consecutive_losses = self._get_consecutive_losses(date)

        # Check for existing cooldown state.
        cooldown_raw = self.db.get_state(f"cooldown_until_{self.mode}")
        cooldown_until: float | None = None
        if cooldown_raw is not None:
            cooldown_ts = float(cooldown_raw)
            if cooldown_ts > time.time():
                cooldown_until = cooldown_ts

        return DailyStats(
            date=date,
            pnl=pnl,
            trade_count=trade_count,
            win_count=win_count,
            consecutive_losses=consecutive_losses,
            cooldown_until=cooldown_until,
        )

    def update_daily_performance(self, date: Optional[str] = None) -> None:
        """Recalculate and persist the ``daily_performance`` row for *date*.

        Typically called automatically after each position close.
        """
        if date is None:
            date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

        conn = self.db._get_connection()
        cur = conn.execute(
            """
            SELECT pnl FROM trades
            WHERE mode = ? AND date(exit_time, 'unixepoch') = ?
            """,
            (self.mode, date),
        )
        day_trades = [dict(r) for r in cur.fetchall()]

        pnl = sum(t["pnl"] for t in day_trades if t["pnl"] is not None)
        trade_count = len(day_trades)
        win_count = sum(1 for t in day_trades if (t["pnl"] or 0) > 0)

        # Retrieve or estimate starting / ending balance.
        existing = self.db.get_daily_performance(date, self.mode)
        if existing:
            starting_balance = existing["starting_balance"]
        else:
            # Use the ending balance of the previous day, or fall back to
            # initial_balance from system_state (set by P3 on startup).
            prev = conn.execute(
                """
                SELECT ending_balance FROM daily_performance
                WHERE mode = ? AND date < ?
                ORDER BY date DESC LIMIT 1
                """,
                (self.mode, date),
            ).fetchone()
            if prev:
                starting_balance = prev["ending_balance"]
            else:
                # First day: use initial balance from config
                init_raw = self.db.get_state(f"initial_balance_{self.mode}")
                starting_balance = float(init_raw) if init_raw else 20.0

        ending_balance = starting_balance + pnl

        self.db.upsert_daily_performance(
            date=date,
            mode=self.mode,
            starting_balance=starting_balance,
            ending_balance=ending_balance,
            pnl=pnl,
            trade_count=trade_count,
            win_count=win_count,
        )
        logger.debug(
            "Daily performance updated for {} | pnl={:.4f} trades={} wins={}",
            date,
            pnl,
            trade_count,
            win_count,
        )

    def get_monthly_summary(self) -> list[dict]:
        """Return a monthly performance summary across all recorded history.

        Returns
        -------
        list[dict]
            Each entry: ``{month, total_pnl, trade_count, win_rate, best_day, worst_day}``.
        """
        conn = self.db._get_connection()
        cur = conn.execute(
            """
            SELECT
                strftime('%%Y-%%m', date) AS month,
                SUM(pnl)                  AS total_pnl,
                SUM(trade_count)          AS trade_count,
                SUM(win_count)            AS win_count,
                MAX(pnl)                  AS best_day,
                MIN(pnl)                  AS worst_day
            FROM daily_performance
            WHERE mode = ?
            GROUP BY month
            ORDER BY month ASC
            """,
            (self.mode,),
        )
        results: list[dict] = []
        for row in cur.fetchall():
            row_d = dict(row)
            tc = row_d.get("trade_count") or 0
            wc = row_d.get("win_count") or 0
            results.append(
                {
                    "month": row_d["month"],
                    "total_pnl": row_d.get("total_pnl") or 0.0,
                    "trade_count": tc,
                    "win_rate": round(wc / tc, 4) if tc > 0 else 0.0,
                    "best_day": row_d.get("best_day") or 0.0,
                    "worst_day": row_d.get("worst_day") or 0.0,
                }
            )
        return results

    # ===================================================================
    # Private helpers
    # ===================================================================

    def _get_position_row(self, position_id: int) -> Optional[dict]:
        """Fetch a single position by ID, scoped to current mode."""
        conn = self.db._get_connection()
        cur = conn.execute(
            "SELECT * FROM positions WHERE id = ? AND mode = ?",
            (position_id, self.mode),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    @staticmethod
    def _calculate_pnl(
        side: str,
        entry_price: float,
        exit_price: float,
        size: float,
    ) -> float:
        """Calculate gross P&L for a position.

        ``size`` is the quantity in base currency (e.g. number of coins).

        * LONG  (Buy):  ``(exit - entry) * size``
        * SHORT (Sell): ``(entry - exit) * size``
        """
        if side == "Buy":
            return (exit_price - entry_price) * size
        else:
            return (entry_price - exit_price) * size

    def _fetch_trades_since_days(self, days: int) -> list[dict]:
        """Return closed trades from the last *days* calendar days."""
        cutoff = self._days_ago_timestamp(days)
        conn = self.db._get_connection()
        cur = conn.execute(
            """
            SELECT * FROM trades
            WHERE mode = ? AND exit_time IS NOT NULL AND exit_time >= ?
            ORDER BY exit_time ASC
            """,
            (self.mode, cutoff),
        )
        return [dict(r) for r in cur.fetchall()]

    def _compute_stats(self, trades: list[dict]) -> dict:
        """Compute aggregate stats from a list of trade dicts."""
        total_trades = len(trades)

        if total_trades == 0:
            return {
                "total_trades": 0,
                "win_count": 0,
                "loss_count": 0,
                "win_rate": 0.0,
                "avg_pnl": 0.0,
                "total_pnl": 0.0,
                "profit_factor": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "win_loss_ratio": 0.0,
                "max_consecutive_wins": 0,
                "max_consecutive_losses": 0,
                "avg_holding_minutes": 0.0,
                "exit_type_breakdown": {},
            }

        pnls = [t.get("pnl") or 0.0 for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        gross_profit = sum(wins) if wins else 0.0
        gross_loss = abs(sum(losses)) if losses else 0.0

        # Consecutive streaks.
        max_con_wins = 0
        max_con_losses = 0
        cur_wins = 0
        cur_losses = 0
        for p in pnls:
            if p > 0:
                cur_wins += 1
                cur_losses = 0
                max_con_wins = max(max_con_wins, cur_wins)
            else:
                cur_losses += 1
                cur_wins = 0
                max_con_losses = max(max_con_losses, cur_losses)

        # Holding time.
        holding_mins: list[float] = []
        for t in trades:
            entry_t = t.get("entry_time") or 0
            exit_t = t.get("exit_time") or 0
            if entry_t and exit_t:
                holding_mins.append(max(0.0, (exit_t - entry_t) / 60.0))

        # Exit type breakdown.
        exit_types: dict[str, int] = {}
        for t in trades:
            et = t.get("exit_type") or "unknown"
            exit_types[et] = exit_types.get(et, 0) + 1

        avg_win = (gross_profit / len(wins)) if wins else 0.0
        avg_loss = (gross_loss / len(losses)) if losses else 0.0

        return {
            "total_trades": total_trades,
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(len(wins) / total_trades, 4),
            "avg_pnl": round(sum(pnls) / total_trades, 4),
            "total_pnl": round(sum(pnls), 4),
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else float("inf"),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "win_loss_ratio": round(avg_win / avg_loss, 4) if avg_loss > 0 else float("inf"),
            "max_consecutive_wins": max_con_wins,
            "max_consecutive_losses": max_con_losses,
            "avg_holding_minutes": round(sum(holding_mins) / len(holding_mins), 2) if holding_mins else 0.0,
            "exit_type_breakdown": exit_types,
        }

    def _get_consecutive_losses(self, date: str) -> int:
        """Count the current streak of consecutive losses ending at *date*.

        Walks backwards through trades (including prior days) until a win is
        encountered or no more trades exist.
        """
        conn = self.db._get_connection()
        cur = conn.execute(
            """
            SELECT pnl FROM trades
            WHERE mode = ? AND exit_time IS NOT NULL AND date(exit_time, 'unixepoch') <= ?
            ORDER BY exit_time DESC
            """,
            (self.mode, date),
        )
        streak = 0
        for row in cur:
            if (row["pnl"] or 0) > 0:
                break
            streak += 1
        return streak

    @staticmethod
    def _days_ago_timestamp(days: int) -> int:
        """Return a Unix timestamp for *days* ago from now."""
        return int(time.time()) - days * 86400

    @staticmethod
    def _days_ago_date_str(days: int) -> str:
        """Return a YYYY-MM-DD string for *days* ago from now (UTC)."""
        ts = time.time() - days * 86400
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

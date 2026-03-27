"""System crash recovery manager.

Handles restoring consistent state after an unexpected shutdown. Synchronises
database records with exchange state (live mode) or validates paper positions
against time limits, and restores daily statistics and drawdown stages.

Task C-10.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger

from src.collector.bybit_client import BybitClient
from src.strategy.asset.base import DailyStats
from src.strategy.position.momentum_scalper import TimeLimitManager
from src.utils.db import DatabaseManager


class RecoveryManager:
    """Handles system crash recovery on startup.

    Compares persisted state in the database against live exchange state
    (or paper mode constraints) and reconciles any discrepancies caused
    by downtime.

    Parameters
    ----------
    db:
        Database manager for reading/writing recovery state.
    bybit_client:
        Authenticated Bybit client for live-mode synchronisation.
        May be ``None`` in paper mode.
    mode:
        ``"paper"`` or ``"live"``.
    """

    def __init__(
        self,
        db: DatabaseManager,
        bybit_client: Optional[BybitClient] = None,
        mode: str = "paper",
    ) -> None:
        if mode not in ("paper", "live"):
            raise ValueError(f"Invalid mode: {mode!r}")
        self.db = db
        self.bybit_client = bybit_client
        self.mode = mode

    # ------------------------------------------------------------------
    # Main recovery entry point
    # ------------------------------------------------------------------

    async def recover(self) -> Dict[str, Any]:
        """Run the full recovery routine.

        Steps:
        1. Load system_state from DB.
        2. If live mode: sync with Bybit (compare DB vs exchange positions).
        3. If paper mode: check paper positions against current prices.
        4. Restore daily stats counters.
        5. Check/restore drawdown stage.
        6. Fix positions with expired time limits.

        Returns
        -------
        dict
            Recovery summary with keys:
            ``recovered_positions``, ``closed_during_downtime``,
            ``daily_stats_restored``, ``drawdown_stage``.
        """
        logger.info("Recovery starting (mode={})", self.mode)

        recovered_positions = 0
        closed_during_downtime = 0

        # 1. Load system state
        last_shutdown = self.db.get_state("last_shutdown_time")
        if last_shutdown:
            logger.info("Last shutdown recorded at: {}", last_shutdown)
        else:
            logger.info("No previous shutdown time recorded (first run or unclean exit)")

        # 2. Sync positions with exchange / validate paper positions
        db_positions = self.db.get_open_positions(self.mode)
        db_positions_list = [dict(p) for p in db_positions]

        if self.mode == "live" and self.bybit_client is not None:
            sync_result = await self._sync_live_positions(db_positions_list)
            recovered_positions = sync_result["synced"]
            closed_during_downtime = sync_result["closed_during_downtime"]
        else:
            # Paper mode: positions persist in DB, just count them
            recovered_positions = len(db_positions_list)
            logger.info(
                "Paper mode: {} positions found in DB",
                recovered_positions,
            )

        # 3. Check for time-expired positions
        expired = self.check_time_expired_positions(db_positions_list)
        if expired:
            logger.warning(
                "{} position(s) have expired time limits, closing",
                len(expired),
            )
            for pos in expired:
                self._close_expired_position(pos)
                closed_during_downtime += 1
                recovered_positions = max(0, recovered_positions - 1)

        # 4. Restore daily stats
        daily_stats = self.restore_daily_stats()
        daily_stats_restored = daily_stats is not None

        # 5. Restore drawdown stage
        drawdown_stage = self._restore_drawdown_stage()

        # 6. Record recovery timestamp
        self.db.set_state("last_recovery_time", str(int(time.time())))

        result = {
            "recovered_positions": recovered_positions,
            "closed_during_downtime": closed_during_downtime,
            "daily_stats_restored": daily_stats_restored,
            "drawdown_stage": drawdown_stage,
        }

        logger.info(
            "Recovery complete: recovered={}, closed_during_downtime={}, "
            "daily_stats={}, drawdown_stage={}",
            recovered_positions,
            closed_during_downtime,
            daily_stats_restored,
            drawdown_stage,
        )

        return result

    # ------------------------------------------------------------------
    # Live position sync
    # ------------------------------------------------------------------

    async def _sync_live_positions(
        self, db_positions: List[Dict[str, Any]]
    ) -> Dict[str, int]:
        """Compare DB positions against Bybit exchange state.

        Returns
        -------
        dict
            ``{synced, closed_during_downtime, orphaned}``
        """
        import asyncio

        synced = 0
        closed_during_downtime = 0
        orphaned = 0

        db_symbols = {p["symbol"] for p in db_positions}

        for pos in db_positions:
            symbol = pos["symbol"]
            position_id = pos["id"]

            try:
                exchange_positions = await asyncio.get_event_loop().run_in_executor(
                    None, self.bybit_client.get_positions, symbol
                )
                has_position = len(exchange_positions) > 0
            except Exception as exc:
                logger.error(
                    "Failed to query exchange for {}: {}", symbol, exc
                )
                # Assume position still exists to avoid data loss
                synced += 1
                continue

            if has_position:
                synced += 1
                logger.info("Position {} ({}) confirmed on exchange", position_id, symbol)
            else:
                closed_during_downtime += 1
                logger.warning(
                    "Position {} ({}) not found on exchange -- closed during downtime",
                    position_id, symbol,
                )
                self._record_downtime_close(pos)

        # Check for orphaned exchange positions (positions on exchange but not in DB)
        try:
            import asyncio
            all_exchange = await asyncio.get_event_loop().run_in_executor(
                None, self.bybit_client.get_positions, None
            )
            for ex_pos in all_exchange:
                ex_symbol = ex_pos.get("symbol", "")
                if ex_symbol and ex_symbol not in db_symbols:
                    orphaned += 1
                    pos_side = ex_pos.get("side", "")
                    logger.warning(
                        "Orphaned exchange position: {} {} size={}",
                        ex_symbol,
                        pos_side,
                        ex_pos.get("size"),
                    )
                    # Create DB record for orphaned exchange position so it can be tracked
                    try:
                        self.db.insert_position(
                            mode=self.mode,
                            symbol=ex_symbol,
                            side=pos_side,
                            size=float(ex_pos.get("size", 0)),
                            entry_price=float(ex_pos.get("avgPrice", 0)),
                            leverage=int(ex_pos.get("leverage", 1)),
                            stop_loss=0.0,
                            take_profit=0.0,
                            margin=float(ex_pos.get("positionIM", 0)),
                            unrealized_pnl=float(ex_pos.get("unrealisedPnl", 0)),
                            strategy="recovered",
                            scanner_direction="",
                            entered_at=int(time.time()),
                        )
                        logger.info("Recovery: created DB record for orphaned position {}", ex_symbol)
                        synced += 1
                    except Exception as exc:
                        logger.warning("Recovery: failed to create DB record for {}: {}", ex_symbol, exc)
        except Exception as exc:
            logger.error("Failed to check for orphaned positions: {}", exc)

        logger.info(
            "Live sync: synced={}, closed_during_downtime={}, orphaned={}",
            synced, closed_during_downtime, orphaned,
        )

        return {
            "synced": synced,
            "closed_during_downtime": closed_during_downtime,
            "orphaned": orphaned,
        }

    # ------------------------------------------------------------------
    # Daily stats restoration
    # ------------------------------------------------------------------

    def restore_daily_stats(self) -> Optional[DailyStats]:
        """Load today's stats from DB.

        Returns
        -------
        DailyStats or None
            The restored stats, or ``None`` if no data exists for today.
        """
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

        conn = self.db._get_connection()
        cur = conn.execute(
            """
            SELECT pnl FROM trades
            WHERE mode = ? AND date(exit_time, 'unixepoch') = ?
            ORDER BY exit_time ASC
            """,
            (self.mode, today),
        )
        day_trades = [dict(r) for r in cur.fetchall()]

        if not day_trades:
            logger.info("No trades found for today ({}), starting fresh", today)
            return DailyStats(date=today)

        pnl = sum(t["pnl"] for t in day_trades if t["pnl"] is not None)
        trade_count = len(day_trades)
        win_count = sum(1 for t in day_trades if (t["pnl"] or 0) > 0)

        # Calculate consecutive losses from the tail
        consecutive_losses = 0
        for t in reversed(day_trades):
            if (t["pnl"] or 0) > 0:
                break
            consecutive_losses += 1

        # Check for cooldown state
        cooldown_raw = self.db.get_state(f"cooldown_until_{self.mode}")
        cooldown_until = None
        if cooldown_raw is not None:
            cooldown_ts = float(cooldown_raw)
            if cooldown_ts > time.time():
                cooldown_until = cooldown_ts

        stats = DailyStats(
            date=today,
            pnl=pnl,
            trade_count=trade_count,
            win_count=win_count,
            consecutive_losses=consecutive_losses,
            cooldown_until=cooldown_until,
        )

        logger.info(
            "Restored daily stats: date={} pnl={:.4f} trades={} wins={} "
            "consecutive_losses={}",
            today, pnl, trade_count, win_count, consecutive_losses,
        )

        return stats

    # ------------------------------------------------------------------
    # Time-expired positions
    # ------------------------------------------------------------------

    def check_time_expired_positions(
        self, positions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Find positions past the 90-minute holding limit.

        Parameters
        ----------
        positions:
            List of open position dicts.

        Returns
        -------
        list
            Positions that have exceeded their maximum holding time.
        """
        expired = []
        for pos in positions:
            entered_at = pos.get("entered_at", 0)
            max_hold = pos.get("max_hold_minutes", 90)

            if entered_at <= 0:
                continue

            status, elapsed = TimeLimitManager.check(
                entered_at=entered_at,
                max_minutes=max_hold,
            )

            if status == "expired":
                logger.info(
                    "Position {} ({}) expired: {}min > {}min",
                    pos.get("id"), pos.get("symbol"), elapsed, max_hold,
                )
                expired.append(pos)

        return expired

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _restore_drawdown_stage(self) -> int:
        """Restore the drawdown stage from system_state."""
        stage_raw = self.db.get_state(f"drawdown_stage_{self.mode}")
        if stage_raw is not None:
            try:
                stage = int(stage_raw)
                logger.info("Restored drawdown stage: {}", stage)
                return stage
            except ValueError:
                pass
        return 0

    def _record_downtime_close(self, pos: Dict[str, Any]) -> None:
        """Record a position that was closed during downtime."""
        exit_price = float(pos.get("entry_price", 0.0))  # At least use entry_price, not 0
        if self.bybit_client is not None:
            try:
                closed = self.bybit_client.get_closed_pnl(pos["symbol"], 5)
                if closed:
                    exit_price = float(closed[0].get("avgExitPrice", exit_price))
            except Exception:
                pass

        self.db.insert_trade(
            mode=self.mode,
            symbol=pos["symbol"],
            side=pos["side"],
            size=float(pos["size"]),
            entry_price=float(pos["entry_price"]),
            exit_price=exit_price,
            pnl=0.0,
            fee=0.0,
            leverage=int(pos.get("leverage", 1)),
            strategy=pos.get("strategy", ""),
            entry_time=int(pos.get("entered_at", 0)),
            exit_time=int(time.time()),
            entry_reason=pos.get("scanner_direction", ""),
            exit_reason="closed_during_downtime",
            exit_type="unknown",
        )
        self.db.delete_position(pos["id"])

    def _close_expired_position(self, pos: Dict[str, Any]) -> None:
        """Close a position that exceeded its time limit during downtime."""
        entered_at = pos.get("entered_at", 0)
        elapsed_min = int((time.time() - entered_at) / 60) if entered_at > 0 else 0

        exit_price = float(pos.get("entry_price", 0.0))  # At least use entry_price, not 0

        self.db.insert_trade(
            mode=self.mode,
            symbol=pos["symbol"],
            side=pos["side"],
            size=float(pos["size"]),
            entry_price=float(pos["entry_price"]),
            exit_price=exit_price,
            pnl=0.0,
            fee=0.0,
            leverage=int(pos.get("leverage", 1)),
            strategy=pos.get("strategy", ""),
            entry_time=int(pos.get("entered_at", 0)),
            exit_time=int(time.time()),
            entry_reason=pos.get("scanner_direction", ""),
            exit_reason=f"time_limit_expired_during_downtime ({elapsed_min}min)",
            exit_type="timeout",
        )
        self.db.delete_position(pos["id"])

        logger.info(
            "Closed expired position {} ({}) held for {}min",
            pos.get("id"), pos.get("symbol"), elapsed_min,
        )

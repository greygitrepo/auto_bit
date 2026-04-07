"""REST API endpoints for the GUI backend.

Provides query endpoints (G-02) for reading trading data from the SQLite
database and control endpoints (G-03) for sending commands to the
Orchestrator via the control queue.

All database access is read-only through ``DatabaseManager`` and
``PositionTracker``.  Control commands are forwarded through a
``multiprocessing.Queue`` -- the GUI never imports trading engine modules
directly.

Tasks: G-02 (Query endpoints), G-03 (Control endpoints).
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api", tags=["api"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class StopRequest(BaseModel):
    """Body for the trading stop endpoint."""

    force_close: bool = False


class ControlResponse(BaseModel):
    """Standard response for control commands."""

    status: str
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_tracker(request: Request):
    """Retrieve the PositionTracker from app state, or raise 503."""
    tracker = getattr(request.app.state, "tracker", None)
    if tracker is None:
        raise HTTPException(status_code=503, detail="Tracker not initialised")
    return tracker


def _get_db(request: Request):
    """Retrieve the DatabaseManager from app state, or raise 503."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    return db


def _get_control_queue(request: Request):
    """Retrieve the control queue from app state, or raise 503."""
    q = getattr(request.app.state, "control_queue", None)
    if q is None:
        raise HTTPException(
            status_code=503,
            detail="Control queue not available (running in read-only mode)",
        )
    return q


def _safe_float(value: Any) -> float:
    """Convert a value to a JSON-safe float (replace inf/nan with 0)."""
    if value is None:
        return 0.0
    f = float(value)
    if math.isinf(f) or math.isnan(f):
        return 0.0
    return f


# ===================================================================
# G-02: Query endpoints
# ===================================================================


@router.get("/status")
async def get_status(request: Request) -> Dict[str, Any]:
    """Return system status overview.

    Returns
    -------
    dict
        ``{mode, trading_active, uptime, process_states}``
    """
    db = _get_db(request)
    mode = getattr(request.app.state, "mode", "paper")
    start_time = getattr(request.app.state, "start_time", time.time())
    uptime_seconds = int(time.time() - start_time)

    # Check trading active state from system_state table
    trading_active_raw = db.get_state("trading_active")
    trading_active = trading_active_raw == "true" if trading_active_raw else True

    # Process states from system_state
    # Build both formats: "process_states" for API clients,
    # "processes" with lowercase keys and {status: ...} objects for the GUI JS.
    process_states = {}
    processes = {}
    for proc_name in ("P1", "P2", "P3", "P5"):
        state = db.get_state(f"process_{proc_name}_state")
        resolved = state or "unknown"
        process_states[proc_name] = resolved
        processes[proc_name.lower()] = {"status": resolved}

    return {
        "mode": mode,
        "trading_active": trading_active,
        "uptime": uptime_seconds,
        "process_states": process_states,
        "processes": processes,
    }


@router.get("/summary")
async def get_summary(request: Request) -> Dict[str, Any]:
    """Return combined summary for the dashboard.

    Merges balance, daily stats, and position count into a single response
    matching the dashboard JS expectations.
    """
    db = _get_db(request)
    mode = getattr(request.app.state, "mode", "paper")

    # Initial balance (from DB or config fallback)
    initial_balance_raw = db.get_state(f"initial_balance_{mode}")
    initial_balance = float(initial_balance_raw) if initial_balance_raw else 0.0

    if initial_balance == 0.0:
        config = getattr(request.app.state, "config", {})
        if isinstance(config, dict):
            initial_balance = config.get("capital", {}).get("initial_balance", 20.0)
        else:
            initial_balance = 20.0

    # Total realized P&L from closed trades (source of truth)
    total_realized_pnl = 0.0
    total_fees = 0.0
    try:
        conn = db._get_connection()
        cur = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total_pnl, COALESCE(SUM(fee), 0) as total_fee FROM trades WHERE mode = ?",
            (mode,),
        )
        row = cur.fetchone()
        if row:
            total_realized_pnl = row["total_pnl"]
            total_fees = row["total_fee"]
    except Exception:
        pass

    # Equity = initial + realized P&L
    # Note: open position entry fees are already deducted from the paper
    # executor balance and will be reflected when those positions close.
    current_balance = round(initial_balance + total_realized_pnl, 4)

    total_pnl = total_realized_pnl
    total_pnl_pct = (total_pnl / initial_balance * 100) if initial_balance > 0 else 0.0

    # Drawdown
    drawdown_pct_raw = db.get_state(f"drawdown_pct_{mode}")
    drawdown_pct = float(drawdown_pct_raw) if drawdown_pct_raw else 0.0
    drawdown_stage_raw = db.get_state(f"drawdown_stage_{mode}")
    drawdown_stage = int(drawdown_stage_raw) if drawdown_stage_raw else 0

    # Daily stats from trades
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    today_pnl = 0.0
    today_trades = 0
    try:
        conn = db._get_connection()
        cur = conn.execute(
            """
            SELECT pnl FROM trades
            WHERE mode = ? AND date(exit_time, 'unixepoch') = ?
            """,
            (mode, today),
        )
        day_trades = [dict(r) for r in cur.fetchall()]
        today_pnl = sum(t["pnl"] for t in day_trades if t["pnl"] is not None)
        today_trades = len(day_trades)
    except Exception:
        pass

    return {
        "initial_balance": round(initial_balance, 2),
        "current_balance": round(current_balance, 2),
        "total_pnl": round(total_pnl, 4),
        "total_pnl_pct": round(total_pnl_pct, 4),
        "drawdown_pct": round(drawdown_pct, 4),
        "drawdown_stage": drawdown_stage,
        "today_pnl": round(today_pnl, 4),
        "today_trades": today_trades,
    }


@router.get("/balance")
async def get_balance(request: Request) -> Dict[str, Any]:
    """Return balance and drawdown information.

    Uses the same trades-based calculation as /api/summary for consistency.
    """
    db = _get_db(request)
    mode = getattr(request.app.state, "mode", "paper")

    # Initial balance
    initial_balance_raw = db.get_state(f"initial_balance_{mode}")
    initial_balance = float(initial_balance_raw) if initial_balance_raw else 20.0

    # Total realized P&L from trades (source of truth)
    total_pnl = 0.0
    try:
        conn = db._get_connection()
        cur = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as p FROM trades WHERE mode = ?",
            (mode,),
        )
        row = cur.fetchone()
        if row:
            total_pnl = row["p"]
    except Exception:
        pass

    current_balance = round(initial_balance + total_pnl, 4)
    total_pnl_pct = (total_pnl / initial_balance * 100) if initial_balance > 0 else 0.0

    # Drawdown
    drawdown_pct_raw = db.get_state(f"drawdown_pct_{mode}")
    drawdown_pct = float(drawdown_pct_raw) if drawdown_pct_raw else 0.0

    drawdown_stage_raw = db.get_state(f"drawdown_stage_{mode}")
    drawdown_stage = int(drawdown_stage_raw) if drawdown_stage_raw else 0

    return {
        "initial_balance": round(initial_balance, 2),
        "current_balance": round(current_balance, 2),
        "total_pnl": round(total_pnl, 4),
        "total_pnl_pct": round(total_pnl_pct, 4),
        "drawdown_pct": round(drawdown_pct, 4),
        "drawdown_stage": drawdown_stage,
    }


@router.get("/positions")
async def get_positions(request: Request) -> List[Dict[str, Any]]:
    """Return all open positions with unrealized P&L and timing info.

    Fetches current prices via the Bybit REST API so that ``current_price``
    and ``unrealized_pnl_pct`` are accurate and up-to-date.
    """
    tracker = _get_tracker(request)
    positions = tracker.get_open_positions()
    now_ts = int(time.time())

    # Fetch latest ticker prices for open position symbols
    ticker_prices: Dict[str, float] = {}
    if positions:
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
            pass  # Fall back to DB unrealized_pnl

    result = []
    for pos in positions:
        entered_at = pos.get("entered_at", now_ts)
        max_hold = pos.get("max_hold_minutes", 90)
        elapsed_min = max(0, (now_ts - entered_at) // 60)
        remaining_min = max(0, max_hold - elapsed_min)

        entry_price = float(pos.get("entry_price", 0))
        size = float(pos.get("size", 0))
        leverage = pos.get("leverage", 1)
        margin = float(pos.get("margin", 0))
        symbol = pos.get("symbol", "")
        side = pos.get("side", "")

        # Use live ticker price, fall back to entry_price
        current_price = ticker_prices.get(symbol, entry_price)

        # Calculate unrealized P&L from current price
        if current_price > 0 and entry_price > 0 and size > 0:
            if side == "Buy":
                unrealized_pnl = (current_price - entry_price) * size
            else:
                unrealized_pnl = (entry_price - current_price) * size
        else:
            unrealized_pnl = _safe_float(pos.get("unrealized_pnl"))

        # P&L % based on margin (ROI) -- reflects leverage
        if margin > 0:
            unrealized_pnl_pct = (unrealized_pnl / margin) * 100
        else:
            unrealized_pnl_pct = 0.0

        result.append({
            "id": pos.get("id"),
            "symbol": symbol,
            "side": side,
            "size": size,
            "entry_price": entry_price,
            "current_price": current_price,
            "leverage": leverage,
            "stop_loss": pos.get("stop_loss"),
            "take_profit": pos.get("take_profit"),
            "margin": margin,
            "unrealized_pnl": round(unrealized_pnl, 6),
            "unrealized_pnl_pct": round(_safe_float(unrealized_pnl_pct), 2),
            "strategy": pos.get("strategy"),
            "scanner_direction": pos.get("scanner_direction"),
            "entry_reason": pos.get("entry_reason", ""),
            "trailing_stop_active": pos.get("trailing_stop_active", False),
            "elapsed_min": elapsed_min,
            "elapsed_minutes": elapsed_min,
            "remaining_min": remaining_min,
            "max_hold_minutes": max_hold,
            "entered_at": entered_at,
        })

    return result


@router.get("/positions/{symbol}")
async def get_position_by_symbol(request: Request, symbol: str) -> Dict[str, Any]:
    """Return position detail for a specific symbol.

    Parameters
    ----------
    symbol:
        Trading pair symbol (e.g. ``BTCUSDT``).

    Raises
    ------
    HTTPException 404
        If no open position exists for the given symbol.
    """
    tracker = _get_tracker(request)
    pos = tracker.get_position_by_symbol(symbol)

    if pos is None:
        raise HTTPException(
            status_code=404,
            detail=f"No open position for symbol '{symbol}'",
        )

    now_ts = int(time.time())
    entered_at = pos.get("entered_at", now_ts)
    max_hold = pos.get("max_hold_minutes", 90)
    elapsed_min = max(0, (now_ts - entered_at) // 60)
    remaining_min = max(0, max_hold - elapsed_min)

    return {
        "id": pos.get("id"),
        "symbol": pos.get("symbol"),
        "side": pos.get("side"),
        "size": pos.get("size"),
        "entry_price": pos.get("entry_price"),
        "leverage": pos.get("leverage", 1),
        "stop_loss": pos.get("stop_loss"),
        "take_profit": pos.get("take_profit"),
        "margin": pos.get("margin"),
        "unrealized_pnl": _safe_float(pos.get("unrealized_pnl")),
        "strategy": pos.get("strategy"),
        "scanner_direction": pos.get("scanner_direction"),
        "elapsed_min": elapsed_min,
        "remaining_min": remaining_min,
        "entered_at": entered_at,
    }


@router.get("/trades")
async def get_trades(
    request: Request,
    mode: Optional[str] = Query(None, description="Trading mode (paper/live)"),
    days: int = Query(30, ge=1, le=365, description="Number of days to look back"),
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    page: int = Query(1, ge=1, description="Page number"),
    per_page: int = Query(50, ge=1, le=200, description="Results per page"),
    from_date: Optional[str] = Query(None, alias="from", description="Start date (YYYY-MM-DD)"),
    to_date: Optional[str] = Query(None, alias="to", description="End date (YYYY-MM-DD)"),
) -> Dict[str, Any]:
    """Return paginated trade history.

    Supports date-range filtering via ``from`` and ``to`` query parameters
    (YYYY-MM-DD).  Falls back to ``days`` look-back when dates are not
    provided.

    Returns
    -------
    dict
        ``{trades, total, page, pages, summary, exit_breakdown}``
    """
    db = _get_db(request)
    effective_mode = mode or getattr(request.app.state, "mode", "paper")

    # Determine time window: prefer explicit from/to, else use days
    if from_date:
        try:
            from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            cutoff_ts = int(from_dt.timestamp())
        except ValueError:
            cutoff_ts = int(time.time()) - days * 86400
    else:
        cutoff_ts = int(time.time()) - days * 86400

    conn = db._get_connection()

    # Build query with optional symbol and date-range filters
    where_clauses = ["mode = ?", "exit_time IS NOT NULL", "exit_time >= ?"]
    params: list[Any] = [effective_mode, cutoff_ts]

    if to_date:
        try:
            to_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            # Include the entire "to" day
            to_ts = int(to_dt.timestamp()) + 86400
            where_clauses.append("exit_time < ?")
            params.append(to_ts)
        except ValueError:
            pass  # ignore invalid to_date

    if symbol:
        where_clauses.append("symbol = ?")
        params.append(symbol)

    where_sql = " AND ".join(where_clauses)

    # Count total matching trades
    count_cur = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM trades WHERE {where_sql}",
        params,
    )
    total = count_cur.fetchone()["cnt"]

    # Fetch ALL trades for summary (not paginated)
    all_cur = conn.execute(
        f"SELECT pnl, exit_type, entry_time, exit_time FROM trades WHERE {where_sql} ORDER BY exit_time DESC",
        params,
    )
    all_trades_for_summary = [dict(r) for r in all_cur.fetchall()]

    # Calculate pagination
    pages = max(1, math.ceil(total / per_page))
    offset = (page - 1) * per_page

    # Fetch page of trades
    data_cur = conn.execute(
        f"""
        SELECT * FROM trades
        WHERE {where_sql}
        ORDER BY exit_time DESC
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, offset],
    )
    trades = [dict(row) for row in data_cur.fetchall()]

    # Sanitize floats
    for trade in trades:
        for key in ("pnl", "fee", "entry_price", "exit_price", "size"):
            if key in trade:
                trade[key] = _safe_float(trade[key])

    # Compute summary stats from ALL matching trades
    total_pnl = sum(t.get("pnl", 0) for t in all_trades_for_summary)
    wins = [t for t in all_trades_for_summary if (t.get("pnl", 0) > 0)]
    losses = [t for t in all_trades_for_summary if (t.get("pnl", 0) <= 0)]
    win_rate = (len(wins) / len(all_trades_for_summary) * 100) if all_trades_for_summary else 0.0
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0.0
    profit_factor = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))) if losses and sum(t["pnl"] for t in losses) != 0 else 0.0

    # Average holding time
    hold_times = []
    for t in all_trades_for_summary:
        if t.get("entry_time") and t.get("exit_time"):
            hold_times.append((t["exit_time"] - t["entry_time"]) / 60)
    avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0.0

    pnl_values = [t.get("pnl", 0) for t in all_trades_for_summary]

    summary = {
        "total_trades": total,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(_safe_float(profit_factor), 2),
        "avg_hold_minutes": round(avg_hold, 1),
        "best_trade": round(max(pnl_values), 4) if pnl_values else 0.0,
        "worst_trade": round(min(pnl_values), 4) if pnl_values else 0.0,
    }

    # Exit type breakdown
    exit_breakdown = {}
    for t in all_trades_for_summary:
        et = t.get("exit_type", "unknown")
        exit_breakdown[et] = exit_breakdown.get(et, 0) + 1

    return {
        "trades": trades,
        "total": total,
        "page": page,
        "total_pages": pages,
        "pages": pages,
        "summary": summary,
        "exit_breakdown": exit_breakdown,
    }


@router.get("/stats")
async def get_stats(
    request: Request,
    days: int = Query(30, ge=1, le=365, description="Number of days to look back"),
) -> Dict[str, Any]:
    """Return aggregate performance statistics.

    Returns
    -------
    dict
        Keys include ``total_trades, win_count, loss_count, win_rate, avg_pnl,
        total_pnl, profit_factor, avg_win, avg_loss, win_loss_ratio,
        max_consecutive_wins, max_consecutive_losses, avg_holding_minutes,
        exit_type_breakdown``.
    """
    tracker = _get_tracker(request)
    stats = tracker.get_performance_stats(days=days)

    # Sanitize infinity values for JSON serialisation
    for key in ("profit_factor", "win_loss_ratio"):
        stats[key] = _safe_float(stats.get(key))

    return stats


@router.get("/stats/today")
async def get_stats_today(request: Request) -> Dict[str, Any]:
    """Return today's statistics for the dashboard."""
    db = _get_db(request)
    mode = getattr(request.app.state, "mode", "paper")

    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    trade_count = 0
    win_count = 0
    daily_loss = 0.0

    try:
        conn = db._get_connection()
        cur = conn.execute(
            """
            SELECT pnl FROM trades
            WHERE mode = ? AND date(exit_time, 'unixepoch') = ?
            """,
            (mode, today),
        )
        for row in cur:
            pnl = row["pnl"] or 0
            trade_count += 1
            if pnl > 0:
                win_count += 1
            else:
                daily_loss += abs(pnl)
    except Exception:
        pass

    win_rate = (win_count / trade_count * 100) if trade_count > 0 else 0.0

    consecutive_losses = 0
    try:
        conn = db._get_connection()
        cur = conn.execute(
            """
            SELECT pnl FROM trades
            WHERE mode = ? AND exit_time IS NOT NULL
            ORDER BY exit_time DESC
            """,
            (mode,),
        )
        for row in cur:
            if (row["pnl"] or 0) > 0:
                break
            consecutive_losses += 1
    except Exception:
        pass

    cooldown_raw = db.get_state(f"cooldown_until_{mode}")
    cooldown = None
    if cooldown_raw and float(cooldown_raw) > time.time():
        remaining = int(float(cooldown_raw) - time.time())
        cooldown = f"{remaining // 60}m {remaining % 60}s"

    return {
        "trade_count": trade_count,
        "win_count": win_count,
        "win_rate": round(win_rate, 1),
        "daily_loss": round(daily_loss, 4),
        "daily_loss_limit": 10.0,
        "max_trades": 100,
        "consecutive_losses": consecutive_losses,
        "cooldown": cooldown,
        "btc_trend": "Mixed",
        "eth_trend": "Mixed",
    }


@router.get("/stats/{symbol}")
async def get_symbol_stats(request: Request, symbol: str) -> Dict[str, Any]:
    """Return performance statistics for a specific symbol.

    Includes a ``trades`` array with individual trade P&L for charting.
    """
    tracker = _get_tracker(request)
    stats = tracker.get_symbol_stats(symbol)

    for key in ("profit_factor", "win_loss_ratio"):
        stats[key] = _safe_float(stats.get(key))

    # Add individual trades for the symbol chart
    db = _get_db(request)
    mode = getattr(request.app.state, "mode", "paper")
    trades_list = []
    try:
        conn = db._get_connection()
        cur = conn.execute(
            """
            SELECT pnl, exit_type, exit_time FROM trades
            WHERE mode = ? AND symbol = ? AND exit_time IS NOT NULL
            ORDER BY exit_time ASC
            """,
            (mode, symbol),
        )
        for row in cur:
            trades_list.append({
                "pnl": _safe_float(row["pnl"]),
                "exit_type": row["exit_type"],
                "exit_time": row["exit_time"],
            })
    except Exception:
        pass

    stats["trades"] = trades_list
    return stats


@router.get("/equity-curve")
async def get_equity_curve(
    request: Request,
    days: int = Query(30, ge=1, le=365, description="Number of days to look back"),
) -> List[Dict[str, Any]]:
    """Return daily equity curve data for charting.

    Queries trades table directly for real-time accuracy.
    """
    db = _get_db(request)
    mode = getattr(request.app.state, "mode", "paper")

    init_raw = db.get_state(f"initial_balance_{mode}")
    initial_balance = float(init_raw) if init_raw else 20.0

    cutoff_ts = int(time.time()) - days * 86400
    try:
        conn = db._get_connection()
        cur = conn.execute(
            """
            SELECT date(exit_time, 'unixepoch') as trade_date,
                   SUM(pnl) as day_pnl,
                   COUNT(*) as trade_count,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as win_count
            FROM trades
            WHERE mode = ? AND exit_time >= ?
            GROUP BY trade_date
            ORDER BY trade_date ASC
            """,
            (mode, cutoff_ts),
        )
        rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        rows = []

    curve = []
    cumulative_pnl = 0.0
    for row in rows:
        day_pnl = row.get("day_pnl", 0.0)
        cumulative_pnl += day_pnl
        tc = row.get("trade_count", 0)
        wc = row.get("win_count", 0)
        win_rate = (wc / tc) if tc > 0 else 0.0
        curve.append({
            "date": row["trade_date"],
            "balance": round(initial_balance + cumulative_pnl, 4),
            "pnl": round(day_pnl, 4),
            "cumulative_pnl": round(cumulative_pnl, 4),
            "trade_count": tc,
            "win_rate": round(win_rate, 4),
        })

    return curve


@router.get("/symbols")
async def get_symbols(request: Request) -> List[str]:
    """Return list of symbols that have been traded.

    Used by positions and history pages for filter dropdowns.
    """
    db = _get_db(request)
    mode = getattr(request.app.state, "mode", "paper")
    symbols: List[str] = []
    try:
        conn = db._get_connection()
        cur = conn.execute(
            "SELECT DISTINCT symbol FROM trades WHERE mode = ? ORDER BY symbol",
            (mode,),
        )
        symbols = [row["symbol"] for row in cur.fetchall()]

        # Also include currently open positions
        cur2 = conn.execute(
            "SELECT DISTINCT symbol FROM positions WHERE mode = ? ORDER BY symbol",
            (mode,),
        )
        for row in cur2.fetchall():
            if row["symbol"] not in symbols:
                symbols.append(row["symbol"])
    except Exception:
        pass
    return sorted(symbols)


@router.get("/daily-stats")
async def get_daily_stats(request: Request) -> Dict[str, Any]:
    """Return today's trading statistics.

    Returns
    -------
    dict
        ``{date, pnl, trade_count, win_count, consecutive_losses,
          cooldown_until, cooldown_active}``
    """
    tracker = _get_tracker(request)
    stats = tracker.get_daily_stats()

    cooldown_active = False
    cooldown_remaining = 0
    if stats.cooldown_until is not None:
        cooldown_active = stats.cooldown_until > time.time()
        cooldown_remaining = max(0, int(stats.cooldown_until - time.time()))

    return {
        "date": stats.date,
        "pnl": round(stats.pnl, 4),
        "trade_count": stats.trade_count,
        "win_count": stats.win_count,
        "consecutive_losses": stats.consecutive_losses,
        "cooldown_until": stats.cooldown_until,
        "cooldown_active": cooldown_active,
        "cooldown_remaining_sec": cooldown_remaining,
    }


# ===================================================================
# G-03: Control endpoints
# ===================================================================


@router.post("/trading/start", response_model=ControlResponse)
async def trading_start(request: Request) -> ControlResponse:
    """Send a start command to the Orchestrator.

    Resumes trading activity (new entries and position management).
    """
    q = _get_control_queue(request)

    try:
        q.put_nowait({"command": "start"})
        logger.info("GUI: sent 'start' command to Orchestrator")
    except Exception as exc:
        logger.error("GUI: failed to send start command: {}", exc)
        raise HTTPException(status_code=500, detail=f"Failed to send command: {exc}")

    return ControlResponse(status="ok", message="Start command sent")


@router.post("/trading/stop", response_model=ControlResponse)
async def trading_stop(request: Request, body: StopRequest) -> ControlResponse:
    """Send a stop command to the Orchestrator.

    Parameters
    ----------
    body:
        ``{force_close: bool}`` -- whether to force-close all open positions.
    """
    q = _get_control_queue(request)

    try:
        q.put_nowait({
            "command": "stop",
            "data": {"force_close": body.force_close},
        })
        logger.info(
            "GUI: sent 'stop' command to Orchestrator (force_close={})",
            body.force_close,
        )
    except Exception as exc:
        logger.error("GUI: failed to send stop command: {}", exc)
        raise HTTPException(status_code=500, detail=f"Failed to send command: {exc}")

    return ControlResponse(status="ok", message="Stop command sent")


@router.post("/trading/pause", response_model=ControlResponse)
async def trading_pause(request: Request) -> ControlResponse:
    """Send a pause command to the Orchestrator.

    Pauses new trade entries while keeping existing positions managed.
    """
    q = _get_control_queue(request)

    try:
        q.put_nowait({"command": "pause"})
        logger.info("GUI: sent 'pause' command to Orchestrator")
    except Exception as exc:
        logger.error("GUI: failed to send pause command: {}", exc)
        raise HTTPException(status_code=500, detail=f"Failed to send command: {exc}")

    return ControlResponse(status="ok", message="Pause command sent")


@router.post("/trading/reset", response_model=ControlResponse)
async def trading_reset(request: Request) -> ControlResponse:
    """Reset paper trading: clear all trades, positions, daily stats, and tuner state.

    This gives a fresh start with the initial balance. Only works in paper mode.
    The system should be stopped before calling this endpoint.
    """
    db = _get_db(request)
    mode = getattr(request.app.state, "mode", "paper")

    if mode != "paper":
        raise HTTPException(
            status_code=400,
            detail="Reset is only available in paper mode",
        )

    try:
        conn = db._get_connection()

        # Clear all paper trades
        conn.execute("DELETE FROM trades WHERE mode = 'paper'")

        # Clear all paper positions
        conn.execute("DELETE FROM positions WHERE mode = 'paper'")

        # Clear daily performance
        conn.execute("DELETE FROM daily_performance WHERE mode = 'paper'")

        # Reset system_state keys related to paper trading
        reset_keys = [
            "current_balance_paper",
            "initial_balance_paper",
            "drawdown_pct_paper",
            "drawdown_stage_paper",
            "cooldown_until_paper",
            "tuner_level",
            "tuner_signal_rate",
            "tuner_stable_streak",
            "tuner_yaml_proposed",
            "tuner_params",
            "tuner_history",
        ]
        for key in reset_keys:
            conn.execute(
                "DELETE FROM system_state WHERE key = ?", (key,)
            )

        conn.commit()

        logger.info("GUI: paper trading data reset (trades, positions, daily stats, tuner)")

    except Exception as exc:
        logger.error("GUI: failed to reset paper trading: {}", exc)
        raise HTTPException(
            status_code=500, detail=f"Failed to reset: {exc}"
        )

    # Signal orchestrator to restart child processes
    db.set_state("restart_requested", "1")
    logger.info("GUI: restart flag set after paper trading reset")

    return ControlResponse(
        status="ok",
        message="Paper trading reset complete. System is restarting...",
    )


@router.post("/drawdown/resume", response_model=ControlResponse)
async def drawdown_resume(request: Request) -> ControlResponse:
    """Manually resume trading after drawdown stage 3 halt.

    Clears the drawdown stage flag and sends a resume command to the
    Orchestrator.
    """
    q = _get_control_queue(request)
    db = _get_db(request)
    mode = getattr(request.app.state, "mode", "paper")

    # Clear drawdown stage in the database
    try:
        db.set_state(f"drawdown_stage_{mode}", "0")
        logger.info("GUI: cleared drawdown stage for mode={}", mode)
    except Exception as exc:
        logger.warning("GUI: failed to clear drawdown stage: {}", exc)

    try:
        q.put_nowait({
            "command": "resume",
            "data": {"reason": "manual_drawdown_resume"},
        })
        logger.info("GUI: sent 'resume' command to Orchestrator (drawdown override)")
    except Exception as exc:
        logger.error("GUI: failed to send resume command: {}", exc)
        raise HTTPException(status_code=500, detail=f"Failed to send command: {exc}")

    return ControlResponse(status="ok", message="Drawdown resume command sent")


# ---------------------------------------------------------------------------
# /api/control -- unified control endpoint for settings.html
# ---------------------------------------------------------------------------


class ControlRequest(BaseModel):
    """Body for the unified control endpoint."""

    action: str  # "start" | "stop" | "pause"
    mode: str = "graceful"  # "graceful" | "force"


@router.post("/control", response_model=ControlResponse)
async def control(request: Request, body: ControlRequest) -> ControlResponse:
    """Unified control endpoint used by settings.html.

    Routes to the existing trading start/stop/pause logic based on the
    ``action`` field.
    """
    q = _get_control_queue(request)

    action = body.action.lower()
    if action not in ("start", "stop", "pause"):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action '{body.action}'. Must be start, stop, or pause.",
        )

    try:
        if action == "start":
            q.put_nowait({"command": "start"})
        elif action == "stop":
            force_close = body.mode == "force"
            q.put_nowait({
                "command": "stop",
                "data": {"force_close": force_close},
            })
        elif action == "pause":
            q.put_nowait({"command": "pause"})

        logger.info(
            "GUI: sent '{}' command via /api/control (mode={})",
            action,
            body.mode,
        )
    except Exception as exc:
        logger.error("GUI: failed to send control command: {}", exc)
        raise HTTPException(status_code=500, detail=f"Failed to send command: {exc}")

    return ControlResponse(
        status="ok",
        message=f"{action.capitalize()} command sent",
    )


# ---------------------------------------------------------------------------
# /api/config -- return current configuration values for settings.html
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


def _load_yaml(path: Path) -> dict:
    """Load a YAML file and return its contents as a dict."""
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


@router.get("/config")
async def get_config() -> Dict[str, Any]:
    """Return current configuration values read from config files."""
    scanner_cfg = _load_yaml(_CONFIG_DIR / "strategy" / "scanner.yaml")
    position_cfg = _load_yaml(_CONFIG_DIR / "strategy" / "position.yaml")
    asset_cfg = _load_yaml(_CONFIG_DIR / "strategy" / "asset.yaml")

    scanner_active = scanner_cfg.get("active", "new_listing")
    position_active = position_cfg.get("active", "momentum_scalper")
    asset_active = asset_cfg.get("active", "fixed_ratio")

    # Asset strategy details
    asset_strategies = asset_cfg.get("strategies", {})
    active_asset = asset_strategies.get(asset_active, {})
    capital_per_pos = active_asset.get("capital_per_position_pct", 6.0)
    risk_per_trade = active_asset.get("risk_per_trade_pct", 2.0) / 100.0
    max_leverage = active_asset.get("max_leverage", 10)
    max_positions = active_asset.get("max_concurrent_positions", 3)

    # Position strategy details
    exit_cfg = position_cfg.get("exit", {})
    time_limit = exit_cfg.get("time_limit", {})
    max_hold_minutes = time_limit.get("max_holding_minutes", 90)

    # Daily limits
    daily_limits = asset_cfg.get("daily_limits", {})
    max_daily_trades = daily_limits.get("max_daily_trades", 100)
    max_daily_loss_pct = daily_limits.get("max_daily_loss_pct", 50.0)

    # Capital info for daily loss limit
    capital_cfg = asset_cfg.get("capital", {})
    initial_balance = capital_cfg.get("initial_balance", 20)
    daily_loss_limit_usdt = round(initial_balance * max_daily_loss_pct / 100, 1)

    # Drawdown
    drawdown_cfg = asset_cfg.get("drawdown", {})
    drawdown_stage1 = drawdown_cfg.get("warning_pct", 50) / 100.0
    drawdown_stage2 = drawdown_cfg.get("reduce_pct", 70) / 100.0

    return {
        "scanner": scanner_active,
        "position": position_active,
        "asset": asset_active,
        "max_positions": max_positions,
        "capital_per_position": capital_per_pos,
        "risk_per_trade": risk_per_trade,
        "max_leverage": max_leverage,
        "max_hold_minutes": max_hold_minutes,
        "max_daily_trades": max_daily_trades,
        "daily_loss_limit": daily_loss_limit_usdt,
        "drawdown_stage1": drawdown_stage1,
        "drawdown_stage2": drawdown_stage2,
    }


# ---------------------------------------------------------------------------
# /api/system-info -- system information for settings.html
# ---------------------------------------------------------------------------


@router.get("/system-info")
async def get_system_info(request: Request) -> Dict[str, Any]:
    """Return system information: database size, uptime, last scan time."""
    db = _get_db(request)

    # Database file size
    db_path = Path(getattr(db, "db_path", "data/auto_bit.db"))
    try:
        db_size = os.path.getsize(db_path)
    except OSError:
        db_size = 0

    # Uptime
    start_time = getattr(request.app.state, "start_time", time.time())
    uptime = int(time.time() - start_time)

    # Last scan time
    last_scan_raw = db.get_state("last_scan_time")
    last_scan_time: Optional[float] = None
    if last_scan_raw is not None:
        try:
            last_scan_time = float(last_scan_raw)
        except (ValueError, TypeError):
            last_scan_time = None

    return {
        "db_size": db_size,
        "uptime": uptime,
        "last_scan_time": last_scan_time,
    }


# ---------------------------------------------------------------------------
# /api/tuner -- strategy tuner status & control
# ---------------------------------------------------------------------------


@router.get("/tuner")
async def get_tuner_status(request: Request) -> Dict[str, Any]:
    """Return current strategy tuner status from DB.

    Includes level, signal rate, stable streak, proposed YAML save,
    current parameters, and tuning history.
    """
    db = _get_db(request)

    level_raw = db.get_state("tuner_level")
    level = int(level_raw) if level_raw else 0

    rate_raw = db.get_state("tuner_signal_rate")
    signal_rate = float(rate_raw) if rate_raw else 0.0

    streak_raw = db.get_state("tuner_stable_streak")
    stable_streak = int(streak_raw) if streak_raw else 0

    proposed_raw = db.get_state("tuner_yaml_proposed")
    yaml_proposed = proposed_raw == "1" if proposed_raw else False

    params_raw = db.get_state("tuner_params")
    current_params = {}
    if params_raw:
        try:
            current_params = json.loads(params_raw)
        except Exception:
            pass

    history_raw = db.get_state("tuner_history")
    history = []
    if history_raw:
        try:
            history = json.loads(history_raw)
        except Exception:
            pass

    # Normalize current_params to consistent keys for the GUI
    # DB stores: rsi_long_range, volume_multiplier, higher_tf_enabled
    # GUI expects: rsi_long, rsi_short, vol_mult, vwap, htf, ema
    normalized_params = {
        "rsi_long": current_params.get("rsi_long_range", current_params.get("rsi_long", "--")),
        "rsi_short": current_params.get("rsi_short_range", current_params.get("rsi_short", "--")),
        "vol_mult": current_params.get("volume_multiplier", current_params.get("vol_mult", "--")),
        "vwap": current_params.get("vwap_enabled", current_params.get("vwap", True)),
        "htf": current_params.get("higher_tf_enabled", current_params.get("htf", True)),
        "ema": current_params.get("ema_alignment_mode", current_params.get("ema", "strict")),
    }

    # Check if tuner is enabled from config
    tuner_enabled = True
    try:
        pos_cfg = _load_yaml(_CONFIG_DIR / "strategy" / "position.yaml")
        ms_cfg = pos_cfg.get("strategies", {}).get("momentum_scalper", {})
        tuner_cfg = ms_cfg.get("tuner", {})
        tuner_enabled = tuner_cfg.get("enabled", True)
    except Exception:
        pass

    return {
        "enabled": tuner_enabled,
        "level": level,
        "max_level": 6,
        "signal_rate": round(signal_rate, 4),
        "stable_streak": stable_streak,
        "yaml_proposed": yaml_proposed,
        "current_params": normalized_params,
        "history": history[-10:],
    }


@router.post("/tuner/apply", response_model=ControlResponse)
async def tuner_apply_yaml(request: Request) -> ControlResponse:
    """Apply current tuned parameters to position.yaml.

    This is a user-initiated action that writes the currently active
    tuner parameters to the YAML config file, making them permanent.
    """
    db = _get_db(request)

    # Read current tuned params from DB
    params_raw = db.get_state("tuner_params")
    if not params_raw:
        raise HTTPException(status_code=400, detail="No tuned parameters available")

    try:
        tuned_params = json.loads(params_raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid tuner params in DB")

    level_raw = db.get_state("tuner_level")
    level = int(level_raw) if level_raw else 0
    rate_raw = db.get_state("tuner_signal_rate")
    signal_rate = float(rate_raw) if rate_raw else 0.0

    # Write to YAML
    yaml_path = _CONFIG_DIR / "strategy" / "position.yaml"
    try:
        with open(yaml_path, "r") as f:
            cfg = yaml.safe_load(f) or {}

        ms = cfg.setdefault("strategies", {}).setdefault("momentum_scalper", {})
        if tuned_params.get("rsi_long_range"):
            ms["rsi_long_range"] = tuned_params["rsi_long_range"]
        if tuned_params.get("rsi_short_range"):
            ms["rsi_short_range"] = tuned_params["rsi_short_range"]
        if tuned_params.get("volume_multiplier") is not None:
            ms["volume_multiplier"] = tuned_params["volume_multiplier"]
        if tuned_params.get("vwap_enabled") is not None:
            ms["vwap_enabled"] = tuned_params["vwap_enabled"]
        if "higher_tf" not in ms:
            ms["higher_tf"] = {}
        if tuned_params.get("higher_tf_enabled") is not None:
            ms["higher_tf"]["enabled"] = tuned_params["higher_tf_enabled"]

        ms["_tuner_applied"] = {
            "level": level,
            "signal_rate": round(signal_rate, 4),
            "applied_at": int(time.time()),
        }

        with open(yaml_path, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

        logger.info("Tuner params applied to {} (level={})", yaml_path, level)

    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write YAML: {exc}")

    return ControlResponse(
        status="ok",
        message=f"Tuner level {level} parameters saved to position.yaml",
    )


@router.post("/tuner/reset", response_model=ControlResponse)
async def tuner_reset(request: Request) -> ControlResponse:
    """Reset tuner to level 0 and clear saved state."""
    db = _get_db(request)

    try:
        db.set_state("tuner_level", "0")
        db.set_state("tuner_signal_rate", "0")
        db.set_state("tuner_stable_streak", "0")
        db.set_state("tuner_yaml_proposed", "0")
        db.set_state("tuner_params", "")
        db.set_state("tuner_history", "[]")
        logger.info("Tuner state reset to level 0")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to reset tuner: {exc}")

    return ControlResponse(status="ok", message="Tuner reset to level 0")


# ===================================================================
# Advanced Analytics endpoints
# ===================================================================


def _get_analytics(request: Request):
    """Build an AdvancedAnalytics instance from the tracker or DB."""
    from src.tracker.advanced_analytics import AdvancedAnalytics

    tracker = getattr(request.app.state, "tracker", None)
    if tracker is not None:
        return tracker.get_analytics()
    # Fallback: build directly from DB
    db = _get_db(request)
    mode = getattr(request.app.state, "mode", "paper")
    return AdvancedAnalytics(db, mode)


def _sanitize_report(obj: Any) -> Any:
    """Recursively replace inf/nan with None for JSON serialisation."""
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_report(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_report(v) for v in obj]
    return obj


@router.get("/analytics")
async def get_analytics_report(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> Dict[str, Any]:
    """Full advanced analytics report."""
    analytics = _get_analytics(request)
    report = analytics.full_report(days=days)
    return _sanitize_report(report)


@router.get("/analytics/drawdown")
async def get_drawdown_series(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> List[Dict[str, Any]]:
    """Drawdown series for charting."""
    analytics = _get_analytics(request)
    return analytics.drawdown_series(days=days)


@router.get("/analytics/attribution")
async def get_symbol_attribution(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> List[Dict[str, Any]]:
    """Per-symbol P&L attribution table."""
    analytics = _get_analytics(request)
    return _sanitize_report(analytics.symbol_attribution(days=days))


@router.get("/analytics/hourly")
async def get_hourly_performance(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
) -> List[Dict[str, Any]]:
    """Hourly performance heatmap data."""
    analytics = _get_analytics(request)
    return analytics.hourly_performance(days=days)


@router.get("/analytics/rolling")
async def get_rolling_metrics(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    sharpe_window: int = Query(default=7, ge=2, le=90),
    win_rate_window: int = Query(default=20, ge=5, le=200),
) -> Dict[str, Any]:
    """Rolling Sharpe ratio and win rate for charts."""
    analytics = _get_analytics(request)
    return _sanitize_report({
        "rolling_sharpe": analytics.rolling_sharpe(
            window_days=sharpe_window, total_days=days,
        ),
        "rolling_win_rate": analytics.rolling_win_rate(
            window_trades=win_rate_window,
        ),
    })

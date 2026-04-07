"""Advanced performance analytics for the auto_bit trading system.

Provides risk-adjusted return metrics (Sharpe, Sortino, Calmar), drawdown
analysis, per-symbol P&L attribution, time-based breakdowns, and rolling
metrics.  All queries are scoped to a single ``mode`` (paper / live).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from src.utils.db import DatabaseManager


class AdvancedAnalytics:
    """Advanced performance analytics for the trading system.

    Parameters
    ----------
    db:
        A ``DatabaseManager`` instance.
    mode:
        ``'paper'`` or ``'live'``.
    """

    def __init__(self, db: DatabaseManager, mode: str) -> None:
        self.db = db
        self.mode = mode

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cutoff_date(self, days: int) -> str:
        """Return YYYY-MM-DD string for *days* ago (UTC)."""
        import time as _time
        ts = _time.time() - days * 86400
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    def _cutoff_timestamp(self, days: int) -> int:
        import time as _time
        return int(_time.time()) - days * 86400

    def _fetch_daily_returns(self, days: int) -> list[dict]:
        """Fetch daily_performance rows for the last *days* days."""
        conn = self.db._get_connection()
        cutoff = self._cutoff_date(days)
        cur = conn.execute(
            """
            SELECT * FROM daily_performance
            WHERE mode = ? AND date >= ?
            ORDER BY date ASC
            """,
            (self.mode, cutoff),
        )
        return [dict(r) for r in cur.fetchall()]

    def _fetch_trades(self, days: int) -> list[dict]:
        """Fetch closed trades for the last *days* days."""
        conn = self.db._get_connection()
        cutoff = self._cutoff_timestamp(days)
        cur = conn.execute(
            """
            SELECT * FROM trades
            WHERE mode = ? AND exit_time IS NOT NULL AND exit_time >= ?
            ORDER BY exit_time ASC
            """,
            (self.mode, cutoff),
        )
        return [dict(r) for r in cur.fetchall()]

    # ==================================================================
    # Risk-adjusted returns
    # ==================================================================

    def sharpe_ratio(self, days: int = 30, risk_free_rate: float = 0.0) -> float:
        """Annualized Sharpe ratio from daily percentage returns.

        ``SR = (mean_daily_return% - rf_daily%) / std_daily_return% * sqrt(365)``

        Uses percentage returns (daily PnL / starting_balance) rather than
        raw PnL so the ratio is scale-independent and comparable across
        different account sizes.
        """
        rows = self._fetch_daily_returns(days)
        if len(rows) < 2:
            return 0.0

        # Convert raw PnL to percentage returns using each day's starting balance
        daily_returns: list[float] = []
        for r in rows:
            sb = r.get("starting_balance", 0.0)
            if sb > 0:
                daily_returns.append(r["pnl"] / sb)
            else:
                daily_returns.append(0.0)

        rf_daily = risk_free_rate / 365.0

        mean_ret = sum(daily_returns) / len(daily_returns)
        excess = mean_ret - rf_daily

        variance = sum((r - mean_ret) ** 2 for r in daily_returns) / len(daily_returns)
        std = math.sqrt(variance)

        if std == 0.0:
            return float("inf") if excess > 0 else 0.0

        return (excess / std) * math.sqrt(365)

    def sortino_ratio(self, days: int = 30, risk_free_rate: float = 0.0) -> float:
        """Sortino ratio using downside deviation of percentage returns only."""
        rows = self._fetch_daily_returns(days)
        if len(rows) < 2:
            return 0.0

        # Convert raw PnL to percentage returns using each day's starting balance
        daily_returns: list[float] = []
        for r in rows:
            sb = r.get("starting_balance", 0.0)
            if sb > 0:
                daily_returns.append(r["pnl"] / sb)
            else:
                daily_returns.append(0.0)

        rf_daily = risk_free_rate / 365.0

        mean_ret = sum(daily_returns) / len(daily_returns)
        excess = mean_ret - rf_daily

        negative_returns = [r for r in daily_returns if r < 0]
        if not negative_returns:
            return float("inf") if excess > 0 else 0.0

        downside_var = sum(r ** 2 for r in negative_returns) / len(daily_returns)
        downside_std = math.sqrt(downside_var)

        if downside_std == 0.0:
            return float("inf") if excess > 0 else 0.0

        return (excess / downside_std) * math.sqrt(365)

    # ==================================================================
    # Drawdown analysis
    # ==================================================================

    def max_drawdown(self, days: int = 30) -> dict:
        """Calculate maximum drawdown from equity curve.

        Returns
        -------
        dict
            ``{max_dd_pct, max_dd_amount, peak_date, trough_date,
            recovery_date, current_dd_pct}``
        """
        rows = self._fetch_daily_returns(days)
        if not rows:
            return {
                "max_dd_pct": 0.0,
                "max_dd_amount": 0.0,
                "peak_date": None,
                "trough_date": None,
                "recovery_date": None,
                "current_dd_pct": 0.0,
            }

        peak = rows[0]["ending_balance"]
        peak_date = rows[0]["date"]
        max_dd_pct = 0.0
        max_dd_amount = 0.0
        best_peak_date = peak_date
        worst_trough_date = rows[0]["date"]
        recovery_date = None

        # Track the drawdown that produced the max
        dd_peak = peak
        dd_peak_date = peak_date

        for row in rows:
            equity = row["ending_balance"]
            date = row["date"]

            if equity >= peak:
                peak = equity
                peak_date = date

            if peak > 0:
                dd_pct = (peak - equity) / peak * 100
                dd_amount = peak - equity
            else:
                dd_pct = 0.0
                dd_amount = 0.0

            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                max_dd_amount = dd_amount
                best_peak_date = peak_date
                worst_trough_date = date
                recovery_date = None  # reset; not recovered yet

            if max_dd_pct > 0 and recovery_date is None and equity >= peak and dd_pct == 0:
                # We've recovered from the max drawdown
                recovery_date = date

        # Current drawdown
        last_equity = rows[-1]["ending_balance"]
        overall_peak = max(r["ending_balance"] for r in rows)
        current_dd_pct = 0.0
        if overall_peak > 0 and last_equity < overall_peak:
            current_dd_pct = (overall_peak - last_equity) / overall_peak * 100

        return {
            "max_dd_pct": round(max_dd_pct, 4),
            "max_dd_amount": round(max_dd_amount, 4),
            "peak_date": best_peak_date,
            "trough_date": worst_trough_date,
            "recovery_date": recovery_date,
            "current_dd_pct": round(current_dd_pct, 4),
        }

    def drawdown_series(self, days: int = 30) -> list[dict]:
        """Daily drawdown series for charting.

        Returns
        -------
        list[dict]
            ``[{date, equity, peak, drawdown_pct}]``
        """
        rows = self._fetch_daily_returns(days)
        if not rows:
            return []

        series: list[dict] = []
        peak = 0.0
        for row in rows:
            equity = row["ending_balance"]
            if equity > peak:
                peak = equity
            dd_pct = (peak - equity) / peak * 100 if peak > 0 else 0.0
            series.append({
                "date": row["date"],
                "equity": equity,
                "peak": peak,
                "drawdown_pct": round(dd_pct, 4),
            })
        return series

    def calmar_ratio(self, days: int = 30) -> float:
        """Calmar ratio = annualized return / max drawdown %."""
        rows = self._fetch_daily_returns(days)
        if len(rows) < 2:
            return 0.0

        first_balance = rows[0]["starting_balance"]
        last_balance = rows[-1]["ending_balance"]

        if first_balance <= 0:
            return 0.0

        total_return_pct = (last_balance - first_balance) / first_balance * 100
        num_days = len(rows)
        annualized_return_pct = total_return_pct * (365 / num_days) if num_days > 0 else 0.0

        dd = self.max_drawdown(days)
        max_dd_pct = dd["max_dd_pct"]

        if max_dd_pct == 0.0:
            return float("inf") if annualized_return_pct > 0 else 0.0

        return annualized_return_pct / max_dd_pct

    # ==================================================================
    # Per-symbol attribution
    # ==================================================================

    def symbol_attribution(self, days: int = 30) -> list[dict]:
        """P&L attribution by symbol.

        Returns
        -------
        list[dict]
            Sorted by ``total_pnl`` descending.
        """
        trades = self._fetch_trades(days)
        if not trades:
            return []

        # Group by symbol
        symbols: dict[str, list[dict]] = {}
        for t in trades:
            sym = t["symbol"]
            symbols.setdefault(sym, []).append(t)

        total_pnl_all = sum(t.get("pnl", 0) or 0 for t in trades)

        result: list[dict] = []
        for sym, sym_trades in symbols.items():
            pnls = [(t.get("pnl") or 0.0) for t in sym_trades]
            total_pnl = sum(pnls)
            wins = sum(1 for p in pnls if p > 0)
            trade_count = len(sym_trades)

            holding_mins: list[float] = []
            for t in sym_trades:
                entry_t = t.get("entry_time") or 0
                exit_t = t.get("exit_time") or 0
                if entry_t and exit_t:
                    holding_mins.append(max(0.0, (exit_t - entry_t) / 60.0))

            contribution = (total_pnl / total_pnl_all * 100) if total_pnl_all != 0 else 0.0

            result.append({
                "symbol": sym,
                "total_pnl": round(total_pnl, 4),
                "trade_count": trade_count,
                "win_rate": round(wins / trade_count, 4) if trade_count else 0.0,
                "avg_pnl": round(total_pnl / trade_count, 4) if trade_count else 0.0,
                "pnl_contribution_pct": round(contribution, 4),
                "best_trade": max(pnls),
                "worst_trade": min(pnls),
                "avg_holding_min": round(sum(holding_mins) / len(holding_mins), 2) if holding_mins else 0.0,
            })

        result.sort(key=lambda x: x["total_pnl"], reverse=True)
        return result

    # ==================================================================
    # Time-based analysis
    # ==================================================================

    def hourly_performance(self, days: int = 30) -> list[dict]:
        """Performance breakdown by hour of day (UTC).

        Returns
        -------
        list[dict]
            ``[{hour, trade_count, total_pnl, win_rate, avg_pnl}]``
        """
        trades = self._fetch_trades(days)
        if not trades:
            return []

        hours: dict[int, list[float]] = {}
        for t in trades:
            exit_time = t.get("exit_time")
            if not exit_time:
                continue
            hour = datetime.fromtimestamp(exit_time, tz=timezone.utc).hour
            pnl = t.get("pnl") or 0.0
            hours.setdefault(hour, []).append(pnl)

        result: list[dict] = []
        for hour in sorted(hours.keys()):
            pnls = hours[hour]
            wins = sum(1 for p in pnls if p > 0)
            result.append({
                "hour": hour,
                "trade_count": len(pnls),
                "total_pnl": round(sum(pnls), 4),
                "win_rate": round(wins / len(pnls), 4) if pnls else 0.0,
                "avg_pnl": round(sum(pnls) / len(pnls), 4) if pnls else 0.0,
            })
        return result

    def weekday_performance(self, days: int = 30) -> list[dict]:
        """Performance breakdown by day of week.

        Returns
        -------
        list[dict]
            ``[{weekday, trade_count, total_pnl, win_rate}]``
            weekday: 0=Monday .. 6=Sunday
        """
        trades = self._fetch_trades(days)
        if not trades:
            return []

        weekdays: dict[int, list[float]] = {}
        for t in trades:
            exit_time = t.get("exit_time")
            if not exit_time:
                continue
            wd = datetime.fromtimestamp(exit_time, tz=timezone.utc).weekday()
            pnl = t.get("pnl") or 0.0
            weekdays.setdefault(wd, []).append(pnl)

        result: list[dict] = []
        for wd in sorted(weekdays.keys()):
            pnls = weekdays[wd]
            wins = sum(1 for p in pnls if p > 0)
            result.append({
                "weekday": wd,
                "trade_count": len(pnls),
                "total_pnl": round(sum(pnls), 4),
                "win_rate": round(wins / len(pnls), 4) if pnls else 0.0,
            })
        return result

    # ==================================================================
    # Rolling metrics
    # ==================================================================

    def rolling_sharpe(self, window_days: int = 7, total_days: int = 30) -> list[dict]:
        """Rolling Sharpe ratio over time using percentage returns.

        Returns
        -------
        list[dict]
            ``[{date, sharpe_ratio}]``
        """
        rows = self._fetch_daily_returns(total_days)
        if len(rows) < window_days:
            return []

        result: list[dict] = []
        for i in range(window_days - 1, len(rows)):
            window = rows[i - window_days + 1 : i + 1]
            # Use percentage returns (PnL / starting_balance)
            returns: list[float] = []
            for r in window:
                sb = r.get("starting_balance", 0.0)
                if sb > 0:
                    returns.append(r["pnl"] / sb)
                else:
                    returns.append(0.0)
            mean_ret = sum(returns) / len(returns)
            variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
            std = math.sqrt(variance)

            if std == 0.0:
                sr = float("inf") if mean_ret > 0 else 0.0
            else:
                sr = (mean_ret / std) * math.sqrt(365)

            result.append({
                "date": rows[i]["date"],
                "sharpe_ratio": round(sr, 4) if not math.isinf(sr) else None,
            })
        return result

    def rolling_win_rate(self, window_trades: int = 20) -> list[dict]:
        """Rolling win rate over the last N trades.

        Returns
        -------
        list[dict]
            ``[{trade_index, exit_time, win_rate}]``
        """
        trades = self._fetch_trades(days=9999)
        if len(trades) < window_trades:
            return []

        result: list[dict] = []
        for i in range(window_trades - 1, len(trades)):
            window = trades[i - window_trades + 1 : i + 1]
            wins = sum(1 for t in window if (t.get("pnl") or 0) > 0)
            result.append({
                "trade_index": i,
                "exit_time": trades[i].get("exit_time"),
                "win_rate": round(wins / window_trades, 4),
            })
        return result

    # ==================================================================
    # Combined report
    # ==================================================================

    def full_report(self, days: int = 30) -> dict:
        """Generate a complete analytics report combining all metrics."""
        return {
            "sharpe_ratio": self.sharpe_ratio(days),
            "sortino_ratio": self.sortino_ratio(days),
            "max_drawdown": self.max_drawdown(days),
            "calmar_ratio": self.calmar_ratio(days),
            "symbol_attribution": self.symbol_attribution(days),
            "hourly_performance": self.hourly_performance(days),
            "weekday_performance": self.weekday_performance(days),
            "rolling_sharpe": self.rolling_sharpe(total_days=days),
            "rolling_win_rate": self.rolling_win_rate(),
            "drawdown_series": self.drawdown_series(days),
        }

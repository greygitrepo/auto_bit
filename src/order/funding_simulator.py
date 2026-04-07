"""Funding Rate Simulator — simulates Bybit 8h funding charges for paper trading.

Addresses Gap E from parity_analysis.md: paper mode does NOT charge funding rates,
but live Bybit charges every 8 hours at 00:00, 08:00, 16:00 UTC.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, List

from loguru import logger


# Bybit funding schedule: 00:00, 08:00, 16:00 UTC
_FUNDING_HOURS = [0, 8, 16]


class FundingSimulator:
    """Simulates funding rate charges for paper trading positions."""

    FUNDING_INTERVAL_HOURS = 8

    def __init__(self, config: dict | None = None):
        if config is None:
            self.enabled = True
            self.extreme_threshold = 0.0005
        else:
            funding_cfg = config.get("funding_simulation", {})
            self.enabled = funding_cfg.get("enabled", True)
            self.extreme_threshold = funding_cfg.get("extreme_threshold", 0.0005)

        self._last_funding_times: Dict[str, float] = {}
        self._funding_rates: Dict[str, float] = {}

    def update_rate(self, symbol: str, rate: float) -> None:
        """Update the cached funding rate for a symbol."""
        self._funding_rates[symbol] = rate

    def check_and_apply(
        self,
        positions: list[dict],
        current_time: float | None = None,
    ) -> list[dict]:
        """Check if funding should be applied and calculate charges.

        Bybit funding schedule: every 8h at 00:00, 08:00, 16:00 UTC.

        For each position, if a funding interval boundary has been crossed since
        the last check, calculate the funding payment:
            funding_payment = position_value * funding_rate
            - Positive rate + Long = pay (negative)
            - Positive rate + Short = receive (positive)
            - Negative rate reverses the direction

        Parameters
        ----------
        positions:
            List of dicts with {symbol, side, size, entry_price, leverage}.
        current_time:
            Override for testing (unix timestamp). Defaults to time.time().

        Returns
        -------
        List of dicts: {symbol, side, funding_payment, rate, interval_start, interval_end}.
        """
        if not self.enabled:
            return []

        if current_time is None:
            current_time = time.time()

        results: list[dict] = []

        for pos in positions:
            symbol = pos["symbol"]
            rate = self._funding_rates.get(symbol)
            if rate is None:
                continue

            # First-time initialization: record current time, don't charge
            if symbol not in self._last_funding_times:
                self._last_funding_times[symbol] = current_time
                continue

            last_ts = self._last_funding_times[symbol]

            # Find funding boundaries crossed between last_ts and current_time
            crossed = self._find_crossed_funding_times(last_ts, current_time)
            if not crossed:
                continue

            # Calculate payment for each crossed boundary
            position_value = float(pos["size"]) * float(pos["entry_price"])
            side = pos["side"]

            for boundary_ts in crossed:
                # Long pays positive rate, short receives
                if side == "Buy":
                    funding_payment = -position_value * rate
                else:
                    funding_payment = position_value * rate

                results.append({
                    "symbol": symbol,
                    "side": side,
                    "funding_payment": funding_payment,
                    "rate": rate,
                    "interval_start": last_ts,
                    "interval_end": boundary_ts,
                })

                if abs(rate) >= self.extreme_threshold:
                    logger.warning(
                        "Extreme funding rate for {}: {:.4f}% | payment={:.4f}",
                        symbol, rate * 100, funding_payment,
                    )

            # Update last funding time to the latest crossed boundary
            self._last_funding_times[symbol] = crossed[-1]

        return results

    def get_next_funding_time(self, current_time: float | None = None) -> float:
        """Return the unix timestamp of the next funding event.

        Funding events: 00:00, 08:00, 16:00 UTC daily.
        """
        if current_time is None:
            current_time = time.time()

        dt = datetime.fromtimestamp(current_time, tz=timezone.utc)
        current_hour = dt.hour

        # Find the next funding hour
        for h in _FUNDING_HOURS:
            if h > current_hour:
                next_dt = dt.replace(hour=h, minute=0, second=0, microsecond=0)
                return next_dt.timestamp()

        # Past all funding hours today → next is 00:00 tomorrow
        next_day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        next_day = next_day.replace(day=dt.day + 1)
        return next_day.timestamp()

    def estimate_daily_funding_cost(self, positions: list[dict]) -> float:
        """Estimate daily funding cost for current positions (3 events/day).

        Returns positive value = net cost (paying), negative = net income (receiving).
        """
        if not positions:
            return 0.0

        total_cost = 0.0
        for pos in positions:
            symbol = pos["symbol"]
            rate = self._funding_rates.get(symbol, 0.0)
            position_value = float(pos["size"]) * float(pos["entry_price"])
            side = pos["side"]

            # Per-event cost
            if side == "Buy":
                per_event = position_value * rate  # positive rate → cost for longs
            else:
                per_event = -position_value * rate  # positive rate → income for shorts

            total_cost += per_event * 3  # 3 funding events per day

        return total_cost

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_crossed_funding_times(start_ts: float, end_ts: float) -> list[float]:
        """Find all funding boundary timestamps between start_ts and end_ts.

        Funding boundaries are at 00:00, 08:00, 16:00 UTC.
        Returns sorted list of boundary timestamps that fall in (start_ts, end_ts].
        """
        if end_ts <= start_ts:
            return []

        results: list[float] = []

        # Start from the hour of start_ts and walk forward
        dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        # Find the first funding boundary after start_ts
        current_day = dt.replace(hour=0, minute=0, second=0, microsecond=0)

        # Check all funding times for multiple days if needed
        end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
        max_days = (end_dt - current_day).days + 2  # safety margin

        for day_offset in range(max_days):
            for h in _FUNDING_HOURS:
                candidate = current_day.replace(hour=h)
                candidate_ts = candidate.timestamp()
                if candidate_ts > start_ts and candidate_ts <= end_ts:
                    results.append(candidate_ts)

            # Move to next day
            current_day = current_day.replace(
                day=current_day.day + 1,
                hour=0, minute=0, second=0, microsecond=0,
            )

        return sorted(results)

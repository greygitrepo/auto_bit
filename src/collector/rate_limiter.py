"""
Async rate limiter for the Bybit API.

Uses a sliding-window slot approach: each call consumes a slot, and slots
expire after 60 seconds.  When all slots are consumed the limiter
``await``-blocks until a slot frees up.

Separate limiters are provided for different endpoint categories so that,
for example, heavy market-data polling does not starve order placement.

Usage:
    from src.collector.rate_limiter import RateLimiterGroup

    limiters = RateLimiterGroup()

    async def fetch_candles():
        async with limiters.market:
            return await client.get_kline(...)

    async def place_order():
        async with limiters.trade:
            return await client.place_order(...)
"""

import asyncio
import time
from collections import deque
from typing import Optional

from loguru import logger


class RateLimiter:
    """Sliding-window rate limiter compatible with ``async with``.

    Args:
        name: Human-readable label used in log messages.
        max_calls: Maximum number of calls allowed in the window.
        window_seconds: Length of the sliding window in seconds.
    """

    def __init__(
        self,
        name: str,
        max_calls: int = 100,
        window_seconds: float = 60.0,
    ) -> None:
        self.name = name
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    # -- public API ----------------------------------------------------------

    async def acquire(self) -> None:
        """Wait until a rate-limit slot is available, then consume it.

        If the current window is full the method sleeps until the oldest
        slot expires, logging a throttle warning on each wait cycle.
        """
        async with self._lock:
            while True:
                now = time.monotonic()
                self._purge_expired(now)

                if len(self._timestamps) < self.max_calls:
                    self._timestamps.append(now)
                    return

                # Calculate how long until the oldest slot expires
                wait_seconds = self._timestamps[0] + self.window_seconds - now
                if wait_seconds <= 0:
                    # Edge case: slot already expired but wasn't purged
                    self._purge_expired(now)
                    continue

                logger.warning(
                    "Rate limiter [{}] throttled — {} calls in window, "
                    "waiting {:.2f}s",
                    self.name,
                    len(self._timestamps),
                    wait_seconds,
                )

                # Release the lock while sleeping so other coroutines can
                # check, but we'll re-acquire and re-check on wakeup.
                # We break out to sleep *outside* the lock, then retry.
                break

            # Sleep outside the lock, then retry acquisition recursively.
            await asyncio.sleep(wait_seconds)  # type: ignore[possibly-undefined]
            await self.acquire()

    @property
    def calls_remaining(self) -> int:
        """Number of calls that can be made right now without waiting."""
        self._purge_expired(time.monotonic())
        return max(0, self.max_calls - len(self._timestamps))

    @property
    def utilisation(self) -> float:
        """Fraction of rate-limit capacity currently consumed (0.0–1.0)."""
        self._purge_expired(time.monotonic())
        if self.max_calls == 0:
            return 1.0
        return len(self._timestamps) / self.max_calls

    # -- async context manager -----------------------------------------------

    async def __aenter__(self) -> "RateLimiter":
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[object],
    ) -> None:
        # Nothing to release; the slot simply ages out of the window.
        pass

    # -- internals -----------------------------------------------------------

    def _purge_expired(self, now: float) -> None:
        """Remove timestamps older than the sliding window."""
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()


class RateLimiterGroup:
    """Pre-configured rate limiters for each Bybit endpoint category.

    Bybit's documented limit is 120 req/min for most endpoints.  We
    default to 100 req/min per category to leave headroom for bursts
    and avoid 403 responses.

    Args:
        market_limit: Max calls/min for market-data endpoints.
        trade_limit: Max calls/min for order/trade endpoints.
        position_limit: Max calls/min for position-query endpoints.
    """

    def __init__(
        self,
        market_limit: int = 100,
        trade_limit: int = 100,
        position_limit: int = 100,
    ) -> None:
        self.market = RateLimiter("market", max_calls=market_limit)
        self.trade = RateLimiter("trade", max_calls=trade_limit)
        self.position = RateLimiter("position", max_calls=position_limit)

    def status(self) -> dict:
        """Return a dict summarising each limiter's current state."""
        return {
            name: {
                "calls_remaining": limiter.calls_remaining,
                "utilisation": round(limiter.utilisation, 3),
            }
            for name, limiter in [
                ("market", self.market),
                ("trade", self.trade),
                ("position", self.position),
            ]
        }

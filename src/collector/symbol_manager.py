"""Symbol manager for discovering and filtering new listing candidates.

Fetches instrument metadata and ticker data from Bybit, filters by listing
age, liquidity, and blacklist criteria, then ranks candidates by volume and
volatility for downstream analysis.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from loguru import logger

from src.collector.bybit_client import BybitClient


class SymbolManager:
    """Discovers, filters, and ranks new-listing trading candidates.

    Uses :class:`BybitClient` to fetch linear USDT instrument metadata and
    ticker snapshots.  Maintains an internal cache with a configurable TTL
    to avoid redundant API calls on rapid successive invocations.

    Parameters
    ----------
    client:
        An initialised :class:`BybitClient` instance (authentication is not
        required -- only public market-data endpoints are used).
    cache_ttl:
        How long (in seconds) cached instrument data remains valid.
        Defaults to 300 (5 minutes).
    """

    def __init__(self, client: BybitClient, cache_ttl: int = 300) -> None:
        self._client = client
        self._cache_ttl = cache_ttl

        # Instrument cache: list of raw instrument dicts from the API.
        self._instruments_cache: List[Dict[str, Any]] = []
        self._instruments_ts: float = 0.0

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _is_cache_valid(self) -> bool:
        """Return True if the instrument cache is still fresh."""
        return (
            len(self._instruments_cache) > 0
            and (time.monotonic() - self._instruments_ts) < self._cache_ttl
        )

    def _refresh_instruments(self) -> List[Dict[str, Any]]:
        """Fetch instruments from the API and update the cache.

        Returns
        -------
        The full list of instrument dicts.
        """
        if self._is_cache_valid():
            logger.debug(
                "Using cached instruments ({} items, age {:.0f}s)",
                len(self._instruments_cache),
                time.monotonic() - self._instruments_ts,
            )
            return self._instruments_cache

        logger.info("Refreshing instrument cache from Bybit API")
        self._instruments_cache = self._client.get_instruments_info()
        self._instruments_ts = time.monotonic()
        logger.info(
            "Instrument cache refreshed: {} instruments",
            len(self._instruments_cache),
        )
        return self._instruments_cache

    def invalidate_cache(self) -> None:
        """Force the next call to re-fetch instrument data."""
        self._instruments_cache = []
        self._instruments_ts = 0.0
        logger.debug("Instrument cache invalidated")

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_launch_time(raw_launch_time: str) -> datetime:
        """Convert Bybit ``launchTime`` (millisecond epoch string) to a
        timezone-aware UTC datetime.

        Parameters
        ----------
        raw_launch_time:
            Millisecond-precision Unix timestamp as a string,
            e.g. ``"1672531200000"``.

        Returns
        -------
        A :class:`datetime` object in UTC.
        """
        ts_seconds = int(raw_launch_time) / 1000.0
        return datetime.fromtimestamp(ts_seconds, tz=timezone.utc)

    @staticmethod
    def _days_since(dt: datetime) -> float:
        """Return the number of days elapsed since *dt* (UTC)."""
        delta = datetime.now(timezone.utc) - dt
        return delta.total_seconds() / 86400.0

    # ------------------------------------------------------------------
    # Ticker look-up helper
    # ------------------------------------------------------------------

    @staticmethod
    def _build_ticker_map(
        tickers: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """Index a list of ticker dicts by symbol for O(1) look-ups."""
        return {t["symbol"]: t for t in tickers if "symbol" in t}

    # ------------------------------------------------------------------
    # Core public method
    # ------------------------------------------------------------------

    def get_new_listing_candidates(
        self,
        tickers: List[Dict[str, Any]],
        max_candidates: int = 30,
        min_turnover: float = 30_000_000.0,
        max_days: int = 30,
        min_days: int = 1,
        blacklist: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Return filtered and ranked new-listing candidates.

        Parameters
        ----------
        tickers:
            Pre-fetched list of ticker dicts (from
            :meth:`BybitClient.get_tickers`).  Each dict must contain at
            least ``symbol``, ``turnover24h``, ``price24hPcnt``,
            ``fundingRate``, and ``lastPrice``.
        max_candidates:
            Maximum number of candidates to return.
        min_turnover:
            Minimum 24-hour turnover in USDT.  Symbols below this
            threshold are excluded.
        max_days:
            Maximum number of days since listing.  Older symbols are
            excluded.
        min_days:
            Minimum number of days since listing.  Very fresh listings
            (potentially illiquid or erratic) are excluded.
        blacklist:
            List of symbols to unconditionally exclude
            (e.g. ``["USDCUSDT"]``).

        Returns
        -------
        A list of candidate dicts, sorted by combined volume + volatility
        score (descending), capped at *max_candidates*.  Each dict
        contains::

            {
                "symbol":               str,
                "launch_time":          str,   # ISO-8601 UTC
                "days_since_listed":    float,
                "turnover_24h":         float,
                "price_change_pct_24h": float,
                "funding_rate":         float,
                "last_price":           float,
            }
        """
        blacklist_set = set(blacklist) if blacklist else set()
        instruments = self._refresh_instruments()
        ticker_map = self._build_ticker_map(tickers)

        logger.info(
            "Scanning candidates: min_days={}, max_days={}, "
            "min_turnover={:,.0f}, blacklist_size={}, tickers={}",
            min_days,
            max_days,
            min_turnover,
            len(blacklist_set),
            len(ticker_map),
        )

        candidates: List[Dict[str, Any]] = []

        for inst in instruments:
            symbol: str = inst.get("symbol", "")

            # --- Basic validity checks ---
            if not symbol or inst.get("status") != "Trading":
                continue

            # Only consider USDT-quoted linear instruments.
            if inst.get("quoteCoin") != "USDT":
                continue

            # --- Blacklist ---
            if symbol in blacklist_set:
                logger.debug("Skipping blacklisted symbol: {}", symbol)
                continue

            # --- Listing-age filter ---
            raw_launch = inst.get("launchTime")
            if not raw_launch:
                logger.debug("Skipping {} (no launchTime)", symbol)
                continue

            try:
                launch_dt = self._parse_launch_time(raw_launch)
            except (ValueError, TypeError):
                logger.warning(
                    "Skipping {} (unparseable launchTime={})", symbol, raw_launch
                )
                continue

            days_listed = self._days_since(launch_dt)

            if days_listed < min_days or days_listed > max_days:
                continue

            # --- Require a matching ticker ---
            ticker = ticker_map.get(symbol)
            if ticker is None:
                logger.debug("Skipping {} (no ticker data)", symbol)
                continue

            # --- Turnover filter ---
            try:
                turnover_24h = float(ticker.get("turnover24h", 0))
            except (ValueError, TypeError):
                turnover_24h = 0.0

            if turnover_24h < min_turnover:
                continue

            # --- Extract remaining ticker fields ---
            try:
                price_change_pct = float(ticker.get("price24hPcnt", 0))
            except (ValueError, TypeError):
                price_change_pct = 0.0

            try:
                funding_rate = float(ticker.get("fundingRate", 0))
            except (ValueError, TypeError):
                funding_rate = 0.0

            try:
                last_price = float(ticker.get("lastPrice", 0))
            except (ValueError, TypeError):
                last_price = 0.0

            candidates.append(
                {
                    "symbol": symbol,
                    "launch_time": launch_dt.isoformat(),
                    "days_since_listed": round(days_listed, 2),
                    "turnover_24h": turnover_24h,
                    "price_change_pct_24h": price_change_pct,
                    "funding_rate": funding_rate,
                    "last_price": last_price,
                }
            )

        logger.info(
            "Found {} candidates after filtering (before ranking)",
            len(candidates),
        )

        # --- Rank by combined volume + volatility ---
        ranked = self._rank_candidates(candidates)

        top = ranked[:max_candidates]
        logger.info(
            "Returning top {} candidates (of {} ranked)",
            len(top),
            len(ranked),
        )
        return top

    # ------------------------------------------------------------------
    # Ranking
    # ------------------------------------------------------------------

    @staticmethod
    def _rank_candidates(
        candidates: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Sort candidates by a combined volume + volatility score.

        The score is a simple composite:

        * **Volume component** -- normalised 24h turnover (higher is
          better, indicates strong liquidity).
        * **Volatility component** -- absolute 24h price change percentage
          (higher absolute move signals higher volatility / opportunity).

        Both components are min-max normalised to [0, 1] across the
        candidate set, then summed with equal weight.

        Returns the list sorted descending by composite score.
        """
        if not candidates:
            return []

        turnovers = [c["turnover_24h"] for c in candidates]
        volatilities = [abs(c["price_change_pct_24h"]) for c in candidates]

        t_min, t_max = min(turnovers), max(turnovers)
        v_min, v_max = min(volatilities), max(volatilities)

        t_range = t_max - t_min if t_max != t_min else 1.0
        v_range = v_max - v_min if v_max != v_min else 1.0

        for c, t, v in zip(candidates, turnovers, volatilities):
            norm_turnover = (t - t_min) / t_range
            norm_volatility = (v - v_min) / v_range
            c["_score"] = norm_turnover + norm_volatility

        candidates.sort(key=lambda c: c["_score"], reverse=True)

        # Remove internal scoring key before returning.
        for c in candidates:
            c.pop("_score", None)

        return candidates

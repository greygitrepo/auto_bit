"""New-listing scanner strategy.

Scores recently listed symbols across five dimensions (volume, volatility,
momentum, listing recency, market environment) and applies entry filters
to produce a ranked shortlist of trading candidates.

Implements tasks S-02 through S-05.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger

from src.collector.symbol_manager import SymbolManager
from src.strategy.scanner.base import BaseScannerStrategy, ScanResult


class NewListingScanner(BaseScannerStrategy):
    """Scanner strategy targeting recently listed perpetual contracts.

    Uses :class:`SymbolManager` to discover candidates that passed basic
    liquidity and listing-age filters, then applies a multi-factor scoring
    model with configurable weights.

    Parameters
    ----------
    symbol_manager:
        Initialised :class:`SymbolManager` used to fetch candidate lists.
    config:
        The ``strategies.new_listing`` section of ``scanner.yaml``.
    """

    def __init__(self, symbol_manager: SymbolManager, config: dict) -> None:
        self._sm = symbol_manager
        self._config = config

        # Unpack scoring weights with fallbacks.
        scoring = config.get("scoring", {})
        self._volume_weight = scoring.get("volume_weight", 0.30)
        self._volatility_weight = scoring.get("volatility_weight", 0.25)
        self._momentum_weight = scoring.get("momentum_weight", 0.20)
        self._listing_weight = scoring.get("listing_recency_weight", 0.10)
        self._market_env_weight = scoring.get("market_env_weight", 0.15)
        self._min_score = scoring.get("min_score", 55)

        # Volatility thresholds.
        vol_cfg = config.get("volatility", {})
        self._min_atr_pct = vol_cfg.get("min_atr_pct", 0.5)
        self._max_atr_pct = vol_cfg.get("max_atr_pct", 5.0)

        # Momentum / RSI guard-rails.
        mom_cfg = config.get("momentum", {})
        self._rsi_exclude_below = mom_cfg.get("rsi_exclude_below", 20)
        self._rsi_exclude_above = mom_cfg.get("rsi_exclude_above", 80)

        # Entry filter parameters.
        ef_cfg = config.get("entry_filter", {})
        self._cooldown_hours = ef_cfg.get("cooldown_after_sl_hours", 4)
        self._volume_decline_threshold = ef_cfg.get("volume_decline_threshold", 0.5)

        # Candidate pool parameters.
        listing_cfg = config.get("listing", {})
        self._max_days = listing_cfg.get("max_days_since_listed", 30)
        self._min_days = listing_cfg.get("min_days_since_listed", 1)

        liq_cfg = config.get("liquidity", {})
        self._min_turnover = liq_cfg.get("min_24h_turnover_usdt", 30_000_000)

        pool_cfg = config.get("pool", {})
        self._max_candidates = pool_cfg.get("max_candidates", 30)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def scan(
        self,
        market_data: dict,
        btc_trend: str,
        eth_trend: str,
        open_positions: list,
        recent_sl_symbols: dict,
    ) -> list[ScanResult]:
        """Run a full new-listing scan cycle.

        Args:
            market_data: ``{symbol: {"tickers": dict, "indicators": DataFrame | None}}``.
            btc_trend: ``"bullish"`` / ``"bearish"`` / ``"neutral"``.
            eth_trend: Same vocabulary as *btc_trend*.
            open_positions: Symbols currently held.
            recent_sl_symbols: ``{symbol: unix_timestamp}`` of recent SL hits.

        Returns:
            Filtered, scored, and sorted list of :class:`ScanResult`.
        """
        # S-02: Build the raw ticker list expected by SymbolManager.
        tickers = self._extract_tickers(market_data)
        if not tickers:
            logger.warning("No ticker data available -- scan aborted")
            return []

        # First filter via SymbolManager (listing age, turnover, blacklist).
        candidates = self._sm.get_new_listing_candidates(
            tickers=tickers,
            max_candidates=self._max_candidates,
            min_turnover=self._min_turnover,
            max_days=self._max_days,
            min_days=self._min_days,
        )

        if not candidates:
            logger.info("No candidates passed initial filter")
            return []

        logger.info("Scoring {} candidates", len(candidates))

        # S-03 / S-05: Score each candidate.
        results: list[ScanResult] = []
        for candidate in candidates:
            symbol = candidate["symbol"]
            indicators = self._get_indicators(market_data, symbol)
            result = self._score_candidate(
                candidate, candidates, indicators, btc_trend, eth_trend
            )
            results.append(result)

        # S-04: Apply entry filters.
        filtered = self._apply_entry_filters(
            results, open_positions, recent_sl_symbols, market_data
        )

        logger.info(
            "Scan complete: {} scored, {} after filters",
            len(results),
            len(filtered),
        )
        return filtered

    def get_default_params(self) -> dict:
        """Return default configuration parameters."""
        return {
            "volume_weight": 0.30,
            "volatility_weight": 0.25,
            "momentum_weight": 0.20,
            "listing_recency_weight": 0.10,
            "market_env_weight": 0.15,
            "min_score": 55,
            "min_atr_pct": 0.5,
            "max_atr_pct": 5.0,
            "rsi_exclude_below": 20,
            "rsi_exclude_above": 80,
            "cooldown_after_sl_hours": 4,
            "volume_decline_threshold": 0.5,
            "max_days_since_listed": 30,
            "min_days_since_listed": 1,
            "min_24h_turnover_usdt": 30_000_000,
            "max_candidates": 30,
        }

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    def _score_candidate(
        self,
        candidate: dict,
        all_candidates: list,
        indicators: Optional[pd.DataFrame],
        btc_trend: str,
        eth_trend: str,
    ) -> ScanResult:
        """Compute composite score for a single candidate.

        Args:
            candidate: Candidate dict from :class:`SymbolManager`.
            all_candidates: Full candidate list (used for relative ranking).
            indicators: Indicator DataFrame or ``None`` when WebSocket data
                is unavailable for this symbol.
            btc_trend: BTC EMA alignment.
            eth_trend: ETH EMA alignment.

        Returns:
            A fully populated :class:`ScanResult`.
        """
        vol_score = self._calculate_volume_score(candidate, all_candidates)
        volatility_score = self._calculate_volatility_score(candidate, indicators)
        momentum_score = self._calculate_momentum_score(
            candidate, indicators, btc_trend
        )
        listing_score = self._calculate_listing_score(
            int(candidate.get("days_since_listed", 15))
        )
        market_score, market_dir, suggested_side = self._calculate_market_env_score(
            btc_trend, eth_trend, candidate
        )

        scores_detail = {
            "volume": round(vol_score, 2),
            "volatility": round(volatility_score, 2),
            "momentum": round(momentum_score, 2),
            "listing_recency": round(listing_score, 2),
            "market_env": round(market_score, 2),
        }

        composite = (
            vol_score * self._volume_weight
            + volatility_score * self._volatility_weight
            + momentum_score * self._momentum_weight
            + listing_score * self._listing_weight
            + market_score * self._market_env_weight
        )
        composite = round(min(max(composite, 0.0), 100.0), 2)

        # Build a human-readable reason string.
        top_factor = max(scores_detail, key=scores_detail.get)  # type: ignore[arg-type]
        reason = (
            f"score={composite} (top_factor={top_factor}={scores_detail[top_factor]}), "
            f"side={suggested_side}, mkt={market_dir}, "
            f"days_listed={candidate.get('days_since_listed', '?')}"
        )

        return ScanResult(
            symbol=candidate["symbol"],
            score=composite,
            market_direction=market_dir,
            suggested_side=suggested_side,
            scores_detail=scores_detail,
            reason=reason,
            metadata={
                "turnover_24h": candidate.get("turnover_24h", 0),
                "price_change_pct_24h": candidate.get("price_change_pct_24h", 0),
                "last_price": candidate.get("last_price", 0),
                "days_since_listed": candidate.get("days_since_listed", 0),
            },
        )

    def _calculate_volume_score(
        self, candidate: dict, all_candidates: list
    ) -> float:
        """Score based on 24h turnover rank within the candidate pool.

        The candidate with the highest turnover scores 100; the lowest
        scores 0.  Ties are broken naturally by the normalisation.

        Args:
            candidate: Single candidate dict.
            all_candidates: All candidates for relative ranking.

        Returns:
            Score in [0, 100].
        """
        if not all_candidates:
            return 0.0

        turnovers = [c.get("turnover_24h", 0.0) for c in all_candidates]
        t_min = min(turnovers)
        t_max = max(turnovers)
        t_range = t_max - t_min if t_max != t_min else 1.0

        candidate_turnover = candidate.get("turnover_24h", 0.0)
        normalised = (candidate_turnover - t_min) / t_range
        return round(normalised * 100.0, 2)

    def _calculate_volatility_score(
        self, candidate: dict, indicators: Optional[pd.DataFrame] = None
    ) -> float:
        """Score based on ATR% or 24h price change as a fallback.

        Sweet spot is defined by ``min_atr_pct`` (0.5%) to ``max_atr_pct``
        (5.0%).  Values within the sweet spot of 0.5-3.0% receive the
        highest scores; values outside 0.5% or above 5.0% receive
        progressively lower scores.

        Args:
            candidate: Candidate dict (used for fallback via price change).
            indicators: Indicator DataFrame with ``atr_14`` and ``close``
                columns, or ``None``.

        Returns:
            Score in [0, 100].
        """
        atr_pct: Optional[float] = None

        # Prefer ATR% from indicator data when available.
        if indicators is not None and not indicators.empty:
            if "atr_14" in indicators.columns and "close" in indicators.columns:
                last_row = indicators.iloc[-1]
                atr_val = last_row.get("atr_14")
                close_val = last_row.get("close")
                if (
                    pd.notna(atr_val)
                    and pd.notna(close_val)
                    and close_val > 0
                ):
                    atr_pct = (atr_val / close_val) * 100.0

        # Fallback: use absolute 24h price change percentage.
        if atr_pct is None:
            raw_pct = candidate.get("price_change_pct_24h", 0.0)
            atr_pct = abs(raw_pct) * 100.0  # price_change_pct is a ratio (e.g. 0.05 = 5%)

        return self._volatility_pct_to_score(atr_pct)

    def _volatility_pct_to_score(self, pct: float) -> float:
        """Map a volatility percentage to a 0-100 score.

        Scoring curve:
            < 0.5%  -> linearly 0-40  (too quiet)
            0.5-3.0% -> 80-100        (sweet spot)
            3.0-5.0% -> 80-50         (getting risky)
            > 5.0%  -> 0              (too volatile)
        """
        if pct < 0.0:
            return 0.0
        if pct > self._max_atr_pct:
            return 0.0
        if pct < self._min_atr_pct:
            # Linearly scale from 0 to 40 as we approach min_atr_pct.
            return (pct / self._min_atr_pct) * 40.0 if self._min_atr_pct > 0 else 0.0
        if pct <= 3.0:
            # Sweet spot: scale 80-100 within 0.5-3.0%.
            ratio = (pct - self._min_atr_pct) / (3.0 - self._min_atr_pct)
            return 80.0 + ratio * 20.0
        # 3.0 to max_atr_pct: linearly decrease from 80 to 50.
        ratio = (pct - 3.0) / (self._max_atr_pct - 3.0)
        return 80.0 - ratio * 30.0

    def _calculate_momentum_score(
        self,
        candidate: dict,
        indicators: Optional[pd.DataFrame] = None,
        market_direction: str = "mixed",
    ) -> float:
        """Score based on RSI and price-change alignment with market trend.

        For bullish markets, RSI 50-70 is ideal for longs; for bearish
        markets, RSI 30-50 is ideal for shorts.  The 24h price change
        direction is also checked for alignment.

        Args:
            candidate: Candidate dict with ``price_change_pct_24h``.
            indicators: Indicator DataFrame with ``rsi_14``, or ``None``.
            market_direction: BTC trend string used to determine alignment.

        Returns:
            Score in [0, 100].
        """
        rsi_score = 50.0  # neutral default
        price_change = candidate.get("price_change_pct_24h", 0.0)

        # Determine effective market bias.
        is_bullish = market_direction in ("bullish", "bull")
        is_bearish = market_direction in ("bearish", "bear")

        # RSI component (60% of momentum score).
        if indicators is not None and not indicators.empty:
            if "rsi_14" in indicators.columns:
                rsi_val = indicators["rsi_14"].iloc[-1]
                if pd.notna(rsi_val):
                    rsi_score = self._rsi_to_score(rsi_val, is_bullish, is_bearish)

        # Price-change alignment component (40% of momentum score).
        alignment_score = 50.0  # neutral default
        if is_bullish and price_change > 0:
            alignment_score = min(100.0, 60.0 + abs(price_change) * 100.0 * 4.0)
        elif is_bearish and price_change < 0:
            alignment_score = min(100.0, 60.0 + abs(price_change) * 100.0 * 4.0)
        elif is_bullish and price_change < 0:
            alignment_score = max(0.0, 40.0 - abs(price_change) * 100.0 * 2.0)
        elif is_bearish and price_change > 0:
            alignment_score = max(0.0, 40.0 - abs(price_change) * 100.0 * 2.0)

        return rsi_score * 0.6 + alignment_score * 0.4

    def _rsi_to_score(
        self, rsi: float, is_bullish: bool, is_bearish: bool
    ) -> float:
        """Convert an RSI value to a 0-100 score based on market context.

        Bullish market: RSI 50-70 is best (trending strength).
        Bearish market: RSI 30-50 is best (weakness with room to fall).
        Mixed: RSI near 50 is acceptable, extremes are penalised.
        """
        # Exclude extreme RSI values entirely.
        if rsi < self._rsi_exclude_below or rsi > self._rsi_exclude_above:
            return 0.0

        if is_bullish:
            if 50 <= rsi <= 70:
                return 80.0 + ((rsi - 50) / 20.0) * 20.0  # 80-100
            if 40 <= rsi < 50:
                return 60.0 + ((rsi - 40) / 10.0) * 20.0  # 60-80
            if 70 < rsi <= 80:
                return max(30.0, 80.0 - ((rsi - 70) / 10.0) * 50.0)
            return 30.0

        if is_bearish:
            if 30 <= rsi <= 50:
                return 80.0 + ((50 - rsi) / 20.0) * 20.0  # 80-100
            if 50 < rsi <= 60:
                return 60.0 + ((60 - rsi) / 10.0) * 20.0  # 60-80
            if 20 <= rsi < 30:
                return max(30.0, 80.0 - ((30 - rsi) / 10.0) * 50.0)
            return 30.0

        # Mixed / neutral: prefer middle ground.
        distance_from_50 = abs(rsi - 50)
        return max(20.0, 100.0 - distance_from_50 * 2.0)

    def _calculate_listing_score(self, days_since_listed: int) -> float:
        """Score based on how recently the symbol was listed.

        Scoring tiers (extended for older symbols):
            1-3 days:    100
            4-7 days:     80
            8-14 days:    60
            15-30 days:   40
            31-90 days:   25
            91-365 days:  15
            > 365 days:   10

        Args:
            days_since_listed: Integer number of days since the symbol
                was listed on the exchange.

        Returns:
            Score in [0, 100].
        """
        if days_since_listed <= 3:
            return 100.0
        if days_since_listed <= 7:
            return 80.0
        if days_since_listed <= 14:
            return 60.0
        if days_since_listed <= 30:
            return 40.0
        if days_since_listed <= 90:
            return 25.0
        if days_since_listed <= 365:
            return 15.0
        return 10.0

    def _calculate_market_env_score(
        self, btc_trend: str, eth_trend: str, candidate: dict
    ) -> Tuple[float, str, str]:
        """Score based on market environment and candidate alignment.

        Combines BTC and ETH trend signals with the candidate's own
        price momentum to assess how favourable conditions are.

        Args:
            btc_trend: ``"bullish"`` / ``"bearish"`` / ``"neutral"``.
            eth_trend: Same vocabulary.
            candidate: Candidate dict with ``price_change_pct_24h``.

        Returns:
            Tuple of ``(score, market_direction, suggested_side)`` where
            *market_direction* is ``"bull"`` / ``"bear"`` / ``"mixed"`` and
            *suggested_side* is ``"LONG"`` / ``"SHORT"`` / ``"NEUTRAL"``.
        """
        price_change = candidate.get("price_change_pct_24h", 0.0)
        has_positive_momentum = price_change > 0

        # Derive aggregate market direction.
        btc_bull = btc_trend == "bullish"
        btc_bear = btc_trend == "bearish"
        eth_bull = eth_trend == "bullish"
        eth_bear = eth_trend == "bearish"

        if btc_bull and eth_bull:
            market_direction = "bull"
        elif btc_bear and eth_bear:
            market_direction = "bear"
        else:
            market_direction = "mixed"

        # Score and side based on alignment.
        if market_direction == "bull":
            if has_positive_momentum:
                return 100.0, market_direction, "LONG"
            else:
                # Counter-trend candidate in a bull market.
                return 30.0, market_direction, "NEUTRAL"

        if market_direction == "bear":
            if not has_positive_momentum:
                return 100.0, market_direction, "SHORT"
            else:
                # Counter-trend candidate in a bear market.
                return 30.0, market_direction, "NEUTRAL"

        # Mixed market.
        suggested_side = "LONG" if has_positive_momentum else "SHORT"
        return 50.0, market_direction, suggested_side

    # ------------------------------------------------------------------
    # Entry filters (S-04)
    # ------------------------------------------------------------------

    def _apply_entry_filters(
        self,
        results: list[ScanResult],
        open_positions: list,
        recent_sl_symbols: dict,
        market_data: dict,
    ) -> list[ScanResult]:
        """Apply post-scoring entry filters and return surviving results.

        Filters applied in order:

        1. Exclude symbols already in *open_positions*.
        2. Exclude symbols whose stop-loss cooldown has not expired.
        3. Exclude symbols with declining 24h volume (below threshold).
        4. Exclude symbols with composite score below ``min_score``.

        Results are returned sorted by score descending.

        Args:
            results: Scored :class:`ScanResult` list.
            open_positions: Currently held symbols.
            recent_sl_symbols: ``{symbol: sl_unix_timestamp}``.
            market_data: Full market data dict for volume-decline checks.

        Returns:
            Filtered and sorted list of :class:`ScanResult`.
        """
        now = time.time()
        cooldown_seconds = self._cooldown_hours * 3600
        open_set = set(open_positions)
        filtered: list[ScanResult] = []

        for result in results:
            symbol = result.symbol

            # 1. Skip symbols already in open positions.
            if symbol in open_set:
                logger.debug("Filter: {} already in open positions", symbol)
                continue

            # 2. Skip symbols still in stop-loss cooldown.
            sl_ts = recent_sl_symbols.get(symbol)
            if sl_ts is not None:
                elapsed = now - sl_ts
                if elapsed < cooldown_seconds:
                    remaining = (cooldown_seconds - elapsed) / 3600.0
                    logger.debug(
                        "Filter: {} in SL cooldown ({:.1f}h remaining)",
                        symbol,
                        remaining,
                    )
                    continue

            # 3. Skip if 24h turnover is declining below threshold.
            if self._is_volume_declining(symbol, market_data):
                logger.debug(
                    "Filter: {} excluded for declining volume", symbol
                )
                continue

            # 4. Skip if composite score below minimum.
            if result.score < self._min_score:
                logger.debug(
                    "Filter: {} score {:.1f} below min {}",
                    symbol,
                    result.score,
                    self._min_score,
                )
                continue

            filtered.append(result)

        # Sort by score descending.
        filtered.sort(key=lambda r: r.score, reverse=True)
        return filtered

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_volume_declining(self, symbol: str, market_data: dict) -> bool:
        """Check if a symbol's volume is declining below the threshold.

        Uses the ``vol_ratio`` indicator (current volume / 5-bar MA) when
        available.  If indicator data is not present the check is skipped
        (returns ``False``).

        Args:
            symbol: Symbol to check.
            market_data: Full market data dict.

        Returns:
            ``True`` if volume is declining below the configured threshold.
        """
        sym_data = market_data.get(symbol)
        if sym_data is None:
            return False

        indicators = sym_data.get("indicators")
        if indicators is None or indicators.empty:
            return False

        if "vol_ratio" not in indicators.columns:
            return False

        vol_ratio = indicators["vol_ratio"].iloc[-1]
        if pd.isna(vol_ratio):
            return False

        return vol_ratio < self._volume_decline_threshold

    @staticmethod
    def _extract_tickers(market_data: dict) -> List[Dict[str, Any]]:
        """Extract a flat list of ticker dicts from the market_data bundle.

        :class:`SymbolManager.get_new_listing_candidates` expects a list of
        ticker dicts with keys like ``symbol``, ``turnover24h``, etc.  This
        method reconstructs that list from the per-symbol market_data
        structure.

        Args:
            market_data: ``{symbol: {"tickers": dict, ...}}``.

        Returns:
            List of ticker dicts suitable for :class:`SymbolManager`.
        """
        tickers: List[Dict[str, Any]] = []
        for symbol, data in market_data.items():
            ticker = data.get("tickers")
            if ticker is not None:
                # Ensure the symbol key is present in the ticker dict.
                if "symbol" not in ticker:
                    ticker = {**ticker, "symbol": symbol}
                tickers.append(ticker)
        return tickers

    @staticmethod
    def _get_indicators(
        market_data: dict, symbol: str
    ) -> Optional[pd.DataFrame]:
        """Safely retrieve indicator data for a symbol.

        Args:
            market_data: Full market data dict.
            symbol: Symbol to look up.

        Returns:
            Indicator DataFrame or ``None`` if unavailable.
        """
        sym_data = market_data.get(symbol)
        if sym_data is None:
            return None
        indicators = sym_data.get("indicators")
        if indicators is None:
            return None
        if isinstance(indicators, pd.DataFrame) and indicators.empty:
            return None
        return indicators

"""Grid engine — manages grid level state machine and fill detection.

Pure logic module with no database or IPC dependencies.
All state is passed in and returned.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from src.strategy.position.base import (
    BiasDirection,
    GridAction,
    GridLevel,
    GridLevelStatus,
    GridSignal,
    GridState,
)


class GridEngine:
    """Manages grid levels: creation, fill detection, TP detection, recentering."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.num_levels = config.get("num_levels", 10)
        self.default_buy_levels = config.get("default_buy_levels", 5)
        self.default_sell_levels = config.get("default_sell_levels", 5)
        self.range_atr_multiplier = config.get("range_atr_multiplier", 2.5)
        self.min_range_pct = config.get("min_range_pct", 1.0) / 100.0
        self.max_range_pct = config.get("max_range_pct", 8.0) / 100.0
        self.recenter_threshold_pct = config.get("recenter_threshold_pct", 1.5) / 100.0
        self.max_open_levels = config.get("max_open_levels", 6)

    # ------------------------------------------------------------------
    # Grid creation
    # ------------------------------------------------------------------

    def create_grid(
        self,
        center_price: float,
        atr_1h: float,
        bias_direction: BiasDirection,
        level_shift: int,
        qty_per_level: float,
        leverage: int,
        mode: str = "paper",
        symbol: str = "",
        vol_ratio: float = 1.0,
    ) -> GridState:
        """Create a new grid centered on *center_price*.

        Returns a GridState with all levels in PENDING status.
        """
        grid_range = self._calc_range(center_price, atr_1h)
        num_levels = self.num_levels
        grid_spacing = grid_range / num_levels

        # --- Dynamic spacing based on volatility ratio ---
        dynamic_cfg = self.config.get("dynamic_spacing", {})
        if dynamic_cfg.get("enabled", False):
            low_thresh = dynamic_cfg.get("vol_ratio_low_threshold", 0.6)
            high_thresh = dynamic_cfg.get("vol_ratio_high_threshold", 1.5)
            low_mult = dynamic_cfg.get("low_vol_multiplier", 0.8)
            high_mult = dynamic_cfg.get("high_vol_multiplier", 1.4)

            if vol_ratio < low_thresh:
                spacing_mult = low_mult
            elif vol_ratio > high_thresh:
                spacing_mult = high_mult
            else:
                spacing_mult = 1.0
            grid_spacing *= spacing_mult

        # --- Adaptive level count ---
        adaptive_cfg = self.config.get("adaptive_levels", {})
        if adaptive_cfg.get("enabled", False):
            target_spacing = adaptive_cfg.get("target_spacing_pct", 0.60) / 100.0
            min_levels = adaptive_cfg.get("min_levels", 4)
            max_levels = adaptive_cfg.get("max_levels", 16)

            if center_price > 0 and grid_range > 0:
                ideal_spacing = center_price * target_spacing
                ideal_levels = max(1, round(grid_range / ideal_spacing))
                num_levels = max(min_levels, min(max_levels, ideal_levels))
                # Recalculate grid_spacing
                grid_spacing = grid_range / num_levels

        # Apply bias shift to buy/sell level counts
        default_half = num_levels // 2
        num_buy = default_half + level_shift
        num_sell = num_levels - default_half - level_shift
        # Clamp to at least 1 of each, total = num_levels
        num_buy = max(1, min(num_levels - 1, num_buy))
        num_sell = num_levels - num_buy

        now = int(time.time())
        state = GridState(
            mode=mode,
            symbol=symbol,
            status="active",
            center_price=center_price,
            grid_range=grid_range,
            grid_spacing=grid_spacing,
            num_buy_levels=num_buy,
            num_sell_levels=num_sell,
            bias=bias_direction.value,
            leverage=leverage,
            qty_per_level=qty_per_level,
            created_at=now,
            updated_at=now,
        )

        levels = []
        # Buy levels below center (level_index: -1, -2, ... -num_buy)
        for i in range(1, num_buy + 1):
            price = center_price - (grid_spacing * i)
            tp = price + grid_spacing  # TP = one spacing above buy price
            levels.append(GridLevel(
                level_index=-i,
                price=round(price, 10),
                side="Buy",
                status=GridLevelStatus.PENDING,
                tp_price=round(tp, 10),
                created_at=now,
                updated_at=now,
            ))

        # Sell levels above center (level_index: +1, +2, ... +num_sell)
        for i in range(1, num_sell + 1):
            price = center_price + (grid_spacing * i)
            tp = price - grid_spacing  # TP = one spacing below sell price
            levels.append(GridLevel(
                level_index=i,
                price=round(price, 10),
                side="Sell",
                status=GridLevelStatus.PENDING,
                tp_price=round(tp, 10),
                created_at=now,
                updated_at=now,
            ))

        state.levels = levels
        logger.info(
            "Grid created: {} center={:.6f} range={:.6f} spacing={:.6f} "
            "buy={} sell={} bias={} shift={}",
            symbol, center_price, grid_range, grid_spacing,
            num_buy, num_sell, bias_direction.value, level_shift,
        )
        return state

    # ------------------------------------------------------------------
    # Fill detection
    # ------------------------------------------------------------------

    def check_fills(
        self, candle: Dict[str, Any], levels: List[GridLevel],
    ) -> List[GridSignal]:
        """Check if any PENDING levels were crossed by the candle.

        Returns list of FILL signals for levels that were hit.
        """
        signals: List[GridSignal] = []
        low = float(candle.get("low", 0))
        high = float(candle.get("high", 0))

        # Count currently open (FILLED + TP_SET) levels
        open_count = sum(
            1 for lv in levels
            if lv.status in (GridLevelStatus.FILLED, GridLevelStatus.TP_SET)
        )

        for level in levels:
            if level.status != GridLevelStatus.PENDING:
                continue

            if open_count >= self.max_open_levels:
                break  # Don't open more than max_open_levels

            filled = False
            if level.side == "Buy" and low <= level.price:
                filled = True
            elif level.side == "Sell" and high >= level.price:
                filled = True

            if filled:
                level.status = GridLevelStatus.FILLED
                level.fill_price = level.price
                level.fill_time = int(candle.get("timestamp", time.time()))
                level.updated_at = int(time.time())
                open_count += 1

                signals.append(GridSignal(
                    symbol="",  # Filled by caller
                    action=GridAction.FILL,
                    level_id=level.id,
                    level_index=level.level_index,
                    level_price=level.price,
                    side=level.side,
                    tp_price=level.tp_price,
                    grid_state_id=level.grid_state_id,
                    reason=f"Level {level.level_index} {level.side} filled at {level.price:.6f}",
                ))

                logger.info(
                    "Grid FILL: level={} side={} price={:.6f} tp={:.6f}",
                    level.level_index, level.side, level.price, level.tp_price,
                )

        return signals

    def check_tp_hits(
        self, candle: Dict[str, Any], levels: List[GridLevel],
    ) -> List[GridSignal]:
        """Check if any TP_SET levels had their TP reached.

        Returns list of TP_HIT signals.
        """
        signals: List[GridSignal] = []
        low = float(candle.get("low", 0))
        high = float(candle.get("high", 0))

        for level in levels:
            # Check both FILLED and TP_SET levels for TP hits.
            # FILLED levels may not have received P3 confirmation yet,
            # but we still check TP to avoid missing fast moves.
            if level.status not in (GridLevelStatus.FILLED, GridLevelStatus.TP_SET):
                continue

            hit = False
            if level.side == "Buy" and high >= level.tp_price:
                hit = True
            elif level.side == "Sell" and low <= level.tp_price:
                hit = True

            if hit:
                level.status = GridLevelStatus.COMPLETED
                level.tp_fill_price = level.tp_price
                level.tp_fill_time = int(candle.get("timestamp", time.time()))
                level.updated_at = int(time.time())

                signals.append(GridSignal(
                    symbol="",
                    action=GridAction.TP_HIT,
                    level_id=level.id,
                    level_index=level.level_index,
                    level_price=level.price,
                    side=level.side,
                    tp_price=level.tp_price,
                    grid_state_id=level.grid_state_id,
                    reason=f"Level {level.level_index} TP hit at {level.tp_price:.6f}",
                ))

                logger.info(
                    "Grid TP_HIT: level={} side={} fill={:.6f} tp={:.6f}",
                    level.level_index, level.side, level.fill_price, level.tp_price,
                )

        return signals

    # ------------------------------------------------------------------
    # Recycle completed levels
    # ------------------------------------------------------------------

    def recycle_completed(self, levels: List[GridLevel]) -> int:
        """Reset COMPLETED levels back to PENDING for the next cycle.

        Returns count of recycled levels.
        """
        count = 0
        now = int(time.time())
        for level in levels:
            if level.status == GridLevelStatus.COMPLETED:
                level.status = GridLevelStatus.PENDING
                level.fill_price = 0.0
                level.fill_time = 0
                level.tp_fill_price = 0.0
                level.tp_fill_time = 0
                level.pnl = 0.0
                level.fee = 0.0
                level.position_id = 0
                level.updated_at = now
                count += 1
        return count

    # ------------------------------------------------------------------
    # Recentering
    # ------------------------------------------------------------------

    def should_recenter(
        self, current_price: float, grid_state: GridState,
    ) -> bool:
        """Check if the grid should be recentered."""
        if grid_state.center_price <= 0:
            return False
        drift = abs(current_price - grid_state.center_price) / grid_state.center_price
        return drift >= self.recenter_threshold_pct

    def recenter(
        self,
        grid_state: GridState,
        new_center: float,
        atr_1h: float,
        bias_direction: BiasDirection,
        level_shift: int,
        vol_ratio: float = 1.0,
    ) -> Tuple[List[GridLevel], List[GridLevel]]:
        """Recenter the grid. Returns (new_levels, cancelled_levels).

        Levels that are FILLED or TP_SET are cancelled and must be closed
        by P3 before removal.
        """
        cancelled = []
        now = int(time.time())

        for level in grid_state.levels:
            if level.status != GridLevelStatus.CANCELLED:
                level.status = GridLevelStatus.CANCELLED
                level.updated_at = now
                cancelled.append(level)

        # Create new levels
        new_grid = self.create_grid(
            center_price=new_center,
            atr_1h=atr_1h,
            bias_direction=bias_direction,
            level_shift=level_shift,
            qty_per_level=grid_state.qty_per_level,
            leverage=grid_state.leverage,
            mode=grid_state.mode,
            symbol=grid_state.symbol,
            vol_ratio=vol_ratio,
        )

        logger.info(
            "Grid RECENTER: {} old_center={:.6f} → new_center={:.6f} "
            "cancelled={} new_levels={}",
            grid_state.symbol, grid_state.center_price, new_center,
            len(cancelled), len(new_grid.levels),
        )

        return new_grid.levels, cancelled

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _calc_range(self, center_price: float, atr_1h: float) -> float:
        """Calculate grid range from ATR, clamped by min/max pct."""
        raw_range = atr_1h * self.range_atr_multiplier
        min_range = center_price * self.min_range_pct
        max_range = center_price * self.max_range_pct
        return max(min_range, min(max_range, raw_range))

    def get_open_level_count(self, levels: List[GridLevel]) -> int:
        """Count currently open (FILLED or TP_SET) levels."""
        return sum(
            1 for lv in levels
            if lv.status in (GridLevelStatus.FILLED, GridLevelStatus.TP_SET)
        )

    def get_unrealized_pnl(
        self, levels: List[GridLevel], current_price: float,
    ) -> float:
        """Calculate total unrealized P&L across all open levels."""
        total = 0.0
        for level in levels:
            if level.status not in (GridLevelStatus.FILLED, GridLevelStatus.TP_SET):
                continue
            if level.fill_price <= 0:
                continue
            if level.side == "Buy":
                total += (current_price - level.fill_price)
            else:
                total += (level.fill_price - current_price)
        return total

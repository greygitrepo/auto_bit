"""Grid sizing strategy — position sizing and risk checks for grid trading.

Evaluates whether a grid fill signal should be executed based on
available margin, exposure limits, and drawdown state.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from loguru import logger

from src.strategy.asset.base import BaseAssetStrategy, DailyStats, OrderRequest
from src.strategy.asset.fixed_ratio import DrawdownManager


class GridSizingStrategy:
    """Position sizing and risk management for grid trading.

    Not a subclass of BaseAssetStrategy since grid signals have
    a different structure than directional signals.
    """

    def __init__(self, config: dict) -> None:
        grid_cfg = config.get("strategies", {}).get("grid_bias", {})

        self.leverage = grid_cfg.get("leverage", 5)
        self.qty_per_level_pct = grid_cfg.get("qty_per_level_pct", 5.0)
        self.max_open_levels = grid_cfg.get("max_open_levels", 6)
        self.max_total_exposure_pct = grid_cfg.get("max_total_exposure_pct", 60.0)
        self.max_drawdown_pct = grid_cfg.get("max_drawdown_pct", 20.0)

        # Daily limits from asset config
        daily_cfg = config.get("daily_limits", {})
        self.max_daily_loss_pct = daily_cfg.get("max_daily_loss_pct", 10.0)
        self.max_daily_trades = daily_cfg.get("max_daily_trades", 200)

        # Drawdown manager (reuse from fixed_ratio)
        dd_cfg = config.get("drawdown", {})
        self.drawdown = DrawdownManager(dd_cfg)

    def evaluate_grid_fill(
        self,
        symbol: str,
        side: str,
        level_price: float,
        qty_per_level: float,
        leverage: int,
        initial_balance: float,
        current_balance: float,
        open_positions: List[Dict[str, Any]],
        daily_stats: DailyStats,
    ) -> OrderRequest:
        """Evaluate whether a grid fill should be executed.

        Args:
            symbol: Trading pair.
            side: "Buy" or "Sell".
            level_price: Grid level price.
            qty_per_level: Quantity to trade (in base currency).
            leverage: Leverage for this grid.
            initial_balance: Starting balance.
            current_balance: Current balance.
            open_positions: All currently open positions.
            daily_stats: Today's performance stats.

        Returns:
            OrderRequest (approved or rejected).
        """
        # 1. Drawdown check
        if not self.drawdown.is_trading_allowed:
            return self._reject(symbol, "Trading disabled (drawdown stage 3)")

        dd_result = self.drawdown.check(initial_balance, current_balance)
        if dd_result["stage"] >= 3:
            return self._reject(symbol, f"Drawdown stage {dd_result['stage']}")

        size_factor = dd_result["size_factor"]

        # 2. Daily loss limit
        if initial_balance > 0:
            daily_loss_pct = abs(min(daily_stats.pnl, 0)) / initial_balance * 100
            if daily_loss_pct >= self.max_daily_loss_pct:
                return self._reject(
                    symbol,
                    f"Daily loss limit: {daily_loss_pct:.1f}% >= {self.max_daily_loss_pct:.1f}%",
                )

        # 3. Daily trade count
        if daily_stats.trade_count >= self.max_daily_trades:
            return self._reject(symbol, f"Max daily trades: {daily_stats.trade_count}")

        # 4. Total exposure check
        total_notional = sum(
            abs(float(p.get("size", 0)) * float(p.get("entry_price", 0)))
            for p in open_positions
        )
        new_notional = qty_per_level * level_price
        max_exposure = initial_balance * leverage * self.max_total_exposure_pct / 100
        if total_notional + new_notional > max_exposure:
            return self._reject(
                symbol,
                f"Exposure limit: {total_notional + new_notional:.2f} > {max_exposure:.2f}",
            )

        # 5. Margin check
        required_margin = new_notional / leverage
        required_margin *= size_factor  # Adjust for drawdown
        if required_margin > current_balance * 0.95:  # Keep 5% buffer
            return self._reject(
                symbol,
                f"Insufficient margin: need {required_margin:.4f}, have {current_balance:.4f}",
            )

        # Approved
        adjusted_qty = qty_per_level * size_factor
        position_size = adjusted_qty * level_price

        return OrderRequest(
            approved=True,
            symbol=symbol,
            side=side,
            size=position_size,
            qty=adjusted_qty,
            leverage=leverage,
            order_type="Market",
            stop_loss=0.0,  # Grid doesn't use per-position SL
            take_profit=0.0,  # TP managed by grid engine
            risk_amount=required_margin,
        )

    def _reject(self, symbol: str, reason: str) -> OrderRequest:
        """Create a rejected OrderRequest."""
        logger.debug("Grid sizing REJECT {}: {}", symbol, reason)
        return OrderRequest(
            approved=False,
            symbol=symbol,
            reject_reason=reason,
        )

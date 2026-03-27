"""
Fixed-ratio asset strategy (S-13 through S-16).

Implements position sizing, rejection logic, drawdown management, and
consecutive-loss tracking as a single cohesive strategy that plugs into
the ``BaseAssetStrategy`` interface.
"""

from __future__ import annotations

import time
from math import ceil
from typing import Any

from loguru import logger

from src.strategy.asset.base import BaseAssetStrategy, DailyStats, OrderRequest
from src.utils.messages import SignalMessage


# ---------------------------------------------------------------------------
# S-15: Drawdown management
# ---------------------------------------------------------------------------

class DrawdownManager:
    """Track account drawdown and throttle position sizing accordingly.

    Stage transitions (by drawdown percentage of initial balance):
        - ``< warning_pct``  : stage 0 -- normal trading, ``size_factor=1.0``
        - ``>= warning_pct`` : stage 1 -- warning only,   ``size_factor=1.0``
        - ``>= reduce_pct``  : stage 2 -- reduced sizing, ``size_factor=reduce_factor``
        - ``>= stop_pct``    : stage 3 -- trading stopped, ``size_factor=0.0``

    Recovery:
        - From stage 2 back to 0 when ``auto_recover`` is enabled and drawdown
          drops below ``warning_pct``.
        - Stage 3 never auto-recovers; call :meth:`force_resume` manually.
    """

    def __init__(self, config: dict) -> None:
        self.warning_pct: float = config.get("warning_pct", 5.0)
        self.reduce_pct: float = config.get("reduce_pct", 10.0)
        self.reduce_factor: float = config.get("reduce_factor", 0.5)
        self.stop_pct: float = config.get("stop_pct", 15.0)
        self.auto_recover: bool = config.get("auto_recover", True)
        self.stage: int = 0

    # ----- public API --------------------------------------------------------

    def check(self, initial_balance: float, current_balance: float) -> dict[str, Any]:
        """Evaluate current drawdown and update the internal stage.

        Returns
        -------
        dict
            Keys: ``stage``, ``drawdown_pct``, ``size_factor``, ``message``.
        """
        if initial_balance <= 0:
            return self._result(0, 0.0, 1.0, "No initial balance set")

        drawdown_pct = (initial_balance - current_balance) / initial_balance * 100.0

        # --- determine target stage from drawdown ---
        if drawdown_pct >= self.stop_pct:
            target_stage = 3
        elif drawdown_pct >= self.reduce_pct:
            target_stage = 2
        elif drawdown_pct >= self.warning_pct:
            target_stage = 1
        else:
            target_stage = 0

        # --- apply transitions ---
        if target_stage >= self.stage:
            # Drawdown worsening (or same) -- always follow
            if target_stage > self.stage:
                logger.warning(
                    "Drawdown stage {} -> {}: drawdown={:.2f}%",
                    self.stage, target_stage, drawdown_pct,
                )
            self.stage = target_stage
        else:
            # Drawdown improving -- recovery rules apply
            if self.stage == 3:
                # Stage 3 never auto-recovers
                pass
            elif self.auto_recover and target_stage == 0 and self.stage == 2:
                logger.info(
                    "Drawdown recovered below {:.1f}% -- resuming normal sizing",
                    self.warning_pct,
                )
                self.stage = 0
            elif self.auto_recover and target_stage < self.stage and self.stage < 3:
                # For stage 1 -> 0 recovery
                self.stage = target_stage

        # --- build result ---
        size_factor, message = self._stage_info(drawdown_pct)
        return self._result(self.stage, drawdown_pct, size_factor, message)

    def force_resume(self) -> None:
        """Manually resume trading after stage-3 stoppage."""
        logger.warning("Manual resume from drawdown stage {} -> 0", self.stage)
        self.stage = 0

    @property
    def is_trading_allowed(self) -> bool:
        """``True`` when the strategy is permitted to open new positions."""
        return self.stage < 3

    # ----- internals ---------------------------------------------------------

    def _stage_info(self, drawdown_pct: float) -> tuple[float, str]:
        if self.stage == 0:
            return 1.0, "Normal trading"
        if self.stage == 1:
            return 1.0, f"Drawdown warning: {drawdown_pct:.2f}%"
        if self.stage == 2:
            return self.reduce_factor, f"Reduced sizing ({self.reduce_factor}x): drawdown {drawdown_pct:.2f}%"
        # stage 3
        return 0.0, f"Trading stopped: drawdown {drawdown_pct:.2f}% >= {self.stop_pct}%"

    @staticmethod
    def _result(stage: int, drawdown_pct: float, size_factor: float, message: str) -> dict[str, Any]:
        return {
            "stage": stage,
            "drawdown_pct": drawdown_pct,
            "size_factor": size_factor,
            "message": message,
        }


# ---------------------------------------------------------------------------
# S-16: Consecutive loss tracking
# ---------------------------------------------------------------------------

class ConsecutiveLossTracker:
    """Track consecutive losing trades and enforce cooldown / daily-stop rules.

    Parameters (from ``config/strategy/asset.yaml`` ``consecutive_loss`` section):
        - ``cooldown_after``: number of consecutive losses before cooldown (default 2)
        - ``cooldown_minutes``: duration of cooldown in minutes (default 30)
        - ``stop_after``: number of consecutive losses that triggers a daily stop (default 3)
    """

    def __init__(self, config: dict) -> None:
        self.cooldown_after: int = config.get("cooldown_after", 2)
        self.cooldown_minutes: int = config.get("cooldown_minutes", 30)
        self.stop_after: int = config.get("stop_after", 3)

        self.count: int = 0
        self.cooldown_until: float | None = None

    def record_trade(self, is_win: bool) -> None:
        """Record the outcome of a closed trade.

        A win resets the consecutive-loss counter.  A loss increments it and,
        if the ``cooldown_after`` threshold is reached, starts a cooldown.
        """
        if is_win:
            self.count = 0
            self.cooldown_until = None
            logger.debug("Win recorded -- consecutive loss counter reset")
        else:
            self.count += 1
            logger.info("Loss recorded -- consecutive losses: {}", self.count)
            if self.count >= self.cooldown_after and self.count < self.stop_after:
                self.cooldown_until = time.time() + self.cooldown_minutes * 60
                logger.warning(
                    "Cooldown triggered for {} minutes (consecutive losses: {})",
                    self.cooldown_minutes, self.count,
                )
            if self.count >= self.stop_after:
                logger.warning(
                    "Daily stop triggered (consecutive losses: {} >= {})",
                    self.count, self.stop_after,
                )

    def is_in_cooldown(self) -> bool:
        """``True`` if currently within a cooldown window."""
        if self.cooldown_until is None:
            return False
        if time.time() < self.cooldown_until:
            return True
        # Cooldown expired
        self.cooldown_until = None
        return False

    def should_stop_today(self) -> bool:
        """``True`` if consecutive losses have reached the daily-stop threshold."""
        return self.count >= self.stop_after

    def reset_daily(self) -> None:
        """Reset all counters -- call at UTC midnight."""
        self.count = 0
        self.cooldown_until = None
        logger.info("Consecutive loss tracker reset for new trading day")


# ---------------------------------------------------------------------------
# S-12 / S-13 / S-14: Fixed-ratio strategy
# ---------------------------------------------------------------------------

class FixedRatioStrategy(BaseAssetStrategy):
    """Fixed-ratio position sizing with built-in risk management.

    Reads its configuration from the ``strategies.fixed_ratio``, ``daily_limits``,
    ``consecutive_loss``, and ``drawdown`` sections of ``config/strategy/asset.yaml``.
    """

    def __init__(self, config: dict) -> None:
        strat_cfg = config.get("strategies", {}).get("fixed_ratio", {})
        self.capital_per_position_pct: float = strat_cfg.get("capital_per_position_pct", 5.0)
        self.risk_per_trade_pct: float = strat_cfg.get("risk_per_trade_pct", 1.0)
        self.min_leverage: int = strat_cfg.get("min_leverage", 1)
        self.max_leverage: int = strat_cfg.get("max_leverage", 5)
        self.max_concurrent_positions: int = strat_cfg.get("max_concurrent_positions", 3)
        self.max_per_symbol: int = strat_cfg.get("max_per_symbol", 1)

        daily_cfg = config.get("daily_limits", {})
        self.max_daily_loss_pct: float = daily_cfg.get("max_daily_loss_pct", 3.0)
        self.max_daily_trades: int = daily_cfg.get("max_daily_trades", 15)

        self.drawdown = DrawdownManager(config.get("drawdown", {}))
        self.loss_tracker = ConsecutiveLossTracker(config.get("consecutive_loss", {}))

        logger.info(
            "FixedRatioStrategy initialised: risk={:.1f}%, cap={:.1f}%, "
            "leverage={}-{}, max_positions={}",
            self.risk_per_trade_pct, self.capital_per_position_pct,
            self.min_leverage, self.max_leverage, self.max_concurrent_positions,
        )

    # ----- BaseAssetStrategy interface ----------------------------------------

    def evaluate(
        self,
        signal: SignalMessage,
        initial_balance: float,
        current_balance: float,
        open_positions: list[dict[str, Any]],
        daily_stats: DailyStats,
    ) -> OrderRequest:
        """Evaluate a signal and return a sized order or rejection.

        Steps:
            1. Run all rejection checks (S-14).
            2. Calculate position size with drawdown factor (S-13 / S-15).
            3. Determine dynamic leverage.
            4. Build and return the ``OrderRequest``.
        """
        # --- S-14: reject checks (order matters -- first hit wins) -----------
        reject = self._check_rejections(signal, initial_balance, current_balance,
                                        open_positions, daily_stats)
        if reject is not None:
            return reject

        # --- S-15: drawdown sizing factor ------------------------------------
        dd_result = self.drawdown.check(initial_balance, current_balance)
        size_factor: float = dd_result["size_factor"]

        # --- S-13: position sizing -------------------------------------------
        entry_price = signal.entry_price
        stop_loss = signal.stop_loss
        take_profit = signal.take_profit
        side = "Buy" if signal.signal == "LONG" else "Sell"

        risk_amount = initial_balance * self.risk_per_trade_pct / 100.0

        sl_distance_pct = abs(entry_price - stop_loss) / entry_price if entry_price > 0 else 0
        if sl_distance_pct <= 0:
            return self._reject(signal.symbol, "Stop-loss distance is zero or negative")

        position_size = risk_amount / sl_distance_pct
        max_position = initial_balance * self.capital_per_position_pct / 100.0
        position_size = min(position_size, max_position)

        # Apply drawdown factor
        position_size *= size_factor

        if position_size <= 0:
            return self._reject(signal.symbol, "Position size is zero after drawdown adjustment")

        # --- dynamic leverage ------------------------------------------------
        balance_per_slot = current_balance / self.max_concurrent_positions if self.max_concurrent_positions > 0 else current_balance
        if balance_per_slot > 0:
            leverage = ceil(position_size / balance_per_slot)
        else:
            leverage = self.min_leverage
        leverage = max(self.min_leverage, min(leverage, self.max_leverage))

        # --- quantity --------------------------------------------------------
        qty = position_size / entry_price if entry_price > 0 else 0.0

        # --- margin check ----------------------------------------------------
        required_margin = position_size / leverage
        if required_margin > current_balance:
            return self._reject(signal.symbol, f"Insufficient balance for margin ({required_margin:.2f} > {current_balance:.2f})")

        logger.info(
            "Order approved: {} {} size={:.2f} qty={:.6f} lev={} sl={:.2f} tp={:.2f} "
            "dd_stage={} size_factor={:.1f}",
            side, signal.symbol, position_size, qty, leverage, stop_loss, take_profit,
            dd_result["stage"], size_factor,
        )

        return OrderRequest(
            approved=True,
            symbol=signal.symbol,
            side=side,
            size=position_size,
            qty=qty,
            leverage=leverage,
            order_type="Market",
            stop_loss=stop_loss,
            take_profit=take_profit,
            risk_amount=risk_amount * size_factor,
        )

    def get_default_params(self) -> dict:
        """Return the default parameter set for this strategy."""
        return {
            "capital_per_position_pct": 5.0,
            "risk_per_trade_pct": 1.0,
            "min_leverage": 1,
            "max_leverage": 5,
            "max_concurrent_positions": 3,
            "max_per_symbol": 1,
            "max_daily_loss_pct": 3.0,
            "max_daily_trades": 15,
            "consecutive_loss": {
                "cooldown_after": 2,
                "cooldown_minutes": 30,
                "stop_after": 3,
            },
            "drawdown": {
                "warning_pct": 5,
                "reduce_pct": 10,
                "reduce_factor": 0.5,
                "stop_pct": 15,
                "auto_recover": True,
            },
        }

    # ----- S-14: rejection checks --------------------------------------------

    def _check_rejections(
        self,
        signal: SignalMessage,
        initial_balance: float,
        current_balance: float,
        open_positions: list[dict[str, Any]],
        daily_stats: DailyStats,
    ) -> OrderRequest | None:
        """Run all rejection checks in priority order.

        Returns ``None`` when no rejection applies, otherwise an
        ``OrderRequest(approved=False, ...)`` with the reason.
        """
        symbol = signal.symbol

        # 1. Trading disabled (drawdown stage 3)
        if not self.drawdown.is_trading_allowed:
            return self._reject(symbol, "Trading disabled (drawdown stage 3)")

        # 2. Daily loss limit
        if initial_balance > 0:
            daily_loss_pct = abs(min(daily_stats.pnl, 0)) / initial_balance * 100.0
            if daily_loss_pct >= self.max_daily_loss_pct:
                return self._reject(
                    symbol,
                    f"Daily loss limit reached ({daily_loss_pct:.2f}% >= {self.max_daily_loss_pct:.1f}%)",
                )

        # 3. Daily trade count
        if daily_stats.trade_count >= self.max_daily_trades:
            return self._reject(
                symbol,
                f"Max daily trades reached ({daily_stats.trade_count} >= {self.max_daily_trades})",
            )

        # 4. Consecutive losses -> daily stop
        if self.loss_tracker.should_stop_today():
            return self._reject(
                symbol,
                f"Daily stop: {self.loss_tracker.count} consecutive losses >= {self.loss_tracker.stop_after}",
            )

        # 5. Consecutive losses -> cooldown
        if self.loss_tracker.count >= self.loss_tracker.cooldown_after and self.loss_tracker.is_in_cooldown():
            remaining = 0.0
            if self.loss_tracker.cooldown_until is not None:
                remaining = max(0.0, self.loss_tracker.cooldown_until - time.time()) / 60.0
            return self._reject(
                symbol,
                f"In cooldown ({remaining:.1f} min remaining after {self.loss_tracker.count} consecutive losses)",
            )

        # 6. Max concurrent positions
        if len(open_positions) >= self.max_concurrent_positions:
            return self._reject(
                symbol,
                f"Max concurrent positions reached ({len(open_positions)} >= {self.max_concurrent_positions})",
            )

        # 7. Duplicate symbol
        open_symbols = {pos.get("symbol", "") for pos in open_positions}
        if symbol in open_symbols:
            return self._reject(symbol, f"Position already open for {symbol}")

        # 8. Insufficient balance for margin (preliminary -- exact check after sizing)
        min_margin = initial_balance * self.risk_per_trade_pct / 100.0 / self.max_leverage
        if current_balance < min_margin:
            return self._reject(
                symbol,
                f"Insufficient balance for minimum margin ({current_balance:.2f} < {min_margin:.2f})",
            )

        return None

    # ----- helpers -----------------------------------------------------------

    @staticmethod
    def _reject(symbol: str, reason: str) -> OrderRequest:
        logger.info("Order rejected [{}]: {}", symbol, reason)
        return OrderRequest(approved=False, symbol=symbol, reject_reason=reason)

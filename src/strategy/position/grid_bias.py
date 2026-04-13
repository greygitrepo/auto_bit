"""Grid Bias Strategy — main strategy class for grid + directional bias hybrid.

Called by P2 (StrategyEngineProcess) on every 5m candle. Manages grid lifecycle:
grid creation, fill detection, TP detection, recenter decisions, and bias updates.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger

from src.order.slippage_guard import SlippageGuard
from src.strategy.position.base import (
    BiasDirection,
    GridAction,
    GridLevel,
    GridLevelStatus,
    GridSignal,
    GridState,
)
from src.strategy.position.bias_calculator import BiasCalculator
from src.strategy.position.grid_engine import GridEngine
from src.strategy.position.mtf_filter import MTFAnalysis, MTFFilter
from src.utils.db import DatabaseManager
from src.strategy.position.registry import register_grid


@register_grid("grid_bias")
class GridBiasStrategy:
    """Grid trading strategy with directional bias.

    Unlike MomentumScalper which returns a single PositionSignal,
    this strategy manages persistent grid state per symbol and emits
    a list of GridSignal actions per evaluation cycle.
    """

    def __init__(self, config: dict, db: Optional[DatabaseManager] = None) -> None:
        grid_cfg = config.get("strategies", {}).get("grid_bias", {})
        self.config = grid_cfg
        self.exit_config = config.get("exit", {})
        self.db = db

        self.engine = GridEngine(grid_cfg)
        self.bias_calc = BiasCalculator(grid_cfg)

        # Multi-timeframe filter
        mtf_cfg = grid_cfg.get("mtf", {})
        self.mtf_filter = MTFFilter(mtf_cfg)
        self._mtf_enabled = mtf_cfg.get("enabled", False)

        # Grid state cache: symbol -> GridState
        self._grids: Dict[str, GridState] = {}

        # Recenter tracking: symbol -> last recenter timestamp
        self._last_recenter: Dict[str, float] = {}
        self.recenter_interval = grid_cfg.get("recenter_interval_minutes", 60) * 60

        # Position sizing
        self.leverage = grid_cfg.get("leverage", 5)
        self.qty_per_level_pct = grid_cfg.get("qty_per_level_pct", 5.0)
        self.max_drawdown_pct = grid_cfg.get("max_drawdown_pct", 20.0)
        self.min_spacing_pct = grid_cfg.get("min_spacing_pct", 0.60) / 100.0
        self.stale_fill_revert_seconds = grid_cfg.get("stale_fill_revert_seconds", 360)
        self.hard_stop_loss_pct = self.exit_config.get("hard_stop_loss_pct", 5.0)
        self.grid_timeout_hours = self.exit_config.get("grid_timeout_hours", 24)

        # Max concurrent symbols
        self.max_symbols = grid_cfg.get("max_symbols", 0)  # 0 = unlimited

        # Funding rate cache: symbol -> rate
        self._funding_rates: Dict[str, float] = {}

        # Live slippage tracking: symbol -> [measured_slippage_bps, ...]
        self._symbol_slippage: Dict[str, list] = {}
        # Symbols banned due to excessive slippage
        self._slippage_banned: set = set()

        # Slippage guard for dynamic min spacing
        paper_cfg = config.get("paper", {})
        self._slippage_guard = SlippageGuard({
            "slippage_bps": paper_cfg.get("slippage_bps", 15),
            "max_slippage_bps": paper_cfg.get("max_slippage_bps", 50),
            "fee_rate": paper_cfg.get("fee_rate", {"taker": 0.0006}),
        })

    # ------------------------------------------------------------------
    # Main evaluation (called per 5m candle)
    # ------------------------------------------------------------------

    def evaluate(
        self,
        symbol: str,
        candle_5m: Dict[str, Any],
        df_1h: Optional[pd.DataFrame] = None,
        btc_trend: str = "mixed",
        eth_trend: str = "mixed",
        current_balance: float = 0.0,
        initial_balance: float = 0.0,
        mode: str = "paper",
        *,
        df_5m: Optional[pd.DataFrame] = None,
        df_15m: Optional[pd.DataFrame] = None,
    ) -> List[GridSignal]:
        """Evaluate the grid for a symbol and return action signals.

        Args:
            symbol: Trading pair.
            candle_5m: Latest 5m candle dict {open, high, low, close, volume, timestamp}.
            df_1h: 1h indicator DataFrame for this symbol (with EMA columns).
            btc_trend: "bull", "bear", or "mixed".
            eth_trend: "bull", "bear", or "mixed".
            current_balance: Current account balance.
            initial_balance: Initial account balance.
            mode: "paper" or "live".
            df_5m: 5m OHLCV DataFrame with indicators (optional, for MTF filter).
            df_15m: 15m OHLCV DataFrame with indicators (optional, for MTF filter).

        Returns:
            List of GridSignal actions to send to P3.
        """
        signals: List[GridSignal] = []

        # --- MTF analysis (if data available) ---
        mtf_analysis: Optional[MTFAnalysis] = None
        if self._mtf_enabled and df_5m is not None and df_15m is not None and df_1h is not None:
            mtf_analysis = self.mtf_filter.analyze(df_5m, df_15m, df_1h)

        # Check hard stop loss at account level
        if initial_balance > 0:
            drawdown_pct = (initial_balance - current_balance) / initial_balance * 100
            if drawdown_pct >= self.hard_stop_loss_pct:
                logger.warning(
                    "Hard stop loss triggered: drawdown {:.1f}% >= {:.1f}%",
                    drawdown_pct, self.hard_stop_loss_pct,
                )
                return self._close_all_grids("hard_stop_loss")

        grid = self._grids.get(symbol)

        if grid is None:
            # Skip banned symbols (consecutive TP losses)
            if symbol in self._slippage_banned:
                return []

            # Enforce max_symbols limit
            if self.max_symbols > 0 and len(self._grids) >= self.max_symbols:
                return []

            # MTF gate: block grid creation when timeframes conflict
            if mtf_analysis is not None and not self.mtf_filter.should_create_grid(mtf_analysis):
                logger.info("{}: grid creation blocked by MTF filter ({})", symbol, mtf_analysis.alignment)
                return []

            # Create a new grid for this symbol (in-memory only; DB persist deferred)
            grid = self._create_grid_for_symbol(
                symbol, candle_5m, df_1h, btc_trend, eth_trend,
                current_balance, initial_balance, mode,
                mtf_analysis=mtf_analysis,
            )
            if grid is None:
                return []
            self._grids[symbol] = grid
            self._last_recenter[symbol] = time.time()

            # Send SETUP signal so P3 can place pre-orders on Bybit
            level_data = [
                {"level_index": lv.level_index, "price": lv.price,
                 "side": lv.side, "tp_price": lv.tp_price, "sl_price": lv.sl_price}
                for lv in grid.levels if lv.status == GridLevelStatus.PENDING
            ]
            signals.append(GridSignal(
                symbol=symbol,
                action=GridAction.SETUP,
                level_price=grid.center_price,
                reason=f"Grid created: {len(level_data)} levels",
            ))
            # Attach levels to the signal for P3
            signals[-1].levels = level_data

        # Check if grid should be recentered
        current_price = float(candle_5m.get("close", 0))
        should_recenter = self._check_recenter(symbol, grid, current_price, df_1h, btc_trend, eth_trend)

        if should_recenter:
            recenter_signals = self._do_recenter(
                symbol, grid, current_price, df_1h, btc_trend, eth_trend,
                current_balance, initial_balance,
            )
            signals.extend(recenter_signals)
            return signals

        # Check per-symbol drawdown
        unrealized = self.engine.get_unrealized_pnl(grid.levels, current_price)
        qty = grid.qty_per_level
        if qty > 0 and initial_balance > 0:
            unrealized_pct = abs(min(unrealized * qty, 0)) / initial_balance * 100
            if unrealized_pct >= self.max_drawdown_pct:
                logger.warning(
                    "{}: grid drawdown {:.1f}% >= {:.1f}%, closing all",
                    symbol, unrealized_pct, self.max_drawdown_pct,
                )
                close_signals = self._close_grid(symbol, grid, "grid_drawdown")
                signals.extend(close_signals)
                return signals

        # Revert stale FILLED levels (no P3 confirmation after 60s)
        now_ts = int(time.time())
        for lv in grid.levels:
            if lv.status == GridLevelStatus.FILLED and lv.fill_time > 0:
                if now_ts - lv.fill_time > self.stale_fill_revert_seconds:
                    lv.status = GridLevelStatus.PENDING
                    lv.fill_price = 0.0
                    lv.fill_time = 0
                    lv.updated_at = now_ts
                    logger.info("{}: reverted stale FILLED level idx={} to PENDING", symbol, lv.level_index)

        # Check fills on PENDING levels
        fill_signals = self.engine.check_fills(candle_5m, grid.levels)
        just_filled_indices = set()

        # MTF fill filtering: block fills that go against strong MTF direction
        if mtf_analysis is not None:
            filtered_fills: List[GridSignal] = []
            for sig in fill_signals:
                if self.mtf_filter.should_allow_fill(mtf_analysis, sig.side):
                    filtered_fills.append(sig)
                else:
                    # Revert the level back to PENDING since we're blocking this fill
                    for lv in grid.levels:
                        if lv.level_index == sig.level_index and lv.status == GridLevelStatus.FILLED:
                            lv.status = GridLevelStatus.PENDING
                            lv.fill_price = 0.0
                            lv.fill_time = 0
                            lv.updated_at = int(time.time())
                            break
                    logger.info(
                        "{}: fill blocked by MTF filter: level={} side={} mtf={}",
                        symbol, sig.level_index, sig.side, mtf_analysis.alignment,
                    )
            fill_signals = filtered_fills

        for sig in fill_signals:
            sig.symbol = symbol
            sig.qty_per_level = grid.qty_per_level
            sig.leverage = grid.leverage
            just_filled_indices.add(sig.level_index)
        signals.extend(fill_signals)

        # Check TP hits on TP_SET/FILLED levels.
        # For levels just filled this candle: mark as deferred so they get
        # TP-checked on the NEXT candle instead of being dropped entirely.
        tp_signals = self.engine.check_tp_hits(candle_5m, grid.levels)
        deferred_tp = []
        immediate_tp = []
        for sig in tp_signals:
            if sig.level_index in just_filled_indices:
                deferred_tp.append(sig)
            else:
                immediate_tp.append(sig)

        # Send immediate TPs now
        for sig in immediate_tp:
            sig.symbol = symbol
        signals.extend(immediate_tp)

        # For deferred TPs: revert level to TP_SET so next candle will re-check.
        # The level stays open (FILLED→TP_SET), ensuring the TP gets processed
        # on the next evaluation cycle rather than being lost forever.
        for sig in deferred_tp:
            for lv in grid.levels:
                if lv.level_index == sig.level_index and lv.status == GridLevelStatus.COMPLETED:
                    lv.status = GridLevelStatus.TP_SET
                    lv.tp_fill_price = 0.0
                    lv.tp_fill_time = 0
                    lv.updated_at = int(time.time())
                    break

        # Check SL hits on FILLED/TP_SET levels
        sl_signals = self.engine.check_sl_hits(candle_5m, grid.levels)
        for sig in sl_signals:
            sig.symbol = symbol
        signals.extend(sl_signals)

        # Recycle completed levels (TP or SL)
        recycled = self.engine.recycle_completed(grid.levels)
        if recycled > 0:
            logger.debug("{}: recycled {} completed levels", symbol, recycled)

        # Persist changes to DB
        if signals:
            if grid.id == 0:
                # First time this grid has signals → do full persist once
                self._persist_grid(grid)
            else:
                self._persist_level_updates(grid)

        return signals

    # ------------------------------------------------------------------
    # Grid lifecycle
    # ------------------------------------------------------------------

    def _create_grid_for_symbol(
        self,
        symbol: str,
        candle_5m: Dict[str, Any],
        df_1h: Optional[pd.DataFrame],
        btc_trend: str,
        eth_trend: str,
        current_balance: float,
        initial_balance: float,
        mode: str,
        mtf_analysis: Optional[MTFAnalysis] = None,
    ) -> Optional[GridState]:
        """Create a new grid for a symbol."""
        close_price = float(candle_5m.get("close", 0))
        if close_price <= 0:
            return None

        # Calculate ATR from 1h data
        atr_1h = self._get_atr_1h(df_1h, close_price)

        # Calculate volatility ratio (current ATR / average ATR over lookback)
        vol_ratio = self._calc_vol_ratio(df_1h, atr_1h)

        # Calculate center price
        center = self._calc_center(candle_5m, df_1h)

        # Calculate bias
        funding_rate = self._funding_rates.get(symbol)
        direction, magnitude, level_shift = self.bias_calc.compute(
            df_1h, funding_rate, btc_trend, eth_trend,
        )

        # MTF bias adjustment
        if mtf_analysis is not None:
            magnitude = self.mtf_filter.adjust_bias(magnitude, mtf_analysis)
            # Recalculate level_shift from adjusted magnitude
            max_shift = self.bias_calc.max_level_shift
            level_shift = int(round(magnitude * max_shift))
            level_shift = max(-max_shift, min(max_shift, level_shift))

        # Calculate qty per level
        qty_per_level = self._calc_qty_per_level(close_price, current_balance)

        grid = self.engine.create_grid(
            center_price=center,
            atr_1h=atr_1h,
            bias_direction=direction,
            level_shift=level_shift,
            qty_per_level=qty_per_level,
            leverage=self.leverage,
            mode=mode,
            symbol=symbol,
            vol_ratio=vol_ratio,
        )
        grid.bias_magnitude = magnitude

        # Filter: reject if spacing is too small to cover fees + slippage
        # Use dynamic min spacing from SlippageGuard (based on estimated slippage)
        if center > 0:
            spacing_pct = grid.grid_spacing / center
            estimated_slippage = self._slippage_guard.estimate_slippage_bps(symbol, qty_per_level)
            dynamic_min = self._slippage_guard.adjust_min_spacing(estimated_slippage) / 100.0
            effective_min = max(self.min_spacing_pct, dynamic_min)
            if spacing_pct < effective_min:
                logger.info(
                    "Grid SKIPPED {}: spacing {:.4f}% < min {:.4f}% "
                    "(static={:.4f}%, dynamic={:.4f}%, est_slippage={:.1f}bps)",
                    symbol, spacing_pct * 100, effective_min * 100,
                    self.min_spacing_pct * 100, dynamic_min * 100,
                    estimated_slippage,
                )
                return None

        logger.info(
            "Grid initialized for {}: center={:.6f} range={:.6f} "
            "spacing={:.6f} ({:.3f}%) bias={} qty_per_level={:.4f}",
            symbol, center, grid.grid_range, grid.grid_spacing,
            (grid.grid_spacing / center * 100) if center > 0 else 0,
            direction.value, qty_per_level,
        )
        return grid

    def _check_recenter(
        self, symbol: str, grid: GridState, current_price: float,
        df_1h: Optional[pd.DataFrame], btc_trend: str, eth_trend: str,
    ) -> bool:
        """Check if the grid needs recentering."""
        # Price drift check
        if self.engine.should_recenter(current_price, grid):
            return True

        # Time-based recenter
        last = self._last_recenter.get(symbol, 0)
        if time.time() - last >= self.recenter_interval:
            return True

        # Timeout check
        age_hours = (time.time() - grid.created_at) / 3600
        if age_hours >= self.grid_timeout_hours:
            return True

        return False

    def _do_recenter(
        self, symbol: str, grid: GridState, current_price: float,
        df_1h: Optional[pd.DataFrame], btc_trend: str, eth_trend: str,
        current_balance: float, initial_balance: float,
    ) -> List[GridSignal]:
        """Perform grid recenter. Keeps open positions, only replaces PENDING levels."""
        signals: List[GridSignal] = []

        # Keep open positions (FILLED/TP_SET) — don't force close them
        kept_levels = [
            lv for lv in grid.levels
            if lv.status in (GridLevelStatus.FILLED, GridLevelStatus.TP_SET)
        ]

        # Create new grid for the PENDING replacement levels
        new_grid = self._create_grid_for_symbol(
            symbol, {"close": current_price, "low": current_price,
                     "high": current_price, "volume": 0},
            df_1h, btc_trend, eth_trend,
            current_balance, initial_balance, grid.mode,
        )

        if new_grid is not None:
            # Merge: keep open positions + new pending levels
            # Remove new levels that conflict with kept levels' indices
            kept_indices = {lv.level_index for lv in kept_levels}
            new_pending = [lv for lv in new_grid.levels if lv.level_index not in kept_indices]
            new_grid.id = grid.id
            new_grid.realized_pnl = grid.realized_pnl
            new_grid.levels = kept_levels + new_pending
            self._grids[symbol] = new_grid
            self._last_recenter[symbol] = time.time()
            self._persist_grid(new_grid)

            logger.info(
                "{}: recenter keeping {} open positions, {} new pending levels",
                symbol, len(kept_levels), len(new_grid.levels) - len(kept_levels),
            )
        else:
            # Can't create new grid (spacing filter etc). Keep existing grid with open positions.
            # Just update recenter timestamp to prevent immediate re-trigger.
            self._last_recenter[symbol] = time.time()
            logger.info("{}: recenter skipped (new grid failed), keeping {} open positions",
                        symbol, len(kept_levels))

        return signals

    def _close_grid(self, symbol: str, grid: GridState, reason: str) -> List[GridSignal]:
        """Close all open levels for a grid."""
        signals: List[GridSignal] = []
        for level in grid.levels:
            if level.status in (GridLevelStatus.FILLED, GridLevelStatus.TP_SET):
                signals.append(GridSignal(
                    symbol=symbol,
                    action=GridAction.CLOSE_ALL,
                    level_id=level.id,
                    level_index=level.level_index,
                    level_price=level.price,
                    side=level.side,
                    grid_state_id=grid.id,
                    reason=reason,
                ))
                level.status = GridLevelStatus.CANCELLED
                level.updated_at = int(time.time())

        grid.status = "stopped"
        if symbol in self._grids:
            del self._grids[symbol]
        self._persist_grid(grid)
        return signals

    def _close_all_grids(self, reason: str) -> List[GridSignal]:
        """Close all grids on all symbols."""
        signals: List[GridSignal] = []
        for symbol in list(self._grids.keys()):
            grid = self._grids[symbol]
            signals.extend(self._close_grid(symbol, grid, reason))
        return signals

    # ------------------------------------------------------------------
    # P3 feedback handling
    # ------------------------------------------------------------------

    def on_fill_confirmed(self, symbol: str, level_id: int, position_id: int) -> None:
        """Called when P3 confirms a position was opened for a grid level."""
        grid = self._grids.get(symbol)
        if grid is None:
            return
        for level in grid.levels:
            if level.id == level_id:
                level.status = GridLevelStatus.TP_SET
                level.position_id = position_id
                level.updated_at = int(time.time())
                break
        self._persist_level_updates(grid)

    def on_tp_confirmed(
        self, symbol: str, level_id: int, pnl: float, fee: float,
    ) -> None:
        """Called when P3 confirms a TP close for a grid level."""
        grid = self._grids.get(symbol)
        if grid is None:
            return
        for level in grid.levels:
            if level.id == level_id:
                level.status = GridLevelStatus.COMPLETED
                level.pnl = pnl
                level.fee = fee
                level.updated_at = int(time.time())
                grid.realized_pnl += pnl
                break

        # Track per-symbol TP results — ban symbols with consistent losses
        if symbol not in self._symbol_slippage:
            self._symbol_slippage[symbol] = []
        self._symbol_slippage[symbol].append(pnl)
        # Keep last 5 results
        self._symbol_slippage[symbol] = self._symbol_slippage[symbol][-5:]

        recent = self._symbol_slippage[symbol]
        if len(recent) >= 3 and all(p < 0 for p in recent[-3:]):
            logger.warning(
                "{}: 3 consecutive TP losses ({}) — banning symbol",
                symbol, [round(p, 6) for p in recent[-3:]],
            )
            self._slippage_banned.add(symbol)
            # Close this grid
            if grid is not None:
                self._close_grid(symbol, grid, "consecutive_tp_losses")

    def on_close_confirmed(self, symbol: str, level_id: int, pnl: float, fee: float) -> None:
        """Called when P3 confirms a recenter/close_all position close."""
        grid = self._grids.get(symbol)
        if grid is None:
            return
        for level in grid.levels:
            if level.id == level_id:
                level.status = GridLevelStatus.CANCELLED
                level.pnl = pnl
                level.fee = fee
                level.updated_at = int(time.time())
                grid.realized_pnl += pnl
                break

    # ------------------------------------------------------------------
    # Funding rate
    # ------------------------------------------------------------------

    def update_funding_rate(self, symbol: str, rate: float) -> None:
        """Update cached funding rate for a symbol."""
        self._funding_rates[symbol] = rate

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _calc_vol_ratio(self, df_1h: Optional[pd.DataFrame], current_atr: float) -> float:
        """Calculate volatility ratio: current ATR / average ATR over lookback period.

        Returns 1.0 if data is insufficient.
        """
        dynamic_cfg = self.config.get("dynamic_spacing", {})
        if not dynamic_cfg.get("enabled", False):
            return 1.0

        lookback = dynamic_cfg.get("atr_lookback_hours", 24)

        if df_1h is None or df_1h.empty or "atr_14" not in df_1h.columns:
            return 1.0

        atr_series = df_1h["atr_14"].dropna()
        if len(atr_series) < 2:
            return 1.0

        # Use up to `lookback` most recent candles for average
        recent_atr = atr_series.tail(lookback)
        avg_atr = float(recent_atr.mean())

        if avg_atr <= 0:
            return 1.0

        return current_atr / avg_atr

    def _get_atr_1h(self, df_1h: Optional[pd.DataFrame], fallback_price: float) -> float:
        """Extract ATR from 1h data or estimate from price."""
        if df_1h is not None and not df_1h.empty and "atr_14" in df_1h.columns:
            atr = df_1h.iloc[-1]["atr_14"]
            if not pd.isna(atr) and atr > 0:
                return float(atr)
        # Fallback: estimate ATR as 2% of price
        return fallback_price * 0.02

    def _calc_center(
        self, candle_5m: Dict[str, Any], df_1h: Optional[pd.DataFrame],
    ) -> float:
        """Calculate grid center price."""
        method = self.config.get("center_method", "vwap")

        if method == "vwap" and df_1h is not None and not df_1h.empty:
            if "vwap" in df_1h.columns:
                vwap = df_1h.iloc[-1].get("vwap")
                if vwap is not None and not pd.isna(vwap) and vwap > 0:
                    return float(vwap)

        if method == "mid_range":
            high = float(candle_5m.get("high", 0))
            low = float(candle_5m.get("low", 0))
            if high > 0 and low > 0:
                return (high + low) / 2

        # Default: last close
        return float(candle_5m.get("close", 0))

    def _calc_qty_per_level(self, price: float, balance: float) -> float:
        """Calculate quantity per grid level."""
        if price <= 0 or balance <= 0:
            return 0.0
        margin_per_level = balance * self.qty_per_level_pct / 100.0
        notional = margin_per_level * self.leverage
        return notional / price

    def _persist_grid(self, grid: GridState) -> None:
        """Full persist: grid state + all levels. Use only on creation/recenter."""
        if self.db is None:
            return

        now = int(time.time())
        grid.updated_at = now

        grid_id = self.db.upsert_grid_state(
            mode=grid.mode,
            symbol=grid.symbol,
            status=grid.status,
            center_price=grid.center_price,
            grid_range=grid.grid_range,
            grid_spacing=grid.grid_spacing,
            num_buy_levels=grid.num_buy_levels,
            num_sell_levels=grid.num_sell_levels,
            bias=grid.bias,
            bias_magnitude=grid.bias_magnitude,
            leverage=grid.leverage,
            qty_per_level=grid.qty_per_level,
            total_margin=grid.total_margin,
            realized_pnl=grid.realized_pnl,
            created_at=grid.created_at,
            updated_at=now,
        )
        grid.id = grid_id

        # Delete old levels and insert new ones
        self.db.delete_grid_levels(grid_id)
        level_rows = []
        for lv in grid.levels:
            lv.grid_state_id = grid_id
            level_rows.append({
                "grid_state_id": grid_id,
                "level_index": lv.level_index,
                "price": lv.price,
                "side": lv.side,
                "status": lv.status.value,
                "tp_price": lv.tp_price,
                "fill_price": lv.fill_price or None,
                "fill_time": lv.fill_time or None,
                "tp_fill_price": lv.tp_fill_price or None,
                "tp_fill_time": lv.tp_fill_time or None,
                "pnl": lv.pnl,
                "fee": lv.fee,
                "position_id": lv.position_id or None,
                "created_at": lv.created_at,
                "updated_at": now,
            })
        if level_rows:
            self.db.insert_grid_levels_bulk(level_rows)

        # Update level IDs from DB
        db_levels = self.db.get_grid_levels(grid_id)
        for db_lv, mem_lv in zip(db_levels, sorted(grid.levels, key=lambda l: l.level_index)):
            mem_lv.id = db_lv["id"]

    def _persist_level_updates(self, grid: GridState) -> None:
        """Lightweight persist: only update changed level statuses. No full rewrite."""
        if self.db is None or grid.id == 0:
            return
        now = int(time.time())
        for lv in grid.levels:
            if lv.id > 0 and lv.updated_at >= now - 1:
                self.db.update_grid_level(
                    lv.id,
                    status=lv.status.value,
                    fill_price=lv.fill_price or None,
                    fill_time=lv.fill_time or None,
                    tp_fill_price=lv.tp_fill_price or None,
                    tp_fill_time=lv.tp_fill_time or None,
                    pnl=lv.pnl,
                    fee=lv.fee,
                    position_id=lv.position_id or None,
                    updated_at=now,
                )

    def restore_from_db(self, mode: str) -> int:
        """Restore active grid states from database. Returns count restored."""
        if self.db is None:
            return 0

        rows = self.db.get_all_grid_states(mode)
        count = 0
        for row in rows:
            symbol = row["symbol"]
            grid = GridState(
                id=row["id"],
                mode=row["mode"],
                symbol=symbol,
                status=row["status"],
                center_price=row["center_price"],
                grid_range=row["grid_range"],
                grid_spacing=row["grid_spacing"],
                num_buy_levels=row["num_buy_levels"],
                num_sell_levels=row["num_sell_levels"],
                bias=row["bias"],
                bias_magnitude=row["bias_magnitude"],
                leverage=row["leverage"],
                qty_per_level=row["qty_per_level"],
                total_margin=row["total_margin"],
                realized_pnl=row["realized_pnl"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

            # Restore levels
            db_levels = self.db.get_active_grid_levels(row["id"])
            for lv_row in db_levels:
                grid.levels.append(GridLevel(
                    id=lv_row["id"],
                    grid_state_id=lv_row["grid_state_id"],
                    level_index=lv_row["level_index"],
                    price=lv_row["price"],
                    side=lv_row["side"],
                    status=GridLevelStatus(lv_row["status"]),
                    tp_price=lv_row["tp_price"] or 0.0,
                    fill_price=lv_row["fill_price"] or 0.0,
                    fill_time=lv_row["fill_time"] or 0,
                    tp_fill_price=lv_row["tp_fill_price"] or 0.0,
                    tp_fill_time=lv_row["tp_fill_time"] or 0,
                    pnl=lv_row["pnl"] or 0.0,
                    fee=lv_row["fee"] or 0.0,
                    position_id=lv_row["position_id"] or 0,
                    created_at=lv_row["created_at"],
                    updated_at=lv_row["updated_at"],
                ))

            self._grids[symbol] = grid
            self._last_recenter[symbol] = time.time()
            count += 1
            logger.info("Restored grid for {}: {} levels", symbol, len(grid.levels))

        return count

    def get_active_symbols(self) -> List[str]:
        """Return list of symbols with active grids."""
        return list(self._grids.keys())

    def get_grid_status(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return grid status dict for GUI/API."""
        grid = self._grids.get(symbol)
        if grid is None:
            return None
        open_count = self.engine.get_open_level_count(grid.levels)
        return {
            "symbol": symbol,
            "status": grid.status,
            "center_price": grid.center_price,
            "grid_range": grid.grid_range,
            "grid_spacing": grid.grid_spacing,
            "bias": grid.bias,
            "num_buy_levels": grid.num_buy_levels,
            "num_sell_levels": grid.num_sell_levels,
            "open_levels": open_count,
            "realized_pnl": grid.realized_pnl,
            "leverage": grid.leverage,
            "qty_per_level": grid.qty_per_level,
        }

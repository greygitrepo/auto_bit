# Strategy Design v1 -- Grid + Directional Bias Improvements

**Date:** 2026-03-27
**System:** auto_bit grid trading on Bybit USDT perpetual futures
**Capital:** 20 USDT, 5x leverage
**Status:** Paper trading, 3 active grids (VVVUSDT, BRUSDT, PTBUSDT)

---

## 1. Current System Analysis

### 1.1 Architecture Summary

The system runs a **Grid + Directional Bias hybrid strategy** on 5-minute candles:

- **GridEngine** (`grid_engine.py`): Pure state machine managing level creation, fill detection (candle crosses level), TP detection, and recentering.
- **GridBiasStrategy** (`grid_bias.py`): Main orchestrator called per 5m candle per symbol. Manages grid lifecycle, bias updates, and DB persistence.
- **BiasCalculator** (`bias_calculator.py`): Combines 1h EMA trend (weight 0.5), funding rate (weight 0.3), and BTC/ETH market trend (weight 0.2) into a directional bias that shifts buy/sell level allocation.
- **GridSizingStrategy** (`grid_sizing.py`): Position sizing gatekeeper checking drawdown, daily limits, exposure, and margin before approving fills.

### 1.2 Current Parameters (grid.yaml)

| Parameter | Value | Notes |
|---|---|---|
| num_levels | 10 | Fixed for all symbols |
| buy/sell split | 5/5 | Neutral default, shifted by bias up to +/-3 |
| range_atr_multiplier | 2.5 | grid_range = ATR_1h * 2.5 |
| min_range_pct | 1.0% | Floor on grid range |
| max_range_pct | 8.0% | Cap on grid range |
| recenter_threshold_pct | 1.5% | Price drift triggers immediate recenter |
| recenter_interval | 60 min | Time-based recenter |
| leverage | 5x | |
| qty_per_level_pct | 5.0% | 5% of balance as margin per level |
| max_open_levels | 6 | Simultaneous open positions |
| max_symbols | 5 | Concurrent grid symbols |
| min_spacing_pct | 0.45% | Profitability filter (must exceed friction) |

### 1.3 Current Performance Data

**3 completed trades (all winners):**

| Symbol | Entry | Exit | Profit % | Fee % | Net PnL |
|---|---|---|---|---|---|
| VVVUSDT | 5.8615 | 5.8869 | 0.43% | 0.06% | 0.0155 |
| BRUSDT | 0.1203 | 0.1230 | 2.31% | 0.06% | 0.1067 |
| PTBUSDT | 0.001838 | 0.001851 | 0.66% | 0.06% | 0.0266 |

**Active grid spacing analysis:**

| Symbol | Spacing % | Friction Cost | Net Per Cell | Profitability Ratio |
|---|---|---|---|---|
| VVVUSDT | 0.58% | 0.42% | 0.16% | 1.38x |
| BRUSDT | 0.80% | 0.42% | 0.38% | 1.90x |
| PTBUSDT | 0.80% | 0.42% | 0.38% | 1.90x |

**Key observations:**
- VVVUSDT is barely profitable (1.38x friction), borderline viability
- BRUSDT and PTBUSDT have healthy margins (1.9x friction)
- All 3 grids are NEUTRAL bias despite PTBUSDT having magnitude 0.217 (threshold is 0.3)
- The BRUSDT first trade had 2.31% profit -- this was likely a fill that skipped multiple levels in one candle, capturing a large move
- Current balance shows 18.14 USDT (down from 20.0 initial) -- the PnL of 0.158 USDT from trades does not account for the full picture; open positions and unrealized losses may explain the gap

### 1.4 Friction Cost Breakdown

```
Entry slippage:   0.15%  (simulated, conservative for altcoins)
Exit slippage:    0.15%
Entry fee:        0.06%  (Bybit taker fee, USDT perps)
Exit fee:         0.06%
-------------------------------
Total friction:   0.42%
```

Minimum profitable spacing must exceed 0.42%. Current filter is 0.45% (7% margin). This is too thin.

---

## 2. Improvement Designs

### A. Dynamic Spacing Based on Volatility Regime

**Problem:** Fixed spacing (ATR * multiplier / num_levels) treats all volatility regimes equally. In low-vol regimes, spacing is tight and fills are frequent but often net-negative after friction. In high-vol regimes, spacing could be wider to capture bigger moves.

**Design:**

```python
# Compute ATR ratio: current volatility vs 24h average
atr_ratio = current_atr_1h / average_atr_24h  # rolling mean of last 24 1h candles

# Dynamic multiplier
if atr_ratio > 1.5:       # High volatility
    spacing_multiplier = 1.4   # 40% wider spacing
elif atr_ratio > 1.0:     # Above average
    spacing_multiplier = 1.15  # 15% wider
elif atr_ratio > 0.6:     # Normal/below average
    spacing_multiplier = 1.0   # Standard
else:                      # Very low volatility
    spacing_multiplier = 0.8   # 20% tighter (more fills to compensate)

# Alternatively, continuous formula:
spacing_multiplier = clamp(0.7 + 0.5 * atr_ratio, 0.7, 1.8)

# Applied in _calc_range:
grid_range = atr_1h * range_atr_multiplier * spacing_multiplier
```

**Where to implement:** Modify `GridEngine._calc_range()` to accept `atr_ratio` parameter. The `GridBiasStrategy` computes `atr_ratio` from `df_1h` (last ATR vs rolling 24h mean ATR) and passes it through.

**Constraints:**
- Floor: `min_spacing_pct` remains at 0.45% (hard profitability constraint)
- Cap: `max_range_pct` at 8.0% prevents absurdly wide grids in flash crashes
- The dynamic multiplier only adjusts the ATR-based range, not the profitability filter

**Expected Impact:**
- High-vol periods: fewer but larger wins per cell (~30-50% more profit per completed cell)
- Low-vol periods: more frequent small wins, maintaining throughput
- Estimated improvement: +15-25% net profitability over static spacing

**Complexity:** Low
**Priority:** P0 -- immediate, highest ROI change

---

### B. Smart Symbol Selection with Grid Profitability Score

**Problem:** Current system picks top 5 by scanner score without considering grid-specific profitability. A symbol with high momentum score but thin spacing will lose money in the grid.

**Design:**

```python
# Grid Profitability Score (GPS)
def compute_grid_profitability_score(symbol_data):
    atr_pct = atr_1h / price * 100
    spacing_pct = atr_pct * range_atr_multiplier / num_levels
    friction_cost = 0.42  # Fixed: 2x slippage + 2x fee

    # Net profit per cell as percentage
    net_per_cell = spacing_pct - friction_cost

    # Profitability ratio (must be > 2.0 to qualify)
    profitability_ratio = spacing_pct / friction_cost

    # Expected daily throughput estimate (higher vol = more fills)
    # Use ATR% as proxy for fill frequency
    expected_fills_per_day = min(atr_pct * 10, 20)  # rough heuristic

    # Combined score
    gps = net_per_cell * expected_fills_per_day

    return {
        'gps': gps,
        'spacing_pct': spacing_pct,
        'profitability_ratio': profitability_ratio,
        'net_per_cell': net_per_cell,
    }

# Selection criteria
MINIMUM_PROFITABILITY_RATIO = 2.0  # spacing must be >= 2x friction
MINIMUM_NET_PER_CELL = 0.20        # at least 0.20% net per cell
```

**Symbol rotation logic:**

```python
# On each recenter cycle (60 min), evaluate all candidates
def select_symbols(candidates, active_grids, max_symbols=5):
    scored = []
    for sym in candidates:
        gps = compute_grid_profitability_score(sym)
        if gps['profitability_ratio'] < 2.0:
            continue  # Skip unprofitable symbols
        if gps['net_per_cell'] < 0.20:
            continue
        scored.append((sym, gps))

    # Sort by GPS descending
    scored.sort(key=lambda x: x[1]['gps'], reverse=True)

    # Drop underperformers from active grids
    for sym in list(active_grids):
        if sym not in [s[0] for s in scored[:max_symbols * 2]]:
            # Symbol no longer in top candidates -- close grid
            close_grid(sym, reason='underperformer_rotation')

    # Select top N
    return [s[0] for s in scored[:max_symbols]]
```

**Where to implement:** New method in `GridBiasStrategy` or a separate `SymbolSelector` class called before `evaluate()`. Integrate with the existing scanner pipeline.

**Expected Impact:**
- Eliminates borderline symbols (like VVVUSDT with 1.38x ratio)
- Focuses capital on symbols with 2x+ profitability ratio
- Estimated improvement: +20-30% net profitability through better symbol allocation

**Complexity:** Medium
**Priority:** P0 -- directly prevents loss-making grid deployments

---

### C. Adaptive Level Count Per Symbol

**Problem:** Fixed 10 levels for all symbols ignores that high-ATR symbols benefit from fewer, wider-spaced levels (bigger profit per cell) while low-ATR symbols need more levels with tighter spacing to generate fill frequency.

**Design:**

```python
def compute_adaptive_levels(atr_pct, target_spacing_pct=0.60, min_levels=4, max_levels=16):
    """
    Compute optimal number of grid levels based on ATR%.

    target_spacing_pct: desired spacing between levels (must exceed friction + margin)
    atr_pct: ATR as percentage of price
    """
    # range_pct = atr_pct * range_atr_multiplier (e.g., 2.5)
    range_pct = atr_pct * 2.5

    # num_levels = range / target_spacing
    raw_levels = round(range_pct / target_spacing_pct)

    # Clamp
    num_levels = max(min_levels, min(max_levels, raw_levels))

    # Verify actual spacing exceeds minimum
    actual_spacing = range_pct / num_levels
    if actual_spacing < 0.45:  # min_spacing_pct
        # Reduce levels to widen spacing
        num_levels = max(min_levels, int(range_pct / 0.45))

    return num_levels
```

**Example calculations:**

| Symbol ATR% | Range (2.5x) | Target Spacing 0.60% | Levels | Actual Spacing |
|---|---|---|---|---|
| 0.5% | 1.25% | 0.60% | 4 (min) | 0.31% -> SKIP (below 0.45%) |
| 1.0% | 2.50% | 0.60% | 4 | 0.63% |
| 2.0% | 5.00% | 0.60% | 8 | 0.63% |
| 3.0% | 7.50% | 0.60% | 12 | 0.63% |
| 4.0% | 8.00% (capped) | 0.60% | 13 | 0.62% |

**Where to implement:** Modify `GridEngine.__init__()` to accept dynamic `num_levels` or compute it in `create_grid()`. Pass ATR% from `GridBiasStrategy._create_grid_for_symbol()`.

**Expected Impact:**
- High-ATR symbols: fewer levels, each with wider spacing -- less exposure, bigger wins
- Low-ATR symbols: more levels, more frequent fills -- higher throughput
- Symbols with ATR too low to profit get automatically filtered out
- Estimated improvement: +10-15% more efficient capital utilization

**Complexity:** Low
**Priority:** P1 -- natural extension of Dynamic Spacing (A)

---

### D. Improved Bias Calculation

**Problem:** Current bias only shifts buy/sell level count (max shift +/-3) and clamps to at least 1 level on each side. Strong directional moves should completely eliminate counter-trend levels. Volume breakouts are ignored.

**Design:**

#### D1. Aggressive Bias Mode (Strong Signals)

```python
# In BiasCalculator.compute():

# Current thresholds: > 0.3 = BULLISH, < -0.3 = BEARISH
# Add STRONG thresholds:
STRONG_THRESHOLD = 0.7

if total > STRONG_THRESHOLD:
    direction = BiasDirection.STRONG_BULLISH
    # Only buy levels, no sell levels
    level_shift = num_levels - 1  # e.g., 9 buy, 1 sell (keep 1 sell for safety)
elif total > 0.3:
    direction = BiasDirection.BULLISH
    # Normal shift
elif total < -STRONG_THRESHOLD:
    direction = BiasDirection.STRONG_BEARISH
    level_shift = -(num_levels - 1)  # e.g., 1 buy, 9 sell
elif total < -0.3:
    direction = BiasDirection.BEARISH
```

**In GridEngine.create_grid(), remove the min-1 clamp for strong bias:**

```python
# Current: num_buy = max(1, min(self.num_levels - 1, num_buy))
# New:
if direction in (BiasDirection.STRONG_BULLISH, BiasDirection.STRONG_BEARISH):
    num_buy = max(0, min(self.num_levels, num_buy))  # Allow 0 on one side
else:
    num_buy = max(1, min(self.num_levels - 1, num_buy))  # Keep at least 1
```

#### D2. Volume-Weighted Bias

```python
def _calc_volume_bias(self, df_1h: Optional[pd.DataFrame]) -> float:
    """Volume breakout bias. Returns [-1, 1]."""
    if df_1h is None or len(df_1h) < 24:
        return 0.0

    # Current volume vs 24h average
    current_vol = df_1h.iloc[-1]['volume']
    avg_vol = df_1h.iloc[-24:]['volume'].mean()

    if avg_vol <= 0:
        return 0.0

    vol_ratio = current_vol / avg_vol

    # Only trigger on significant volume (> 2x average)
    if vol_ratio < 2.0:
        return 0.0

    # Determine direction from price action
    close = df_1h.iloc[-1]['close']
    open_ = df_1h.iloc[-1]['open']

    if close > open_:
        return min(1.0, (vol_ratio - 2.0) / 3.0)   # Bullish breakout
    else:
        return max(-1.0, -(vol_ratio - 2.0) / 3.0)  # Bearish breakout
```

**Updated weight distribution:**

```yaml
bias:
  ema_weight: 0.40       # was 0.50
  funding_weight: 0.20   # was 0.30
  btc_eth_weight: 0.15   # was 0.20
  volume_weight: 0.25    # NEW
```

**Where to implement:**
- Add `STRONG_BULLISH` / `STRONG_BEARISH` to `BiasDirection` enum
- Add `_calc_volume_bias()` to `BiasCalculator`
- Modify `GridEngine.create_grid()` to handle strong bias

**Expected Impact:**
- Strong trend periods: eliminates counter-trend levels that would lose money
- Volume breakouts: catches directional moves early, reduces adverse fills
- Estimated improvement: +15-25% during trending markets (reduces losses from counter-trend fills)

**Complexity:** Medium
**Priority:** P1 -- significant edge in trending markets

---

### E. Grid Profit Compounding

**Problem:** `qty_per_level` is fixed at grid creation time based on `balance * 5%`. As profits accumulate, the system does not reinvest. Conversely, after losses, it does not scale down.

**Design:**

#### E1. Equity-Based Recalculation on Recenter

```python
def _do_recenter(self, symbol, grid, current_price, ...):
    # ... existing recenter logic ...

    # Recalculate qty_per_level based on CURRENT balance (not initial)
    # This happens naturally if _create_grid_for_symbol uses current_balance
    # (which it already does via _calc_qty_per_level)

    # The key change: ensure current_balance is the REAL current equity
    # including unrealized PnL, not just the wallet balance
    effective_balance = current_balance + total_unrealized_pnl
    qty_per_level = self._calc_qty_per_level(current_price, effective_balance)
```

This is **already partially implemented** -- `_do_recenter` calls `_create_grid_for_symbol` which uses `current_balance`. The gap is that `current_balance` may not include unrealized PnL from other grids.

#### E2. Winner-Symbol Allocation Boost

```python
def _calc_qty_per_level(self, price, balance, symbol=None):
    """Calculate quantity per grid level with performance weighting."""
    if price <= 0 or balance <= 0:
        return 0.0

    base_pct = self.qty_per_level_pct  # 5.0%

    # Performance-based adjustment
    if symbol and symbol in self._grids:
        grid = self._grids[symbol]
        if grid.realized_pnl > 0:
            # Winner: boost allocation by up to 50%
            boost = min(0.5, grid.realized_pnl / (balance * 0.01))
            base_pct *= (1.0 + boost)
        elif grid.realized_pnl < 0:
            # Loser: reduce allocation by up to 30%
            reduction = min(0.3, abs(grid.realized_pnl) / (balance * 0.01))
            base_pct *= (1.0 - reduction)

    margin_per_level = balance * base_pct / 100.0
    notional = margin_per_level * self.leverage
    return notional / price
```

**Constraints:**
- Total exposure across all grids must still respect `max_total_exposure_pct` (60%)
- Per-symbol allocation cannot exceed `max_symbol_allocation_pct` (new param, default 15%)
- Drawdown manager still overrides with `size_factor`

**Where to implement:**
- Modify `_calc_qty_per_level` in `grid_bias.py` to accept symbol
- Add performance tracking per symbol in the grid state
- Apply on every recenter

**Expected Impact:**
- Winning symbols compound faster, increasing returns on what works
- Losing symbols get smaller allocation, limiting damage
- With 20 USDT capital, the effect is small in absolute terms but compounds over time
- Estimated improvement: +5-10% over medium term (weeks)

**Complexity:** Low
**Priority:** P2 -- marginal improvement at current capital size, becomes more important as capital grows

---

## 3. Additional Improvements Identified During Analysis

### F. Minimum Spacing Filter Increase

**Current:** 0.45% (only 7% above 0.42% friction)
**Recommended:** 0.60% (43% above friction)

Rationale: At 0.45%, a single adverse tick of slippage beyond the model wipes out the entire cell profit. At 0.60%, there is a 0.18% buffer per cell, making the system robust to real-world execution variance.

**Impact:** +10-15% reduction in unprofitable trades
**Complexity:** Trivial (config change)
**Priority:** P0

### G. Stale Fill Revert Timeout

**Current:** 60 seconds before reverting FILLED to PENDING
**Issue:** In a 5m candle system, fills are detected once per 5 minutes. A 60-second revert timeout means fills get reverted before the next candle can process them.

**Recommended:** Increase to 360 seconds (6 minutes, slightly more than one candle period) or tie it to candle interval.

**Impact:** Prevents spurious fill-revert cycles
**Complexity:** Trivial (config change)
**Priority:** P0

### H. Multi-Timeframe ATR for Range Calculation

**Current:** Uses only 1h ATR(14) for grid range
**Proposed:** Blend 1h ATR and 4h ATR for more stable range estimates

```python
atr_blended = 0.6 * atr_1h + 0.4 * (atr_4h / 4)  # Normalize 4h to 1h scale
```

This reduces grid recentering frequency caused by short-term ATR spikes.

**Impact:** +5% from fewer unnecessary recenters
**Complexity:** Medium (needs 4h candle data pipeline)
**Priority:** P2

---

## 4. Priority Matrix

| ID | Improvement | Impact | Complexity | Priority | Dependencies |
|---|---|---|---|---|---|
| F | Raise min_spacing to 0.60% | +10-15% | Trivial | P0 | None |
| G | Fix stale fill timeout | Bug fix | Trivial | P0 | None |
| A | Dynamic spacing (ATR ratio) | +15-25% | Low | P0 | None |
| B | Smart symbol selection (GPS) | +20-30% | Medium | P0 | Scanner integration |
| D | Improved bias (strong + volume) | +15-25% | Medium | P1 | df_1h volume data |
| C | Adaptive level count | +10-15% | Low | P1 | Pairs with A |
| E | Profit compounding | +5-10% | Low | P2 | None |
| H | Multi-timeframe ATR | +5% | Medium | P2 | 4h candle pipeline |

---

## 5. Implementation Roadmap

### Phase 1 (Immediate -- config changes only)
1. Raise `min_spacing_pct` from 0.45 to 0.60
2. Fix stale fill revert timeout (60s -> 360s)
3. Both are zero-code changes in `grid.yaml`

### Phase 2 (Week 1 -- core engine improvements)
1. Dynamic spacing: add `atr_ratio` to `_calc_range()`
2. Smart symbol selection: add GPS calculation and selection filter
3. Adaptive level count: parameterize `num_levels` per symbol

### Phase 3 (Week 2 -- bias and compounding)
1. Strong bias modes (STRONG_BULLISH/BEARISH)
2. Volume-weighted bias component
3. Equity-based qty recalculation on recenter
4. Winner-symbol allocation boost

### Phase 4 (Week 3+ -- advanced)
1. Multi-timeframe ATR blending
2. Performance-based symbol rotation with cooldown periods
3. Historical backtesting framework to validate changes

---

## 6. Risk Considerations

- **Dynamic spacing in extreme volatility:** Very high ATR ratios could push spacing so wide that no fills occur for hours. The `max_range_pct` cap (8%) mitigates this.
- **Strong bias with 0 counter-trend levels:** If the trend reverses sharply, all levels are on the wrong side. Mitigated by the recenter mechanism (1.5% threshold) and the hard stop loss (5% account).
- **Profit compounding amplifies losses too:** If a winning symbol turns, the boosted allocation means bigger losses. The 50% boost cap and drawdown manager provide guardrails.
- **Symbol rotation churn:** Rotating symbols too aggressively wastes the fills already accumulated. A minimum hold period (e.g., 2 hours) before rotation prevents churn.

---

## 7. Monitoring Metrics (Post-Implementation)

Track these to validate improvements:

1. **Cell profit ratio**: `avg_spacing_pct / friction_cost` -- target > 2.0x
2. **Fill rate**: fills per hour per symbol -- higher is better up to a point
3. **Net PnL per cell**: after all fees and slippage
4. **Recenter frequency**: too many recenters waste fills
5. **Bias accuracy**: % of bias predictions that align with subsequent price moves
6. **Symbol GPS correlation**: does higher GPS predict higher actual profit?

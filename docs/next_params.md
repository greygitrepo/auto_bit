# Next Iteration Parameter Recommendations

**Date:** 2026-03-27
**Applies to:** `config/strategy/grid.yaml`
**Based on:** strategy_design_v1.md analysis

---

## Summary of Changes

These are concrete parameter changes recommended for the next deployment iteration,
ordered from safest (config-only) to most impactful (requires code changes).

---

## Tier 1: Config-Only Changes (Deploy Immediately)

These require zero code changes -- just update `grid.yaml`.

### 1.1 Raise min_spacing_pct: 0.45 -> 0.60

**Rationale:** Current 0.45% gives only 7% margin above the 0.42% friction floor.
VVVUSDT is running at 0.58% spacing with a 1.38x profitability ratio -- one bad fill
wipes the profit. At 0.60%, the minimum profitability ratio becomes 1.43x, and more
importantly, symbols like VVVUSDT with sub-0.60% spacing get filtered out entirely,
preventing loss-making grid deployments.

```yaml
# BEFORE
min_spacing_pct: 0.45
# AFTER
min_spacing_pct: 0.60
```

### 1.2 Fix Stale Fill Revert Timeout

This is actually hardcoded at 60 seconds in `grid_bias.py` line 157. It should be
a config parameter and increased to 360 seconds.

**Rationale:** The system evaluates on 5-minute candles. A 60-second revert timeout
means a FILLED level gets reverted to PENDING before P3 can even process the next
candle. This causes fills to be lost and re-triggered, creating duplicates or missed TPs.

```yaml
# ADD to grid_bias section:
stale_fill_revert_seconds: 360   # 6 minutes (> 1 candle period)
```

### 1.3 Increase recenter_threshold_pct: 1.5 -> 2.0

**Rationale:** With 10 levels and ~0.6-0.8% spacing, the total grid range is 6-8%.
A 1.5% threshold means recentering triggers when price moves only 1/4 of the grid
range, which is too aggressive. Many fills in progress get cancelled. At 2.0%, the
grid has more room to work before recentering disrupts it.

```yaml
# BEFORE
recenter_threshold_pct: 1.5
# AFTER
recenter_threshold_pct: 2.0
```

### 1.4 Reduce max_open_levels: 6 -> 4

**Rationale:** With 20 USDT capital and 5% per level (1 USDT margin per level),
6 open levels = 6 USDT margin = 30% of capital at risk simultaneously. In a trending
market, all 6 levels on one side get filled and move against you. At 4 levels,
maximum single-direction exposure is 20%, leaving more buffer.

```yaml
# BEFORE
max_open_levels: 6
# AFTER
max_open_levels: 4
```

---

## Tier 2: Parameters for Code Changes (Phase 2)

These parameters support the code improvements described in strategy_design_v1.md.
Add them to `grid.yaml` now; the code will use defaults until the implementation
is complete.

### 2.1 Dynamic Spacing Parameters

```yaml
# Dynamic spacing (Improvement A)
dynamic_spacing:
  enabled: true
  atr_lookback_hours: 24          # Hours of ATR history for baseline
  low_vol_multiplier: 0.8         # Spacing shrinks 20% in low vol
  high_vol_multiplier: 1.4        # Spacing widens 40% in high vol
  vol_ratio_low_threshold: 0.6    # ATR ratio below this = low vol
  vol_ratio_high_threshold: 1.5   # ATR ratio above this = high vol
```

### 2.2 Smart Symbol Selection Parameters

```yaml
# Symbol selection (Improvement B)
symbol_selection:
  min_profitability_ratio: 2.0    # spacing_pct / friction_cost must exceed this
  min_net_per_cell_pct: 0.20      # Minimum net profit per cell in %
  rotation_interval_minutes: 120  # Check for symbol rotation every 2 hours
  min_hold_minutes: 120           # Minimum time before rotating a symbol out
  friction_cost_pct: 0.42         # Total friction (slippage + fees) per round trip
```

### 2.3 Adaptive Level Count Parameters

```yaml
# Adaptive levels (Improvement C)
adaptive_levels:
  enabled: true
  target_spacing_pct: 0.60        # Desired spacing between levels
  min_levels: 4
  max_levels: 16
```

### 2.4 Improved Bias Parameters

```yaml
# Enhanced bias (Improvement D)
bias:
  enabled: true
  max_level_shift: 3
  strong_bias_threshold: 0.7      # NEW: above this, use aggressive allocation
  allow_zero_counter_levels: true  # NEW: allow 0 levels on counter-trend side
  ema_periods: [20, 50]
  ema_weight: 0.40                # was 0.50
  btc_eth_weight: 0.15            # was 0.20
  volume_weight: 0.25             # NEW
  volume_breakout_threshold: 2.0  # NEW: volume ratio to trigger breakout bias
  funding_rate:
    enabled: true
    extreme_threshold: 0.01
    weight: 0.20                  # was 0.30
```

### 2.5 Profit Compounding Parameters

```yaml
# Compounding (Improvement E)
compounding:
  enabled: true
  winner_boost_max_pct: 50        # Max allocation boost for winners
  loser_reduction_max_pct: 30     # Max allocation reduction for losers
  max_symbol_allocation_pct: 15   # Cap per-symbol allocation
```

---

## Tier 3: Complete Recommended grid.yaml

Below is the full recommended config for the next iteration. Tier 1 changes are
active immediately. Tier 2 parameters are present for when code is deployed.

```yaml
active: grid_bias

strategies:
  grid_bias:
    # Grid structure
    num_levels: 10                    # Default; overridden by adaptive_levels when enabled
    default_buy_levels: 5
    default_sell_levels: 5

    # Grid range sizing
    range_atr_multiplier: 2.5
    min_range_pct: 1.0
    max_range_pct: 8.0

    # Center price
    center_method: last_close

    # Recalculation
    recenter_interval_minutes: 60
    recenter_threshold_pct: 2.0       # CHANGED: was 1.5

    # Stale fill handling
    stale_fill_revert_seconds: 360    # NEW: was hardcoded 60

    # Directional bias
    bias:
      enabled: true
      max_level_shift: 3
      strong_bias_threshold: 0.7
      allow_zero_counter_levels: true
      ema_periods: [20, 50]
      ema_weight: 0.40
      btc_eth_weight: 0.15
      volume_weight: 0.25
      volume_breakout_threshold: 2.0
      funding_rate:
        enabled: true
        extreme_threshold: 0.01
        weight: 0.20

    # Position sizing per level
    leverage: 5
    qty_per_level_pct: 5.0
    max_open_levels: 4                # CHANGED: was 6
    max_total_exposure_pct: 60.0
    max_symbols: 5

    # Profitability filter
    min_spacing_pct: 0.60             # CHANGED: was 0.45

    # Safety
    max_drawdown_pct: 20.0

    # Dynamic spacing
    dynamic_spacing:
      enabled: true
      atr_lookback_hours: 24
      low_vol_multiplier: 0.8
      high_vol_multiplier: 1.4
      vol_ratio_low_threshold: 0.6
      vol_ratio_high_threshold: 1.5

    # Symbol selection
    symbol_selection:
      min_profitability_ratio: 2.0
      min_net_per_cell_pct: 0.20
      rotation_interval_minutes: 120
      min_hold_minutes: 120
      friction_cost_pct: 0.42

    # Adaptive levels
    adaptive_levels:
      enabled: true
      target_spacing_pct: 0.60
      min_levels: 4
      max_levels: 16

    # Compounding
    compounding:
      enabled: true
      winner_boost_max_pct: 50
      loser_reduction_max_pct: 30
      max_symbol_allocation_pct: 15

exit:
  grid_timeout_hours: 24
  hard_stop_loss_pct: 5.0
```

---

## Expected Cumulative Impact

| Change | Individual Impact | Cumulative |
|---|---|---|
| Raise min_spacing to 0.60% | +10-15% | +10-15% |
| Fix stale fill timeout | Bug fix (prevents lost fills) | +10-15% |
| Recenter threshold 1.5->2.0 | +5% (fewer wasted recenters) | +15-20% |
| Max open levels 6->4 | -5% throughput, +10% risk reduction | +20-25% |
| Dynamic spacing (code) | +15-25% | +35-45% |
| Smart symbol selection (code) | +20-30% | +50-65% |
| Adaptive level count (code) | +10-15% | +55-70% |
| Improved bias (code) | +15-25% | +60-80% |
| Profit compounding (code) | +5-10% | +65-85% |

**Conservative estimate: +40-50% improvement in net profitability after all changes.**

Note: These estimates assume the improvements are independent, which they are not.
Real-world compounding of improvements will likely be lower. A conservative target
of +40% net profitability improvement over the current baseline is reasonable.

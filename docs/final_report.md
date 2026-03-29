# Final Report — Grid + Bias Hybrid Strategy
**Date:** 2026-03-29 16:00 KST
**Duration:** ~18 hours of live paper trading (since last DB reset at 21:23 Mar 28)
**Optimization Cycles:** 78 cycles total

## Performance Summary

| Metric | Value |
|--------|-------|
| Initial Capital | 20.00 USDT |
| Final Equity | 21.06 USDT |
| Realized PnL | +1.116 USDT |
| ROI | **+5.32%** |
| Total Trades | 187 |
| Win Rate | **97.3%** (182/187) |
| Profit Factor | ~34x |
| Total Fees | 0.454 USDT |
| Open Positions | 38 |
| Large Losses (>0.01) | 5 (2.7%) |
| Recenters in trades | 0 |
| Daily Rate | ~1.5 USDT/day = ~7.5%/day |

## Strategy Configuration (Final)

```yaml
range_atr_multiplier: 1.2
min_spacing_pct: 0.55%
target_spacing_pct: 0.55%
recenter_threshold_pct: 3.0
recenter_interval_minutes: 180
max_symbols: 8
max_open_levels: 8
leverage: 5x
qty_per_level_pct: 2.0%
slippage_bps: 15
```

## Top Performers
1. PIPPINUSDT: 29 trades, +0.366 USDT
2. B3USDT: 24 trades, +0.318 USDT
3. CUSDT: 48 trades, +0.187 USDT
4. CFGUSDT: 27 trades, +0.137 USDT

## Optimization Journey (78 cycles)

| Cycle | Change | Effect |
|-------|--------|--------|
| 1-2 | range 2.5→1.5, spacing 0.60→0.50 | Util 2%→5%, but fee margin too thin |
| 3 | range 1.0, spacing 0.55 | Too tight → recenter losses |
| 4 | range 1.2, recenter_th 3.0 | WR 100%, recenters reduced |
| 7 | recenter_interval 60→180 | Recenter losses eliminated, PnL 3.7x |
| 12 | Recenter keeps open positions | Critical: prevents forced close loss |
| 14 | Recenter fallback: skip if no new grid | Prevents forced close on spacing filter |
| 18 | max_symbols 5→8 | Trade frequency +88% |
| 24 | Recenter index conflict fix | BEATUSDT -0.063 bug fixed |
| 25-78 | Observation | Stable profit, 97% WR |

## Key Bugs Fixed
1. `level_id=0` mapping → `(symbol, level_index)` composite key
2. Same-candle Fill+TP prevention
3. Margin accounting: `close_position_by_key` + entry fee tracking
4. Recenter: keep open positions instead of forced close
5. Recenter: skip when new grid creation fails
6. Recenter: prevent level_index collision between kept and new levels
7. Balance DB sync in grid mode
8. DB connection churn in watchdog loop

## Paper-Live Gaps (from Parity Analysis)
- CRITICAL: Bybit net position model vs independent micro-positions
- HIGH: LiveExecutor.place_market_order parameter mismatch
- HIGH: Real slippage 25-100bps vs paper 15bps
- MEDIUM: Funding rates not charged in paper
- MEDIUM: Rate limiting (mitigated with 150ms order delay)

## Files Modified/Created
- 6 new files (grid_engine, grid_bias, bias_calculator, grid_sizing, grid_manager, grid.yaml)
- 8 modified files (messages, db, base, process×2, paper_executor, symbols, asset.yaml, main, config)
- 3 test files (22→39 tests, all passing)
- 6 documentation files

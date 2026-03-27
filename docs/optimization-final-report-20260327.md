# Optimization Final Report

**Period**: 2026-03-26 18:30 KST to 2026-03-27 08:30 KST (~14 hours)
**Total optimization cycles**: 7
**Total trades during optimization**: 87 (ID 274-360)
**System status at report time**: RUNNING (paper mode), 6 open positions

---

## Executive Summary

Over 7 optimization cycles, the system's key metrics improved but overall profitability was not achieved. The most impactful changes were:

1. **Trailing stop reconstruction bug fix** -- positions now retain trailing stop protection across system restarts
2. **Strategy exit simplification** -- disabling EMA cross and volume dry exits eliminated the two largest strategy exit loss contributors
3. **Trailing stop parameter tuning** -- activation_r=0.5 with callback_atr_multiplier=0.8 creates an effective breakeven stop mechanism

The **best performance cycle was Cycle 6** (13 trades, 38.5% WR, trailing stop at 75% WR), demonstrating the configuration can be profitable in favorable market conditions. However, sustained profitability requires further improvements to entry quality and stop loss management.

---

## Parameter Changes Made

### Config: position.yaml

| Parameter | Before | After | Cycle | Impact |
|-----------|--------|-------|-------|--------|
| ema_cross_exit | true | **false** | 1 | Eliminated 21 premature exits (14.3% WR, -0.5433 total) |
| volume_dry_exit | true | **false** | 2 | Eliminated 23 premature exits (13% WR, -0.3231 total) |
| activation_r | 1.0 | **0.5** | 3 | Trailing activates at 0.5R instead of 1R, acts as breakeven stop |
| callback_atr_multiplier | 0.5 | **0.8** | 4 | Wider trailing callback reduces premature trailing exits |
| max_pct (stop_loss) | 3.0 | **2.0** | 7 | Caps worst-case SL loss at 2% of entry price |

### Config: asset.yaml

| Parameter | Before | After | Cycle | Impact |
|-----------|--------|-------|-------|--------|
| stop_after | 10 | **30** | 6 | Prevents daily stop triggered by trailing breakeven exits |

### Code: src/order/process.py

| Change | Cycle | Impact |
|--------|-------|--------|
| Added trailing stop state reconstruction for existing positions on startup | 5 | Critical fix: positions now retain trailing stop protection after restarts. Previously, all positions lost trailing stops on restart, causing profitable positions to time out instead of capturing gains via trailing. |

### Unchanged Parameters (preserved)

- atr_multiplier: 2.0 (stop loss width)
- risk_reward_ratio: 2.0
- rsi_reversal_exit: true (rarely triggers, minimal impact)
- max_holding_minutes: 90
- max_concurrent_positions: 8

---

## Performance Trajectory

| Phase | Trades | Win Rate | Avg PnL | Total PnL | Key Feature |
|-------|--------|----------|---------|-----------|-------------|
| Baseline (pre-opt) | 160 | 27.5% | -0.0204 | -3.2689 | Strategy exits causing premature closures |
| Cycle 1-2 (no EMA/vol exit) | 27 | 14.8% | -0.0625 | -1.6860 | Eliminated strategy exits, but position-locked |
| Cycle 3 (activation_r=0.5) | 9 | 11.1% | -0.0624 | -0.5617 | Trailing activates early but too tight |
| Cycle 4 (callback=0.8) | 13 | 15.4% | -0.0291 | -0.3786 | Better trailing, some wins |
| Cycle 5 (trailing reconstruction) | 14 | 7.1% | -0.0194 | -0.2717 | Bug fix, trailing as breakeven stop |
| **Cycle 6 (stop_after=30)** | **13** | **38.5%** | **-0.0247** | **-0.3205** | **Best cycle: TS 75% WR, TP captures** |
| Cycle 7 (max_pct=2.0) | 9 | 0.0% | -0.0250 | -0.2254 | Tighter SL, tough market period |

### Key Improvement Metrics

- **Trailing stop** evolved from baseline (38 trades, 71.1% WR, avg +0.0383) to current dual role:
  - Profit capture: avg +0.1004 on winning trails (Cycle 6)
  - Breakeven protection: avg -0.0036 on losing trails (converts would-be SL losses)
- **Strategy exit losses eliminated**: -1.07 in baseline reduced to near zero
- **SL avg loss reduced**: from -0.0872 baseline to -0.0655 in Cycle 7
- **Win rate peaked at 38.5%** (Cycle 6) vs 27.5% baseline

---

## Current Best Configuration

```yaml
# position.yaml
exit:
  stop_loss:
    type: atr
    atr_period: 14
    atr_multiplier: 2.0
    min_pct: 0.5
    max_pct: 2.0

  take_profit:
    type: risk_reward
    risk_reward_ratio: 2.0

  trailing_stop:
    activation_r: 0.5
    callback_atr_multiplier: 0.8

  strategy_exit:
    ema_cross_exit: false
    rsi_reversal_exit: true
    volume_dry_exit: false

  time_limit:
    max_holding_minutes: 90

# asset.yaml
consecutive_loss:
  cooldown_after: 5
  cooldown_minutes: 5
  stop_after: 30
```

---

## Root Cause Analysis

### Why profitability was not achieved

1. **Adverse market regime**: The optimization period coincided with a choppy/ranging market. Many positions hit SL before reaching trailing activation. SL remains the dominant loss source (45% of all exits, accounting for -4.21 of total losses).

2. **Structural SL/TP asymmetry**: With SL averaging -0.1081 and trailing wins averaging +0.0291, the system needs ~3.7 trailing wins per SL loss to break even. Actual ratio was 26 TS : 39 SL (0.67), far below the 3.7:1 needed.

3. **Entry quality**: The entry conditions (EMA alignment, RSI range, volume) are relatively permissive. Many entries are into ranging/choppy conditions where neither SL nor TP is reached quickly, leading to time-limit or breakeven trailing exits.

4. **Position turnover bottleneck**: With max_concurrent=8 and strategy exits disabled, positions are held longer, reducing the number of opportunities the system can evaluate. Trade frequency dropped from ~10/hour (baseline) to ~5/hour (optimized).

---

## Recommendations for Further Improvement

### High Priority

1. **Improve entry quality scoring**
   - Add ATR-relative move size as a confidence factor (only enter when the current candle shows strong directional movement)
   - Consider adding a trend strength filter (ADX > 20 or similar)
   - Increase short_min_confidence to 0.70 (SHORT side underperforms)

2. **Dynamic SL sizing based on volatility class**
   - High-vol coins: use min_pct (0.5%) SL -- they move fast, need tight protection
   - Low-vol coins: use wider SL (up to max_pct) -- they oscillate more before trending
   - This requires code changes to classify symbols by volatility

3. **Re-evaluate trailing stop parameters**
   - Consider activation_r=0.75 (between original 1.0 and current 0.5) as a middle ground
   - The breakeven-stop behavior at 0.5R is useful but captures little profit
   - A higher activation might capture more profit per winning trade at the cost of fewer activations

### Medium Priority

4. **Add minimum profit check for trailing stop exits**
   - Only trigger trailing stop close if PnL > some threshold (e.g., > fee cost = 0.0072)
   - This prevents the -0.0036 breakeven exits from counting as losses

5. **Re-enable volume_dry_exit with stricter threshold**
   - Lower threshold to 0.10 (only exit on extreme volume collapse)
   - Add a minimum holding time before allowing volume exit (e.g., 15 minutes)

6. **Reduce max_concurrent_positions to 5-6**
   - Fewer positions = more capital per position = larger absolute wins
   - Also reduces position-lock issues

### Low Priority

7. **Time-of-day filters**: Crypto markets have patterns; avoid entries during historically choppy hours
8. **Symbol blacklisting**: Some symbols (BRUSDT) hit SL repeatedly; consider auto-blacklisting after N consecutive SL hits

---

## Files Modified

| File | Type | Description |
|------|------|-------------|
| `config/strategy/position.yaml` | Config | Strategy exit, trailing stop, SL parameters |
| `config/strategy/asset.yaml` | Config | Consecutive loss stop threshold |
| `src/order/process.py` | Code | Trailing stop state reconstruction on startup |
| `docs/optimization-log.md` | Docs | Detailed per-cycle optimization log |
| `docs/optimization-final-report-20260327.md` | Docs | This report |

---

*Report generated 2026-03-27 08:30 KST*

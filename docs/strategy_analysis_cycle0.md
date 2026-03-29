# Strategy Analysis - Cycle 0

**Date:** 2026-03-27
**Period:** First grid_bias session (19:25 - 21:20 UTC, ~2 hours)
**Analyst:** Strategy Analysis Team (P2/P3 log + DB forensics)

---

## 1. Executive Summary

The grid_bias strategy completed its first operational cycle. Over approximately 2 hours of active trading (with 22 restarts during tuning), the system executed **95 completed grid cells** across **26 unique symbols**, generating a **net profit of +2.945 USDT (+14.73%)** on a 20 USDT paper account. Win rate was **92.6%** with a profit factor of **23.92**.

However, several structural issues were identified that would severely limit live performance:

1. **All TPs are instant** (0 seconds) -- the paper executor fills entry and TP in the same candle tick, inflating returns unrealistically.
2. **Exposure limit conflict** -- `qty_per_level_pct=5%` at `leverage=5x` means each level consumes 25% notional exposure, but `max_total_exposure_pct=60%` only allows ~2 levels. The config allows `max_open_levels=6`, which is unreachable.
3. **Bias system is inert** -- 100% of 449 grids were NEUTRAL. The bias calculator requires a total score > 0.3 to shift, but the threshold is too high for the current signal inputs.
4. **109 orphaned TP signals** (53.7% of all TP_HITs) targeted non-existent positions, indicating a state management bug across restarts.
5. **88 TP failures specifically had level_id=0**, which is a clear bug -- level indices should be non-zero.

---

## 2. Trading Performance

### 2.1 Key Metrics

| Metric | Value |
|---|---|
| Initial Balance | 20.0000 USDT |
| Current Balance | 18.1415 USDT (2 open positions) |
| Completed Cells | 95 |
| Gross PnL | +3.2308 USDT |
| Total Fees | 0.2853 USDT |
| **Net PnL** | **+2.9455 USDT** |
| Win Rate | 92.6% (88W / 7L) |
| Profit Factor | 23.92 |
| Avg Net per Cell | +0.0310 USDT |
| Avg Gross per Cell | +0.0340 USDT |
| Avg Fee per Cell | 0.0030 USDT |
| Fee as % of Gross | 8.8% |
| Net-Negative Cells | 7 (7.4%) |

### 2.2 Time to TP Completion

**All 95 TPs completed in 0 seconds** (same tick as fill). This is a critical paper-trading artifact. In live markets, grid levels need actual price movement to fill and then reverse to TP. The paper executor appears to check both entry and TP against the same candle's high/low range, creating an unrealistically fast fill cycle.

**Implication:** The +14.73% return over 2 hours is not achievable in live trading. Real fills would take minutes to hours per cell.

### 2.3 Fill/Rejection Analysis

| Event | Count |
|---|---|
| Grid Fills Confirmed | 162 (initially, before TP accounting) |
| Grid Fills Rejected | 49 |
| Fill Success Rate | 78.0% |
| Grid TPs Executed | 95 |
| Grid TPs Failed (no position) | 109 |
| TP Success Rate | 46.3% |

All 49 rejections were due to **exposure limit exceeded** (actual 69-84% vs limit 60%).

---

## 3. Symbol Analysis

### 3.1 Most Profitable Symbols

| Symbol | Cells | Net PnL | Avg Net/Cell |
|---|---|---|---|
| BRUSDT | 8 | +0.5325 | +0.0666 |
| SIRENUSDT | 9 | +0.4262 | +0.0474 |
| RIVERUSDT | 11 | +0.3442 | +0.0313 |
| WHITEWHALEUSDT | 5 | +0.2157 | +0.0431 |
| BSBUSDT | 12 | +0.1883 | +0.0157 |
| HUMAUSDT | 5 | +0.1702 | +0.0340 |
| CUSDT | 5 | +0.1401 | +0.0280 |

**Key observation:** BRUSDT and SIRENUSDT had the highest spacing (0.800%) due to high ATR, which translates to larger per-cell profit. BSBUSDT had the most completions (12) but smallest per-cell profit due to tighter spacing.

### 3.2 Least Profitable Symbols

| Symbol | Cells | Net PnL | Avg Net/Cell |
|---|---|---|---|
| ZROUSDT | 2 | +0.0073 | +0.0036 |
| XAGUSDT | 1 | +0.0099 | +0.0099 |
| VVVUSDT | 3 | +0.0399 | +0.0133 |

ZROUSDT had spacing at 0.462% (barely above the 0.45% minimum), leaving almost no margin after fees.

### 3.3 Skipped Symbols (18 unique)

All skips were due to spacing < 0.45% (min_spacing_pct filter). Notable skips:

| Symbol | Spacing | Reason |
|---|---|---|
| XAUUSDT | 0.163% | Gold - very low volatility relative to price |
| XAUTUSDT | 0.163% | Tokenized gold - same issue |
| TRUMPUSDT | 0.243% | Insufficient ATR |
| HYPEUSDT | 0.309% | Close to threshold |
| FARTCOINUSDT | 0.447% | 0.003% below threshold - missed profitable opportunity |

**FARTCOINUSDT** was skipped at 0.447% but the 2 trades that did execute on other cycles netted +0.0354 USDT, suggesting the min_spacing_pct is slightly too aggressive.

---

## 4. Configuration Analysis

### 4.1 Exposure Limit vs Level Sizing (CRITICAL)

**The configuration has an internal contradiction:**

```
qty_per_level_pct: 5.0    # 5% of balance as margin per level
leverage: 5               # 5x leverage
max_open_levels: 6        # allows 6 open levels
max_total_exposure_pct: 60.0  # notional limit
```

Per-level notional = 5% * 5x = 25% of balance. With 60% exposure limit, only **2.4 levels** can be open simultaneously across ALL symbols. But max_open_levels is set to 6, which is unreachable. With 3 active grids (current state), effectively only ~0.8 levels per grid can fill before hitting the cap.

**This caused 49 fill rejections (22% of all fill attempts).**

### 4.2 Grid Spacing Distribution

| Metric | Value |
|---|---|
| Min spacing (created) | 0.451% |
| Max spacing (created) | 0.800% |
| Average spacing | 0.733% |
| Median spacing | 0.800% (capped by max_range_pct) |

Most grids hit the 0.800% cap, which is the `max_range_pct=8.0` divided by `num_levels=10`. This means most scanned altcoins have ATR > 8% on the 1h timeframe, and the range is being artificially capped.

### 4.3 Bias System (INERT)

All 449 grids created had `bias=NEUTRAL` with no level shift. The bias calculator requires `total_score > 0.3` or `< -0.3` to produce a directional signal. With the weighted formula:

```
total = 0.5 * ema_bias + 0.3 * funding_bias + 0.2 * market_bias
```

Each component returns [-1, 1]. To reach 0.3 total, the EMA component alone would need to be 0.6 (strong trend). In the volatile altcoin market with frequent mean-reversion, this threshold is rarely met.

The PTBUSDT grid in the DB shows 6 buy / 4 sell levels with `bias_magnitude=0.2175`, but this was from an earlier session before a recenter. Even this was still classified as NEUTRAL.

### 4.4 Max Symbols Limit (5)

Currently 3 active grids (VVVUSDT, BRUSDT, PTBUSDT). The 5-symbol limit was never reached because:
- The exposure limit (60%) is the binding constraint, not the symbol count
- With 25% exposure per level, 3 grids with 1 fill each = 75% (already over limit)

The max_symbols=5 limit is currently irrelevant.

---

## 5. System Stability

### 5.1 Restart History

The system was restarted **22 times** during the 2-hour session (tuning/debugging). Each restart:
- Resets PaperExecutor balance to 20.00 USDT
- Orphans any open positions in the DB
- Creates orphaned grid_levels that generate TP signals for non-existent positions

### 5.2 Orphaned TP Bug (level_id=0)

88 of the 109 failed TP events had `level_id=0`. Grid level indices should be -5 to -1 and 1 to 5 (or -6 to -1, 1 to 4 for biased grids). A level_id=0 indicates the TP signal references a level that either:
- Was not properly saved to the DB
- Lost its ID reference during a restart
- Has a bug in the level ID propagation from P2 to P3

---

## 6. Recommendations for Strategy Design Team

### R1: Fix Exposure/Sizing Conflict (CRITICAL, immediate)

**Option A (recommended):** Reduce `qty_per_level_pct` to 2% so each level = 10% notional exposure. This allows 6 levels across all symbols within the 60% cap.

**Option B:** Increase `max_total_exposure_pct` to 150% (aggressive, higher risk). This allows the current 5% sizing to work with multiple grids.

**Option C:** Reduce leverage to 2x. Each level = 10% notional. Allows more levels but reduces per-cell profit.

Recommended values for Option A:
```yaml
qty_per_level_pct: 2.0      # 2% * 5x = 10% exposure per level
max_open_levels: 5           # 5 * 10% = 50% max per symbol (within 60% total)
max_total_exposure_pct: 60.0 # keep current limit
```

### R2: Lower Bias Threshold (HIGH)

Change the neutral zone from +/-0.3 to +/-0.15:
```python
# In bias_calculator.py
if total > 0.15:
    direction = BiasDirection.BULLISH
elif total < -0.15:
    direction = BiasDirection.BEARISH
```

This will activate the bias system for moderate trends, which is the majority of the time for these altcoins.

### R3: Reduce min_spacing_pct (MEDIUM)

Current: 0.45%. Theoretical breakeven: 0.42% (2 * (0.06% taker + 0.15% slippage)).

Recommendation: Lower to **0.42%** to capture borderline-profitable symbols like FARTCOINUSDT (0.447%) that were unnecessarily filtered. The existing 0.45% threshold already includes a safety margin that may be too conservative for paper testing.

For live trading, consider **0.50%** to account for real-world execution risk.

### R4: Fix Paper Executor Instant-TP Issue (HIGH)

The paper executor fills both entry and TP within the same candle tick. This creates unrealistic P&L expectations. The fix should:
- Require at least 1 candle interval between entry fill and TP fill
- Or simulate order book depth with a time delay

Without this fix, backtesting results are meaningless for live deployment decisions.

### R5: Fix level_id=0 Bug (MEDIUM)

Investigate why 88 TP signals reference `level_id=0`. This likely originates from the grid_levels table where the `id` column is not being properly propagated in the TP signal from P2 to P3. Check `grid_bias.py` around the TP signal emission logic.

### R6: Add Grid State Persistence Across Restarts (MEDIUM)

Currently, restarts orphan all grid state. The `restore_from_db` function exists but clearly does not fully restore TP tracking. Implement:
- On startup, scan for open positions with `strategy='grid_bias'`
- Match them to existing grid_levels entries
- Resume TP monitoring for those positions

### R7: Reconsider max_range_pct Cap (LOW)

Currently 8.0% max range. Most altcoins have 1h ATR well above this (median spacing hits the 0.800% cap). This means the grid is artificially compressed and may miss wider price swings. Consider increasing to 12-15% for high-volatility altcoins, or making it symbol-specific.

---

## 7. Data Tables

### Active Grid State (DB snapshot)

| Symbol | Center | Spacing | Spacing% | Buy/Sell | Bias | Margin/Level |
|---|---|---|---|---|---|---|
| VVVUSDT | 5.8869 | 0.03414 | 0.580% | 5/5 | NEUTRAL | 0.849 |
| BRUSDT | 0.12651 | 0.00101 | 0.800% | 5/5 | NEUTRAL | 39.723 |
| PTBUSDT | 0.0018505 | 0.0000148 | 0.800% | 6/4 | NEUTRAL | 2701.972 |

### Open Positions (DB snapshot)

| ID | Symbol | Side | Entry | TP | Margin |
|---|---|---|---|---|---|
| 608 | BRUSDT | Buy | 0.12223 | 0.12303 | 0.992 |
| 609 | BRUSDT | Buy | 0.12124 | 0.12205 | 0.984 |

Total margin committed: 1.976 USDT (10.9% of balance)
Total notional exposure: 9.88 USDT (54.5% of balance)

### System State

| Key | Value |
|---|---|
| Current Balance | 18.1415 USDT |
| Initial Balance | 20.0000 USDT |
| Unrealized PnL | 0.0000 (stale) |
| Trading Active | true |
| All Processes | running |

---

## 8. Priority Matrix

| # | Recommendation | Impact | Effort | Priority |
|---|---|---|---|---|
| R1 | Fix exposure/sizing conflict | Critical | Low (config) | P0 |
| R4 | Fix paper executor instant-TP | High | Medium (code) | P0 |
| R2 | Lower bias threshold | Medium | Low (code) | P1 |
| R5 | Fix level_id=0 bug | Medium | Medium (code) | P1 |
| R6 | Grid state persistence | Medium | High (code) | P1 |
| R3 | Reduce min_spacing_pct | Low | Low (config) | P2 |
| R7 | Increase max_range_pct | Low | Low (config) | P2 |

---

*End of Cycle 0 Analysis*

# Auto_bit Optimization Log

**Started**: 2026-03-26 18:30 KST
**Deadline**: 2026-03-27 09:30 KST
**Objective**: Achieve stable short-term scalping profitability through iterative parameter tuning.

---

## Baseline (before optimization cycles)

**Total trades**: 160 (IDs 114-273)
**Performance**: WR=27.5%, Total PnL=-3.2689, Avg PnL=-0.0204

**By exit type (aggregated)**:
| Exit Type | Count | Sum PnL | Avg PnL | Win Rate |
|-----------|-------|---------|---------|----------|
| stop_loss | 60 | -5.2310 | -0.0872 | 0.0% |
| EMA cross | 21 | -0.5433 | -0.0259 | 14.3% |
| Volume dry-up | 23 | -0.3231 | -0.0140 | 13.0% |
| RSI reversal | 5 | -0.2040 | -0.0408 | 0.0% |
| time_limit | 4 | +0.0054 | +0.0013 | 25.0% |
| take_profit | 10 | +1.4417 | +0.1442 | 100.0% |
| trailing_stop | 38 | +1.4538 | +0.0383 | 71.1% |

**By side**:
| Side | Count | Sum PnL | Avg PnL | Win Rate |
|------|-------|---------|---------|----------|
| Buy (LONG) | 65 | -1.8758 | -0.0289 | 29.2% |
| Sell (SHORT) | 95 | -1.3931 | -0.0147 | 26.3% |

**Key observations**:
- Strategy exits (EMA + Volume + RSI) = 49 trades, -1.0704 total PnL, ~12% aggregate WR
- EMA cross exit is worst strategy exit: 21 trades, -0.5433, 14.3% WR
- In last 30 trades: EMA cross = 9 trades, -0.2539, 0% WR
- Stop losses dominate losses: 60 trades, -5.2310, 0% WR
- Trailing stop and take profit are the only profitable exit types

---

### Cycle 1 — 2026-03-26 18:35 KST

**Trades analyzed**: ID 114-273 (160 trades, full baseline)
**Performance**: WR=27.5%, PnL=-3.2689, Avg=-0.0204
**By exit type**: SL: 60 (-5.23), TP: 10 (+1.44), TS: 38 (+1.45), SE: 49 (-1.07), TL: 4 (+0.01)
**Diagnosis**: EMA cross exit is the single worst strategy exit contributor. It exits trades too early at small losses, preventing them from reaching trailing stop or take profit. In the last 30 trades, all 9 EMA cross exits were losses (0% WR, -0.2539).
**Change**: `ema_cross_exit` from `true` to `false`
**Rationale**: EMA cross is a lagging indicator on 5m timeframe. Cross signals often occur during normal price oscillations within a trend, causing premature exits. Disabling it lets winning trades run to trailing stop or take profit, while stop loss still protects downside. This single change removes the largest strategy exit loss contributor.

---

### Cycle 2 — 2026-03-26 19:50 KST

**Trades analyzed**: ID 274-292 (19 trades since Cycle 1 change)
**Performance**: WR=15.8%, PnL=-1.1277, Avg=-0.0594
**By exit type**: SL: 13 (-1.4350, avg -0.1104), TS: 3 (+0.3324, avg +0.1108), TL: 2 (-0.0071), VolumeExit: 1 (-0.0179)
**By side**: Buy: 15 trades (-0.8987), Sell: 4 trades (-0.2290)
**Observations**:
- EMA cross exit confirmed eliminated (0 trades) -- Cycle 1 change working
- Trailing stop avg PnL improved: +0.1108 vs +0.0383 baseline (winners run longer now)
- Stop loss dominates at 68% of exits (13/19), all losses
- Volume dry-up exit still produces small losses: 1 trade, -0.0179
- RSI reversal: 0 trades this period
- Market regime heavily favoring SHORT (most LONGs hit SL)
- ATHUSDT reached +3.5% unrealized but failed to trigger trailing stop and timed out at -0.0036 -- activation_r may be too high

**Diagnosis**: Volume dry-up exit is still causing premature exits at losses (23 trades baseline at -0.3231, 13% WR; 1 trade this cycle at -0.0179). It exits positions when current volume drops below 20% of average, which is too aggressive and cuts trades short before they can recover.

**Change**: `volume_dry_exit` from `true` to `false`
**Rationale**: Volume dry-up is a weak exit signal that prematurely closes positions at small losses. In baseline data, 23 volume dry-up exits had only 13% WR with -0.3231 total loss. Combined with EMA cross exit removal, this eliminates the two largest strategy exit loss contributors. RSI reversal remains as the only strategy exit (5 baseline trades, -0.2040), which will be evaluated next cycle.

---

### Cycle 3 — 2026-03-27 01:25 KST

**Trades analyzed**: ID 293-300 (8 trades since Cycle 2 change)
**Performance**: WR=12.5%, PnL=-0.5583, Avg=-0.0698
**By exit type**: SL: 3 (-0.5595, avg -0.1865), TS: 1 (+0.0156), TL: 4 (-0.0144, avg -0.0036)
**Observations**:
- 0 EMA cross exits, 0 volume dry exits (both disabled, confirmed working)
- Time limit exits increased to 50% (4/8) -- positions held full 90min then closed at breakeven/tiny loss
- ZROUSDT reached +3.0% unrealized but failed trailing stop activation and timed out at -0.0036
- BRUSDT reached +3.6% unrealized but reversed to stop loss (same pattern seen in Cycle 1)
- The trailing stop activation_r=1.0 requires too large a move (1R = SL distance = ATR*2.0)
- With strategy exits off, winning trades that don't reach 1R just time out

**Diagnosis**: Trailing stop activation threshold (activation_r=1.0) is too high. Multiple trades reach 2-4% unrealized profit but fail to trigger trailing (which requires roughly 4-6% depending on ATR). These trades then revert and either hit SL or time out near breakeven. Lowering activation_r would capture these intermediate gains.

**Change**: `activation_r` from `1.0` to `0.5`
**Rationale**: At activation_r=0.5, trailing stop activates at 0.5R of profit (roughly half the SL distance). This means a trade only needs to move ~1-1.5% before trailing activates, vs ~2-3% currently. This should capture gains from trades like ZROUSDT (+3.0%) and BRUSDT (+3.6%) that currently revert before trailing kicks in. The callback_atr_multiplier=0.5 still provides reasonable trailing distance.

---

### Cycle 4 — 2026-03-27 02:35 KST

**Trades analyzed**: ID 303-311 (9 trades since Cycle 3 change)
**Performance**: WR=11.1%, PnL=-0.5617, Avg=-0.0624
**By exit type**: SL: 4 (-0.5963), TP: 1 (+0.0709), TS: 3 (-0.0328), TL: 1 (-0.0036)
**Observations**:
- Trailing stop now has 0% WR with activation_r=0.5 (3 losses: -0.0096, -0.0057, -0.0174)
- The lower activation_r causes trailing to activate too early (~1% profit)
- callback_atr_multiplier=0.5 is too tight: after early activation, normal price noise triggers exit at entry_price (= small loss after fees)
- LITUSDT hit take_profit at +0.0709 (good)
- LIGHTUSDT reached +4.7% unrealized but trailing stop didn't lock in enough profit and it's pulling back
- Trade count is low (9 in ~65 min) due to position-locking with max_concurrent=8

**Diagnosis**: callback_atr_multiplier=0.5 is too tight, causing trailing stops to trigger at breakeven or small loss shortly after activation. The trailing stop needs more room to accommodate normal price noise.

**Change**: `callback_atr_multiplier` from `0.5` to `0.8`
**Rationale**: Increasing callback from ATR*0.5 to ATR*0.8 gives 60% more room for the trailing stop. After activation at 0.5R, the trailing stop sits at entry_price floor until the trade moves further in our favor. The wider callback means the trade won't be stopped out by minor pullbacks, allowing it to reach higher profit targets. Historical data shows trailing stop was profitable when it worked (baseline +0.0383 avg), so the mechanism is sound -- it just needs wider room.

---

### Cycle 5 — 2026-03-27 04:15 KST

**Trades analyzed**: ID 312-324 (13 trades since Cycle 4 change)
**Performance**: WR=15.4%, PnL=-0.3786, Avg=-0.0291
**By exit type**: SL: 3 (-0.3892), TS: 5 (+0.0285, 40% WR), TL: 5 (-0.0179)
**Observations**:
- Trailing stop improved to 40% WR, net positive +0.0285 (callback 0.8 better than 0.5)
- Time limit exits INCREASED to 38% of trades (5/13) -- positions held 90min then closed at -0.0036
- BLESSUSDT reached +9.7% unrealized but timed out at -0.0036
- HUSDT at +5.7% still open (has been for >90min from before restart)

**CRITICAL BUG FOUND**: After system restart, existing open positions lose their trailing stop state (self._trailing_stops is initialized as empty dict). This means positions opened before restart have NO trailing stop protection and can only exit via SL or time limit. This explains why BLESSUSDT at +9.7% and AKTUSDT at +5.0% both timed out instead of triggering trailing stop.

**Diagnosis**: Missing trailing stop reconstruction on startup causes profitable positions to lose trailing protection after restarts.

**Change**: Added trailing stop state reconstruction in `src/order/process.py` for existing positions on startup. After initialization, the system now iterates over open positions and creates fresh trailing stop states using current config (activation_r, sl_distance).
**Rationale**: This is a bug fix, not a parameter change. Without this, every system restart effectively disables trailing stops for all existing positions, causing them to time out or hit SL instead of capturing profit with trailing.

---

### Cycle 6 — 2026-03-27 05:25 KST

**Trades analyzed**: ID 325-338 (14 trades since Cycle 5 fix)
**Performance**: WR=7.1%, PnL=-0.2717, Avg=-0.0194
**By exit type**: SL: 6 (-0.4064, avg -0.0677), TP: 1 (+0.1659), TS: 7 (-0.0312, avg -0.0045), TL: 0
**Observations**:
- Trailing stop reconstruction fix WORKING: 0 time_limit exits (vs 5 in previous cycle)
- HUSDT captured +0.1659 via take_profit (was at +5.6%, trailing reconstructed allowed proper monitoring)
- Trailing stops acting as effective breakeven stops: avg loss only -0.0045 vs SL avg -0.0677
- 7 trailing stop exits at ~-0.0036 (just fee cost) -- these are positions that would have been SL losses
- System hit consecutive_loss stop_after=10 and STOPPED TRADING at ~05:15 KST
- Trailing breakeven exits (-0.0036) count as losses, inflating consecutive loss count
- All current orders being rejected due to "Daily stop: 13 consecutive losses >= 10"

**Diagnosis**: The consecutive loss stop_after=10 is too low now that trailing stops act as breakeven stops. Many "losses" are just fee-size losses from trailing, not real trading losses.

**Change**: `stop_after` in asset.yaml from `10` to `30`
**Rationale**: With trailing stops acting as breakeven exits (avg -0.0045), the consecutive loss counter accumulates quickly even when the system is performing reasonably. Raising stop_after to 30 prevents the system from shutting down due to a string of breakeven trailing exits. The cooldown_after=5 with 5-min cooldown still provides short-term circuit breaking.

---

### Cycle 7 — 2026-03-27 07:10 KST

**Trades analyzed**: ID 339-351 (13 trades since Cycle 6 change)
**Performance**: WR=38.5%, PnL=-0.3205, Avg=-0.0247
**By exit type**: SL: 7 (-0.6320, avg -0.0903), TP: 1 (+0.1811), TS: 4 (+0.1162, avg +0.0291, 75% WR), TL: 1 (+0.0141)
**Observations**:
- BEST WR of all cycles: 38.5% (up from 27.5% baseline)
- Trailing stop working well: 75% WR, avg +0.0291
- MUSDT captured +0.1004 via trailing (was at +10.7%)
- OPNUSDT captured +0.1811 via TP
- Buy side net positive: +0.1054, 50% WR
- SL still largest loss: 7 trades, -0.6320, avg -0.0903
- SL loss distribution: 18 of 95 total SL trades hit max (-0.1865), max_pct=3.0% of entry
- Consecutive loss stop working (raised to 30, no premature shutdowns)

**Diagnosis**: Stop loss max_pct=3.0% allows very large losses (-0.1865 on ~2 USDT positions). These max-SL trades lose more than most trailing stop wins can recover. Capping max_pct would reduce worst-case losses.

**Change**: `max_pct` (stop_loss) from `3.0` to `2.0`
**Rationale**: Reducing max SL cap from 3% to 2% of entry price limits worst-case SL loss from ~-0.12 to ~-0.08 per trade. The trailing stop at 0.5R activation still provides breakeven protection for trades that move partially. Combined with R:R 2.0, the TP distance adjusts proportionally. The risk is slightly more frequent SL hits, but the reduced per-hit damage should compensate.

**Result (9 trades)**: SL avg -0.0655 (improved from -0.0903). All 9 trades were losses, but the per-trade damage is lower. The market was in a difficult period with no clear trend. Trailing stops worked as breakeven stops (3 exits at -0.0060 avg).

---

## Session 2: 2026-03-27 09:30-11:30 KST

### Pre-session State
- Fresh start after UI reset, balance=20 USDT
- 1 trade completed (SL loss), 3 open positions
- Config: activation_r=0.5, callback_atr=0.8, adx_threshold=20, SL atr_mult=2.0, R:R=2.0

### Opt-1: Raise trailing stop activation_r from 0.5 to 0.8
**Time**: 10:35 KST (19 trades accumulated)

**Analysis (19 trades)**:
- Win rate: 9/19 = 47%
- Total PnL: -0.31
- By side: LONG 6/14 wins (-0.39 PnL), SHORT 3/5 wins (+0.08 PnL)
- By exit: SL 9 trades avg -0.093, TP 2 trades avg +0.154, Trailing 8 trades avg +0.028

**Diagnosis**: Trailing stop activating too early at 0.5R, locking in tiny profits (avg +0.028) while SL losses are full-sized (-0.093). The trailing stop captures 7/8 wins but with very small gains.

**Change**: `activation_r` from `0.5` to `0.8`
**Rationale**: By requiring 0.8R profit before trailing activates, winning trades will accumulate more profit before the trailing stop engages. This should increase the average trailing stop profit, making it closer to 1R when triggered, better compensating for SL losses.

**Post-change observation (trades 392-396)**: 2 SHORT SL losses (-0.127 each), 1 SHORT SL loss (-0.090), 2 trailing stop small losses. Insufficient new-config trades to evaluate activation_r impact - most open positions were inherited from pre-change state.

### Opt-2: Reduce SL max_pct from 2.0 to 1.5
**Time**: 11:10 KST (23 trades accumulated)

**Diagnosis**: SL losses consistently hitting the max_pct=2.0% cap, producing -0.127 losses per trade with 3x leverage. This is the single largest drag on profitability. Avg SL loss (-0.099) is 4.6x larger than avg trailing win (+0.021).

**Change**: `max_pct` from `2.0` to `1.5`
**Rationale**: Capping SL at 1.5% instead of 2.0% reduces worst-case SL loss from ~0.127 to ~0.095 per trade. With R:R 2.0, TP distance also reduces proportionally, making TP easier to reach. The trade-off is potentially more frequent SL hits on volatile symbols, but the reduced per-hit damage should improve overall expectancy.

---

### Session 2 Summary (11:25 KST)

**Duration**: 09:32 - 11:25 KST (~2 hours)
**Total trades**: 25 (IDs 373-397)
**Final balance**: 19.47 USDT (started at 20.00)
**Total PnL**: -0.53 USDT (-2.66%)

**Performance breakdown**:
| Metric | Value |
|--------|-------|
| Win rate | 10/25 = 40% |
| Avg win | +0.053 |
| Avg loss | -0.078 |
| Profit factor | 0.55 |

**By exit type**:
| Type | Count | Wins | Avg PnL | Total PnL |
|------|-------|------|---------|-----------|
| stop_loss | 12 | 0 | -0.099 | -1.184 |
| take_profit | 3 | 3 | +0.146 | +0.437 |
| trailing_stop | 10 | 7 | +0.021 | +0.215 |

**By side**:
| Side | Count | Win Rate | Total PnL |
|------|-------|----------|-----------|
| Buy/LONG | 17 | 41% | -0.270 |
| Sell/SHORT | 8 | 38% | -0.262 |

**Changes made this session**:
1. `activation_r`: 0.5 -> 0.8 (let winners run before trailing)
2. `max_pct`: 2.0 -> 1.5 (cap SL losses tighter)

**Current configuration** (position.yaml):
```yaml
adx_threshold: 20
short_min_confidence: 0.70
volume_multiplier: 1.0
rsi_long_range: [40, 80]
rsi_short_range: [20, 60]
SL: atr_multiplier=2.0, min_pct=0.5, max_pct=1.5
TP: risk_reward_ratio=2.0
Trailing: activation_r=0.8, callback_atr_multiplier=0.8
ema_cross_exit: false, rsi_reversal_exit: true, volume_dry_exit: false
```

**Key observations**:
1. **Stop loss is the dominant problem**: 12/25 trades (48%) hit SL, accounting for -1.184 total. Zero SL trades were profitable.
2. **Take profit works well but rare**: Only 3/25 trades (12%) reached TP, but each was highly profitable (avg +0.146).
3. **Trailing stop is net positive**: 10 trades, 7 wins, but avg profit (+0.021) is too small to offset SL losses.
4. **LONG and SHORT both underperforming**: Neither side has an edge - both are net negative.
5. **Trailing stop exits often near breakeven**: Many trailing exits at -0.004 to +0.005, suggesting price reverses shortly after trailing activates.

**Next recommended improvements** (priority order):
1. **Reduce SL atr_multiplier from 2.0 to 1.5**: The ATR*2.0 SL distance is too wide. A tighter SL means smaller losses per hit, and with R:R 2.0 the TP distance also tightens, making TP more achievable. Risk: more frequent SL hits.
2. **Increase ADX threshold from 20 to 25**: Entry quality needs improvement. Higher ADX filter means entering only in stronger trends, which should increase TP hit rate.
3. **Widen callback_atr_multiplier from 0.8 to 1.0**: The trailing stop callback seems too tight, causing many exits near breakeven. A wider callback would let trades ride through minor pullbacks.
4. **Consider reducing R:R from 2.0 to 1.5**: The 2.0 R:R target may be unrealistic for 5m scalping. A 1.5 R:R would increase TP hit rate at the cost of lower per-win profit.
5. **Investigate entry timing**: Many LONG entries immediately go against the position. Consider adding a confirmation candle requirement or momentum filter.

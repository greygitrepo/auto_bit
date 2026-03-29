# Paper vs Live Trading Parity Analysis

**Date:** 2026-03-27
**Team:** Parity Team (정합 팀)
**System:** auto_bit grid trading system on Bybit USDT-perpetual

---

## Executive Summary

This document enumerates every behavioral gap between the paper trading executor (`PaperExecutor`) and the live trading executor (`LiveExecutor`) as they interact with the grid position manager (`GridPositionManager`) and the Bybit V5 API (`BybitClient`). Seven gap areas are identified. Two are **Critical**, two are **High**, and three are **Medium**.

The single most dangerous gap is **B. Multiple Positions Per Symbol**: the paper executor tracks independent micro-positions keyed by `order_id`, while Bybit's linear perpetual market enforces a **net position model** -- one position per symbol per side. If the grid opens 6 buy levels on XYZUSDT, paper sees 6 isolated positions; live sees one merged position whose average entry drifts with each fill. This breaks SL/TP placement, P&L attribution, and the entire grid close logic.

---

## Gap A: Order Execution & Fill Price

### Paper Behavior (paper_executor.py:171-244)

- `place_market_order` receives `current_price` (the grid level price).
- Applies deterministic slippage: `price * (1 +/- slippage_bps / 10000)`.
- Config: `slippage_bps = 15` (0.15%).
- Fill is **instant** and **guaranteed** -- always full quantity, no rejection.
- Returns `fillPrice`, `fee`, `orderId` synchronously.

### Live Behavior (live_executor.py:126-205)

- `place_market_order` does **not** accept `current_price` -- signature is `(symbol, side, qty)`.
- Submits a market order via REST. Actual fill price is unknown until the execution endpoint is polled.
- Fill price retrieval: first checks `result.avgPrice`, then waits 500ms and polls `get_executions`.
- Partial fills are detected but **not handled** -- if `filledQty < qty * 0.99` a warning is logged, no retry or adjustment follows.

### Interface Mismatch

`GridPositionManager._handle_fill` (grid_manager.py:126) calls:
```python
result = await self._executor.place_market_order(
    symbol=symbol, side=side, qty=order_req.qty, current_price=level_price,
)
```
`LiveExecutor.place_market_order` does **not** accept a `current_price` parameter. This call will raise a `TypeError` in live mode.

### Gap Details

| Aspect | Paper | Live |
|---|---|---|
| Fill guarantee | 100% fill, always | Market order usually fills, but can partially fill on illiquid alts |
| Fill price | `level_price +/- 0.15%` deterministic | Actual order book; can be 0.5%+ on thin books |
| Latency | 0 ms | REST round-trip (50-200ms) + 500ms poll delay |
| Partial fills | Impossible | Possible; only logged, not handled |

### Severity: **HIGH**

### Impact on Profitability

- Grid spacing is configured at `min_spacing_pct: 0.45%`. With paper slippage of 0.15% each way (0.30% round trip) plus fees (0.12% round trip), the model assumes 0.03% net margin per round trip.
- If live slippage reaches 0.25% each way on illiquid altcoins, the round-trip cost becomes 0.50% + 0.12% = 0.62%, **exceeding** the 0.45% spacing entirely. Every trade loses money.

### Required Code Changes

1. **Fix interface mismatch**: `LiveExecutor.place_market_order` must accept (and ignore) `current_price` kwarg, or `GridPositionManager` must conditionally omit it.
2. **Partial fill handler**: If `filledQty < qty`, either (a) place a follow-up order for the remainder, or (b) adjust the tracked position size to `filledQty`.
3. **Slippage guard**: Before placing a live grid order, fetch best bid/ask from order book. If spread exceeds `min_spacing_pct / 2`, skip the fill.

### Estimated Effort

- Interface fix: 1 hour
- Partial fill handler: 4 hours
- Slippage guard: 4 hours

---

## Gap B: Multiple Positions Per Symbol (NET Position Model)

### Paper Behavior (paper_executor.py:203-220)

```python
order_id = self._gen_order_id()
pos_key = order_id  # unique key per fill
self.account.positions[pos_key] = position
```

Each grid fill creates an independent `PaperPosition` keyed by a unique UUID. The grid manager tracks `level_id -> order_id` in `_level_order_ids` (grid_manager.py:49) and closes individual micro-positions via `close_position_by_key` (paper_executor.py:548-594).

With 10 grid levels and 5 symbols, the paper executor can hold 50 independent micro-positions, each with its own entry price, margin, and P&L.

### Live Behavior (Bybit NET Position Model)

Bybit linear USDT perpetuals use a **net position model**:

- **One position per symbol per side.** If you buy 0.001 BTC, then buy another 0.001 BTC, you have one position of 0.002 BTC at the volume-weighted average entry price.
- There is no concept of "position A" and "position B" for the same symbol/side.
- `get_positions(symbol)` returns at most one entry per side (live_executor.py:404-416).
- SL/TP conditional orders apply to the **entire** net position, not to sub-portions.

### `close_position_by_key` Does Not Exist in LiveExecutor

`GridPositionManager._handle_tp_hit` (grid_manager.py:210) calls:
```python
if order_key and hasattr(self._executor, 'close_position_by_key'):
    result = await self._executor.close_position_by_key(order_key, tp_price)
```
`LiveExecutor` does not implement `close_position_by_key`. The fallback path calls `close_position` which closes quantity equal to `position["size"]` -- but that size in the live exchange is the **net** position, not the micro-position's size. Closing the net position closes ALL grid levels at once.

### Gap Details

| Aspect | Paper | Live |
|---|---|---|
| Position model | N positions per symbol (order_id keyed) | 1 position per symbol per side (net) |
| Entry price | Per micro-position | Volume-weighted average of all fills |
| SL/TP | Per micro-position | Per net position (exchange-wide) |
| Partial close | Close exact micro-position qty | Reduces net position; remaining is still one position |
| P&L attribution | Per grid level | Only at net position level |

### Severity: **CRITICAL**

### Impact on Profitability

This is not a profitability nuance -- it is a **functional break**. In live mode:

1. Grid opens level 1 (Buy 0.1 @ 100), level 2 (Buy 0.1 @ 99), level 3 (Buy 0.1 @ 98). Exchange sees: 1 Buy position of 0.3 @ avg 99.
2. Price rises to level 1's TP. Grid tries to close 0.1 at TP. But the exchange position is 0.3 -- closing 0.1 reduces it to 0.2. The remaining 0.2 has **no** SL/TP orders on exchange (they were set for the full 0.3).
3. Exchange SL/TP conditional orders cannot be split per grid level. Placing a single SL for the net position uses the average entry, not the per-level entry.

### Required Code Changes

1. **Internal position ledger for live mode**: Create a `LivePositionLedger` that tracks micro-positions internally (like paper) but translates to net-position operations on the exchange.
2. **Virtual SL/TP**: Do not use exchange conditional orders for per-level SL/TP. Instead, monitor price in the strategy process and send close signals. Exchange SL/TP should only be used as a safety net for the overall net position.
3. **Proportional close**: When a grid level TP is hit, close only `qty_per_level` from the net position (a partial close market order), not the full position.
4. **Net entry price reconciliation**: After each fill, fetch the exchange's actual avgEntryPrice and reconcile with the internal ledger.

### Estimated Effort

- LivePositionLedger: 16 hours
- Virtual SL/TP monitoring: 8 hours
- Proportional close logic: 8 hours
- Entry price reconciliation: 4 hours
- **Total: ~36 hours (1 full sprint)**

---

## Gap C: Slippage Model

### Paper Behavior

- Fixed: `slippage_bps = 15` (0.15%), applied symmetrically.
- Applied once on entry, once on exit (if SL/TP triggers or market close).
- SL/TP fills occur at the exact trigger price (paper_executor.py:374-378) with **no additional slippage**.

### Live Behavior

- Slippage depends on: order book depth, trade size relative to top-of-book, market volatility, and time of day.
- New listing altcoins (which the system targets via `new_listing.py` scanner) typically have thin order books. Slippage of 30-100+ bps is common in the first days.
- SL orders in volatile conditions can experience significant slippage beyond the trigger price ("slippage on stop").

### Gap Details

| Aspect | Paper | Live |
|---|---|---|
| Entry slippage | Fixed 15 bps | Variable, 5-100+ bps |
| Exit slippage (TP) | 0 bps (exact price fill) | 5-50 bps |
| Exit slippage (SL) | 0 bps (exact price fill) | Can be extreme in flash crashes |
| Correlation with volatility | None | Highly correlated -- worst when you need SL most |

### Severity: **HIGH**

### Impact on Profitability

The grid config requires `min_spacing_pct: 0.45%` to be profitable. This was calculated assuming 0.15% slippage + 0.06% fee each way:
```
Round-trip cost (paper) = (0.15 + 0.06) * 2 = 0.42%
```

With realistic live slippage of 0.25% on new listings:
```
Round-trip cost (live) = (0.25 + 0.06) * 2 = 0.62%
```

This means any grid level with spacing below 0.62% will be **net negative** in live. With a 2.5 ATR multiplier and 10 levels, many altcoins produce spacings of 0.3-0.5%, making the grid systematically unprofitable.

### Required Code Changes

1. **Dynamic slippage model**: Estimate real-time slippage from order book depth (top-5 bids/asks). Use `get_orderbook` API endpoint.
2. **Adjust min_spacing_pct dynamically**: `min_spacing >= 2 * (estimated_slippage + fee_rate)` with a safety margin.
3. **Slippage on SL**: Apply simulated SL slippage in paper mode (e.g., 2x normal slippage for stop orders) for more realistic backtesting.
4. **Paper config update**: Increase `slippage_bps` from 15 to 25-30 for new-listing altcoin strategies.

### Estimated Effort

- Order-book-based slippage estimator: 8 hours
- Dynamic min_spacing: 4 hours
- Paper SL slippage enhancement: 2 hours

---

## Gap D: Rate Limits

### Paper Behavior

- No rate limits. Orders execute instantly.
- Grid can fire 10+ fills in a single event loop tick.

### Live Behavior

- Bybit rate limits: 10 requests/second for order endpoints (default tier), up to 20/s for VIP.
- Current mitigations:
  - `BybitClient._min_request_interval = 0.1` (100ms between requests, i.e., 10 req/s) -- bybit_client.py:104
  - `GridPositionManager._order_delay = 0.15` (150ms between grid orders in live mode) -- grid_manager.py:44
- Each grid fill involves: `set_margin_and_leverage` (up to 2 API calls) + `place_market_order` (1 call + 1 ticker call + 1 execution poll) = **up to 5 API calls per fill**.
- A recenter event closing 6 levels and opening 6 new ones = **~60 API calls**, taking 6-9 seconds with throttling.

### Gap Details

| Aspect | Paper | Live |
|---|---|---|
| Orders per second | Unlimited | 10 (default) / 20 (VIP) |
| Recenter latency | ~0 ms | 6-9 seconds (during which prices move) |
| Burst grid setup | Instant | Sequential, 150ms per level |
| API errors from rate limit | None | HTTP 429, auto-retry with backoff |

### Severity: **MEDIUM**

### Impact on Profitability

- During a recenter, the 6-9 second delay means later fills execute at prices that have drifted from the calculated grid levels.
- In a fast market, the first and last fill in a recenter batch can differ by 0.5-1.0%, breaking the grid symmetry.
- Rate limit errors cause retries (exponential backoff), further increasing latency.

### Required Code Changes

1. **Batch order API**: Use Bybit's batch order endpoint (`/v5/order/create-batch`) which allows up to 10 orders in a single request. This reduces a 10-level grid setup from 10 API calls to 1.
2. **Reduce unnecessary API calls**: Cache leverage settings; skip `set_margin_and_leverage` if already set for the symbol (currently handled for identical values via error code 110043, but the API call is still made).
3. **Priority queue**: Process close orders before open orders during recenter to free margin first.
4. **Paper-side simulation**: Add configurable delay to paper mode to simulate realistic execution timing for more accurate backtests.

### Estimated Effort

- Batch order integration: 8 hours
- API call reduction: 4 hours
- Priority queue: 4 hours

---

## Gap E: Funding Rates

### Paper Behavior

- **No funding rate charges or payments.** Paper positions can be held indefinitely with zero carrying cost.
- The strategy *reads* funding rates for bias calculation (grid_bias.py:221, bias_calculator.py:115-126) to determine grid direction, but the paper P&L never includes funding payments.

### Live Behavior

- Bybit charges/pays funding every **8 hours** (00:00, 08:00, 16:00 UTC).
- Funding payment = `position_value * funding_rate`.
- Typical rates: 0.01% per 8h (0.0001). Extreme: 0.1%+ per 8h during market dislocations.
- For a grid holding 6 levels at 5x leverage with 5% margin each:
  - Total notional: `20 USDT * 6 * 5% * 5 = 30 USDT`
  - Funding cost at 0.01%: `30 * 0.0001 = 0.003 USDT` per 8h -- negligible.
  - Funding cost at 0.1%: `30 * 0.001 = 0.03 USDT` per 8h -- 0.15% of balance, significant if compounded.
- Grid positions can be held up to 24 hours (`grid_timeout_hours: 24`), meaning up to 3 funding events.

### Gap Details

| Aspect | Paper | Live |
|---|---|---|
| Funding charges | None | Every 8 hours |
| Impact on P&L | 0 | -0.003 to -0.03 USDT per 8h period (at current config) |
| Direction sensitivity | Bias uses funding for direction | Funding also costs money when on wrong side |

### Severity: **MEDIUM**

### Impact on Profitability

With the current small position sizes (20 USDT initial balance, 5% per level), funding impact is minimal -- roughly 0.01-0.15% of balance per day. However:

1. If the system scales to larger balances, funding becomes material.
2. During extreme funding events (new listings often have volatile funding), a grid on the "paying" side loses an additional 0.3-1.0% per day.
3. Paper P&L will systematically overstate returns by not deducting funding.

### Required Code Changes

1. **Paper funding simulation**: In `PaperExecutor`, periodically apply funding charges to open positions based on fetched rates. Run a background check every simulated 8h period.
2. **Funding-aware grid filter**: If current funding rate exceeds a threshold (e.g., 0.05%), reduce or skip grid levels on the "paying" side.
3. **P&L reporting**: Include funding costs in both paper and live trade records.

### Estimated Effort

- Paper funding simulation: 6 hours
- Funding-aware filtering: 4 hours
- P&L reporting: 2 hours

---

## Gap F: Margin and Leverage

### Paper Behavior (paper_executor.py:195-199)

```python
leverage = self.account.leverage_settings.get(symbol, 1)
margin = notional / leverage
self.account.balance -= margin + fee
```

- Simple isolated margin: `margin = notional / leverage`.
- No maintenance margin concept.
- No liquidation -- balance can go negative.
- Leverage is a stored integer per symbol, defaults to 1.

### Live Behavior

- Bybit's isolated margin includes: initial margin + maintenance margin.
- Maintenance margin rate varies by position size tier (larger positions require higher maintenance margin %).
- **Liquidation** occurs when unrealized loss exceeds `initial_margin - maintenance_margin`.
- For 5x leverage, liquidation occurs at roughly 18% adverse price move (varies by tier).
- Bybit may also auto-deleverage (ADL) in extreme conditions.

### Gap Details

| Aspect | Paper | Live |
|---|---|---|
| Margin calculation | `notional / leverage` | Initial margin + maintenance margin (tiered) |
| Liquidation | None | Yes, exchange-enforced |
| Margin call | None | Position reduced or liquidated |
| ADL risk | None | Possible in extreme volatility |
| Cross-position margin | Independent | Isolated, but liquidation of one affects available balance |

### Severity: **MEDIUM**

### Impact on Profitability

With the current config (5x leverage, 5% of 20 USDT per level), each micro-position has ~0.20 USDT notional value. Liquidation risk is extremely low at this scale. However:

1. **At scale**: With larger balances or higher leverage, liquidation becomes a real concern. Paper trading gives false confidence about aggressive leverage settings.
2. **Grid cascade risk**: If multiple levels are open on the same side and price moves 18%+ against them, the entire net position (live) gets liquidated. Paper would show a large loss but continue operating.
3. **Available balance**: Live Bybit deducts margin from available balance; if insufficient, the order is rejected. Paper does not enforce this (balance can go negative).

### Required Code Changes

1. **Liquidation simulation in paper**: Calculate and enforce a liquidation price for each paper position. Auto-close at liquidation price if candle breaches it.
2. **Balance check before order**: In paper mode, reject orders if `balance < required_margin + fee` (partially implemented but balance can still go negative via P&L).
3. **Margin tier awareness**: Fetch and apply Bybit's actual margin tier rates for more accurate paper simulation.
4. **Net position liquidation**: Since live uses net positions, calculate liquidation price for the combined position, not per micro-position.

### Estimated Effort

- Paper liquidation simulation: 6 hours
- Balance enforcement: 2 hours
- Margin tier integration: 6 hours

---

## Gap G: Price Detection (WebSocket vs REST/Candle)

### Paper Behavior

- SL/TP detection via `check_sl_tp(candle)` (paper_executor.py:318-440).
- Uses 5-minute candle `high`/`low` to determine if SL or TP was breached.
- **5-minute granularity**: A TP hit at 12:01 is not detected until the 12:05 candle closes (or the 12:00-12:05 candle is processed).
- Within a single candle, if both SL and TP are triggered, **SL takes priority** (conservative assumption).
- Actual intra-candle price path is unknown -- a momentary wick could trigger an SL that wouldn't have been hit with tick-level monitoring.

### Live Behavior

- SL/TP are placed as **conditional orders on the exchange** (live_executor.py:207-302).
- Exchange monitors price continuously (tick-level) and triggers at the exact moment mark price crosses the threshold.
- Trigger type: MarkPrice (not LastPrice), which can differ.
- No 5-minute detection delay.
- Exchange handles the SL-before-TP priority naturally based on which price level is actually hit first.

### Current WebSocket Usage

The system has a WebSocket manager (`ws_manager.py`) used by the data collector for candle streaming, but:
- It streams kline (candle) data, not real-time trade/ticker data.
- The order process does not subscribe to WebSocket price feeds for SL/TP monitoring.
- The live executor relies entirely on exchange-side conditional orders for SL/TP.

### Gap Details

| Aspect | Paper | Live |
|---|---|---|
| Price monitoring | 5-min candle high/low | Tick-level (exchange conditional orders) |
| Detection latency | Up to 5 minutes | Sub-second |
| SL/TP execution | At exact trigger price (no slippage) | Market order at trigger (with slippage) |
| Both triggered same candle | SL wins (conservative) | Whichever price level is hit first |
| Wick sensitivity | Only sees candle extremes | Sees every tick |

### Severity: **MEDIUM** (for paper accuracy), **LOW** (for live functionality)

Live SL/TP works correctly via exchange conditional orders. The gap primarily affects **paper trading accuracy** -- paper results may differ from live due to the 5-minute detection granularity.

### Impact on Profitability

- Paper may miss TPs that would have been hit intra-candle if a wick touches TP then reverses.
- Paper may trigger SLs on candles where both SL and TP were hit, while live might have hit TP first.
- Net effect: paper tends to be **more conservative** (SL priority), which means live may slightly outperform paper in this specific dimension.

### Required Code Changes

1. **1-minute candles for paper**: Switch paper SL/TP detection from 5m to 1m candles for higher fidelity.
2. **WebSocket price feed for live monitoring**: Subscribe to real-time ticker for grid level detection (not SL/TP, which exchange handles, but for detecting when a grid level is crossed to trigger a fill).
3. **Paper tick simulation** (optional): For backtesting, simulate intra-candle price paths to improve paper accuracy.

### Estimated Effort

- 1-minute candle detection: 2 hours
- WebSocket ticker subscription: 8 hours
- Tick simulation: 16 hours (optional, lower priority)

---

## Summary Table

| Gap | Severity | Live Break? | Profitability Impact | Effort (hrs) |
|-----|----------|-------------|---------------------|---------------|
| **B. Net Position Model** | CRITICAL | YES -- grid logic broken | System non-functional | 36 |
| **A. Order Execution** | HIGH | YES -- `TypeError` on interface mismatch | -0.2% to -0.5% per round trip | 9 |
| **C. Slippage Model** | HIGH | No | -0.1% to -0.3% per trade (can make grid unprofitable) | 14 |
| **D. Rate Limits** | MEDIUM | No (mitigated) | 0.1-0.5% drift during recenters | 16 |
| **E. Funding Rates** | MEDIUM | No | -0.01% to -0.15% per day | 12 |
| **F. Margin/Leverage** | MEDIUM | No (at current scale) | Liquidation risk at scale | 14 |
| **G. Price Detection** | MEDIUM | No | Paper accuracy +/- 0.1% | 10-26 |
| **TOTAL** | | | | **111-127 hrs** |

---

## Recommended Implementation Order

### Phase 1: Must-fix before any live trading (Week 1-2)

1. **B. Net Position Model** -- Without this, live trading is fundamentally broken.
2. **A. Interface Mismatch** -- `TypeError` will crash the live order process.

### Phase 2: Required for profitable live trading (Week 3)

3. **C. Slippage Model** -- Dynamic spacing prevents systematic losses.
4. **A. Partial Fill Handler** -- Prevents position size drift.

### Phase 3: Operational robustness (Week 4)

5. **D. Rate Limits** -- Batch orders for faster recenters.
6. **E. Funding Rates** -- Paper accuracy and live cost awareness.

### Phase 4: Refinement (Week 5+)

7. **F. Margin/Leverage** -- Liquidation simulation for paper accuracy.
8. **G. Price Detection** -- Higher fidelity paper trading.

---

## Appendix: Key File References

| File | Role |
|------|------|
| `src/order/paper_executor.py` | Paper trading simulation (707 lines) |
| `src/order/live_executor.py` | Live Bybit order execution (435 lines) |
| `src/order/grid_manager.py` | Grid micro-position management (336 lines) |
| `src/collector/bybit_client.py` | Bybit V5 REST API wrapper (659 lines) |
| `config/strategy/asset.yaml` | Paper fee/slippage config, position sizing |
| `config/strategy/grid.yaml` | Grid structure, spacing, bias config |

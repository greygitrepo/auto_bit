# Live Transition Plan: Paper to Live Trading on Bybit

**Date:** 2026-03-27
**System:** auto_bit Grid + Directional Bias Hybrid Strategy
**Exchange:** Bybit V5 USDT Linear Perpetuals
**Current State:** Paper trading operational, all issues below must be resolved before live

---

## Table of Contents

1. [Issue A: Net Position Model (CRITICAL)](#issue-a-net-position-model)
2. [Issue B: Order Execution Interface Mismatch (CRITICAL)](#issue-b-order-execution-interface-mismatch)
3. [Issue C: close_position_by_key Missing in LiveExecutor (CRITICAL)](#issue-c-close_position_by_key-missing-in-liveexecutor)
4. [Issue D: Fill Detection -- Candle-Based vs Real-Time (HIGH)](#issue-d-fill-detection)
5. [Issue E: Slippage Model (HIGH)](#issue-e-slippage-model)
6. [Issue F: Error Handling -- Network, Partial Fills, Rejections (HIGH)](#issue-f-error-handling)
7. [Issue G: State Recovery on P3 Crash (HIGH)](#issue-g-state-recovery-on-p3-crash)
8. [Issue H: Rate Limits (MEDIUM)](#issue-h-rate-limits)
9. [Issue I: Funding Rates (MEDIUM)](#issue-i-funding-rates)
10. [Issue J: Margin and Liquidation (MEDIUM)](#issue-j-margin-and-liquidation)
11. [Issue K: WebSocket Price Feed (MEDIUM)](#issue-k-websocket-price-feed)
12. [Issue L: Order Types -- Market vs Limit (LOW)](#issue-l-order-types)
13. [Phased Implementation Plan](#phased-implementation-plan)
14. [Testing Strategy](#testing-strategy)

---

## Issue A: Net Position Model

### Severity: CRITICAL

### Current Paper Behavior

**File:** `src/order/paper_executor.py` lines 203-220

```python
order_id = self._gen_order_id()
pos_key = order_id  # unique UUID per fill
self.account.positions[pos_key] = position
```

Each grid fill creates an independent `PaperPosition` keyed by a unique UUID. The paper executor can hold N independent micro-positions for the same symbol simultaneously. With 10 grid levels across 8 symbols, the paper executor manages up to 80 independent micro-positions, each with its own entry price, margin, SL/TP, and P&L.

**File:** `src/order/grid_manager.py` lines 51-53

```python
self._level_positions: Dict[LevelKey, int] = {}    # level -> position_id in DB
self._level_order_ids: Dict[LevelKey, str] = {}    # level -> orderId in paper executor
self._level_entry_fees: Dict[LevelKey, float] = {} # level -> entry fee
```

The grid manager maps each `(symbol, level_index)` to a unique executor `orderId`, enabling per-level close operations.

### Expected Live Behavior

Bybit linear USDT perpetuals enforce a **net position model**: one position per symbol per side. If the grid opens 6 Buy levels on XYZUSDT:

- Paper sees: 6 independent positions at prices 100, 99, 98, 97, 96, 95
- Live sees: 1 position of 6x qty at volume-weighted average entry ~97.5

**File:** `src/order/live_executor.py` lines 404-416 -- `get_position()` returns at most one position per symbol.

**File:** `src/collector/bybit_client.py` lines 314-341 -- `get_positions()` filters zero-size entries, returning the single net position.

### Impact If Not Fixed

**System non-functional.** The grid logic fundamentally assumes independent micro-positions. In live mode:

1. Grid opens level -1 (Buy 0.1 @ 100), level -2 (Buy 0.1 @ 99), level -3 (Buy 0.1 @ 98). Exchange sees: 1 Buy of 0.3 @ avg 99.
2. Level -1 TP hits. Grid tries to close 0.1 via `close_position_by_key` (which doesn't exist on LiveExecutor) or falls back to `close_position(qty=0.1)`. The exchange reduces the net position from 0.3 to 0.2, but the remaining 0.2 has stale SL/TP orders sized for 0.3.
3. P&L attribution is impossible -- the exchange only reports the net position's PnL, not per-level PnL.
4. When level -2 TP hits, the grid tries to close another 0.1, but the internal state and exchange state are now desynchronized.

### Resolution Plan

#### Step 1: Create `LivePositionLedger` class

**New file:** `src/order/live_position_ledger.py`

This internal ledger tracks micro-positions identically to paper mode but translates them to net-position operations on the exchange.

```python
class LivePositionLedger:
    """Internal ledger mapping grid micro-positions to exchange net position.

    Tracks:
    - Per-level entry price, qty, and status (mirrors paper executor's positions dict)
    - Net position state (sum of all micro-positions per symbol/side)
    - Maps close_position_by_key() to partial close on exchange
    """

    def __init__(self):
        # {pos_key: MicroPosition} -- same structure as PaperExecutor
        self._positions: Dict[str, MicroPosition] = {}
        # {symbol: NetPositionState} -- tracks aggregate exchange state
        self._net_state: Dict[str, NetPositionState] = {}

    def record_fill(self, symbol: str, side: str, qty: float,
                    fill_price: float, order_id: str) -> str:
        """Record a micro-position after a live fill. Returns pos_key."""
        pos_key = order_id
        self._positions[pos_key] = MicroPosition(
            symbol=symbol, side=side, qty=qty,
            entry_price=fill_price, order_id=order_id,
        )
        self._update_net_state(symbol)
        return pos_key

    def get_micro_position(self, pos_key: str) -> Optional[MicroPosition]:
        """Retrieve a specific micro-position by key."""
        return self._positions.get(pos_key)

    def close_micro_position(self, pos_key: str) -> Optional[MicroPosition]:
        """Remove a micro-position from the ledger. Returns it for P&L calc."""
        return self._positions.pop(pos_key, None)

    def get_net_qty(self, symbol: str, side: str) -> float:
        """Calculate total qty for a symbol/side across all micro-positions."""
        return sum(
            p.qty for p in self._positions.values()
            if p.symbol == symbol and p.side == side
        )

    def reconcile_with_exchange(self, symbol: str, exchange_pos: dict) -> dict:
        """Compare internal ledger with exchange position. Returns discrepancies."""
        internal_qty = self.get_net_qty(symbol, exchange_pos.get("side", "Buy"))
        exchange_qty = float(exchange_pos.get("size", 0))
        return {
            "internal_qty": internal_qty,
            "exchange_qty": exchange_qty,
            "drift": abs(internal_qty - exchange_qty),
            "exchange_avg_entry": float(exchange_pos.get("avgPrice", 0)),
        }
```

#### Step 2: Add `close_position_by_key()` to `LiveExecutor`

**File:** `src/order/live_executor.py` -- add method

```python
async def close_position_by_key(
    self, pos_key: str, current_price: float,
    ledger: LivePositionLedger = None,
) -> dict:
    """Close a specific micro-position by placing a partial close on exchange.

    Looks up the micro-position in the ledger, then places a market order
    for that micro-position's qty on the opposite side.
    """
    if ledger is None:
        raise ValueError("LiveExecutor.close_position_by_key requires a ledger")

    micro = ledger.get_micro_position(pos_key)
    if micro is None:
        logger.warning("close_position_by_key: no micro-position for key={}", pos_key)
        return {"orderId": "", "fillPrice": current_price, "pnl": 0.0,
                "fee": 0.0, "side": "", "qty": 0}

    close_side = "Sell" if micro.side == "Buy" else "Buy"
    result = await self.place_market_order(
        symbol=micro.symbol, side=close_side, qty=micro.qty,
    )

    fill_price = result.get("fillPrice", current_price)
    # Calculate P&L for this micro-position
    if micro.side == "Buy":
        raw_pnl = (fill_price - micro.entry_price) * micro.qty
    else:
        raw_pnl = (micro.entry_price - fill_price) * micro.qty

    fee = abs(fill_price * micro.qty) * 0.0006  # taker fee estimate
    net_pnl = raw_pnl - fee

    # Remove from ledger
    ledger.close_micro_position(pos_key)

    return {
        "orderId": result.get("orderId", ""),
        "fillPrice": fill_price,
        "pnl": net_pnl,
        "fee": fee,
        "side": close_side,
        "qty": micro.qty,
    }
```

#### Step 3: Integrate ledger into `GridPositionManager`

**File:** `src/order/grid_manager.py` -- modify `__init__` and `_handle_fill`

- Pass the `LivePositionLedger` to `GridPositionManager.__init__()` when `mode == "live"`
- After each live fill, call `ledger.record_fill()` with the exchange's actual fill price and order ID
- On TP/close, call `executor.close_position_by_key(pos_key, price, ledger=self._ledger)`

#### Step 4: Virtual SL/TP for Grid Levels

Do NOT use exchange conditional orders for per-level SL/TP. The exchange SL/TP applies to the entire net position, which conflicts with per-level management. Instead:

- Place a single "safety net" SL on the exchange for the net position at the worst-case level (e.g., 5% below the lowest grid Buy level)
- Monitor TP hits in the strategy process (P2's `GridEngine.check_tp_hits()`) as currently done
- Send close signals to P3 which executes partial closes via the ledger

#### Step 5: Periodic reconciliation

Every 30 seconds in P3's monitor loop:
1. Fetch exchange position via `get_positions(symbol)`
2. Call `ledger.reconcile_with_exchange(symbol, exchange_pos)`
3. If drift exceeds threshold (e.g., 1% of net qty), log a warning and adjust ledger

### Estimated Effort: 40 hours

### Testing Strategy

1. Unit test `LivePositionLedger` with multiple micro-positions per symbol
2. Integration test: simulate 5 grid fills, verify net qty matches sum of micro-positions
3. Integration test: close 2 micro-positions, verify remaining net qty is correct
4. Live testnet: Run grid on Bybit testnet for 24h with 3 symbols, verify reconciliation reports zero drift

---

## Issue B: Order Execution Interface Mismatch

### Severity: CRITICAL

### Current Paper Behavior

**File:** `src/order/paper_executor.py` lines 171-173

```python
async def place_market_order(
    self, symbol: str, side: str, qty: float, current_price: float
) -> dict:
```

Paper executor requires 4 parameters: `symbol`, `side`, `qty`, `current_price`.

### Expected Live Behavior

**File:** `src/order/live_executor.py` lines 126-128

```python
async def place_market_order(
    self, symbol: str, side: str, qty: float
) -> dict:
```

Live executor requires 3 parameters: `symbol`, `side`, `qty`. No `current_price` parameter.

### Where It Breaks

**File:** `src/order/grid_manager.py` lines 125-128

```python
result = await self._executor.place_market_order(
    symbol=symbol, side=side, qty=order_req.qty,
    current_price=level_price,
)
```

This passes `current_price=level_price` to the executor. When `self._executor` is a `LiveExecutor`, this will raise `TypeError: place_market_order() got an unexpected keyword argument 'current_price'`.

**File:** `src/order/order_manager.py` lines 102-109 also branches on mode:

```python
if self.mode == "paper":
    order_info = await self.executor.place_market_order(
        symbol, side, qty, signal.entry_price
    )
else:
    order_info = await self.executor.place_market_order(
        symbol, side, qty
    )
```

The `OrderManager` correctly handles the difference, but `GridPositionManager` does not.

### Impact If Not Fixed

**Immediate crash.** Any grid fill in live mode raises `TypeError`, killing P3. No orders can be placed.

### Resolution Plan

**Option A (preferred): Make LiveExecutor accept and ignore `current_price`.**

**File:** `src/order/live_executor.py` -- change signature:

```python
async def place_market_order(
    self, symbol: str, side: str, qty: float, current_price: float = 0.0
) -> dict:
```

The `current_price` parameter can serve as fallback for fill price if execution polling fails (it already uses `current_price` as fallback in the existing code at line 177).

**Option B: Make GridPositionManager mode-aware.**

**File:** `src/order/grid_manager.py` lines 124-128 -- change to:

```python
if self._mode == "live":
    result = await self._executor.place_market_order(
        symbol=symbol, side=side, qty=order_req.qty,
    )
else:
    result = await self._executor.place_market_order(
        symbol=symbol, side=side, qty=order_req.qty,
        current_price=level_price,
    )
```

**Recommendation:** Option A is cleaner. The `current_price` parameter is useful as a fallback price reference for the live executor anyway.

### Estimated Effort: 1 hour

### Testing Strategy

1. Unit test: call `LiveExecutor.place_market_order(symbol, side, qty, current_price=100.0)` -- verify no TypeError
2. Unit test: call without `current_price` -- verify backward compatibility

---

## Issue C: close_position_by_key Missing in LiveExecutor

### Severity: CRITICAL

### Current Paper Behavior

**File:** `src/order/paper_executor.py` lines 548-594

```python
async def close_position_by_key(self, pos_key: str, current_price: float) -> dict:
```

Looks up the specific micro-position by `pos_key` (the `orderId` from `place_market_order`), calculates P&L from that micro-position's entry price, and removes it.

### Expected Live Behavior

**File:** `src/order/live_executor.py` -- **method does not exist**.

### Where It Breaks

**File:** `src/order/grid_manager.py` lines 198-201

```python
if order_key and hasattr(self._executor, 'close_position_by_key'):
    result = await self._executor.close_position_by_key(order_key, tp_price)
else:
    close_side = "Sell" if position["side"] == "Buy" else "Buy"
    result = await self._executor.close_position(
        symbol=symbol, side=close_side,
        qty=float(position["size"]), current_price=tp_price,
    )
```

The `hasattr` check means it falls through to `close_position()` for `LiveExecutor`. But `close_position()` in live mode closes `qty=float(position["size"])` -- where `position["size"]` is the tracker's stored size for that specific grid level (the micro-position size). This happens to be correct IF the exchange net position is larger than this qty.

However, the P&L calculation in the fallback path is wrong because:
- `LiveExecutor.close_position()` calculates P&L from the tracker's stored entry price
- But the exchange's actual average entry price is different (it's the net average of all fills)
- The exchange closes at the net position's average entry, creating a P&L discrepancy

### Impact If Not Fixed

P&L tracking becomes inaccurate. The system thinks it made/lost X but the exchange shows Y. Over time, the internal balance diverges from the actual account balance. Risk management decisions (drawdown limits, position sizing) are based on wrong data.

### Resolution Plan

This is resolved as part of Issue A (LivePositionLedger). The `close_position_by_key()` method on `LiveExecutor` uses the ledger to look up micro-position details and issues a partial close for that exact qty.

Additionally, at lines 262-266 in grid_manager.py, the same pattern appears for `_handle_close()`. Both paths need to use the ledger-aware close.

### Estimated Effort: Included in Issue A (40 hours)

---

## Issue D: Fill Detection -- Candle-Based vs Real-Time

### Severity: HIGH

### Current Paper Behavior

**File:** `src/strategy/position/grid_engine.py` lines 159-213

```python
def check_fills(self, candle: Dict[str, Any], levels: List[GridLevel]) -> List[GridSignal]:
    low = float(candle.get("low", 0))
    high = float(candle.get("high", 0))
    # ...
    if level.side == "Buy" and low <= level.price:
        filled = True
    elif level.side == "Sell" and high >= level.price:
        filled = True
```

Grid fills are detected by comparing 5-minute candle high/low against grid level prices. This means:

- A fill is detected at most once per 5-minute candle
- If price crosses a grid level at 12:01 and reverses by 12:05, the fill is detected but the TP might not be reachable by the time P3 places the order
- Multiple levels can appear "filled" in a single candle even though they were crossed sequentially over 5 minutes

**File:** `src/strategy/position/grid_engine.py` lines 215-262 -- `check_tp_hits()` also uses candle high/low:

```python
if level.side == "Buy" and high >= level.tp_price:
    hit = True
```

### Expected Live Behavior

In live mode, grid fill detection should ideally use real-time price data to:
1. Detect the exact moment a grid level is crossed
2. Place the market order immediately (not wait for candle close)
3. Track precise fill sequence when multiple levels are crossed

Currently, the system uses the same 5-minute candle detection for both paper and live modes. In live mode, this creates a latency of up to 5 minutes between the actual price crossing a grid level and the system detecting it.

### Impact If Not Fixed

- **Missed fills**: Price crosses a level and reverses within 5 minutes. Candle shows it was crossed, system places order, but price has already moved away. Market order fills at a worse price.
- **Stale entry prices**: Grid level says "Buy at 100" but by the time the order is placed 1-3 minutes later, price is at 100.5. The 0.5% drift eats into the 0.55% grid spacing.
- **TP hit timing**: TP detection is similarly delayed. Price hits TP at 12:01, close order placed after 12:05 candle. Price may have reversed.

### Resolution Plan

#### Phase 1: Use 1-minute candles for grid detection in live mode

**File:** `src/strategy/process.py` -- modify `_evaluate_grid_strategy()`

Change grid evaluation to trigger on every 1-minute candle (not just 5-minute) when in live mode. This reduces detection latency from 5 minutes to 1 minute with minimal code change.

```python
def _handle_candle(self, msg: MarketDataMessage) -> None:
    symbol = msg.symbol
    timeframe = msg.timeframe
    self._update_market_data_cache(symbol, timeframe, candle)

    # Grid strategy: evaluate on 1m candles in live mode for faster fill detection
    if self._grid_strategy is not None:
        eval_tf = "1m" if self._config.get("mode") == "live" else self._primary_tf
        if timeframe == eval_tf and symbol in self._active_trading_symbols:
            self._evaluate_grid_strategy(symbol, candle)
    elif timeframe == self._primary_tf and symbol in self._active_trading_symbols:
        self._evaluate_strategy(symbol)
```

Requires P1 to also stream 1-minute candles. Add `"1m"` to the `timeframes.secondary` list when in live mode.

#### Phase 2: WebSocket ticker-based fill detection (recommended for production)

Subscribe to real-time ticker WebSocket (`tickers.XYZUSDT`) in P2 or a dedicated fill-detection process. When last price crosses a grid level, immediately emit a fill signal. This reduces detection latency to sub-second.

**File:** `src/collector/ws_manager.py` -- add ticker topic support alongside kline topics.

```python
# New topic type: tickers
def _make_ticker_topic(symbol: str) -> str:
    return f"tickers.{symbol}"
```

Create a `GridFillMonitor` that:
1. Maintains current grid level prices from P2
2. Subscribes to ticker WebSocket for all active grid symbols
3. On each tick, checks if any level is crossed
4. Emits `GridSignalMessage` directly to P3 via queue

#### Phase 3: Exchange conditional orders for grid fills (ideal)

Instead of detecting fills via price monitoring, place limit orders at each grid level price on the exchange. When the exchange fills the limit order, the fill is guaranteed at the exact price with zero slippage. See Issue L for details.

### Estimated Effort

- Phase 1 (1m candles): 4 hours
- Phase 2 (WebSocket ticker): 12 hours
- Phase 3 (exchange limit orders): 20 hours

---

## Issue E: Slippage Model

### Severity: HIGH

### Current Paper Behavior

**File:** `src/order/paper_executor.py` lines 128-136

```python
def _apply_slippage(self, price: float, side: str) -> float:
    factor = self.slippage_bps / 10_000  # 15 bps = 0.0015
    if side == "Buy":
        return price * (1 + factor)
    return price * (1 - factor)
```

**File:** `config/strategy/asset.yaml` line 39: `slippage_bps: 15`

Paper uses a fixed 15 bps (0.15%) slippage on every order, symmetrically applied. SL/TP fills in paper mode execute at the exact trigger price with zero additional slippage (`check_sl_tp` at line 374: `fill_price = pos.sl_price`).

### Expected Live Behavior

Real slippage on Bybit depends on:
- **Order book depth**: New-listing altcoins (targeted by the scanner) have thin books. Top-of-book may only support $500-2000 of notional before significant price impact.
- **Market conditions**: During volatility spikes, slippage can exceed 100 bps.
- **Order size**: The system uses small orders (2% of 20 USDT = 0.4 USDT margin, 5x leverage = 2 USDT notional), so slippage should be minimal for individual orders. But during recenters, 6-8 closes + 6-8 opens can move the market.

### Impact If Not Fixed

**Grid profitability at risk.** Current config:

```
min_spacing_pct: 0.55%
Round-trip cost (paper): (0.15% slippage + 0.06% fee) * 2 = 0.42%
Net margin per round trip: 0.55% - 0.42% = 0.13%
```

With live slippage of 25 bps on illiquid alts:

```
Round-trip cost (live): (0.25% slippage + 0.06% fee) * 2 = 0.62%
Net margin per round trip: 0.55% - 0.62% = -0.07% (LOSING money)
```

### Resolution Plan

#### 1. Pre-trade spread check

**File:** `src/order/grid_manager.py` -- add to `_handle_fill()` before placing order

Before each grid fill, fetch the current bid-ask spread. If spread exceeds `min_spacing_pct / 3`, skip the fill.

```python
async def _check_spread(self, symbol: str, min_spacing_pct: float) -> bool:
    """Return True if spread is acceptable for grid trading."""
    try:
        orderbook = await self._executor._run_sync(
            self._executor.client.get_orderbook, symbol, limit=5
        )
        best_bid = float(orderbook["b"][0][0])
        best_ask = float(orderbook["a"][0][0])
        spread_pct = (best_ask - best_bid) / best_bid
        return spread_pct < min_spacing_pct / 3
    except Exception:
        return True  # Allow trade if we can't check
```

Requires adding `get_orderbook()` to `BybitClient`:

```python
@_retry()
def get_orderbook(self, symbol: str, limit: int = 25) -> Dict[str, Any]:
    raw = self._http.get_orderbook(category=self.CATEGORY, symbol=symbol, limit=limit)
    return self._parse_response(raw, "get_orderbook")
```

#### 2. Dynamic min_spacing_pct

**File:** `src/strategy/position/grid_bias.py` -- modify `_create_grid_for_symbol()`

Calculate `min_spacing_pct` dynamically based on estimated real slippage:

```python
min_spacing = 2 * (estimated_slippage_bps / 10000 + taker_fee_rate) + safety_margin
```

Where `estimated_slippage_bps` is derived from recent spread data for the symbol.

#### 3. Increase paper slippage for realism

**File:** `config/strategy/asset.yaml` -- change `slippage_bps: 15` to `slippage_bps: 25`

This makes paper results more conservative and closer to live performance.

#### 4. Add slippage on SL fills in paper mode

**File:** `src/order/paper_executor.py` -- modify `check_sl_tp()` at line 374

```python
if sl_triggered:
    # Apply 2x slippage on SL fills (stop orders get worse execution)
    sl_slippage = self.slippage_bps * 2 / 10_000
    if pos.side == "Buy":  # LONG, SL is a sell -> price worse (lower)
        fill_price = pos.sl_price * (1 - sl_slippage)
    else:  # SHORT, SL is a buy -> price worse (higher)
        fill_price = pos.sl_price * (1 + sl_slippage)
    fill_type = "stop_loss"
```

### Estimated Effort: 12 hours

---

## Issue F: Error Handling -- Network, Partial Fills, Rejections

### Severity: HIGH

### Current Paper Behavior

Paper executor never fails. Every order fills instantly and completely. No network errors, no partial fills, no order rejections.

### Expected Live Behavior

**Network errors**: REST API calls can fail due to timeouts, DNS resolution failures, or Bybit maintenance windows. The `_retry` decorator in `bybit_client.py` lines 39-75 retries 3 times with exponential backoff, but after 3 failures it raises `BybitAPIError`.

**Partial fills**: `LiveExecutor.place_market_order()` at lines 179-194 detects partial fills but only logs a warning:

```python
if 0 < filled_qty < qty * 0.99:
    logger.warning("PARTIAL FILL detected...")
    result["partialFill"] = True
    result["filledQty"] = filled_qty
```

No corrective action is taken. The grid manager does not check for `partialFill` in the result.

**Order rejections**: Bybit can reject orders for:
- Insufficient margin (error 110007)
- Position size exceeds limit (error 110012)
- Symbol in pre-market or settlement period
- Rate limit exceeded (HTTP 429)
- Reduce-only mode active on the symbol

Currently, `GridPositionManager._handle_fill()` at lines 128-133 catches exceptions but returns a FAILED status without any retry or cleanup:

```python
except Exception as exc:
    logger.error("Grid fill order failed {}: {}", symbol, exc)
    return GridUpdateMessage(
        symbol=symbol, level_id=msg.level_id, action="FAILED",
        reason=str(exc),
    )
```

**SL/TP order placement failures**: `LiveExecutor.place_sl_tp()` at lines 270-291 retries TP placement 3 times, but if it ultimately fails, it sets `tp_order_id = ""`. The grid manager does not know the TP order failed.

### Impact If Not Fixed

- **Partial fill**: Grid thinks it has a full position but exchange has a partial. TP close tries to close the full amount, creating an unintended position on the opposite side.
- **Unhandled rejection**: Grid level stays in FILLED status but no exchange position exists. The level is "ghost filled" -- it never reaches TP and never gets cleaned up.
- **Network failure during close**: Grid level TP detected, close order fails, grid marks level as COMPLETED. The exchange position remains open with no active SL/TP orders.

### Resolution Plan

#### 1. Partial fill handler in GridPositionManager

**File:** `src/order/grid_manager.py` -- modify `_handle_fill()`

After placing a market order, check for partial fill and adjust tracked qty:

```python
result = await self._executor.place_market_order(...)
fill_price = result.get("fillPrice", level_price)
fee = result.get("fee", 0.0)
order_id = result.get("orderId", "")
actual_qty = result.get("filledQty", order_req.qty) if result.get("partialFill") else order_req.qty

# Use actual_qty for position tracking
position_id = self._tracker.add_position({
    "size": actual_qty,  # not order_req.qty
    ...
})
```

#### 2. Close order retry with exponential backoff

**File:** `src/order/grid_manager.py` -- modify `_handle_tp_hit()` and `_handle_close()`

Wrap the close call in a retry loop:

```python
async def _close_with_retry(self, close_fn, max_retries=3, backoff=1.0):
    for attempt in range(max_retries):
        try:
            return await close_fn()
        except Exception as exc:
            if attempt == max_retries - 1:
                raise
            logger.warning("Close attempt {} failed: {}, retrying in {}s",
                          attempt + 1, exc, backoff * (2 ** attempt))
            await asyncio.sleep(backoff * (2 ** attempt))
```

#### 3. Ghost fill detection and cleanup

**File:** `src/order/process.py` -- add to monitor loop

Every 60 seconds, verify that all FILLED/TP_SET levels in the grid have corresponding exchange positions:

```python
async def _verify_grid_positions(self):
    """Cross-check internal grid state against exchange positions."""
    if self._mode != "live" or self._grid_manager is None:
        return
    for key, order_id in self._grid_manager._level_order_ids.items():
        symbol = key[0]
        exchange_pos = await self._executor.get_position(symbol)
        if exchange_pos is None:
            logger.error("Ghost fill detected: {} level {} has no exchange position",
                        symbol, key[1])
            # Clean up: mark level as failed, remove from tracker
```

#### 4. Margin pre-check before grid fill

**File:** `src/order/grid_manager.py` -- add to `_handle_fill()` before order placement

```python
if self._mode == "live":
    try:
        wallet = await self._executor._run_sync(
            self._executor.client.get_wallet_balance
        )
        available = float(wallet.get("totalAvailableBalance", 0))
        required = (order_req.qty * level_price / leverage) * 1.1  # 10% buffer
        if available < required:
            logger.warning("Insufficient margin for {} fill: available={:.2f} required={:.2f}",
                          symbol, available, required)
            return GridUpdateMessage(symbol=symbol, level_id=msg.level_id,
                                   action="FAILED", reason="insufficient_margin")
    except Exception:
        pass  # Don't block on margin check failure
```

### Estimated Effort: 16 hours

---

## Issue G: State Recovery on P3 Crash

### Severity: HIGH

### Current Paper Behavior

Paper mode state is split between:
- `PaperExecutor.account.positions` -- in-memory dict of open micro-positions (lost on crash)
- `PositionTracker` / `DatabaseManager` -- SQLite DB with positions table (persisted)
- `GridPositionManager._level_positions`, `_level_order_ids`, `_level_entry_fees` -- in-memory dicts (lost on crash)

On P3 crash and restart:
1. `PositionTracker` loads open positions from DB -- these survive
2. `PaperExecutor` starts fresh with no positions -- out of sync with DB
3. `GridPositionManager` starts fresh with empty mappings -- out of sync with DB
4. The grid strategy (P2) restores from DB via `restore_from_db()`, but P3's grid manager has no restore capability

### Expected Live Behavior

In live mode, the exchange IS the source of truth. On restart:
1. Exchange positions persist -- they don't disappear
2. Open SL/TP conditional orders persist on exchange
3. But P3's `GridPositionManager` mappings are lost -- it doesn't know which grid level maps to which exchange position

`OrderManager.sync_with_exchange()` at lines 417-507 exists but:
- It only checks if DB positions match exchange positions (symbol-level)
- It does NOT rebuild `GridPositionManager` mappings
- It does NOT reconcile per-level micro-positions with the net exchange position

### Impact If Not Fixed

After P3 restart in live mode:
1. Exchange has open positions with active SL/TP orders
2. P3 grid manager has no knowledge of them
3. P2 grid strategy restored from DB and continues emitting fill/TP signals
4. P3 receives TP_HIT signals but can't find the corresponding micro-positions
5. Grid tries to open new fills on levels that already have exchange positions
6. Result: doubled positions, orphaned SL/TP orders, balance desynchronization

### Resolution Plan

#### 1. Persist GridPositionManager state to DB

**File:** `src/order/grid_manager.py` -- add persistence methods

```python
def persist_state(self, db: DatabaseManager) -> None:
    """Save all internal mappings to DB for crash recovery."""
    for key, pos_id in self._level_positions.items():
        db.set_state(f"grid_level_pos_{key[0]}_{key[1]}", str(pos_id))
    for key, order_id in self._level_order_ids.items():
        db.set_state(f"grid_level_order_{key[0]}_{key[1]}", order_id)
    for key, fee in self._level_entry_fees.items():
        db.set_state(f"grid_level_fee_{key[0]}_{key[1]}", str(fee))

def restore_state(self, db: DatabaseManager, symbols: List[str]) -> int:
    """Restore internal mappings from DB after crash."""
    # Load all grid_level_* keys from state table
    # Reconstruct _level_positions, _level_order_ids, _level_entry_fees
    ...
```

Call `persist_state()` after every fill confirmation and TP close in `_handle_fill()` and `_handle_tp_hit()`.

#### 2. Exchange reconciliation on startup

**File:** `src/order/process.py` -- add to `_main()` after initialization

```python
if self._mode == "live":
    # Step 1: Sync order_manager with exchange
    sync_result = await self._order_manager.sync_with_exchange()
    logger.info("P3 startup sync: {}", sync_result)

    # Step 2: Restore grid manager state from DB
    if self._grid_manager is not None:
        restored = self._grid_manager.restore_state(self._db, active_symbols)
        logger.info("P3 grid manager restored {} level mappings", restored)

    # Step 3: Cancel orphaned SL/TP orders on exchange
    # (orders that don't match any tracked position)
    await self._cleanup_orphaned_orders()
```

#### 3. LivePositionLedger persistence

If using the ledger from Issue A, the ledger must also persist its micro-position state to DB and restore on startup.

### Estimated Effort: 16 hours

---

## Issue H: Rate Limits

### Severity: MEDIUM

### Current Paper Behavior

No rate limits. All orders execute in a single event loop tick. A 10-level grid setup completes in microseconds.

### Expected Live Behavior

**File:** `src/collector/bybit_client.py` lines 103-104

```python
self._min_request_interval = 0.1  # 100ms between requests (10 req/s)
```

**File:** `src/order/grid_manager.py` line 49

```python
self._order_delay = 0.15 if mode == "live" else 0.0  # 150ms between grid orders
```

Per grid fill, the API calls are:
1. `set_margin_and_leverage`: up to 2 calls (set_margin_mode + set_leverage). Currently called every fill even if already set.
2. `place_market_order`: 1 call + 1 `get_tickers` call + 0.5s sleep + 1 `get_executions` call = 3 calls + 0.5s wall time.
3. Total: ~5 API calls per fill, taking ~1.5 seconds minimum.

A recenter closing 6 levels and opening 6 new ones = ~60 API calls = 6-9 seconds. During this time, prices move.

Bybit rate limits:
- Default tier: 10 requests/second for trade endpoints
- VIP1+: 20 requests/second
- Batch order endpoint: up to 10 orders per request

### Impact If Not Fixed

- Recenter latency: 6-9 seconds where early and late fills diverge in price
- Rate limit errors (HTTP 429) during burst activity cause exponential backoff retries
- Grid symmetry broken by price drift during sequential order placement

### Resolution Plan

#### 1. Cache leverage settings

**File:** `src/order/grid_manager.py` -- add leverage cache

```python
self._leverage_set: Dict[str, int] = {}  # symbol -> leverage already set

# In _handle_fill:
if symbol not in self._leverage_set or self._leverage_set[symbol] != leverage:
    await self._executor.set_margin_and_leverage(symbol, leverage)
    self._leverage_set[symbol] = leverage
```

This eliminates 2 API calls per fill for symbols already configured.

#### 2. Remove unnecessary ticker fetch from place_market_order

**File:** `src/order/live_executor.py` lines 147-153

The `get_tickers` call fetches ALL linear tickers just to get one symbol's price. Replace with the `current_price` parameter (after Issue B fix):

```python
async def place_market_order(
    self, symbol: str, side: str, qty: float, current_price: float = 0.0
) -> dict:
    qty = self._round_qty(symbol, qty)
    result = await self._run_sync(
        self.client.place_order,
        symbol=symbol, side=side, qty=str(qty), order_type="Market",
    )
    # Use current_price as fallback instead of fetching tickers
    fill_price = float(result.get("avgPrice", 0)) or current_price
    ...
```

This saves 1 API call per order.

#### 3. Batch order endpoint for recenters

**File:** `src/collector/bybit_client.py` -- add batch order method

```python
@_retry()
def place_batch_orders(self, orders: List[Dict]) -> List[Dict]:
    """Place up to 10 orders in a single API call via /v5/order/create-batch."""
    self._require_auth()
    raw = self._http.place_batch_order(
        category=self.CATEGORY,
        request=orders,
    )
    result = self._parse_response(raw, "place_batch_orders")
    return result.get("list", [])
```

Use during recenters: batch all close orders (up to 10) in one call, then batch all open orders.

#### 4. Priority ordering: closes before opens

During recenters, process close orders first to free margin, then open new positions. This prevents margin exhaustion.

### Estimated Effort: 12 hours

---

## Issue I: Funding Rates

### Severity: MEDIUM

### Current Paper Behavior

Paper positions incur zero funding charges. The strategy reads funding rates for directional bias calculation (`grid_bias.py` line 228, `bias_calculator.py`) but never deducts funding from the paper balance.

### Expected Live Behavior

Bybit charges/pays funding every 8 hours (00:00, 08:00, 16:00 UTC).

Funding payment = `position_value * funding_rate`

With current config: 20 USDT balance, 2% per level, 5x leverage, up to 8 open levels:
- Max notional: 20 * 0.02 * 5 * 8 = 16 USDT
- Normal funding (0.01%): 16 * 0.0001 = 0.0016 USDT per 8h -- negligible
- Extreme funding (0.1%): 16 * 0.001 = 0.016 USDT per 8h -- 0.08% of balance

Grid positions can be held up to 24 hours (`grid_timeout_hours: 24`), meaning up to 3 funding events.

### Impact If Not Fixed

- Paper P&L systematically overstates returns by 0.01-0.08% per day at current scale
- During extreme funding events on new listings (which can reach 0.5%+), the discrepancy grows significantly
- At larger scale (e.g., 1000 USDT balance), funding becomes material: 0.80 USDT per 8h at extreme rates

### Resolution Plan

#### 1. Paper funding simulation

**File:** `src/order/paper_executor.py` -- add method

```python
async def apply_funding(self, funding_rates: Dict[str, float]) -> float:
    """Apply funding charges to all open positions. Returns total funding paid."""
    total_funding = 0.0
    for pos_key, pos in self.account.positions.items():
        rate = funding_rates.get(pos.symbol, 0.0)
        if rate == 0:
            continue
        notional = pos.entry_price * pos.qty
        # Long pays when rate > 0, short pays when rate < 0
        if pos.side == "Buy":
            funding = notional * rate
        else:
            funding = -notional * rate
        self.account.balance -= funding
        total_funding += funding
    return total_funding
```

**File:** `src/order/process.py` -- call every 8 hours (simulate funding events at 00:00, 08:00, 16:00 UTC)

#### 2. Funding-aware grid filter

**File:** `src/strategy/position/grid_bias.py` -- in `_create_grid_for_symbol()`

If current funding rate exceeds threshold (e.g., 0.05%), reduce levels on the paying side:

```python
funding = self._funding_rates.get(symbol, 0)
if abs(funding) > 0.0005:  # 0.05%
    if funding > 0:  # Longs pay -> reduce buy levels
        num_buy = max(1, num_buy - 2)
        num_sell = num_levels - num_buy
    else:  # Shorts pay -> reduce sell levels
        num_sell = max(1, num_sell - 2)
        num_buy = num_levels - num_sell
```

#### 3. Include funding in P&L reporting

Add a `funding_paid` field to the trade records and daily stats.

### Estimated Effort: 10 hours

---

## Issue J: Margin and Liquidation

### Severity: MEDIUM

### Current Paper Behavior

**File:** `src/order/paper_executor.py` lines 195-199

```python
leverage = self.account.leverage_settings.get(symbol, 1)
margin = notional / leverage
self.account.balance -= margin + fee
```

Simple margin calculation. No maintenance margin, no liquidation price, balance can go negative.

### Expected Live Behavior

Bybit isolated margin includes initial margin + maintenance margin (tiered by position size). Liquidation occurs when unrealized loss exceeds `initial_margin - maintenance_margin`.

At 5x leverage, approximate liquidation distance: ~18% adverse price move.

With the current grid config: center price at 1.0, grid range ~1% (min_range_pct: 0.8%), all levels within ~0.4% of center. Liquidation at 18% is very far from grid range -- low risk.

### Impact If Not Fixed

- **At current scale**: Minimal risk. 20 USDT balance with 2% per level means 0.4 USDT margin per level, 2.0 USDT notional. Liquidation of any single level requires an 18% move.
- **At scale**: With 1000+ USDT balance and aggressive leverage, liquidation becomes possible during flash crashes.
- **Net position cascade**: In live mode with net positions, if 6 Buy levels are open, the combined position is 12 USDT notional. A 18% crash liquidates the entire combined position, not just the furthest level. Paper mode would show 6 independent liquidations at different prices.

### Resolution Plan

#### 1. Paper liquidation simulation

**File:** `src/order/paper_executor.py` -- add to `check_sl_tp()`

Calculate liquidation price for each position and check against candle data:

```python
def _liquidation_price(self, pos: PaperPosition) -> float:
    """Calculate approximate liquidation price for isolated margin."""
    margin_pct = 1.0 / pos.leverage
    maintenance_rate = 0.004  # 0.4% maintenance margin (Bybit tier 1)
    liq_pct = margin_pct - maintenance_rate
    if pos.side == "Buy":
        return pos.entry_price * (1 - liq_pct)
    else:
        return pos.entry_price * (1 + liq_pct)
```

#### 2. Pre-order margin check in live mode

Already covered in Issue F, Resolution Plan item 4.

#### 3. Maximum net exposure limit

**File:** `src/order/grid_manager.py` -- add check in `_handle_fill()`

Before opening a new level, verify that total net notional exposure doesn't exceed a configurable limit:

```python
max_exposure = self._initial_balance * max_total_exposure_pct / 100
current_exposure = sum(
    float(p["size"]) * float(p["entry_price"])
    for p in open_positions
    if p.get("strategy") == "grid_bias"
)
if current_exposure + (order_req.qty * level_price) > max_exposure:
    return GridUpdateMessage(action="FAILED", reason="exposure_limit")
```

The config already has `max_total_exposure_pct: 80.0` in `grid.yaml` line 39, but it's not checked in the grid manager (only in `GridSizingStrategy`).

### Estimated Effort: 10 hours

---

## Issue K: WebSocket Price Feed

### Severity: MEDIUM

### Current Paper Behavior

**File:** `src/order/process.py` lines 790-813

```python
def _refresh_ticker_prices(self, symbols: list[str]) -> None:
    now = time.time()
    if now - self._last_ticker_fetch < 5.0:
        return  # Only refresh every 5 seconds
    tickers = self._rest_client.get_tickers()  # REST call fetching ALL tickers
```

P3 monitors positions using REST ticker prices refreshed every 5 seconds. This is the price source for:
- Paper mode SL/TP checking in the monitor loop
- Trailing stop updates
- Unrealized P&L calculation

### Expected Live Behavior

For grid trading, 5-second price updates are adequate for monitoring (actual SL/TP execution is handled by exchange conditional orders in live mode). However:

1. The `get_tickers()` call fetches ALL linear tickers (~500+), wasting bandwidth and an API call every 5 seconds
2. For trailing stop management, 5-second granularity may miss rapid reversals
3. Grid fill detection in P2 already uses candle data (5-minute), not ticker data

### Impact If Not Fixed

- **Trailing stop latency**: In volatile conditions, price can move 1-2% in 5 seconds. The trailing stop may not trigger until the next refresh, by which time the price has reversed.
- **API waste**: Fetching all tickers every 5 seconds uses 12 API calls/minute. This competes with order placement calls for rate limit budget.
- **Stale prices**: If the REST call fails or takes >5 seconds, positions use entry_price as fallback (process.py line 787), which could be very stale.

### Resolution Plan

#### 1. WebSocket ticker subscription for active symbols

**File:** `src/order/process.py` -- replace REST polling with WebSocket

Use the private WebSocket endpoint for real-time position/order updates:
```
wss://stream.bybit.com/v5/private
```

Topics:
- `position` -- real-time position updates (entry price, P&L, liquidation price)
- `execution` -- real-time fill notifications
- `order` -- order status changes (SL/TP triggered)

This eliminates the need for REST ticker polling entirely. When an exchange SL/TP triggers, P3 receives a WebSocket notification within milliseconds.

#### 2. Selective ticker fetch (interim solution)

Instead of `get_tickers()` (all symbols), fetch only the symbols we care about:

```python
for symbol in symbols:
    ticker = self._rest_client.get_ticker(symbol)  # single-symbol endpoint
```

Or better, use the WebSocket public stream for ticker data on active symbols only.

### Estimated Effort: 12 hours

---

## Issue L: Order Types -- Market vs Limit

### Severity: LOW

### Current Paper Behavior

All grid fills use market orders:

**File:** `src/order/grid_manager.py` line 125

```python
result = await self._executor.place_market_order(...)
```

**File:** `src/order/paper_executor.py` line 171 -- `place_market_order()` applies slippage and fills instantly.

### Expected Live Behavior

Market orders are the simplest but most expensive order type:
- Taker fee: 0.06% (vs 0.01% maker fee for limit orders)
- Slippage: variable, always adverse (buy above mid, sell below mid)

For grid trading, the grid levels ARE known prices. Instead of waiting for price to cross a level and then placing a market order (which fills at a worse price), we could place limit orders at each grid level price in advance.

### Impact If Not Fixed

- Round-trip fee cost: 0.12% (taker + taker) vs 0.02% (maker + maker) = 0.10% savings per round trip
- With 0.55% spacing: net margin improves from 0.13% to 0.23% -- nearly doubling profitability
- Slippage eliminated: limit orders fill at exact price or better

### Resolution Plan

This is a significant architectural change. Limit orders change the fill model:

#### 1. Place limit orders at grid level prices

**File:** `src/collector/bybit_client.py` -- already supports `order_type="Limit"` in `place_order()`

**File:** `src/order/live_executor.py` -- add `place_limit_order()`:

```python
async def place_limit_order(
    self, symbol: str, side: str, qty: float, price: float
) -> dict:
    qty = self._round_qty(symbol, qty)
    price = self._round_price(symbol, price)
    result = await self._run_sync(
        self.client.place_order,
        symbol=symbol, side=side, qty=str(qty),
        order_type="Limit", price=str(price),
    )
    return result
```

#### 2. Monitor limit order fills via WebSocket

Subscribe to the `order` private WebSocket topic. When a limit order transitions to `Filled` status:
1. Update the grid level status to FILLED
2. Place the TP limit order for that level
3. Update the position tracker

#### 3. Hybrid approach: Limit for fills, market for closes

- Grid fill orders: limit orders at level prices (maker fee, no slippage)
- TP orders: limit orders at TP prices (maker fee, no slippage)
- Emergency closes (recenter, hard stop): market orders (guaranteed execution)

#### 4. Handle limit order management

Unlike market orders, limit orders can:
- Sit unfilled for extended periods (need cancellation logic during recenters)
- Partially fill (need partial fill tracking)
- Miss fills if price gaps through the level

### Estimated Effort: 24 hours (significant refactor)

---

## Phased Implementation Plan

### Phase 1: CRITICAL -- Must-fix Before Any Live Trade

**Total estimated effort: 41 hours (~1 week)**

| Priority | Issue | Description | Effort | Dependencies |
|----------|-------|-------------|--------|--------------|
| P1.1 | B | Fix `place_market_order` interface mismatch (add `current_price` param to LiveExecutor) | 1h | None |
| P1.2 | A | Create `LivePositionLedger` for micro-position tracking | 20h | P1.1 |
| P1.3 | A | Add `close_position_by_key()` to LiveExecutor using ledger | 8h | P1.2 |
| P1.4 | A | Integrate ledger into GridPositionManager | 8h | P1.3 |
| P1.5 | A | Virtual SL/TP: safety-net SL on net position only | 4h | P1.4 |

**Validation gate:** Run on Bybit testnet for 48 hours with 3 active grid symbols. Verify:
- [ ] No `TypeError` on grid fill
- [ ] Each grid level opens/closes independently
- [ ] Exchange net position qty matches sum of micro-positions
- [ ] P&L attribution per level is accurate
- [ ] Safety-net SL fires correctly if all levels go against

### Phase 2: HIGH -- Required for Stable Live Operation

**Total estimated effort: 48 hours (~1.5 weeks)**

| Priority | Issue | Description | Effort | Dependencies |
|----------|-------|-------------|--------|--------------|
| P2.1 | F.1 | Partial fill handler in GridPositionManager | 4h | Phase 1 |
| P2.2 | F.2 | Close order retry with backoff | 4h | Phase 1 |
| P2.3 | F.3 | Ghost fill detection and cleanup | 4h | Phase 1 |
| P2.4 | F.4 | Pre-order margin check | 2h | Phase 1 |
| P2.5 | G.1 | Persist GridPositionManager state to DB | 8h | Phase 1 |
| P2.6 | G.2 | Exchange reconciliation on P3 startup | 8h | P2.5 |
| P2.7 | E.1 | Pre-trade spread check (orderbook) | 4h | None |
| P2.8 | E.2 | Dynamic min_spacing_pct | 4h | P2.7 |
| P2.9 | D.1 | Use 1-minute candles for grid detection in live mode | 4h | None |
| P2.10 | E.3 | Increase paper slippage_bps to 25 | 0.5h | None |
| P2.11 | E.4 | Add slippage on SL fills in paper mode | 1.5h | None |
| P2.12 | A.5 | Periodic exchange position reconciliation (30s) | 4h | Phase 1 |

**Validation gate:** Run on Bybit testnet for 72 hours with 5 symbols. Verify:
- [ ] Partial fills are tracked with correct qty
- [ ] P3 crash and restart recovers all positions correctly
- [ ] No ghost fills after 72 hours
- [ ] Spread check correctly skips illiquid symbols
- [ ] 1-minute candle detection is timely

### Phase 3: MEDIUM -- Recommended for Production

**Total estimated effort: 44 hours (~1.5 weeks)**

| Priority | Issue | Description | Effort | Dependencies |
|----------|-------|-------------|--------|--------------|
| P3.1 | H.1 | Cache leverage settings | 2h | None |
| P3.2 | H.2 | Remove unnecessary ticker fetch in place_market_order | 2h | Phase 1 (B fix) |
| P3.3 | H.3 | Batch order endpoint for recenters | 8h | None |
| P3.4 | I.1 | Paper funding simulation | 6h | None |
| P3.5 | I.2 | Funding-aware grid filter | 4h | P3.4 |
| P3.6 | J.1 | Paper liquidation simulation | 6h | None |
| P3.7 | J.3 | Net exposure limit enforcement in grid manager | 4h | None |
| P3.8 | K.1 | WebSocket private stream for position/order updates | 12h | None |

**Validation gate:** Run on mainnet with minimum balance (20 USDT) for 1 week. Monitor:
- [ ] API rate usage stays below 50% of limit
- [ ] Recenters complete in <3 seconds with batch orders
- [ ] Funding charges match exchange records
- [ ] WebSocket provides real-time position updates

### Phase 4: LOW -- Nice to Have

**Total estimated effort: 36 hours**

| Priority | Issue | Description | Effort | Dependencies |
|----------|-------|-------------|--------|--------------|
| P4.1 | D.2 | WebSocket ticker-based grid fill detection | 12h | Phase 3 (K) |
| P4.2 | L | Limit orders for grid fills and TPs | 24h | P4.1 |

**Validation gate:** A/B test limit orders vs market orders for 1 week on a subset of symbols. Measure:
- [ ] Fill rate (limit orders may miss some fills)
- [ ] Net P&L per round trip (should improve by ~0.10%)
- [ ] Execution complexity (limit order lifecycle management)

---

## Testing Strategy

### 1. Unit Tests

- `LivePositionLedger`: micro-position CRUD, net qty calculation, reconciliation
- `LiveExecutor.close_position_by_key()`: correct P&L calculation, partial close
- `LiveExecutor.place_market_order()`: accepts `current_price` kwarg
- Partial fill handler: correct qty tracking
- Close retry logic: exponential backoff, max retries

### 2. Integration Tests (Bybit Testnet)

Bybit provides a testnet at `https://api-testnet.bybit.com` with the same API. Use testnet for:

- Full grid lifecycle: create grid, fill levels, hit TPs, recenter, close all
- P3 crash simulation: kill P3 mid-operation, restart, verify recovery
- Rate limit testing: burst 20 orders, verify throttling works
- Partial fill simulation: use limit orders with tiny qty to create partial fills

### 3. Shadow Mode (Mainnet)

Before going fully live, run in "shadow mode":
- Paper executor continues as primary (decisions based on paper)
- Live executor runs in parallel but with 1% of paper's qty (minimum lot size)
- Compare paper and live results after 1 week
- Verify P&L discrepancy is within acceptable range (< 0.5% daily)

### 4. Gradual Rollout

1. **Week 1**: 1 symbol, minimum qty, manual monitoring 24/7
2. **Week 2**: 3 symbols, minimum qty, automated monitoring with alerts
3. **Week 3**: 5 symbols, normal qty (2% per level), automated monitoring
4. **Week 4**: Full 8 symbols if metrics are stable

### 5. Kill Switch

Implement an emergency kill switch that:
1. Cancels all open orders on exchange
2. Closes all positions at market
3. Disables grid creation
4. Sends alert via configured channel (Telegram/Discord/email)

**File:** `src/order/process.py` -- add `_emergency_shutdown()` method triggered by:
- Account equity drops below configurable threshold
- More than N consecutive failed orders
- Manual trigger via control queue

---

## Summary

| Phase | Issues | Effort | Timeline |
|-------|--------|--------|----------|
| **Phase 1: CRITICAL** | A (Net Position), B (Interface), C (close_by_key) | 41h | Week 1-2 |
| **Phase 2: HIGH** | D (Fill Detection), E (Slippage), F (Error Handling), G (State Recovery) | 48h | Week 3-4 |
| **Phase 3: MEDIUM** | H (Rate Limits), I (Funding), J (Margin), K (WebSocket) | 44h | Week 5-6 |
| **Phase 4: LOW** | D.2 (WS Fills), L (Limit Orders) | 36h | Week 7+ |
| **TOTAL** | | **169 hours** | **~7 weeks** |

The most critical path item is the **Net Position Model** (Issue A). Until this is resolved, no live trading is possible. The interface mismatch (Issue B) is a quick fix that should be done first as it unblocks all other work. State recovery (Issue G) should closely follow as it prevents data loss during development and testing.

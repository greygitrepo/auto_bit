"""Bybit V5 REST API client wrapper.

Wraps pybit's unified_trading HTTP client for the Bybit V5 linear/USDT
perpetual market. Supports both authenticated (trading) and unauthenticated
(market-data-only) modes.
"""

from __future__ import annotations

import time
from functools import wraps
from typing import Any, Dict, List, Optional

from loguru import logger
from pybit.unified_trading import HTTP


class BybitAPIError(Exception):
    """Raised when a Bybit API call fails after all retries."""

    def __init__(self, message: str, ret_code: int | None = None, endpoint: str = ""):
        self.ret_code = ret_code
        self.endpoint = endpoint
        super().__init__(message)

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.ret_code is not None:
            parts.append(f"ret_code={self.ret_code}")
        if self.endpoint:
            parts.append(f"endpoint={self.endpoint}")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# Retry decorator
# ---------------------------------------------------------------------------

def _retry(max_retries: int = 3, backoff: float = 1.0):
    """Decorator that retries a method up to *max_retries* times with
    exponential back-off.  Re-raises the last exception as a
    :class:`BybitAPIError` if all attempts fail.
    """

    def decorator(fn):
        @wraps(fn)
        def wrapper(self, *args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(1, max_retries + 1):
                try:
                    return fn(self, *args, **kwargs)
                except BybitAPIError:
                    raise  # already wrapped – propagate immediately
                except Exception as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        wait = backoff * (2 ** (attempt - 1))
                        logger.warning(
                            "Attempt {}/{} for {} failed ({}). "
                            "Retrying in {:.1f}s ...",
                            attempt,
                            max_retries,
                            fn.__name__,
                            exc,
                            wait,
                        )
                        time.sleep(wait)
            raise BybitAPIError(
                f"{fn.__name__} failed after {max_retries} retries: {last_exc}",
                endpoint=fn.__name__,
            )

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class BybitClient:
    """Thin wrapper around :pymod:`pybit.unified_trading.HTTP` for the
    Bybit V5 linear USDT-perpetual market.

    Parameters
    ----------
    api_key:
        Bybit API key.  If *None* the client operates in
        **unauthenticated** (market-data-only) mode.
    api_secret:
        Bybit API secret.  Required when *api_key* is provided.
    """

    CATEGORY = "linear"

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> None:
        self._authenticated = api_key is not None and api_secret is not None
        self._last_request_time = 0.0
        self._min_request_interval = 0.1  # 100ms between requests (10 req/s)

        http_kwargs: Dict[str, Any] = {
            "testnet": False,
            "recv_window": 10000,  # 10 second timeout
        }
        if self._authenticated:
            http_kwargs["api_key"] = api_key
            http_kwargs["api_secret"] = api_secret

        self._http = HTTP(**http_kwargs)
        logger.info(
            "BybitClient initialised (authenticated={})", self._authenticated
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        """Simple rate limiting: ensure minimum interval between requests."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._min_request_interval:
            time.sleep(self._min_request_interval - elapsed)
        self._last_request_time = time.time()

    @staticmethod
    def _parse_response(response: dict, endpoint: str = "") -> Any:
        """Extract the ``result`` payload from a raw pybit response.

        Raises :class:`BybitAPIError` when *retCode* != 0.
        """
        ret_code = response.get("retCode", -1)
        if ret_code != 0:
            msg = response.get("retMsg", "Unknown error")
            logger.error("API error on {}: {} (code={})", endpoint, msg, ret_code)
            raise BybitAPIError(msg, ret_code=ret_code, endpoint=endpoint)
        return response.get("result")

    def _require_auth(self) -> None:
        """Guard for endpoints that need authentication."""
        if not self._authenticated:
            raise BybitAPIError(
                "This operation requires an authenticated client "
                "(provide api_key and api_secret).",
                endpoint="auth_check",
            )

    # ==================================================================
    # Market-data endpoints (no auth required)
    # ==================================================================

    @_retry()
    def get_tickers(self) -> List[Dict[str, Any]]:
        """Fetch all linear USDT tickers.

        Returns a list of dicts with keys including:
        ``symbol``, ``lastPrice``, ``price24hPcnt``, ``volume24h``,
        ``turnover24h``, ``fundingRate``, ``nextFundingTime``, etc.
        """
        self._throttle()
        logger.debug("get_tickers: fetching all linear tickers")
        raw = self._http.get_tickers(category=self.CATEGORY)
        result = self._parse_response(raw, "get_tickers")
        tickers = result.get("list", [])
        logger.debug("get_tickers: received {} tickers", len(tickers))
        return tickers

    @_retry()
    def get_instruments_info(self) -> List[Dict[str, Any]]:
        """Fetch instrument metadata for all linear USDT pairs.

        Each item contains ``symbol``, ``baseCoin``, ``quoteCoin``,
        ``launchTime``, ``status``, ``lotSizeFilter``, ``priceFilter``,
        ``leverageFilter``, etc.
        """
        logger.debug("get_instruments_info: fetching all linear instruments")
        all_instruments: List[Dict[str, Any]] = []
        cursor: str | None = None

        while True:
            kwargs: Dict[str, Any] = {"category": self.CATEGORY}
            if cursor:
                kwargs["cursor"] = cursor

            raw = self._http.get_instruments_info(**kwargs)
            result = self._parse_response(raw, "get_instruments_info")
            items = result.get("list", [])
            all_instruments.extend(items)

            cursor = result.get("nextPageCursor")
            if not cursor:
                break

        logger.debug(
            "get_instruments_info: received {} instruments", len(all_instruments)
        )
        return all_instruments

    @_retry()
    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 200,
    ) -> List[Dict[str, str]]:
        """Fetch OHLCV kline/candle data.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTCUSDT"``.
        interval:
            Kline interval – ``"1"``, ``"3"``, ``"5"``, ``"15"``, ``"30"``,
            ``"60"``, ``"120"``, ``"240"``, ``"360"``, ``"720"``, ``"D"``,
            ``"W"``, ``"M"``.
        limit:
            Number of candles to return (max 1000, default 200).

        Returns
        -------
        List of dicts with keys: ``startTime``, ``open``, ``high``, ``low``,
        ``close``, ``volume``, ``turnover``.  Ordered oldest-first.
        """
        self._throttle()
        logger.debug(
            "get_klines: symbol={} interval={} limit={}", symbol, interval, limit
        )
        raw = self._http.get_kline(
            category=self.CATEGORY,
            symbol=symbol,
            interval=interval,
            limit=limit,
        )
        result = self._parse_response(raw, "get_klines")
        # Bybit returns newest-first; reverse to oldest-first.
        raw_list: list = result.get("list", [])
        klines = [
            {
                "startTime": k[0],
                "open": k[1],
                "high": k[2],
                "low": k[3],
                "close": k[4],
                "volume": k[5],
                "turnover": k[6],
            }
            for k in reversed(raw_list)
        ]
        logger.debug("get_klines: received {} candles for {}", len(klines), symbol)
        return klines

    @_retry()
    def get_funding_rate(self, symbol: str) -> List[Dict[str, Any]]:
        """Fetch recent funding-rate history for *symbol*.

        Returns a list of dicts with keys: ``symbol``, ``fundingRate``,
        ``fundingRateTimestamp``.
        """
        logger.debug("get_funding_rate: symbol={}", symbol)
        raw = self._http.get_funding_rate_history(
            category=self.CATEGORY,
            symbol=symbol,
            limit=1,
        )
        result = self._parse_response(raw, "get_funding_rate")
        entries = result.get("list", [])
        logger.debug(
            "get_funding_rate: received {} entries for {}", len(entries), symbol
        )
        return entries

    # ==================================================================
    # Account / position endpoints (auth required)
    # ==================================================================

    @_retry()
    def get_wallet_balance(self) -> Dict[str, Any]:
        """Return the USDT unified-trading-account balance.

        Returns a dict with keys such as ``totalEquity``,
        ``totalWalletBalance``, ``totalAvailableBalance``, ``coin`` (list),
        etc.
        """
        self._require_auth()
        logger.debug("get_wallet_balance: fetching USDT balance")
        raw = self._http.get_wallet_balance(accountType="UNIFIED")
        result = self._parse_response(raw, "get_wallet_balance")

        accounts = result.get("list", [])
        if not accounts:
            raise BybitAPIError(
                "No account data returned", endpoint="get_wallet_balance"
            )

        account = accounts[0]
        # Extract the USDT coin entry for convenience.
        coins = account.get("coin", [])
        usdt_coin = next((c for c in coins if c.get("coin") == "USDT"), None)
        return {
            "totalEquity": account.get("totalEquity"),
            "totalWalletBalance": account.get("totalWalletBalance"),
            "totalAvailableBalance": account.get("totalAvailableBalance"),
            "totalMarginBalance": account.get("totalMarginBalance"),
            "totalUnrealisedPnl": account.get("totalInitialMargin"),
            "usdt": usdt_coin,
        }

    @_retry()
    def get_positions(
        self, symbol: str | None = None
    ) -> List[Dict[str, Any]]:
        """Fetch open positions.

        Parameters
        ----------
        symbol:
            If provided, return only the position for that symbol.
            Otherwise return all linear positions.
        """
        self._require_auth()
        logger.debug("get_positions: symbol={}", symbol)
        kwargs: Dict[str, Any] = {"category": self.CATEGORY}
        if symbol:
            kwargs["symbol"] = symbol
        else:
            kwargs["settleCoin"] = "USDT"

        raw = self._http.get_positions(**kwargs)
        result = self._parse_response(raw, "get_positions")
        positions = result.get("list", [])
        # Filter out entries with zero size (no open position).
        open_positions = [
            p for p in positions if p.get("size", "0") != "0"
        ]
        logger.debug("get_positions: {} open position(s)", len(open_positions))
        return open_positions

    # ==================================================================
    # Order endpoints (auth required)
    # ==================================================================

    @_retry()
    def place_order(
        self,
        symbol: str,
        side: str,
        qty: str,
        order_type: str = "Market",
    ) -> Dict[str, Any]:
        """Place a new order.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTCUSDT"``.
        side:
            ``"Buy"`` or ``"Sell"``.
        qty:
            Order quantity as a string (Bybit convention).
        order_type:
            ``"Market"`` (default) or ``"Limit"``.

        Returns
        -------
        Dict with ``orderId``, ``orderLinkId``.
        """
        self._require_auth()
        logger.debug(
            "place_order: symbol={} side={} qty={} type={}",
            symbol,
            side,
            qty,
            order_type,
        )
        raw = self._http.place_order(
            category=self.CATEGORY,
            symbol=symbol,
            side=side,
            qty=qty,
            orderType=order_type,
        )
        result = self._parse_response(raw, "place_order")
        logger.debug("place_order: result={}", result)
        return result

    @_retry()
    def place_conditional_order(
        self,
        symbol: str,
        side: str,
        qty: str,
        trigger_price: str,
        order_type: str = "Market",
    ) -> Dict[str, Any]:
        """Place a conditional (stop / take-profit) order.

        Parameters
        ----------
        symbol:
            Trading pair.
        side:
            ``"Buy"`` or ``"Sell"``.
        qty:
            Order quantity as a string.
        trigger_price:
            The mark-price at which the order triggers.
        order_type:
            ``"Market"`` (default) or ``"Limit"``.

        Returns
        -------
        Dict with ``orderId``, ``orderLinkId``.
        """
        self._require_auth()
        logger.debug(
            "place_conditional_order: symbol={} side={} qty={} "
            "trigger={} type={}",
            symbol,
            side,
            qty,
            trigger_price,
            order_type,
        )
        # Determine trigger direction: triggerBy uses mark price.
        # triggerDirection: 1 = rise above, 2 = fall below.
        # Buy-side stop orders trigger when price rises; sell-side when it
        # falls.  The caller is responsible for choosing the correct side.
        trigger_direction = "1" if side == "Buy" else "2"

        raw = self._http.place_order(
            category=self.CATEGORY,
            symbol=symbol,
            side=side,
            qty=qty,
            orderType=order_type,
            triggerPrice=trigger_price,
            triggerBy="MarkPrice",
            triggerDirection=trigger_direction,
        )
        result = self._parse_response(raw, "place_conditional_order")
        logger.debug("place_conditional_order: result={}", result)
        return result

    @_retry()
    def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """Cancel an active order.

        Parameters
        ----------
        symbol:
            Trading pair.
        order_id:
            The ``orderId`` returned by :meth:`place_order`.

        Returns
        -------
        Dict with ``orderId``.
        """
        self._require_auth()
        logger.debug("cancel_order: symbol={} order_id={}", symbol, order_id)
        raw = self._http.cancel_order(
            category=self.CATEGORY,
            symbol=symbol,
            orderId=order_id,
        )
        result = self._parse_response(raw, "cancel_order")
        logger.debug("cancel_order: result={}", result)
        return result

    @_retry()
    def get_open_orders(
        self, symbol: str | None = None
    ) -> List[Dict[str, Any]]:
        """Fetch currently open (active) orders.

        Parameters
        ----------
        symbol:
            Filter by trading pair.  If *None*, returns all open linear
            orders.
        """
        self._require_auth()
        logger.debug("get_open_orders: symbol={}", symbol)
        kwargs: Dict[str, Any] = {"category": self.CATEGORY}
        if symbol:
            kwargs["symbol"] = symbol
        else:
            kwargs["settleCoin"] = "USDT"

        raw = self._http.get_open_orders(**kwargs)
        result = self._parse_response(raw, "get_open_orders")
        orders = result.get("list", [])
        logger.debug("get_open_orders: {} order(s)", len(orders))
        return orders

    @_retry()
    def get_order_history(
        self,
        symbol: str | None = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Fetch historical (closed / cancelled) orders.

        Parameters
        ----------
        symbol:
            Filter by trading pair.
        limit:
            Maximum number of records (default 50, max 50).
        """
        self._require_auth()
        logger.debug("get_order_history: symbol={} limit={}", symbol, limit)
        kwargs: Dict[str, Any] = {"category": self.CATEGORY, "limit": limit}
        if symbol:
            kwargs["symbol"] = symbol

        raw = self._http.get_order_history(**kwargs)
        result = self._parse_response(raw, "get_order_history")
        orders = result.get("list", [])
        logger.debug("get_order_history: {} order(s)", len(orders))
        return orders

    # ==================================================================
    # Leverage / margin configuration (auth required)
    # ==================================================================

    @_retry()
    def set_leverage(self, symbol: str, leverage: str) -> Dict[str, Any]:
        """Set the leverage for a symbol.

        Parameters
        ----------
        symbol:
            Trading pair, e.g. ``"BTCUSDT"``.
        leverage:
            Leverage multiplier as a string, e.g. ``"10"``.

        Returns
        -------
        Empty dict on success (Bybit returns no body).
        """
        self._require_auth()
        logger.debug("set_leverage: symbol={} leverage={}", symbol, leverage)
        raw = self._http.set_leverage(
            category=self.CATEGORY,
            symbol=symbol,
            buyLeverage=leverage,
            sellLeverage=leverage,
        )
        result = self._parse_response(raw, "set_leverage")
        logger.debug("set_leverage: done for {}", symbol)
        return result if result else {}

    @_retry()
    def set_margin_mode(
        self, symbol: str, mode: str = "ISOLATED_MARGIN", leverage: int = 1
    ) -> Dict[str, Any]:
        """Switch a symbol's margin mode.

        Parameters
        ----------
        symbol:
            Trading pair.
        mode:
            ``"ISOLATED_MARGIN"`` (default) or ``"CROSS_MARGIN"``.
        leverage:
            Leverage multiplier to set alongside the margin switch (default 1).

        Returns
        -------
        Empty dict on success.
        """
        self._require_auth()
        # Bybit API uses tradeMode: 0 = cross, 1 = isolated
        trade_mode = 1 if "ISOLATED" in mode.upper() else 0
        logger.debug(
            "set_margin_mode: symbol={} mode={} (tradeMode={}) leverage={}",
            symbol,
            mode,
            trade_mode,
            leverage,
        )
        raw = self._http.switch_margin_mode(
            category=self.CATEGORY,
            symbol=symbol,
            tradeMode=trade_mode,
            buyLeverage=str(leverage),
            sellLeverage=str(leverage),
        )
        result = self._parse_response(raw, "set_margin_mode")
        logger.debug("set_margin_mode: done for {}", symbol)
        return result if result else {}

    # ==================================================================
    # Execution / fill endpoints (auth required)
    # ==================================================================

    @_retry()
    def get_executions(
        self, symbol: str, order_id: str = None, limit: int = 20
    ) -> List[Dict[str, str]]:
        """Fetch execution/fill details for recent trades.

        Used to get actual fill prices after placing market orders.
        Bybit V5 endpoint: /v5/execution/list
        """
        self._require_auth()
        params: Dict[str, Any] = {
            "category": self.CATEGORY,
            "symbol": symbol,
            "limit": limit,
        }
        if order_id:
            params["orderId"] = order_id

        self._throttle()
        logger.debug("get_executions: symbol={} order_id={} limit={}", symbol, order_id, limit)
        raw = self._http.get_executions(**params)
        result = self._parse_response(raw, "get_executions")
        executions = result.get("list", [])
        logger.debug("get_executions: {} fill(s)", len(executions))
        return executions

    @_retry()
    def get_instrument_info(self, symbol: str) -> Dict[str, Any]:
        """Fetch instrument details including lotSizeFilter and priceFilter.

        Returns dict with keys like ``lotSizeFilter``, ``priceFilter``, etc.
        """
        logger.debug("get_instrument_info: symbol={}", symbol)
        raw = self._http.get_instruments_info(
            category=self.CATEGORY, symbol=symbol
        )
        result = self._parse_response(raw, "get_instrument_info")
        items = result.get("list", [])
        if items and len(items) > 0:
            return items[0]
        return {}

    @_retry()
    def get_closed_pnl(
        self, symbol: str, limit: int = 20
    ) -> List[Dict[str, str]]:
        """Fetch closed P&L records from the exchange."""
        self._require_auth()
        logger.debug("get_closed_pnl: symbol={} limit={}", symbol, limit)
        raw = self._http.get_closed_pnl(
            category=self.CATEGORY, symbol=symbol, limit=limit
        )
        result = self._parse_response(raw, "get_closed_pnl")
        records = result.get("list", [])
        logger.debug("get_closed_pnl: {} record(s)", len(records))
        return records

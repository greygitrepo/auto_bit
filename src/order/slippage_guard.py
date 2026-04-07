"""Dynamic Slippage Guard — estimates real-time slippage and gates unprofitable orders.

Addresses Gap C from parity_analysis.md: paper mode uses fixed slippage_bps=15,
but real slippage on new listing altcoins can be 25-100+ bps.
"""

from __future__ import annotations

from loguru import logger


class SlippageGuard:
    """Estimates real-time slippage and gates orders that would be unprofitable."""

    SAFETY_MARGIN = 1.2  # 20% buffer on min spacing calculation

    def __init__(self, config: dict):
        self.base_slippage_bps: float = config.get("slippage_bps", 15)
        self.max_slippage_bps: float = config.get("max_slippage_bps", 50)
        self.fee_rate: float = config.get("fee_rate", {}).get("taker", 0.0006)

    def estimate_slippage_bps(
        self, symbol: str, qty: float, orderbook: dict | None = None,
    ) -> float:
        """Estimate slippage in bps for a given order size.

        If orderbook is provided (dict with 'bids' and 'asks' as list of [price, qty]):
            Walk the ask side to calculate market impact for a buy of *qty*.
        Otherwise:
            Return base_slippage_bps (conservative fallback).

        Returns
        -------
        Estimated slippage in basis points, capped at max_slippage_bps.
        """
        if orderbook is None:
            return float(self.base_slippage_bps)

        asks = orderbook.get("asks", [])
        bids = orderbook.get("bids", [])

        if not asks or not bids:
            return float(self.base_slippage_bps)

        # Mid price as reference
        best_ask = float(asks[0][0])
        best_bid = float(bids[0][0])
        mid_price = (best_ask + best_bid) / 2.0

        if mid_price <= 0:
            return float(self.base_slippage_bps)

        # Walk the ask side to find the volume-weighted average fill price
        remaining = qty
        total_cost = 0.0

        for level in asks:
            price = float(level[0])
            available = float(level[1])
            fill_qty = min(remaining, available)
            total_cost += price * fill_qty
            remaining -= fill_qty
            if remaining <= 0:
                break

        if remaining > 0:
            # Not enough liquidity — cap at max
            return float(self.max_slippage_bps)

        vwap = total_cost / qty
        slippage_pct = (vwap - mid_price) / mid_price
        slippage_bps = slippage_pct * 10_000

        return min(max(slippage_bps, 0.0), float(self.max_slippage_bps))

    def check_profitability(
        self,
        symbol: str,
        grid_spacing_pct: float,
        estimated_slippage_bps: float,
    ) -> dict:
        """Check if a grid trade would be profitable after slippage + fees.

        Round-trip cost = 2 * (slippage_pct + fee_rate_pct)
        Profitable if grid_spacing_pct > round_trip_cost_pct

        Parameters
        ----------
        symbol:
            Trading pair (for logging).
        grid_spacing_pct:
            Grid spacing as a percentage (e.g. 1.0 = 1%).
        estimated_slippage_bps:
            Estimated slippage in basis points.

        Returns
        -------
        Dict with keys: profitable, round_trip_cost_pct, net_margin_pct, reason.
        """
        slippage_pct = estimated_slippage_bps / 100.0  # bps → percentage
        fee_pct = self.fee_rate * 100.0  # decimal → percentage

        round_trip_cost_pct = 2 * (slippage_pct + fee_pct)
        net_margin_pct = grid_spacing_pct - round_trip_cost_pct
        profitable = net_margin_pct > 0

        if profitable:
            reason = f"profitable: spacing {grid_spacing_pct:.3f}% > cost {round_trip_cost_pct:.3f}%"
        else:
            reason = (
                f"unprofitable: spacing {grid_spacing_pct:.3f}% <= "
                f"round-trip cost {round_trip_cost_pct:.3f}% "
                f"(slippage={slippage_pct:.3f}% + fee={fee_pct:.3f}% x2)"
            )

        return {
            "profitable": profitable,
            "round_trip_cost_pct": round_trip_cost_pct,
            "net_margin_pct": net_margin_pct,
            "reason": reason,
        }

    def adjust_min_spacing(self, estimated_slippage_bps: float) -> float:
        """Calculate dynamic min_spacing_pct based on estimated slippage.

        Formula: min_spacing = 2 * (slippage_pct + fee_rate_pct) * safety_margin

        Returns
        -------
        Recommended minimum spacing as a percentage (e.g. 0.504 means 0.504%).
        """
        slippage_pct = estimated_slippage_bps / 100.0  # bps → percentage
        fee_pct = self.fee_rate * 100.0  # decimal → percentage

        return 2 * (slippage_pct + fee_pct) * self.SAFETY_MARGIN

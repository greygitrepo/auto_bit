"""
IPC message dataclasses for multiprocessing Queue communication.

All messages are plain dataclasses so they can be pickled and sent through
:class:`multiprocessing.Queue` without extra serialisation work.

Usage:
    from src.utils.messages import SignalMessage

    msg = SignalMessage(
        symbol="BTCUSDT",
        signal="LONG",
        entry_price=65000.0,
        stop_loss=64000.0,
        take_profit=67000.0,
        strategy="trend_follow",
        confidence=0.85,
        scanner_direction="bull",
        suggested_side="LONG",
        reason="EMA crossover confirmed by volume",
    )
    queue.put(msg)
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class MarketDataMessage:
    """A single candle update pushed from the data collector."""

    symbol: str
    timeframe: str
    candle: Dict[str, Any]  # {open, high, low, close, volume, timestamp}
    timestamp: float = field(default_factory=time.time)
    msg_type: str = field(default="market_data", init=False)


@dataclass
class SignalMessage:
    """Trading signal emitted by a strategy process."""

    symbol: str
    signal: str  # LONG | SHORT | CLOSE | HOLD
    entry_price: float
    stop_loss: float
    take_profit: float
    strategy: str
    confidence: float
    scanner_direction: str  # bull | bear | mixed
    suggested_side: str  # LONG | SHORT | NEUTRAL
    reason: str
    timestamp: float = field(default_factory=time.time)
    msg_type: str = field(default="signal", init=False)


@dataclass
class PositionUpdateMessage:
    """Snapshot of current positions and P&L from the order/tracker process."""

    positions: List[Dict[str, Any]]
    daily_pnl: float
    balance: float
    trade_count: int
    consecutive_losses: int
    timestamp: float = field(default_factory=time.time)
    msg_type: str = field(default="position_update", init=False)


@dataclass
class ControlMessage:
    """Control command sent between processes (e.g. start, stop, pause)."""

    command: str  # start | stop | pause | scan | subscribe | unsubscribe
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    msg_type: str = field(default="control", init=False)


@dataclass
class SlotAvailableMessage:
    """Notification that trading slots are available for new positions."""

    available_slots: int
    current_positions: List[str]  # symbols currently held
    timestamp: float = field(default_factory=time.time)
    msg_type: str = field(default="slot_available", init=False)


@dataclass
class ScanResultMessage:
    """Results of a market scan with overall direction assessment."""

    results: List[Dict[str, Any]]  # list of ScanResult dicts
    market_direction: str  # bull | bear | mixed
    timestamp: float = field(default_factory=time.time)
    msg_type: str = field(default="scan_result", init=False)

"""SQLite database manager for auto_bit.

Provides thread/process-safe connection management with WAL mode,
automatic schema initialization, and CRUD helpers for all core tables.

Each caller receives its own connection via threading.local(), so the
manager is safe to use from multiple threads without external locking.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from loguru import logger

# ---------------------------------------------------------------------------
# Project root is three levels up from this file:
#   src/utils/db.py  ->  project root
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config" / "app.yaml"


def _load_db_path_from_config() -> str:
    """Read database.path from config, respecting --config-dir override."""
    try:
        import yaml  # noqa: WPS433 (nested import to keep yaml optional at module level)
        import os

        # Check for config dir override (set by --config-dir or env var)
        config_dir = os.environ.get("AUTO_BIT_CONFIG_DIR")
        if config_dir:
            config_path = Path(config_dir) / "app.yaml"
        else:
            config_path = _DEFAULT_CONFIG_PATH

        with open(config_path) as fh:
            cfg = yaml.safe_load(fh)
        rel = cfg.get("database", {}).get("path", "data/auto_bit.db")

        # If path is absolute, use as-is; otherwise resolve relative to project root
        if Path(rel).is_absolute():
            return str(rel)
    except Exception:
        rel = "data/auto_bit.db"
    return str(_PROJECT_ROOT / rel)


# ── Schema DDL ─────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS candles (
    symbol      TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,
    timestamp   INTEGER NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      REAL    NOT NULL,
    PRIMARY KEY (symbol, timeframe, timestamp)
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mode          TEXT    NOT NULL CHECK (mode IN ('paper', 'live')),
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL CHECK (side IN ('Buy', 'Sell')),
    size          REAL    NOT NULL,
    entry_price   REAL    NOT NULL,
    exit_price    REAL,
    pnl           REAL,
    fee           REAL,
    leverage      INTEGER NOT NULL DEFAULT 1,
    strategy      TEXT,
    entry_time    INTEGER NOT NULL,
    exit_time     INTEGER,
    entry_reason  TEXT,
    exit_reason   TEXT,
    exit_type     TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mode                TEXT    NOT NULL CHECK (mode IN ('paper', 'live')),
    symbol              TEXT    NOT NULL,
    side                TEXT    NOT NULL CHECK (side IN ('Buy', 'Sell')),
    size                REAL    NOT NULL,
    entry_price         REAL    NOT NULL,
    leverage            INTEGER NOT NULL DEFAULT 1,
    stop_loss           REAL,
    take_profit         REAL,
    margin              REAL,
    unrealized_pnl      REAL,
    strategy            TEXT,
    scanner_direction   TEXT,
    entered_at          INTEGER NOT NULL,
    max_hold_minutes    INTEGER NOT NULL DEFAULT 90,
    sl_order_id         TEXT DEFAULT '',
    tp_order_id         TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS daily_performance (
    date              TEXT    NOT NULL,
    mode              TEXT    NOT NULL CHECK (mode IN ('paper', 'live')),
    starting_balance  REAL    NOT NULL,
    ending_balance    REAL    NOT NULL,
    pnl               REAL    NOT NULL DEFAULT 0,
    trade_count       INTEGER NOT NULL DEFAULT 0,
    win_count         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, mode)
);

CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT    PRIMARY KEY,
    value       TEXT,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS grid_state (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mode            TEXT    NOT NULL CHECK (mode IN ('paper', 'live')),
    symbol          TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'paused', 'stopped')),
    center_price    REAL    NOT NULL,
    grid_range      REAL    NOT NULL,
    grid_spacing    REAL    NOT NULL,
    num_buy_levels  INTEGER NOT NULL,
    num_sell_levels INTEGER NOT NULL,
    bias            TEXT    NOT NULL DEFAULT 'NEUTRAL',
    bias_magnitude  REAL    NOT NULL DEFAULT 0.0,
    leverage        INTEGER NOT NULL DEFAULT 5,
    qty_per_level   REAL    NOT NULL,
    total_margin    REAL    NOT NULL DEFAULT 0.0,
    realized_pnl    REAL    NOT NULL DEFAULT 0.0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    UNIQUE (mode, symbol)
);

CREATE TABLE IF NOT EXISTS grid_levels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_state_id   INTEGER NOT NULL REFERENCES grid_state(id),
    level_index     INTEGER NOT NULL,
    price           REAL    NOT NULL,
    side            TEXT    NOT NULL CHECK (side IN ('Buy', 'Sell')),
    status          TEXT    NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING', 'FILLED', 'TP_SET', 'COMPLETED', 'CANCELLED')),
    tp_price        REAL,
    fill_price      REAL,
    fill_time       INTEGER,
    tp_fill_price   REAL,
    tp_fill_time    INTEGER,
    pnl             REAL    DEFAULT 0.0,
    fee             REAL    DEFAULT 0.0,
    position_id     INTEGER,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
"""

# ── Index DDL (created once alongside tables) ─────────────────────────────

_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_candles_symbol_tf
    ON candles (symbol, timeframe, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_trades_mode
    ON trades (mode, entry_time DESC);

CREATE INDEX IF NOT EXISTS idx_positions_mode
    ON positions (mode);

CREATE INDEX IF NOT EXISTS idx_grid_levels_state
    ON grid_levels (grid_state_id, status);

CREATE INDEX IF NOT EXISTS idx_grid_state_mode
    ON grid_state (mode, symbol);
"""


# ── Database Manager ──────────────────────────────────────────────────────

class DatabaseManager:
    """Thread-safe SQLite database manager.

    Parameters
    ----------
    db_path:
        Explicit path to the SQLite file.  When *None*, the path is read
        from ``config/app.yaml`` (``database.path``).

    Usage
    -----
    ::

        db = DatabaseManager()

        # simple call
        db.insert_candle("BTCUSDT", "5", 170000000, 100.0, 105.0, 99.0, 102.0, 500.0)

        # context-manager for explicit transaction control
        with db.transaction() as cur:
            cur.execute("INSERT INTO system_state VALUES (?, ?, ?)", ("k", "v", 0))
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or _load_db_path_from_config()
        self._local = threading.local()

        # Ensure the parent directory exists.
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        # Initialise schema using a temporary connection so the first
        # real caller does not pay the cost.
        self._init_schema()
        logger.info("Database ready at {}", self._db_path)

    # ── connection helpers ────────────────────────────────────────────────

    def _get_connection(self) -> sqlite3.Connection:
        """Return a per-thread connection, creating one if necessary."""
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
            logger.debug("Opened new SQLite connection on thread {}", threading.current_thread().name)
        return conn

    def _init_schema(self) -> None:
        """Create tables and indices if they do not already exist."""
        conn = self._get_connection()
        conn.executescript(_SCHEMA_SQL)
        conn.executescript(_INDEX_SQL)
        conn.commit()

        # Migrations: add columns that may not exist in older databases
        try:
            conn.execute("ALTER TABLE positions ADD COLUMN sl_order_id TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            conn.execute("ALTER TABLE positions ADD COLUMN tp_order_id TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # Column already exists
        conn.commit()

        logger.debug("Schema initialised")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """Yield a cursor inside an explicit transaction.

        Commits on clean exit, rolls back on exception.
        """
        conn = self._get_connection()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self) -> None:
        """Close the connection owned by the calling thread."""
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
            logger.debug("Closed SQLite connection on thread {}", threading.current_thread().name)

    # ── candles ───────────────────────────────────────────────────────────

    def insert_candle(
        self,
        symbol: str,
        timeframe: str,
        timestamp: int,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> None:
        """Insert or replace a single candle row."""
        conn = self._get_connection()
        conn.execute(
            """
            INSERT OR REPLACE INTO candles
                (symbol, timeframe, timestamp, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (symbol, timeframe, timestamp, open_, high, low, close, volume),
        )
        conn.commit()

    def insert_candles_bulk(
        self,
        rows: list[tuple[str, str, int, float, float, float, float, float]],
    ) -> int:
        """Bulk-insert candles. Returns the number of rows inserted."""
        if not rows:
            return 0
        conn = self._get_connection()
        conn.executemany(
            """
            INSERT OR REPLACE INTO candles
                (symbol, timeframe, timestamp, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        logger.debug("Bulk-inserted {} candles", len(rows))
        return len(rows)

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
    ) -> list[sqlite3.Row]:
        """Return the most recent *limit* candles, ordered oldest-first."""
        conn = self._get_connection()
        cur = conn.execute(
            """
            SELECT * FROM candles
            WHERE symbol = ? AND timeframe = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (symbol, timeframe, limit),
        )
        rows = cur.fetchall()
        rows.reverse()  # oldest first
        return rows

    # ── trades ────────────────────────────────────────────────────────────

    def insert_trade(self, **kwargs: Any) -> int:
        """Insert a trade and return the new row id.

        Accepted keyword arguments match the ``trades`` table columns
        (excluding ``id``).
        """
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join(["?"] * len(kwargs))
        conn = self._get_connection()
        cur = conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
            tuple(kwargs.values()),
        )
        conn.commit()
        logger.info("Recorded trade id={} symbol={} side={}", cur.lastrowid, kwargs.get("symbol"), kwargs.get("side"))
        return cur.lastrowid  # type: ignore[return-value]

    def get_trades(
        self,
        mode: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        """Return trades for the given mode, newest first."""
        conn = self._get_connection()
        cur = conn.execute(
            """
            SELECT * FROM trades
            WHERE mode = ?
            ORDER BY entry_time DESC
            LIMIT ? OFFSET ?
            """,
            (mode, limit, offset),
        )
        return cur.fetchall()

    # ── positions ─────────────────────────────────────────────────────────

    def insert_position(self, **kwargs: Any) -> int:
        """Insert a new open position and return its id."""
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join(["?"] * len(kwargs))
        conn = self._get_connection()
        cur = conn.execute(
            f"INSERT INTO positions ({cols}) VALUES ({placeholders})",
            tuple(kwargs.values()),
        )
        conn.commit()
        logger.info("Opened position id={} symbol={}", cur.lastrowid, kwargs.get("symbol"))
        return cur.lastrowid  # type: ignore[return-value]

    def update_position(self, position_id: int, **kwargs: Any) -> None:
        """Update fields on an existing position."""
        if not kwargs:
            return
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        conn = self._get_connection()
        conn.execute(
            f"UPDATE positions SET {set_clause} WHERE id = ?",
            (*kwargs.values(), position_id),
        )
        conn.commit()

    def delete_position(self, position_id: int) -> None:
        """Delete a position row (typically after closing it)."""
        conn = self._get_connection()
        conn.execute("DELETE FROM positions WHERE id = ?", (position_id,))
        conn.commit()
        logger.info("Deleted position id={}", position_id)

    def get_open_positions(self, mode: str) -> list[sqlite3.Row]:
        """Return all open positions for the given mode."""
        conn = self._get_connection()
        cur = conn.execute(
            "SELECT * FROM positions WHERE mode = ? ORDER BY entered_at DESC",
            (mode,),
        )
        return cur.fetchall()

    # ── daily performance ─────────────────────────────────────────────────

    def upsert_daily_performance(self, **kwargs: Any) -> None:
        """Insert or update a daily_performance row.

        Required keys: ``date``, ``mode``.
        """
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join(["?"] * len(kwargs))
        update_clause = ", ".join(f"{k} = excluded.{k}" for k in kwargs if k not in ("date", "mode"))
        conn = self._get_connection()
        conn.execute(
            f"""
            INSERT INTO daily_performance ({cols}) VALUES ({placeholders})
            ON CONFLICT (date, mode) DO UPDATE SET {update_clause}
            """,
            tuple(kwargs.values()),
        )
        conn.commit()

    def get_daily_performance(self, date: str, mode: str) -> Optional[sqlite3.Row]:
        """Return the daily performance row for a given date and mode, or None."""
        conn = self._get_connection()
        cur = conn.execute(
            "SELECT * FROM daily_performance WHERE date = ? AND mode = ?",
            (date, mode),
        )
        return cur.fetchone()

    # ── system state (key-value) ──────────────────────────────────────────

    def set_state(self, key: str, value: str) -> None:
        """Set a system_state key to the given value (upsert)."""
        now = int(time.time())
        conn = self._get_connection()
        conn.execute(
            """
            INSERT INTO system_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                            updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
        conn.commit()

    def get_state(self, key: str) -> Optional[str]:
        """Return the value for a system_state key, or None if missing."""
        conn = self._get_connection()
        cur = conn.execute(
            "SELECT value FROM system_state WHERE key = ?",
            (key,),
        )
        row = cur.fetchone()
        return row["value"] if row else None

    # ── grid state ───────────────────────────────────────────────────────

    def upsert_grid_state(self, **kwargs: Any) -> int:
        """Insert or update a grid_state row. Returns the row id."""
        mode = kwargs.get("mode", "paper")
        symbol = kwargs.get("symbol", "")
        conn = self._get_connection()
        # Check if exists
        cur = conn.execute(
            "SELECT id FROM grid_state WHERE mode = ? AND symbol = ?",
            (mode, symbol),
        )
        row = cur.fetchone()
        if row:
            grid_id = row["id"]
            update_cols = {k: v for k, v in kwargs.items() if k not in ("id",)}
            set_clause = ", ".join(f"{k} = ?" for k in update_cols)
            conn.execute(
                f"UPDATE grid_state SET {set_clause} WHERE id = ?",
                (*update_cols.values(), grid_id),
            )
            conn.commit()
            return grid_id
        else:
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join(["?"] * len(kwargs))
            cur = conn.execute(
                f"INSERT INTO grid_state ({cols}) VALUES ({placeholders})",
                tuple(kwargs.values()),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def get_grid_state(self, mode: str, symbol: str) -> Optional[sqlite3.Row]:
        """Return the grid_state row for a symbol, or None."""
        conn = self._get_connection()
        cur = conn.execute(
            "SELECT * FROM grid_state WHERE mode = ? AND symbol = ?",
            (mode, symbol),
        )
        return cur.fetchone()

    def get_all_grid_states(self, mode: str) -> list[sqlite3.Row]:
        """Return all active grid states for a mode."""
        conn = self._get_connection()
        cur = conn.execute(
            "SELECT * FROM grid_state WHERE mode = ? AND status = 'active'",
            (mode,),
        )
        return cur.fetchall()

    def delete_grid_state(self, grid_state_id: int) -> None:
        """Delete a grid_state and all its levels."""
        conn = self._get_connection()
        conn.execute("DELETE FROM grid_levels WHERE grid_state_id = ?", (grid_state_id,))
        conn.execute("DELETE FROM grid_state WHERE id = ?", (grid_state_id,))
        conn.commit()

    # ── grid levels ────────────────────────────────────────────────────

    def insert_grid_level(self, **kwargs: Any) -> int:
        """Insert a grid level row and return its id."""
        cols = ", ".join(kwargs.keys())
        placeholders = ", ".join(["?"] * len(kwargs))
        conn = self._get_connection()
        cur = conn.execute(
            f"INSERT INTO grid_levels ({cols}) VALUES ({placeholders})",
            tuple(kwargs.values()),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def insert_grid_levels_bulk(self, rows: list[dict[str, Any]]) -> int:
        """Bulk insert grid levels. Returns count inserted."""
        if not rows:
            return 0
        cols = list(rows[0].keys())
        col_str = ", ".join(cols)
        placeholders = ", ".join(["?"] * len(cols))
        conn = self._get_connection()
        conn.executemany(
            f"INSERT INTO grid_levels ({col_str}) VALUES ({placeholders})",
            [tuple(r[c] for c in cols) for r in rows],
        )
        conn.commit()
        return len(rows)

    def update_grid_level(self, level_id: int, **kwargs: Any) -> None:
        """Update fields on a grid level."""
        if not kwargs:
            return
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        conn = self._get_connection()
        conn.execute(
            f"UPDATE grid_levels SET {set_clause} WHERE id = ?",
            (*kwargs.values(), level_id),
        )
        conn.commit()

    def get_grid_levels(self, grid_state_id: int) -> list[sqlite3.Row]:
        """Return all levels for a grid_state, ordered by level_index."""
        conn = self._get_connection()
        cur = conn.execute(
            "SELECT * FROM grid_levels WHERE grid_state_id = ? ORDER BY level_index",
            (grid_state_id,),
        )
        return cur.fetchall()

    def get_active_grid_levels(self, grid_state_id: int) -> list[sqlite3.Row]:
        """Return non-cancelled levels for a grid_state."""
        conn = self._get_connection()
        cur = conn.execute(
            """SELECT * FROM grid_levels
            WHERE grid_state_id = ? AND status != 'CANCELLED'
            ORDER BY level_index""",
            (grid_state_id,),
        )
        return cur.fetchall()

    def delete_grid_levels(self, grid_state_id: int) -> None:
        """Delete all levels for a grid_state."""
        conn = self._get_connection()
        conn.execute("DELETE FROM grid_levels WHERE grid_state_id = ?", (grid_state_id,))
        conn.commit()

    # ── dunder helpers ────────────────────────────────────────────────────

    def __enter__(self) -> "DatabaseManager":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"DatabaseManager(db_path={self._db_path!r})"

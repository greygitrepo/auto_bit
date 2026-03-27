"""
Configuration loader for auto_bit.

Loads YAML config files and .env credentials into a singleton AppConfig
instance with typed dataclass sections and dot-notation access.

Usage:
    from src.utils.config import AppConfig

    cfg = AppConfig()          # first call loads everything
    cfg2 = AppConfig()         # same instance (singleton)

    cfg.app.mode               # "paper"
    cfg.symbols.market         # {"category": "linear", "quote_currency": "USDT"}
    cfg["app"]["mode"]         # dict-style access also works
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project root is three levels up from this file:
#   src/utils/config.py  ->  src/utils  ->  src  ->  project root
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"


# ── Exceptions ─────────────────────────────────────────────────────────────


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


# ── Typed config sections (dataclasses) ────────────────────────────────────


@dataclass
class LoggingConfig:
    """Logging configuration from app.yaml -> logging."""

    level: str = "INFO"
    file: str = "logs/auto_bit.log"
    rotation: str = "10MB"
    retention: str = "30 days"


@dataclass
class DatabaseConfig:
    """Database configuration from app.yaml -> database."""

    type: str = "sqlite"
    path: str = "data/auto_bit.db"


@dataclass
class LoopConfig:
    """Main-loop timing from app.yaml -> loop."""

    rescan_delay_sec: int = 900
    health_check_sec: int = 60


@dataclass
class GuiConfig:
    """GUI server settings from app.yaml -> gui."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class AppSection:
    """Top-level application configuration assembled from app.yaml."""

    mode: str = "paper"
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)


@dataclass
class MarketConfig:
    """Market settings from symbols.yaml -> market."""

    category: str = "linear"
    quote_currency: str = "USDT"


@dataclass
class TimeframesConfig:
    """Timeframe settings from symbols.yaml -> timeframes."""

    primary: str = "5m"
    secondary: List[str] = field(default_factory=lambda: ["15m"])
    btc_eth_trend: str = "1h"
    candle_history: int = 100


@dataclass
class SymbolsSection:
    """Symbols / market configuration from symbols.yaml."""

    market: MarketConfig = field(default_factory=MarketConfig)
    base_symbols: List[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    blacklist: List[str] = field(default_factory=lambda: ["USDCUSDT"])
    timeframes: TimeframesConfig = field(default_factory=TimeframesConfig)


@dataclass
class CredentialsSection:
    """Bybit API credentials (from credentials.yaml or env vars)."""

    api_key: str = ""
    api_secret: str = ""


@dataclass
class StrategySection:
    """Aggregated strategy configs loaded as raw dicts.

    Each sub-file (scanner.yaml, position.yaml, asset.yaml) may contain
    arbitrarily nested strategy parameters that change frequently, so we
    keep them as plain dicts rather than rigid dataclasses.
    """

    scanner: Dict[str, Any] = field(default_factory=dict)
    position: Dict[str, Any] = field(default_factory=dict)
    asset: Dict[str, Any] = field(default_factory=dict)


# ── YAML helpers ───────────────────────────────────────────────────────────


def _load_yaml(path: Path) -> Dict[str, Any]:
    """Load a YAML file and return its contents as a dict.

    Args:
        path: Absolute or relative path to the YAML file.

    Returns:
        Parsed YAML content. Returns an empty dict for empty files.

    Raises:
        ConfigError: If the file does not exist or cannot be parsed.
    """
    if not path.exists():
        raise ConfigError(f"Required config file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse {path}: {exc}") from exc


def _load_yaml_optional(path: Path) -> Dict[str, Any]:
    """Load a YAML file if it exists; return an empty dict otherwise."""
    if not path.exists():
        return {}
    return _load_yaml(path)


# ── Field builders ─────────────────────────────────────────────────────────


def _build_app_section(raw: Dict[str, Any]) -> AppSection:
    """Construct an ``AppSection`` from the raw app.yaml dict."""
    return AppSection(
        mode=raw.get("mode", "paper"),
        logging=LoggingConfig(**raw.get("logging", {})),
        database=DatabaseConfig(**raw.get("database", {})),
        loop=LoopConfig(**raw.get("loop", {})),
        gui=GuiConfig(**raw.get("gui", {})),
    )


def _build_symbols_section(raw: Dict[str, Any]) -> SymbolsSection:
    """Construct a ``SymbolsSection`` from the raw symbols.yaml dict."""
    tf_raw = raw.get("timeframes", {})
    return SymbolsSection(
        market=MarketConfig(**raw.get("market", {})),
        base_symbols=raw.get("base_symbols", ["BTCUSDT", "ETHUSDT"]),
        blacklist=raw.get("blacklist", ["USDCUSDT"]),
        timeframes=TimeframesConfig(
            primary=tf_raw.get("primary", "5m"),
            secondary=tf_raw.get("secondary", ["15m"]),
            btc_eth_trend=tf_raw.get("btc_eth_trend", "1h"),
            candle_history=tf_raw.get("candle_history", 100),
        ),
    )


def _build_credentials(
    raw: Dict[str, Any],
) -> CredentialsSection:
    """Build credentials from YAML content, falling back to env vars.

    Priority:
        1. credentials.yaml ``bybit.api_key`` / ``bybit.api_secret``
        2. Environment variables ``BYBIT_API_KEY`` / ``BYBIT_API_SECRET``
    """
    bybit = raw.get("bybit", {})
    api_key = bybit.get("api_key", "") or ""
    api_secret = bybit.get("api_secret", "") or ""

    # Ignore placeholder values from the example file.
    placeholders = {"", "YOUR_API_KEY", "your_api_key_here"}
    if api_key in placeholders:
        api_key = os.getenv("BYBIT_API_KEY", "")
    if api_secret in placeholders or api_secret == "YOUR_API_SECRET":
        api_secret = os.getenv("BYBIT_API_SECRET", "")

    return CredentialsSection(api_key=api_key, api_secret=api_secret)


# ── Validation ─────────────────────────────────────────────────────────────


_REQUIRED_APP_FIELDS = ("mode",)
_REQUIRED_SYMBOLS_FIELDS = ("market", "timeframes")


def _validate_required(data: Dict[str, Any], fields: tuple, source: str) -> None:
    """Raise ``ConfigError`` if any *fields* are missing from *data*."""
    missing = [f for f in fields if f not in data]
    if missing:
        raise ConfigError(
            f"Missing required fields in {source}: {', '.join(missing)}"
        )


def _validate_credentials(creds: CredentialsSection, mode: str) -> None:
    """Warn (paper) or raise (live) when credentials are absent."""
    if not creds.api_key or not creds.api_secret:
        if mode == "live":
            raise ConfigError(
                "Bybit API credentials are required for live mode. "
                "Set them in config/credentials.yaml or via BYBIT_API_KEY / "
                "BYBIT_API_SECRET environment variables."
            )
        logger.warning(
            "Bybit API credentials not configured. "
            "Set them in config/credentials.yaml or .env before going live."
        )


# ── Singleton AppConfig ───────────────────────────────────────────────────


class AppConfig:
    """Singleton configuration container.

    On first instantiation the class loads every config source exactly once.
    Subsequent calls to ``AppConfig()`` return the same instance.

    Attributes:
        app:          Application settings  (``AppSection``)
        symbols:      Symbol / market settings (``SymbolsSection``)
        credentials:  API credentials (``CredentialsSection``)
        strategy:     Strategy parameters (``StrategySection``)

    Both dot-notation and dict-style access are supported::

        cfg = AppConfig()
        cfg.app.mode          # dot
        cfg["app"]["mode"]    # dict
    """

    _instance: Optional[AppConfig] = None
    _loaded: bool = False

    def __new__(cls, config_dir: Optional[Path] = None) -> AppConfig:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        if self._loaded:
            return
        self._config_dir = Path(config_dir) if config_dir else _CONFIG_DIR
        self._load()
        AppConfig._loaded = True

    # ── public helpers ────────────────────────────────────────────────

    def __getitem__(self, key: str) -> Any:
        """Support ``cfg["app"]`` style access."""
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def reload(self, config_dir: Optional[Path] = None) -> None:
        """Force a full reload of all configuration sources.

        Useful during testing or after config files have changed on disk.
        """
        if config_dir is not None:
            self._config_dir = Path(config_dir)
        self._load()

    # ── internals ─────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load .env, then all YAML files, validate, and populate attrs."""
        # 1. Load .env (no-op if file missing)
        dotenv_path = _PROJECT_ROOT / ".env"
        load_dotenv(dotenv_path, override=False)

        # 2. Required YAML files
        app_raw = _load_yaml(self._config_dir / "app.yaml")
        symbols_raw = _load_yaml(self._config_dir / "symbols.yaml")

        # 3. Optional credentials
        creds_raw = _load_yaml_optional(self._config_dir / "credentials.yaml")

        # 4. Strategy sub-configs (required)
        strategy_dir = self._config_dir / "strategy"
        scanner_raw = _load_yaml(strategy_dir / "scanner.yaml")
        position_raw = _load_yaml(strategy_dir / "position.yaml")
        asset_raw = _load_yaml(strategy_dir / "asset.yaml")

        # 5. Validate required fields
        _validate_required(app_raw, _REQUIRED_APP_FIELDS, "app.yaml")
        _validate_required(symbols_raw, _REQUIRED_SYMBOLS_FIELDS, "symbols.yaml")

        # 6. Build typed sections
        self.app: AppSection = _build_app_section(app_raw)
        self.symbols: SymbolsSection = _build_symbols_section(symbols_raw)
        self.credentials: CredentialsSection = _build_credentials(creds_raw)
        self.strategy: StrategySection = StrategySection(
            scanner=scanner_raw,
            position=position_raw,
            asset=asset_raw,
        )

        # 7. Credential validation (warn or raise)
        _validate_credentials(self.credentials, self.app.mode)

        # Keep raw dicts available for edge-case access.
        self._raw: Dict[str, Dict[str, Any]] = {
            "app": app_raw,
            "symbols": symbols_raw,
            "credentials": creds_raw,
            "scanner": scanner_raw,
            "position": position_raw,
            "asset": asset_raw,
        }

        logger.info(
            "Configuration loaded (mode=%s, config_dir=%s)",
            self.app.mode,
            self._config_dir,
        )

    @classmethod
    def reset(cls) -> None:
        """Destroy the singleton instance. Intended for testing only."""
        cls._instance = None
        cls._loaded = False

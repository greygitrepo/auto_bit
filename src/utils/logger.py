"""
Logging setup using loguru with per-process log files, rotation, and retention.

Usage:
    from src.utils.logger import setup_logger

    logger = setup_logger("collector", level="DEBUG")
    logger.info("Collector started")
"""

import sys
from pathlib import Path

from loguru import logger

# Default log format used across all sinks
LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{function}:{line} | {message}"
)


def setup_logger(
    process_name: str,
    level: str = "INFO",
    log_dir: str = "logs",
    rotation: str = "50 MB",
    retention: str = "7 days",
) -> "logger":
    """Configure and return a loguru logger for a specific process.

    Creates a dedicated log file for the given process name and adds a
    colored console sink. Existing default sinks are removed to avoid
    duplicate output.

    Args:
        process_name: Identifier used in the log filename
            (e.g. ``"collector"``, ``"strategy"``).
        level: Minimum log level for both console and file sinks.
        log_dir: Directory where log files are written. Created if it
            does not exist.
        rotation: When to rotate the log file. Accepts size strings
            like ``"50 MB"`` or time strings like ``"00:00"``
            (midnight).
        retention: How long to keep rotated log files before they are
            cleaned up (e.g. ``"7 days"``).

    Returns:
        The configured :mod:`loguru` ``logger`` instance. This is the
        global loguru logger; calling this function reconfigures it.
    """
    # Remove any previously-added sinks so we start clean
    logger.remove()

    # Ensure the log directory exists
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Console sink — coloured, human-friendly
    logger.add(
        sys.stderr,
        format=LOG_FORMAT,
        level=level,
        colorize=True,
        backtrace=True,
        diagnose=True,
    )

    # Per-process file sink — rotated & retained automatically
    file_path = log_path / f"{process_name}.log"
    logger.add(
        str(file_path),
        format=LOG_FORMAT,
        level=level,
        rotation=rotation,
        retention=retention,
        compression="gz",
        backtrace=True,
        diagnose=True,
        enqueue=False,
    )

    # Error-only file for quick triage
    error_path = log_path / f"{process_name}_error.log"
    logger.add(
        str(error_path),
        format=LOG_FORMAT,
        level="ERROR",
        rotation=rotation,
        retention=retention,
        compression="gz",
        backtrace=True,
        diagnose=True,
        enqueue=False,
    )

    logger.info(
        "Logger initialised for process={} level={} log_dir={}",
        process_name,
        level,
        log_dir,
    )

    return logger

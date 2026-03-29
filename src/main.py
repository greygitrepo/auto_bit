"""Main Orchestrator for auto_bit.

Manages the lifecycle of all child processes (P1, P2, P3), handles
inter-process communication via multiprocessing queues, performs health
monitoring, and coordinates graceful shutdown.

Tasks C-08 (Orchestrator), C-09 (Shutdown).
"""

from __future__ import annotations

import argparse
import asyncio
import multiprocessing
import os
import queue
import signal
import sys
import time
from typing import Any, Dict, List, Optional

from loguru import logger

from src.recovery import RecoveryManager
from src.utils.config import AppConfig
from src.utils.db import DatabaseManager
from src.utils.logger import setup_logger
from src.utils.messages import (
    ControlMessage,
    ScanResultMessage,
    SlotAvailableMessage,
)


# Watchdog constants
_WATCHDOG_POLL_SEC = 1.0
_HEALTH_CHECK_SEC = 60.0
_PROCESS_RESTART_COOLDOWN_SEC = 10.0
_GRACEFUL_SHUTDOWN_TIMEOUT_SEC = 15.0


class Orchestrator:
    """Main process that manages all child processes.

    Processes:
    - P1: DataCollectorProcess (async, WebSocket candle streaming)
    - P2: StrategyEngineProcess (synchronous, indicator + strategy evaluation)
    - P3: OrderManagerProcess (async, order execution and position management)
    - P5: GUI Server (Phase 4 placeholder)

    The Orchestrator itself runs synchronously in the main process,
    polling queues with ``queue.get(timeout=1)`` in its watchdog loop.
    """

    def __init__(self, headless: bool = False) -> None:
        # Reset singleton in case of re-init in tests
        AppConfig.reset()
        self.config = AppConfig()
        self.headless = headless

        # Extract config sections
        self._mode = self.config.app.mode
        self._credentials = {
            "api_key": self.config.credentials.api_key,
            "api_secret": self.config.credentials.api_secret,
        }

        # Build combined config dict for child processes
        self._process_config = {
            "mode": self._mode,
            "asset": self.config.strategy.asset,
            "position": self.config.strategy.position,
            "scanner": self.config.strategy.scanner,
            "grid": self.config.strategy.grid,
            "symbols": {
                "base_symbols": self.config.symbols.base_symbols,
                "blacklist": self.config.symbols.blacklist,
                "market": {
                    "category": self.config.symbols.market.category,
                    "quote_currency": self.config.symbols.market.quote_currency,
                },
                "timeframes": {
                    "primary": self.config.symbols.timeframes.primary,
                    "secondary": self.config.symbols.timeframes.secondary,
                    "btc_eth_trend": self.config.symbols.timeframes.btc_eth_trend,
                    "candle_history": self.config.symbols.timeframes.candle_history,
                },
            },
            "loop": {
                "rescan_delay_sec": self.config.app.loop.rescan_delay_sec,
                "health_check_sec": self.config.app.loop.health_check_sec,
            },
        }

        # Create all IPC queues
        self.market_data_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=1000)
        self.signal_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=100)
        self.position_update_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=10)
        self.p1_control_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=50)
        self.p2_control_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=50)
        self.p3_control_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=50)
        self.event_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=100)
        self.scan_result_queue: multiprocessing.Queue = multiprocessing.Queue(maxsize=50)

        # Process references
        self._p1: Optional[multiprocessing.Process] = None
        self._p2: Optional[multiprocessing.Process] = None
        self._p3: Optional[multiprocessing.Process] = None
        self._p5: Optional[multiprocessing.Process] = None

        # State
        self._running = False
        self._shutdown_requested = False
        self._last_health_check = 0.0
        self._process_restart_times: Dict[str, float] = {}

        # Cached DB instance for the watchdog loop (avoids re-creating every second)
        self._watchdog_db: Optional[DatabaseManager] = None

        logger.info(
            "Orchestrator initialised: mode={}, headless={}",
            self._mode, self.headless,
        )

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the system: run recovery, launch processes, enter watchdog loop.

        Steps:
        1. Run recovery to restore consistent state.
        2. Start P1, P2, P3 processes.
        3. Send initial scan trigger if slots are available.
        4. Enter the watchdog loop.
        """
        self._setup_signal_handlers()
        self._running = True

        logger.info("=" * 60)
        logger.info("auto_bit starting (mode={})", self._mode)
        logger.info("=" * 60)

        # 1. Run recovery
        db = DatabaseManager()
        try:
            recovery_result = self._run_recovery(db)
            logger.info("Recovery result: {}", recovery_result)
        except Exception as exc:
            logger.error("Recovery failed: {}", exc)
            # Continue anyway -- better to run with potentially stale state
            # than to not start at all.

        # Record startup time
        db.set_state("last_startup_time", str(int(time.time())))
        db.close()

        # 2. Start child processes
        self._start_processes()

        # 3. Send initial scan trigger
        self._send_initial_scan_trigger()

        # 4. Write initial process states to DB
        self._log_health()

        # 5. Enter watchdog loop
        logger.info("Entering watchdog loop")
        try:
            self._watchdog_loop()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received in watchdog loop")
        finally:
            self.stop(graceful=True)

    def stop(self, graceful: bool = True) -> None:
        """Shut down all child processes.

        Parameters
        ----------
        graceful:
            If ``True``, send stop commands and wait for clean exit.
            If ``False``, terminate processes immediately.
        """
        if self._shutdown_requested:
            logger.warning("Shutdown already in progress, forcing termination")
            self._force_terminate_all()
            return

        self._shutdown_requested = True
        self._running = False

        logger.info(
            "Shutting down (graceful={})", graceful,
        )

        # Record shutdown time
        try:
            db = DatabaseManager()
            db.set_state("last_shutdown_time", str(int(time.time())))
            db.close()
        except Exception as exc:
            logger.warning("Failed to record shutdown time: {}", exc)

        if graceful:
            self._graceful_shutdown()
        else:
            self._force_terminate_all()

        logger.info("=" * 60)
        logger.info("auto_bit stopped")
        logger.info("=" * 60)

    def _start_processes(self) -> None:
        """Create and start all child processes."""
        # P3: Order Manager (start first so it's ready for signals)
        self._p3 = self._create_p3()
        self._p3.start()
        logger.info("P3 (OrderManager) started, pid={}", self._p3.pid)

        # P1: Data Collector
        self._p1 = self._create_p1()
        if self._p1 is not None:
            self._p1.start()
            logger.info("P1 (DataCollector) started, pid={}", self._p1.pid)

        # P2: Strategy Engine
        self._p2 = self._create_p2()
        if self._p2 is not None:
            self._p2.start()
            logger.info("P2 (StrategyEngine) started, pid={}", self._p2.pid)

        # P5: GUI Server (unless headless)
        if not self.headless:
            self._p5 = self._create_p5()
            if self._p5 is not None:
                self._p5.start()
                gui_cfg = self.config.app.gui if hasattr(self.config.app, 'gui') else {}
                port = gui_cfg.get("port", 8080) if isinstance(gui_cfg, dict) else 8080
                logger.info("P5 (GUIServer) started, pid={} → http://localhost:{}", self._p5.pid, port)

    def _create_p1(self) -> Optional[multiprocessing.Process]:
        """Create the P1 DataCollectorProcess.

        Returns ``None`` if the process class is not yet implemented.
        """
        try:
            from src.collector.process import DataCollectorProcess

            return DataCollectorProcess(
                config=self._process_config,
                credentials=self._credentials,
                market_data_queue=self.market_data_queue,
                control_queue=self.p1_control_queue,
            )
        except ImportError:
            logger.warning(
                "P1 DataCollectorProcess not available (src.collector.process not found). "
                "Running without data collection."
            )
            return None

    def _create_p2(self) -> Optional[multiprocessing.Process]:
        """Create the P2 StrategyEngineProcess.

        Returns ``None`` if the process class is not yet implemented.
        """
        try:
            from src.strategy.process import StrategyEngineProcess

            return StrategyEngineProcess(
                config=self._process_config,
                credentials=self._credentials,
                market_data_queue=self.market_data_queue,
                position_update_queue=self.position_update_queue,
                control_queue=self.p2_control_queue,
                signal_queue=self.signal_queue,
                scan_result_queue=self.scan_result_queue,
                p1_control_queue=self.p1_control_queue,
            )
        except ImportError:
            logger.warning(
                "P2 StrategyEngineProcess not available (src.strategy.process not found). "
                "Running without strategy engine."
            )
            return None

    def _create_p3(self) -> multiprocessing.Process:
        """Create the P3 OrderManagerProcess."""
        from src.order.process import OrderManagerProcess

        return OrderManagerProcess(
            config=self._process_config,
            credentials=self._credentials,
            signal_queue=self.signal_queue,
            control_queue=self.p3_control_queue,
            position_update_queue=self.position_update_queue,
            event_queue=self.event_queue,
        )

    def _create_p5(self) -> Optional[multiprocessing.Process]:
        """Create the P5 GUIServerProcess.

        Returns ``None`` if the GUI module is not available.
        """
        try:
            from src.gui.app import GUIServerProcess

            gui_config = {
                "mode": self._mode,
                "gui": {
                    "host": self.config.app.gui.host,
                    "port": self.config.app.gui.port,
                },
                "database": {
                    "path": self.config.app.database.path,
                },
            }

            return GUIServerProcess(config=gui_config)
        except ImportError:
            logger.warning(
                "P5 GUIServerProcess not available (src.gui.app not found). "
                "Running without GUI."
            )
            return None
        except Exception as exc:
            logger.warning("Failed to create GUIServerProcess: {}", exc)
            return None

    # ------------------------------------------------------------------
    # Watchdog loop
    # ------------------------------------------------------------------

    def _watchdog_loop(self) -> None:
        """Main loop: monitor processes, handle events, perform health checks.

        Runs synchronously in the main process, polling queues with
        short timeouts to remain responsive to signals.
        """
        while self._running:
            # 0. Check for restart request (from GUI reset)
            self._check_restart_request()

            # 1. Check all processes alive (restart if dead)
            self._check_processes()

            # 2. Process event_queue (slot_available, etc.)
            self._drain_event_queue()

            # 3. Process scan_result_queue
            self._drain_scan_result_queue()

            # 4. Periodic health check logging
            now = time.time()
            if now - self._last_health_check >= _HEALTH_CHECK_SEC:
                self._log_health()
                self._last_health_check = now

            # Sleep briefly to avoid busy-spinning
            time.sleep(_WATCHDOG_POLL_SEC)

    def _check_restart_request(self) -> None:
        """Check if a restart was requested via DB flag (e.g. from GUI reset)."""
        try:
            db = self._get_watchdog_db()
            flag = db.get_state("restart_requested")
            if flag == "1":
                db.set_state("restart_requested", "0")
                logger.info("Restart requested — restarting P1, P2, P3...")
                self._restart_trading_processes()
                return
        except Exception:
            pass

    def _restart_trading_processes(self) -> None:
        """Stop and recreate P1, P2, P3 with fresh state and reloaded config."""
        # Stop existing processes
        for name, q in [("P1", self.p1_control_queue), ("P2", self.p2_control_queue), ("P3", self.p3_control_queue)]:
            try:
                q.put_nowait(ControlMessage(command="stop"))
            except queue.Full:
                pass

        # Wait briefly for graceful exit
        for name, proc in [("P1", self._p1), ("P2", self._p2), ("P3", self._p3)]:
            if proc is not None and proc.is_alive():
                proc.join(timeout=5.0)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=3.0)
                    if proc.is_alive():
                        proc.kill()
                        proc.join(timeout=2.0)

        # Reload config from YAML files (picks up any changes)
        AppConfig.reset()
        self.config = AppConfig()
        self._process_config = {
            "mode": self._mode,
            "asset": self.config.strategy.asset,
            "position": self.config.strategy.position,
            "scanner": self.config.strategy.scanner,
            "grid": self.config.strategy.grid,
            "symbols": {
                "base_symbols": self.config.symbols.base_symbols,
                "blacklist": self.config.symbols.blacklist,
                "market": {
                    "category": self.config.symbols.market.category,
                    "quote_currency": self.config.symbols.market.quote_currency,
                },
                "timeframes": {
                    "primary": self.config.symbols.timeframes.primary,
                    "secondary": self.config.symbols.timeframes.secondary,
                    "btc_eth_trend": self.config.symbols.timeframes.btc_eth_trend,
                    "candle_history": self.config.symbols.timeframes.candle_history,
                },
            },
            "loop": {
                "rescan_delay_sec": self.config.app.loop.rescan_delay_sec,
                "health_check_sec": self.config.app.loop.health_check_sec,
            },
        }
        logger.info("Config reloaded for restart")

        # Recreate and start fresh processes
        self._p3 = self._create_p3()
        self._p3.start()
        logger.info("P3 restarted (pid={})", self._p3.pid)

        self._p1 = self._create_p1()
        if self._p1 is not None:
            self._p1.start()
            logger.info("P1 restarted (pid={})", self._p1.pid)

        self._p2 = self._create_p2()
        if self._p2 is not None:
            self._p2.start()
            logger.info("P2 restarted (pid={})", self._p2.pid)

        # Send initial scan trigger
        self._send_initial_scan_trigger()
        logger.info("Trading processes restarted successfully")

    def _check_processes(self) -> None:
        """Check if child processes are alive and restart dead ones."""
        now = time.time()

        for name, proc_attr in [("P1", "_p1"), ("P2", "_p2"), ("P3", "_p3"), ("P5", "_p5")]:
            proc = getattr(self, proc_attr)
            if proc is None:
                continue

            if not proc.is_alive():
                exit_code = proc.exitcode
                logger.error(
                    "{} process died (exit_code={})", name, exit_code,
                )

                # Respect cooldown to avoid restart loops
                last_restart = self._process_restart_times.get(name, 0.0)
                if now - last_restart < _PROCESS_RESTART_COOLDOWN_SEC:
                    logger.warning(
                        "{} restart cooldown active, skipping restart", name,
                    )
                    continue

                logger.info("Restarting {} process", name)
                self._process_restart_times[name] = now

                if name == "P1":
                    self._p1 = self._create_p1()
                    if self._p1 is not None:
                        self._p1.start()
                        logger.info("{} restarted, pid={}", name, self._p1.pid)
                elif name == "P2":
                    self._p2 = self._create_p2()
                    if self._p2 is not None:
                        self._p2.start()
                        logger.info("{} restarted, pid={}", name, self._p2.pid)
                elif name == "P3":
                    self._p3 = self._create_p3()
                    self._p3.start()
                    logger.info("{} restarted, pid={}", name, self._p3.pid)
                elif name == "P5":
                    self._p5 = self._create_p5()
                    if self._p5 is not None:
                        self._p5.start()
                        logger.info("{} restarted, pid={}", name, self._p5.pid)

    def _drain_event_queue(self) -> None:
        """Process all pending messages on the event queue."""
        while True:
            try:
                msg = self.event_queue.get_nowait()
            except queue.Empty:
                break

            if isinstance(msg, SlotAvailableMessage):
                self._handle_slot_available(msg)
            else:
                logger.debug(
                    "Orchestrator ignoring event of type: {}",
                    type(msg).__name__,
                )

    def _drain_scan_result_queue(self) -> None:
        """Process all pending scan results."""
        while True:
            try:
                msg = self.scan_result_queue.get_nowait()
            except queue.Empty:
                break

            if isinstance(msg, ScanResultMessage):
                self._handle_scan_result(msg)
            else:
                logger.debug(
                    "Orchestrator ignoring scan_result of type: {}",
                    type(msg).__name__,
                )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _handle_slot_available(self, msg: SlotAvailableMessage) -> None:
        """Position closed: trigger a new scan to find candidates.

        Sends a scan trigger to P2 so it can run the scanner and
        potentially open a new position in the freed slot.
        """
        logger.info(
            "Slot available: {} slot(s) free, current positions: {}",
            msg.available_slots,
            msg.current_positions,
        )

        # Tell P2 to run a scan
        scan_cmd = ControlMessage(
            command="scan",
            data={
                "available_slots": msg.available_slots,
                "current_positions": msg.current_positions,
            },
        )
        try:
            self.p2_control_queue.put_nowait(scan_cmd)
        except queue.Full:
            logger.warning("P2 control queue full, scan trigger dropped")

    def _handle_scan_result(self, msg: ScanResultMessage) -> None:
        """Scanner found candidates: tell P1 to subscribe to their data feeds.

        Extracts symbols from scan results and sends subscribe commands
        to P1 so it begins streaming candle data for the new candidates.
        """
        candidates = msg.results
        if not candidates:
            logger.info("Scan returned no candidates")
            return

        symbols = [r.get("symbol", "") for r in candidates if r.get("symbol")]
        logger.info(
            "Scan returned {} candidate(s): {} (direction={})",
            len(symbols), symbols[:5], msg.market_direction,
        )

        # Tell P1 to subscribe to these symbols
        subscribe_cmd = ControlMessage(
            command="subscribe",
            data={"symbols": symbols},
        )
        try:
            self.p1_control_queue.put_nowait(subscribe_cmd)
        except queue.Full:
            logger.warning("P1 control queue full, subscribe command dropped")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _graceful_shutdown(self) -> None:
        """Send stop commands and wait for processes to exit cleanly."""
        # Send stop commands to all processes
        for name, q in [
            ("P1", self.p1_control_queue),
            ("P2", self.p2_control_queue),
            ("P3", self.p3_control_queue),
        ]:
            try:
                q.put_nowait(ControlMessage(command="stop"))
                logger.info("Sent stop command to {}", name)
            except queue.Full:
                logger.warning("Could not send stop to {} (queue full)", name)

        # Wait for processes to exit
        deadline = time.time() + _GRACEFUL_SHUTDOWN_TIMEOUT_SEC
        processes = [
            ("P1", self._p1),
            ("P2", self._p2),
            ("P3", self._p3),
        ]

        for name, proc in processes:
            if proc is None or not proc.is_alive():
                continue

            remaining = max(0.1, deadline - time.time())
            logger.info("Waiting for {} to exit (timeout={:.1f}s)", name, remaining)
            proc.join(timeout=remaining)

            if proc.is_alive():
                logger.warning("{} did not exit gracefully, terminating", name)
                proc.terminate()
                proc.join(timeout=3.0)

                if proc.is_alive():
                    logger.error("{} still alive after terminate, killing", name)
                    proc.kill()
                    proc.join(timeout=2.0)
            else:
                logger.info("{} exited cleanly (exit_code={})", name, proc.exitcode)

    def _force_terminate_all(self) -> None:
        """Immediately terminate all child processes."""
        for name, proc in [("P1", self._p1), ("P2", self._p2), ("P3", self._p3), ("P5", self._p5)]:
            if proc is None:
                continue
            if proc.is_alive():
                logger.warning("Force terminating {}", name)
                proc.terminate()
                proc.join(timeout=3.0)
                if proc.is_alive():
                    proc.kill()
                    proc.join(timeout=2.0)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _setup_signal_handlers(self) -> None:
        """Register SIGTERM and SIGINT for graceful shutdown."""

        def _signal_handler(signum: int, frame: Any) -> None:
            sig_name = signal.Signals(signum).name
            logger.info("Received {} -- initiating graceful shutdown", sig_name)
            self._running = False

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_watchdog_db(self) -> DatabaseManager:
        """Return a cached DatabaseManager for watchdog-loop use."""
        if self._watchdog_db is None:
            self._watchdog_db = DatabaseManager()
        return self._watchdog_db

    def _send_initial_scan_trigger(self) -> None:
        """Send an initial scan trigger to P2 if trading slots are available."""
        try:
            db = self._get_watchdog_db()
            open_positions = db.get_open_positions(self._mode)
            open_count = len(open_positions)
        except Exception:
            open_count = 0

        max_positions = (
            self.config.strategy.asset
            .get("strategies", {})
            .get("fixed_ratio", {})
            .get("max_concurrent_positions", 3)
        )
        available = max(0, max_positions - open_count)

        if available > 0:
            current_symbols = []
            try:
                current_symbols = [dict(p)["symbol"] for p in open_positions]
            except Exception:
                pass

            scan_cmd = ControlMessage(
                command="scan",
                data={
                    "available_slots": available,
                    "current_positions": current_symbols,
                },
            )
            try:
                self.p2_control_queue.put_nowait(scan_cmd)
                logger.info(
                    "Initial scan trigger sent: {} slot(s) available",
                    available,
                )
            except queue.Full:
                logger.warning("P2 control queue full, initial scan trigger dropped")
        else:
            logger.info("No trading slots available, skipping initial scan")

    def _run_recovery(self, db: DatabaseManager) -> Dict[str, Any]:
        """Run the recovery manager synchronously.

        Creates a temporary event loop to run the async recovery routine.
        """
        from src.collector.bybit_client import BybitClient

        bybit_client = None
        if self._mode == "live" and self._credentials.get("api_key"):
            try:
                bybit_client = BybitClient(
                    api_key=self._credentials["api_key"],
                    api_secret=self._credentials["api_secret"],
                )
            except Exception as exc:
                logger.warning("Failed to create BybitClient for recovery: {}", exc)

        recovery = RecoveryManager(
            db=db,
            bybit_client=bybit_client,
            mode=self._mode,
        )

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(recovery.recover())
        finally:
            loop.close()

        return result

    def _log_health(self) -> None:
        """Log a health check summary and persist process states to the DB."""
        statuses = []
        all_alive = True

        try:
            db = self._get_watchdog_db()
        except Exception:
            db = None

        for name, proc in [("P1", self._p1), ("P2", self._p2), ("P3", self._p3), ("P5", self._p5)]:
            if proc is None:
                statuses.append(f"{name}=N/A")
                state = "n/a"
            elif proc.is_alive():
                statuses.append(f"{name}=alive(pid={proc.pid})")
                state = "running"
            else:
                statuses.append(f"{name}=dead(exit={proc.exitcode})")
                state = "dead"
                all_alive = False

            if db is not None:
                try:
                    db.set_state(f"process_{name}_state", state)
                except Exception:
                    pass

        # Persist trading_active flag
        if db is not None:
            try:
                db.set_state("trading_active", "true" if (self._running and all_alive) else "false")
                db.set_state("last_health_check", str(int(time.time())))
            except Exception:
                pass

        logger.info("Health check: {}", " | ".join(statuses))


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="auto_bit",
        description="Automated cryptocurrency trading system",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=False,
        help="Run without GUI server (headless mode)",
    )
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default=None,
        help="Override trading mode from config (paper or live)",
    )
    return parser.parse_args()


def main() -> None:
    """Application entry point."""
    setup_logger("orchestrator")
    args = parse_args()

    # Override mode if specified on command line
    if args.mode is not None:
        os.environ["AUTOBIT_MODE_OVERRIDE"] = args.mode

    orchestrator = Orchestrator(headless=args.headless)

    # Apply mode override if set via CLI
    if args.mode is not None:
        orchestrator._mode = args.mode
        orchestrator._process_config["mode"] = args.mode
        logger.info("Mode overridden via CLI: {}", args.mode)

    try:
        orchestrator.start()
    except KeyboardInterrupt:
        orchestrator.stop(graceful=True)
    except Exception as exc:
        logger.exception("Fatal error in orchestrator: {}", exc)
        orchestrator.stop(graceful=False)
        sys.exit(1)


if __name__ == "__main__":
    main()

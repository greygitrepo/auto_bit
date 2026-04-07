"""FastAPI application setup and GUI server process.

Provides the main FastAPI app instance with page routes (Jinja2 templates),
static file mounting, and a ``GUIServerProcess`` (P5) that runs the uvicorn
server in a dedicated child process.

Tasks: G-01 (FastAPI app setup), G-09 (GUI process integration).
"""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

_GUI_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _GUI_DIR / "templates"
_STATIC_DIR = _GUI_DIR / "static"

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Auto Bit",
    description="Automated cryptocurrency trading system dashboard",
    version="1.0.0",
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# Jinja2 templates
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Startup / shutdown hooks
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def on_startup() -> None:
    """Initialise database connection and start WebSocket updater on startup."""
    from src.gui.websocket import gui_updater, manager, websocket_endpoint  # noqa: F811

    # Register the WebSocket route
    app.add_api_websocket_route("/ws", websocket_endpoint)

    # Register API router
    from src.gui.api import router as api_router

    app.include_router(api_router)

    # Initialise shared resources from app.state
    db_path: Optional[str] = getattr(app.state, "db_path", None)
    mode: str = getattr(app.state, "mode", "paper")

    from src.utils.db import DatabaseManager

    db = DatabaseManager(db_path=db_path)
    app.state.db = db
    app.state.start_time = time.time()

    # Create and store the position tracker (read-only queries)
    from src.tracker.position_tracker import PositionTracker

    tracker = PositionTracker(db=db, mode=mode)
    app.state.tracker = tracker

    # Start the WebSocket updater background task
    from src.gui.websocket import GUIUpdater

    updater = GUIUpdater(db=db, mode=mode, manager=manager)
    app.state.updater = updater
    import asyncio

    asyncio.create_task(updater.start())

    logger.info("GUI startup complete (mode={}, db_path={})", mode, db_path)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Clean up database connection on shutdown."""
    db = getattr(app.state, "db", None)
    if db is not None:
        db.close()
        logger.info("GUI database connection closed")

    updater = getattr(app.state, "updater", None)
    if updater is not None:
        updater.stop()
        logger.info("GUI updater stopped")


# ---------------------------------------------------------------------------
# Page routes (serve Jinja2 templates)
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Serve the main dashboard page."""
    mode = getattr(request.app.state, "mode", "paper")
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"mode": mode, "page": "dashboard", "active_tab": "dashboard"},
    )


@app.get("/positions", response_class=HTMLResponse)
async def positions_page(request: Request) -> HTMLResponse:
    """Serve the positions page."""
    mode = getattr(request.app.state, "mode", "paper")
    return templates.TemplateResponse(
        request=request,
        name="positions.html",
        context={"mode": mode, "page": "positions", "active_tab": "positions"},
    )


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request) -> HTMLResponse:
    """Serve the trade history page."""
    mode = getattr(request.app.state, "mode", "paper")
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={"mode": mode, "page": "history", "active_tab": "history"},
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Serve the settings page."""
    mode = getattr(request.app.state, "mode", "paper")
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"mode": mode, "page": "settings", "active_tab": "settings"},
    )


# ---------------------------------------------------------------------------
# GUI Server Process (P5)
# ---------------------------------------------------------------------------


class GUIServerProcess(multiprocessing.Process):
    """P5: GUI Server Process.

    Runs the FastAPI/uvicorn server in a dedicated child process so it does
    not block the main Orchestrator watchdog loop.

    Parameters
    ----------
    config:
        Application configuration dict.  Must contain at minimum:
        - ``gui.host`` (str): bind address (default ``"0.0.0.0"``)
        - ``gui.port`` (int): bind port (default ``8080``)
        - ``mode`` (str): ``"paper"`` or ``"live"``
        - ``database.path`` (str, optional): SQLite database path
    control_queue:
        Multiprocessing queue for sending commands (start/stop/pause)
        back to the Orchestrator.  ``None`` disables control commands.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        control_queue: Optional[multiprocessing.Queue] = None,
    ) -> None:
        super().__init__(name="P5-GUIServer", daemon=True)
        self._config = config
        self._control_queue = control_queue

    def run(self) -> None:
        """Start the uvicorn server.

        This method executes in the child process.  It configures
        ``app.state`` with shared references before launching uvicorn.
        """
        import uvicorn

        # Populate app.state so routes and hooks can access shared resources.
        gui_cfg = self._config.get("gui", {})
        host = gui_cfg.get("host", "0.0.0.0")
        port = gui_cfg.get("port", 8080)

        app.state.mode = self._config.get("mode", "paper")
        app.state.db_path = self._config.get("database", {}).get("path", None)
        app.state.control_queue = self._control_queue
        app.state.config = self._config

        logger.info(
            "Starting GUI server on {}:{} (mode={})",
            host,
            port,
            app.state.mode,
        )

        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )

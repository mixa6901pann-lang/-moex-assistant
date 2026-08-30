"""Async health-check HTTP endpoint."""

from __future__ import annotations

from datetime import datetime, timezone

from aiohttp import web

from core import db
from core.config import HEALTH_PORT
from core.moex import MoexClient


async def health_handler(request: web.Request) -> web.Response:
    """Return JSON health status."""
    assistant = request.app["assistant"]
    status: dict = {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

    # Scheduler
    try:
        status["scheduler_running"] = assistant.scheduler.running
    except Exception as exc:
        status["scheduler_running"] = False
        status["scheduler_error"] = str(exc)

    # Open positions
    try:
        positions = await db.open_positions()
        status["open_positions"] = len(positions)
    except Exception as exc:
        status["open_positions"] = -1
        status["db_error"] = str(exc)

    # MOEX connectivity (lightweight — index check)
    try:
        idx = await assistant.moex.index_value("IMOEX")
        status["moex_connected"] = idx is not None
    except Exception as exc:
        status["moex_connected"] = False
        status["moex_error"] = str(exc)

    http_status = 200 if status["moex_connected"] and status["scheduler_running"] else 503
    return web.json_response(status, status=http_status)


async def start_health_server(assistant) -> None:
    """Start aiohttp health endpoint in background."""
    app = web.Application()
    app["assistant"] = assistant
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
    await site.start()

"""Web UI stub — returns a maintenance page while the SPA is disabled."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from core.config import LLM_PROVIDER, OLLAMA_URL, OLLAMA_MODEL, CBR_MEETING_DATES, CBR_SOFT_MODE_ENABLED

app = FastAPI(title="MOEX Assistant (web UI disabled)")

STUB_HTML = """<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>MOEX Assistant — веб-панель недоступна</title>
    <style>
        body {
            font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            margin: 0;
        }
        .box {
            text-align: center;
            max-width: 480px;
            padding: 2rem;
            border-radius: 1rem;
            background: #1e293b;
            box-shadow: 0 10px 40px rgba(0,0,0,0.3);
        }
        h1 { margin-top: 0; font-size: 1.5rem; }
        p { line-height: 1.6; color: #94a3b8; }
        .status {
            display: inline-block;
            margin-top: 1rem;
            padding: 0.5rem 1rem;
            border-radius: 999px;
            background: #334155;
            font-size: 0.875rem;
        }
    </style>
</head>
<body>
    <div class="box">
        <h1>Веб-панель временно недоступна</h1>
        <p>MOEX Assistant продолжает работать в фоновом режиме.<br>Веб-интерфейс отключён до завершения доработок.</p>
        <span class="status">Статус: работаем в фоне</span>
    </div>
</body>
</html>
"""


@app.get("/health")
async def health(request: Request):
    assistant = getattr(request.app.state, "assistant", None)
    scheduler_running = (
        assistant.scheduler.running
        if assistant and hasattr(assistant, "scheduler")
        else False
    )
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "llm_provider": LLM_PROVIDER,
        "ollama_url": OLLAMA_URL if LLM_PROVIDER == "ollama" else None,
        "ollama_model": OLLAMA_MODEL if LLM_PROVIDER == "ollama" else None,
        "scheduler_running": scheduler_running,
    }


@app.get("/api/health")
async def api_health():
    return {"status": "ok"}


@app.get("/api/guards")
async def api_guards():
    """Guard status available even when the web UI is disabled."""
    from core import db
    cbr_meeting, cbr_pre, next_cbr = db.cbr_soft_mode_state()
    upcoming = await db.tickers_with_upcoming_dividend_cutoff(look_ahead_days=14)
    return {
        "today": datetime.now(timezone.utc).date().isoformat(),
        "cbr_soft_mode": {
            "enabled": CBR_SOFT_MODE_ENABLED,
            "is_meeting_day": cbr_meeting,
            "is_pre_meeting_day": cbr_pre,
            "next_meeting_date": next_cbr.isoformat() if next_cbr else None,
            "configured_dates": sorted(CBR_MEETING_DATES),
        },
        "upcoming_dividends": upcoming,
    }


@app.get("/{full_path:path}")
async def stub_page(request: Request, full_path: str):
    return HTMLResponse(content=STUB_HTML, status_code=503)

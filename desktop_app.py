"""MOEX Assistant — Desktop WebView launcher.

Runs the FastAPI web server in a background thread and opens
a native window (pywebview / Edge WebView2) pointing at /desktop.
"""
from __future__ import annotations

import threading
import time
import uvicorn
import webview
from loguru import logger

from api.mobile_api import app as web_app

WEB_PORT = 8456
WEB_HOST = "127.0.0.1"
URL = f"http://{WEB_HOST}:{WEB_PORT}/desktop"


def _run_server():
    """Start Uvicorn in a background thread."""
    config = uvicorn.Config(
        web_app,
        host=WEB_HOST,
        port=WEB_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.run()


def main():
    # Start server in background
    thread = threading.Thread(target=_run_server, daemon=True)
    thread.start()

    # Wait briefly for the server to come up
    time.sleep(0.8)
    logger.info(f"Opening desktop window at {URL}")

    # Create native window
    class Api:
        def exit(self):
            logger.info("Exit requested from UI")
            window.destroy()

    api = Api()

    window = webview.create_window(
        title="MOEX Assistant",
        url=URL,
        width=1300,
        height=860,
        min_size=(900, 600),
        confirm_close=False,
        js_api=api,
    )

    webview.start(
        private_mode=False,
        gui="edgechromium",  # use Edge WebView2 on Windows
    )

    logger.info("Window closed")


if __name__ == "__main__":
    main()

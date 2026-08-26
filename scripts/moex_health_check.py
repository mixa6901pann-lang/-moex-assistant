#!/usr/bin/env python3
"""Health probe for moex-assistant systemd unit.

Used by /etc/systemd/system/moex-assistant.service ExecStart= to verify
main.py is up and the scheduler is running. Exits 0 on success, 1 otherwise.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    port = os.environ.get("HEALTH_PORT", "8080")
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"health probe failed: {e}", file=sys.stderr)
        return 1
    if data.get("status") == "ok" and data.get("scheduler_running"):
        return 0
    print(f"health probe: bad payload {data}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

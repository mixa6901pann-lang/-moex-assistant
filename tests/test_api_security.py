"""Test API-key protection on write endpoints."""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("API_KEY", "test-api-key-do-not-use-in-prod")
os.environ.setdefault("WEB_UI_ENABLED", "false")

from fastapi.testclient import TestClient

from api import mobile_api
from api.mobile_api import app

TEST_KEY = "test-api-key-do-not-use-in-prod"


def _client() -> TestClient:
    return TestClient(app)


def test_health_no_auth():
    """Health endpoint must stay open without API key."""
    mobile_api.API_KEY = TEST_KEY
    response = _client().get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_write_blocked_when_key_not_configured():
    """If API_KEY is empty, write endpoints return 503."""
    mobile_api.API_KEY = ""
    client = _client()
    r = client.post("/api/run_screener", headers={})
    assert r.status_code == 503
    assert "API_KEY not configured" in r.text


def test_write_blocked_with_wrong_key():
    """Requests with wrong/missing X-API-Key return 401."""
    mobile_api.API_KEY = TEST_KEY
    client = _client()
    r = client.post("/api/run_screener", headers={})
    assert r.status_code == 401
    r2 = client.post("/api/run_screener", headers={"x-api-key": "wrong"})
    assert r2.status_code == 401


def test_add_position_with_valid_key():
    """Valid key lets us add a journal entry; clean it up afterwards."""
    mobile_api.API_KEY = TEST_KEY
    client = _client()
    payload = {"ticker": "TEST", "side": "long", "entry_px": 100.0, "qty": 1}
    response = client.post("/api/positions/add", json=payload, headers={"x-api-key": TEST_KEY})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    trade_id = data["trade_id"]

    # Cleanup the test trade
    async def _delete():
        from core import db
        conn = await db.get_db()
        await conn.execute("DELETE FROM journal WHERE id = ?", (trade_id,))
        await conn.commit()

    asyncio.run(_delete())


if __name__ == "__main__":
    test_health_no_auth()
    test_write_blocked_when_key_not_configured()
    test_write_blocked_with_wrong_key()
    test_add_position_with_valid_key()
    print("OK: API-key security tests passed")

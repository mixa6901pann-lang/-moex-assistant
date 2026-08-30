"""End-to-end test for confirming a robot proposal in sandbox/semi-auto mode.

Scenario:
- TINKOFF_SANDBOX=true, PAPER_TRADING=false, SEMI_AUTO_TRADING=true
- Create a pending robot proposal.
- POST /api/proposals/{id}/confirm with x-api-key and proposal_mode=semi_auto.
- Expect status=executed, paper position opened, prediction diary row written.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Ensure project root is on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from core import db
from api.mobile_api import app, API_KEY
from core.config import TINKOFF_SANDBOX, PAPER_TRADING, SEMI_AUTO_TRADING


async def _reset_test_state(ticker: str) -> None:
    """Remove prior test proposal/position/prediction rows for the ticker."""
    conn = await db.get_db()
    await conn.execute("DELETE FROM robot_proposals WHERE ticker = ? AND source = 'test_confirm'", (ticker,))
    await conn.execute("DELETE FROM paper_positions WHERE ticker = ? AND status = 'open'", (ticker,))
    await conn.execute("DELETE FROM predictions WHERE ticker = ? AND source = 'robot_proposal'", (ticker,))
    await conn.commit()


async def _create_test_proposal(ticker: str) -> int:
    """Create a pending sandbox proposal and return its id."""
    return await db.save_robot_proposal(
        ticker=ticker,
        side="long",
        source="test_confirm",
        signal="test",
        entry_px=250.0,
        qty=10,
        stop_px=240.0,
        take_px=270.0,
        confidence=65,
        reason="test_confirm_proposal",
        horizon="1d",
        proposal_mode="semi_auto",
    )


async def _inspect_state(ticker: str, proposal_id: int) -> dict:
    """Collect proposal, paper position, and prediction state."""
    proposal = await db.get_robot_proposal(proposal_id)
    positions = await db.get_open_paper_positions()
    position = next((p for p in positions if p["ticker"] == ticker), None)

    conn = await db.get_db()
    cursor = await conn.execute(
        "SELECT id, ticker, predicted_direction, predicted_price, environment, source "
        "FROM predictions WHERE ticker = ? ORDER BY id DESC LIMIT 1",
        (ticker,),
    )
    pred_row = await cursor.fetchone()

    return {
        "proposal": proposal,
        "position": position,
        "prediction": dict(pred_row) if pred_row else None,
    }


async def _run_test() -> dict:
    ticker = "TEST_CONFIRM"
    await _reset_test_state(ticker)
    proposal_id = await _create_test_proposal(ticker)

    client = TestClient(app)

    # Sanity: health works
    health = client.get("/api/health")
    assert health.status_code == 200, health.text

    # Confirm the proposal
    resp = client.post(
        f"/api/proposals/{proposal_id}/confirm",
        json={"decided_by": "test_runner", "proposal_mode": "semi_auto"},
        headers={"x-api-key": API_KEY},
    )

    state = await _inspect_state(ticker, proposal_id)
    await _reset_test_state(ticker)

    return {
        "config": {
            "TINKOFF_SANDBOX": TINKOFF_SANDBOX,
            "PAPER_TRADING": PAPER_TRADING,
            "SEMI_AUTO_TRADING": SEMI_AUTO_TRADING,
            "API_KEY_LEN": len(API_KEY),
        },
        "confirm_response_status": resp.status_code,
        "confirm_response_body": resp.json() if resp.status_code == 200 else resp.text,
        "proposal_status": state["proposal"]["status"] if state["proposal"] else None,
        "proposal_mode": state["proposal"]["proposal_mode"] if state["proposal"] else None,
        "position": state["position"],
        "prediction": state["prediction"],
    }


def main() -> None:
    result = asyncio.run(_run_test())
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    # Assert golden path
    assert result["confirm_response_status"] == 200, f"Expected 200, got {result['confirm_response_status']}"
    body = result["confirm_response_body"]
    assert body.get("status") == "executed", f"Expected status=executed, got {body}"
    assert result["position"] is not None, "Paper position was not opened"
    assert result["prediction"] is not None, "Diary prediction was not written"
    print("\nOK: proposal confirmation in sandbox/semi-auto mode works end-to-end.")


if __name__ == "__main__":
    main()

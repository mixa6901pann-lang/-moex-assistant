"""Tests for core/broker_executor.py.

`market_phase` is a pure function — it can be tested directly.
`can_execute_market_order` wraps it — tested through the same flow.
`check_trading_guards` and `run_broker_order_executor` need a Tinkoff
adapter and DB mocks; that integration is exercised via the live
sandbox cron job, not here.
"""
from __future__ import annotations

import inspect
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import broker_executor  # noqa: E402


def test_market_phase_weekend():
    """Saturday and Sunday are closed_weekend."""
    # 2026-08-22 is a Saturday.
    saturday = datetime(2026, 8, 22, 12, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Europe/Moscow"))
    with patch.object(broker_executor, "datetime") as mock_dt:
        mock_dt.now.return_value = saturday
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
        assert broker_executor.market_phase() == "closed_weekend"


def test_market_phase_main_session():
    """12:00 Monday is main_session."""
    monday_noon = datetime(
        2026, 8, 17, 12, 0,
        tzinfo=__import__("zoneinfo").ZoneInfo("Europe/Moscow"),
    )
    with patch.object(broker_executor, "datetime") as mock_dt:
        mock_dt.now.return_value = monday_noon
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
        assert broker_executor.market_phase() == "main_session"


def test_market_phase_pre_open():
    """06:00 Monday is pre_open (before 07:00)."""
    monday_pre = datetime(
        2026, 8, 17, 6, 0,
        tzinfo=__import__("zoneinfo").ZoneInfo("Europe/Moscow"),
    )
    with patch.object(broker_executor, "datetime") as mock_dt:
        mock_dt.now.return_value = monday_pre
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
        assert broker_executor.market_phase() == "pre_open"


def test_market_phase_evening_session():
    """20:00 Monday is evening_session."""
    monday_eve = datetime(
        2026, 8, 17, 20, 0,
        tzinfo=__import__("zoneinfo").ZoneInfo("Europe/Moscow"),
    )
    with patch.object(broker_executor, "datetime") as mock_dt:
        mock_dt.now.return_value = monday_eve
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
        assert broker_executor.market_phase() == "evening_session"


def test_can_execute_in_tradable_phase():
    """When in a tradable phase, returns (True, '')."""
    monday_main = datetime(
        2026, 8, 17, 12, 0,
        tzinfo=__import__("zoneinfo").ZoneInfo("Europe/Moscow"),
    )
    with patch.object(broker_executor, "datetime") as mock_dt:
        mock_dt.now.return_value = monday_main
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
        ok, reason = broker_executor.can_execute_market_order()
        assert ok is True
        assert reason == ""


def test_can_execute_in_auction_phase():
    """Auction phases must NOT allow market orders."""
    # 10:02 = opening_auction (600-605)
    monday_auction = datetime(
        2026, 8, 17, 10, 2,
        tzinfo=__import__("zoneinfo").ZoneInfo("Europe/Moscow"),
    )
    with patch.object(broker_executor, "datetime") as mock_dt:
        mock_dt.now.return_value = monday_auction
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)
        ok, reason = broker_executor.can_execute_market_order()
        assert ok is False
        assert "аукцион" in reason.lower()


def test_broker_executor_exports():
    """The 3 public functions must be importable and async where appropriate."""
    assert inspect.iscoroutinefunction(broker_executor.check_trading_guards)
    assert inspect.iscoroutinefunction(broker_executor.run_broker_order_executor)
    # market_phase and can_execute_market_order are sync.
    assert not inspect.iscoroutinefunction(broker_executor.market_phase)
    assert not inspect.iscoroutinefunction(broker_executor.can_execute_market_order)


# --- broker-side dedup guard (27.08.2026) ---------------------------------

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_broker_side_dedup_blocks_same_side():
    """If broker already has a long position not tracked in DB, skip proposal."""
    tinkoff = MagicMock()
    tinkoff.ready = True
    tinkoff.sandbox = True
    tinkoff.resolve_account_id = AsyncMock(return_value="acc-1")
    tinkoff.find_instrument = AsyncMock(return_value={"lot": 1})
    # Broker already holds a long OZON that we don't know about.
    tinkoff.get_portfolio = AsyncMock(return_value=SimpleNamespace(
        positions=[SimpleNamespace(ticker="OZON", quantity=10)]
    ))
    place_called = []

    async def fake_place(**kwargs):
        place_called.append(kwargs)
        return SimpleNamespace(status="EXECUTION_REPORT_STATUS_FILL",
                               executed_price=kwargs.get("expected_price", 0),
                               executed_qty=kwargs.get("lots", 1) * 1)

    tinkoff.place_market_order = fake_place

    proposal = {
        "id": 100, "ticker": "OZON", "side": "long",
        "proposal_mode": "auto_trade", "status": "pending",
        "confidence": 80, "qty": 1, "entry_px": 100.0,
        "stop_px": 95.0, "take_px": 110.0,
    }

    async def fake_get_robot_proposals(**kwargs):
        return [proposal]
    async def fake_get_broker_orders_for_proposal(pid):
        return []
    async def fake_get_open_broker_positions(**kwargs):
        return []  # local DB empty
    rejected = []

    async def fake_reject(pid, decided_by="", reject_reason=""):
        rejected.append((pid, reject_reason))

    with patch.object(broker_executor, "app_config") as cfg, \
         patch.object(broker_executor.db, "get_robot_proposals", fake_get_robot_proposals), \
         patch.object(broker_executor.db, "get_broker_orders_for_proposal", fake_get_broker_orders_for_proposal), \
         patch.object(broker_executor.db, "get_open_broker_positions", fake_get_open_broker_positions), \
         patch.object(broker_executor.db, "reject_robot_proposal", fake_reject), \
         patch.object(broker_executor, "check_trading_guards", AsyncMock(return_value=(True, ""))), \
         patch.object(broker_executor, "can_execute_market_order", lambda: (True, "")):
        cfg.PAPER_TRADING = False
        cfg.AUTO_TRADING_ENABLED = True
        cfg.AUTO_TRADE = True
        cfg.AUTO_TRADE_MIN_CONFIDENCE = 70
        cfg.MANUAL_CONFIRM_MIN_CONFIDENCE = 55

        _run(broker_executor.run_broker_order_executor(tinkoff, guard_new_position=AsyncMock(return_value=(True, ""))))

    assert place_called == [], "place_market_order must NOT be called when broker already has the position"
    assert len(rejected) == 1
    pid, reason = rejected[0]
    assert pid == 100
    assert "broker already has long OZON" in reason
    assert "not in local DB" in reason


def test_broker_side_dedup_blocks_opposite_side():
    """If broker has an opposite-side position, skip and tell user to close manually."""
    tinkoff = MagicMock()
    tinkoff.ready = True
    tinkoff.sandbox = True
    tinkoff.resolve_account_id = AsyncMock(return_value="acc-1")
    tinkoff.find_instrument = AsyncMock(return_value={"lot": 1})
    # Broker has SHORT OZON, we want to LONG it. Don't auto-revert.
    tinkoff.get_portfolio = AsyncMock(return_value=SimpleNamespace(
        positions=[SimpleNamespace(ticker="OZON", quantity=-5)]
    ))
    place_called = []

    async def fake_place(**kwargs):
        place_called.append(kwargs)
        return SimpleNamespace(status="EXECUTION_REPORT_STATUS_FILL",
                               executed_price=100.0, executed_qty=1)

    tinkoff.place_market_order = fake_place

    proposal = {
        "id": 200, "ticker": "OZON", "side": "long",
        "proposal_mode": "auto_trade", "status": "pending",
        "confidence": 80, "qty": 1, "entry_px": 100.0,
        "stop_px": 95.0, "take_px": 110.0,
    }

    rejected = []

    async def fake_get_robot_proposals(**kwargs):
        return [proposal]
    async def fake_get_broker_orders_for_proposal(pid):
        return []
    async def fake_get_open_broker_positions(**kwargs):
        return []
    async def fake_reject(pid, decided_by="", reject_reason=""):
        rejected.append((pid, reject_reason))

    with patch.object(broker_executor, "app_config") as cfg, \
         patch.object(broker_executor.db, "get_robot_proposals", fake_get_robot_proposals), \
         patch.object(broker_executor.db, "get_broker_orders_for_proposal", fake_get_broker_orders_for_proposal), \
         patch.object(broker_executor.db, "get_open_broker_positions", fake_get_open_broker_positions), \
         patch.object(broker_executor.db, "reject_robot_proposal", fake_reject), \
         patch.object(broker_executor, "check_trading_guards", AsyncMock(return_value=(True, ""))), \
         patch.object(broker_executor, "can_execute_market_order", lambda: (True, "")):
        cfg.PAPER_TRADING = False
        cfg.AUTO_TRADING_ENABLED = True
        cfg.AUTO_TRADE = True
        cfg.AUTO_TRADE_MIN_CONFIDENCE = 70
        cfg.MANUAL_CONFIRM_MIN_CONFIDENCE = 55

        _run(broker_executor.run_broker_order_executor(tinkoff, guard_new_position=AsyncMock(return_value=(True, ""))))

    assert place_called == [], "must not place when broker has opposite position"
    assert len(rejected) == 1
    pid, reason = rejected[0]
    assert pid == 200
    assert "broker has opposite short OZON" in reason
    assert "close manually" in reason


def test_broker_side_dedup_passes_when_no_conflict():
    """If broker has no position for this ticker, proceed with order."""
    tinkoff = MagicMock()
    tinkoff.ready = True
    tinkoff.sandbox = True
    tinkoff.resolve_account_id = AsyncMock(return_value="acc-1")
    tinkoff.find_instrument = AsyncMock(return_value={"lot": 1})
    # Broker holds a different ticker — should not block.
    tinkoff.get_portfolio = AsyncMock(return_value=SimpleNamespace(
        positions=[SimpleNamespace(ticker="SBER", quantity=10)]
    ))
    place_called = []

    async def fake_place(**kwargs):
        place_called.append(kwargs)
        return SimpleNamespace(status="EXECUTION_REPORT_STATUS_FILL",
                               executed_price=100.0, executed_qty=1)

    tinkoff.place_market_order = fake_place

    proposal = {
        "id": 300, "ticker": "OZON", "side": "long",
        "proposal_mode": "auto_trade", "status": "pending",
        "confidence": 80, "qty": 1, "entry_px": 100.0,
        "stop_px": 95.0, "take_px": 110.0,
    }

    rejected = []

    async def fake_get_robot_proposals(**kwargs):
        return [proposal]
    async def fake_get_broker_orders_for_proposal(pid):
        return []
    async def fake_get_open_broker_positions(**kwargs):
        return []
    async def fake_reject(pid, decided_by="", reject_reason=""):
        rejected.append((pid, reject_reason))

    with patch.object(broker_executor, "app_config") as cfg, \
         patch.object(broker_executor.db, "get_robot_proposals", fake_get_robot_proposals), \
         patch.object(broker_executor.db, "get_broker_orders_for_proposal", fake_get_broker_orders_for_proposal), \
         patch.object(broker_executor.db, "get_open_broker_positions", fake_get_open_broker_positions), \
         patch.object(broker_executor.db, "reject_robot_proposal", fake_reject), \
         patch.object(broker_executor, "check_trading_guards", AsyncMock(return_value=(True, ""))), \
         patch.object(broker_executor, "can_execute_market_order", lambda: (True, "")):
        cfg.PAPER_TRADING = False
        cfg.AUTO_TRADING_ENABLED = True
        cfg.AUTO_TRADE = True
        cfg.AUTO_TRADE_MIN_CONFIDENCE = 70
        cfg.MANUAL_CONFIRM_MIN_CONFIDENCE = 55

        _run(broker_executor.run_broker_order_executor(tinkoff, guard_new_position=AsyncMock(return_value=(True, ""))))

    assert len(place_called) == 1, "should place order when no conflict"
    assert place_called[0]["ticker"] == "OZON"
    assert place_called[0]["side"] == "long"
    assert rejected == []


def test_broker_side_dedup_falls_back_when_portfolio_fails():
    """If get_portfolio raises, log warning and proceed (don't block trading)."""
    tinkoff = MagicMock()
    tinkoff.ready = True
    tinkoff.sandbox = True
    tinkoff.resolve_account_id = AsyncMock(return_value="acc-1")
    tinkoff.find_instrument = AsyncMock(return_value={"lot": 1})
    tinkoff.get_portfolio = AsyncMock(side_effect=RuntimeError("tinkoff 503"))

    place_called = []

    async def fake_place(**kwargs):
        place_called.append(kwargs)
        return SimpleNamespace(status="EXECUTION_REPORT_STATUS_FILL",
                               executed_price=100.0, executed_qty=1)

    tinkoff.place_market_order = fake_place

    proposal = {
        "id": 400, "ticker": "OZON", "side": "long",
        "proposal_mode": "auto_trade", "status": "pending",
        "confidence": 80, "qty": 1, "entry_px": 100.0,
        "stop_px": 95.0, "take_px": 110.0,
    }

    rejected = []

    async def fake_get_robot_proposals(**kwargs):
        return [proposal]
    async def fake_get_broker_orders_for_proposal(pid):
        return []
    async def fake_get_open_broker_positions(**kwargs):
        return []
    async def fake_reject(pid, decided_by="", reject_reason=""):
        rejected.append((pid, reject_reason))

    with patch.object(broker_executor, "app_config") as cfg, \
         patch.object(broker_executor.db, "get_robot_proposals", fake_get_robot_proposals), \
         patch.object(broker_executor.db, "get_broker_orders_for_proposal", fake_get_broker_orders_for_proposal), \
         patch.object(broker_executor.db, "get_open_broker_positions", fake_get_open_broker_positions), \
         patch.object(broker_executor.db, "reject_robot_proposal", fake_reject), \
         patch.object(broker_executor, "check_trading_guards", AsyncMock(return_value=(True, ""))), \
         patch.object(broker_executor, "can_execute_market_order", lambda: (True, "")):
        cfg.PAPER_TRADING = False
        cfg.AUTO_TRADING_ENABLED = True
        cfg.AUTO_TRADE = True
        cfg.AUTO_TRADE_MIN_CONFIDENCE = 70
        cfg.MANUAL_CONFIRM_MIN_CONFIDENCE = 55

        _run(broker_executor.run_broker_order_executor(tinkoff, guard_new_position=AsyncMock(return_value=(True, ""))))

    assert len(place_called) == 1, "should still place order when portfolio fetch fails (fail-open)"
    assert rejected == []

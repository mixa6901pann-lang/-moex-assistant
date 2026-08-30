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

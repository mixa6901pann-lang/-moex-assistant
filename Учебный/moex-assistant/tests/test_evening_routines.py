"""Tests for core/evening_routines.py.

Only the pure helper _hit_stop_or_take is testable without a Tinkoff mock.
The other functions need DB + tinkoff + indicators — that's exercised via
the live sandbox cron job, not here.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import evening_routines  # noqa: E402


def test_hit_stop_long_low_below_stop():
    """Long stop hit when daily low <= stop_px."""
    hit_stop, hit_take = evening_routines._hit_stop_or_take(
        side="long", stop_px=100.0, take_px=110.0, low=99.0, high=109.0,
    )
    assert hit_stop is True
    assert hit_take is False


def test_hit_stop_long_low_above_stop():
    """Long stop not hit when daily low > stop_px."""
    hit_stop, hit_take = evening_routines._hit_stop_or_take(
        side="long", stop_px=100.0, take_px=110.0, low=101.0, high=109.0,
    )
    assert hit_stop is False
    assert hit_take is False


def test_hit_take_long_high_above_take():
    """Long take hit when daily high >= take_px."""
    hit_stop, hit_take = evening_routines._hit_stop_or_take(
        side="long", stop_px=100.0, take_px=110.0, low=101.0, high=111.0,
    )
    assert hit_stop is False
    assert hit_take is True


def test_hit_stop_short_high_above_stop():
    """Short stop hit when daily high >= stop_px."""
    hit_stop, hit_take = evening_routines._hit_stop_or_take(
        side="short", stop_px=110.0, take_px=90.0, low=99.0, high=111.0,
    )
    assert hit_stop is True
    assert hit_take is False


def test_hit_take_short_low_below_take():
    """Short take hit when daily low <= take_px."""
    hit_stop, hit_take = evening_routines._hit_stop_or_take(
        side="short", stop_px=110.0, take_px=100.0, low=99.0, high=109.0,
    )
    assert hit_stop is False
    assert hit_take is True


def test_no_stop_no_take_no_hit():
    """When stop_px and take_px are both None, neither hits."""
    hit_stop, hit_take = evening_routines._hit_stop_or_take(
        side="long", stop_px=None, take_px=None, low=99.0, high=109.0,
    )
    assert hit_stop is False
    assert hit_take is False


def test_evening_routines_async_exports():
    """All 4 public coroutines must be importable."""
    expected = [
        "run_evening_paper_check",
        "run_evening_broker_check",
        "run_intraday_broker_stop_check",
        "run_intraday_broker_reconcile",
    ]
    for name in expected:
        assert hasattr(evening_routines, name), f"missing {name}"
        assert inspect.iscoroutinefunction(getattr(evening_routines, name)), \
            f"{name} must be async"


def test_evening_routines_uses_broker_executor():
    """Source must import can_execute_market_order from broker_executor."""
    src = inspect.getsource(evening_routines)
    assert "from core.broker_executor import can_execute_market_order" in src

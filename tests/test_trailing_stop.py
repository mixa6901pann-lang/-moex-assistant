"""Tests for core.broker_utils.trailing_stop_for_side.

Mirrors the trailing-stop math in main.py at ~line 1465 (paper) and
~line 1583 (broker). If this drifts from the production code, the
unit tests will pass but real trailing will not — keep them in sync.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.broker_utils import trailing_stop_for_side  # noqa: E402


def test_long_trailing_moves_up_only():
    """For a long, the stop should only trail UP (protective direction)."""
    close = 100.0
    atr = 5.0
    mult = 1.8
    # stop_px = 100 - 5*1.8 = 91.0
    assert trailing_stop_for_side("long", close, atr, mult, current_stop=80.0) == 91.0


def test_long_does_not_move_down():
    """If the new stop is below the current stop, return None."""
    # Price at 100, ATR 5, mult 1.8 -> new_stop = 91. Current stop is 95.
    # The new stop is worse — do not move.
    assert trailing_stop_for_side("long", 100.0, 5.0, 1.8, current_stop=95.0) is None


def test_short_trailing_moves_down_only():
    """For a short, the stop should only trail DOWN."""
    close = 100.0
    atr = 5.0
    mult = 1.8
    # stop_px = 100 + 5*1.8 = 109.0
    assert trailing_stop_for_side("short", close, atr, mult, current_stop=120.0) == 109.0


def test_short_does_not_move_up():
    """If the new stop is above the current stop, return None."""
    # For short, new_stop = 109. Current stop is 105. Do not move.
    assert trailing_stop_for_side("short", 100.0, 5.0, 1.8, current_stop=105.0) is None


def test_no_current_stop_returns_new_value():
    """When the position has no stop yet (first calc), return the new stop."""
    assert trailing_stop_for_side("long", 100.0, 5.0, 1.8, current_stop=None) == 91.0
    assert trailing_stop_for_side("short", 100.0, 5.0, 1.8, current_stop=None) == 109.0


def test_invalid_inputs_return_none():
    """ATR mult <= 0 or close <= 0 must not raise."""
    assert trailing_stop_for_side("long", 0, 5.0, 1.8, current_stop=None) is None
    assert trailing_stop_for_side("long", -100, 5.0, 1.8, current_stop=None) is None
    assert trailing_stop_for_side("long", 100.0, 0, 1.8, current_stop=None) is None
    assert trailing_stop_for_side("long", 100.0, 5.0, 0, current_stop=None) is None
    assert trailing_stop_for_side("long", 100.0, -5.0, 1.8, current_stop=None) is None


def test_unknown_side_returns_none():
    """Side must be 'long' or 'short'."""
    assert trailing_stop_for_side("buy", 100.0, 5.0, 1.8, current_stop=None) is None
    assert trailing_stop_for_side("", 100.0, 5.0, 1.8, current_stop=None) is None


def test_result_is_rounded_to_2dp():
    """Mirrors `round(new_stop, 2)` in main.py."""
    # close=100.13, atr=5.0, mult=1.8 -> 100.13 - 9.0 = 91.13
    assert trailing_stop_for_side("long", 100.13, 5.0, 1.8, current_stop=80.0) == 91.13

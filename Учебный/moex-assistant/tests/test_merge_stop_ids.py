"""Tests for core.broker_utils.merge_stop_ids.

Used in _persist_stop_order_id to update the first element of
stop_order_ids (the stop-loss) while preserving the take-profit id.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.broker_utils import merge_stop_ids  # noqa: E402


def test_replace_first_keeps_take():
    """Existing [stop, take] preserves take when stop is replaced."""
    assert merge_stop_ids(["stop-old", "take-1"], "stop-new") == ["stop-new", "take-1"]


def test_replace_only_first_when_only_stop():
    """Existing [stop] -> [stop-new]."""
    assert merge_stop_ids(["stop-old"], "stop-new") == ["stop-new"]


def test_append_when_empty():
    """No existing ids -> the new id is the only entry."""
    assert merge_stop_ids([], "stop-new") == ["stop-new"]


def test_none_new_id_returns_existing_unchanged():
    """If new_stop_id is None, return the existing list unchanged."""
    assert merge_stop_ids(["stop-old", "take-1"], None) == ["stop-old", "take-1"]


def test_empty_new_id_is_ignored():
    """Empty string is falsy and treated as None."""
    assert merge_stop_ids(["stop-old", "take-1"], "") == ["stop-old", "take-1"]


def test_does_not_mutate_input():
    """Pure function — the caller's list is not modified."""
    original = ["stop-old", "take-1"]
    snapshot = list(original)
    merge_stop_ids(original, "stop-new")
    assert original == snapshot

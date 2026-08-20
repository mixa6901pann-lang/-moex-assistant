"""Tests for core.broker_utils.parse_stop_ids.

The trailing-stop path calls parse_stop_ids on every broker_order row. A
broken parser silently swallows trailing-stop updates and leaves
stale stop-orders in Tinkoff. These tests verify the parser handles
the formats actually stored in the DB.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.broker_utils import parse_stop_ids  # noqa: E402


def test_parse_none_returns_empty():
    """None / empty values must not raise."""
    assert parse_stop_ids(None) == []
    assert parse_stop_ids("") == []
    assert parse_stop_ids("[]") == []


def test_parse_json_string():
    """Standard JSON-encoded list from the DB."""
    raw = '["01a01b2b-388d-4e13", "02c02c3c-4f4f-5a5b"]'
    assert parse_stop_ids(raw) == ["01a01b2b-388d-4e13", "02c02c3c-4f4f-5a5b"]


def test_parse_already_list():
    """Some code paths pass a list directly."""
    assert parse_stop_ids(["a", "b"]) == ["a", "b"]


def test_parse_corrupted_legacy_returns_empty():
    """Legacy / corrupted rows must not crash the trailing-stop path.

    Aug 2026: a row with stop_order_ids='[' caused `ids[0]` to return
    '[' and the cancel call to fail silently.
    """
    assert parse_stop_ids("[") == []
    assert parse_stop_ids("not json") == []
    # Non-list JSON (e.g. a dict or a string) returns empty instead of crashing.
    assert parse_stop_ids('{"foo": "bar"}') == []


def test_parse_coerces_inner_to_str():
    """If a row was stored with int IDs, coerce to str."""
    assert parse_stop_ids("[123, 456]") == ["123", "456"]


def test_parse_bytes_input():
    """SQLite sometimes returns bytes for TEXT columns."""
    assert parse_stop_ids(b'["a", "b"]') == ["a", "b"]
    assert parse_stop_ids(b"corrupt") == []

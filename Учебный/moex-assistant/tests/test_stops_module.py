"""Static checks for core/stops.py.

The real broker interactions (place_stop_order, cancel_stop_order) need
a running Tinkoff sandbox and are tested via scripts/_test_*.py on the
server. These tests just guard the import surface and the function
signatures so a refactor cannot silently break the wiring.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import stops  # noqa: E402


def test_stops_public_functions_exist():
    """All 6 stop helpers must be importable from core.stops."""
    expected = [
        "attach_stop_orders",
        "reattach_stop_orders_after_recalc",
        "replace_stop_order",
        "latest_stop_order_id",
        "persist_stop_order_id",
        "cancel_open_stop_orders",
    ]
    for name in expected:
        assert hasattr(stops, name), f"missing {name} in core.stops"
        assert inspect.iscoroutinefunction(getattr(stops, name)), \
            f"{name} must be async"


def test_attach_stop_orders_signature():
    """The wrapper signature in main.py must still match the function."""
    sig = inspect.signature(stops.attach_stop_orders)
    params = list(sig.parameters.keys())
    # First 6 positional args — tinkoff, ticker, side, lots, account_id, prop
    assert params[:6] == ["tinkoff", "ticker", "side", "lots", "account_id", "prop"]
    # order_id is optional
    assert "order_id" in params


def test_replace_stop_order_signature():
    """Used by evening_broker_check trailing path."""
    sig = inspect.signature(stops.replace_stop_order)
    params = list(sig.parameters.keys())
    assert "tinkoff" in params
    assert "old_stop_order_id" in params
    assert "new_stop_price" in params


def test_stops_uses_broker_utils():
    """core/stops.py must use parse_stop_ids from core.broker_utils (single source)."""
    src = inspect.getsource(stops)
    assert "from core.broker_utils import parse_stop_ids" in src
    # Must not reimplement its own JSON parser
    assert "json.loads" not in src, "Use parse_stop_ids, not local json.loads"

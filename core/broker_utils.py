"""Pure helpers for broker-order parsing and trailing-stop calculations.

Lives in `core/` so it can be unit-tested on Windows (no fcntl, no
Tinkoff, no scheduler). Anything that depends on platform-specific
modules (fcntl, Tinkoff API, aiosqlite) stays in main.py.
"""
from __future__ import annotations

import json
from typing import Iterable


def parse_stop_ids(raw) -> list[str]:
    """Parse the stop_order_ids field from broker_orders.

    Stored as a JSON string in the DB; some legacy rows may hold `'[]'`
    or Python repr. Return an empty list on any failure rather than
    raising, so the trailing-stop path can no-op gracefully.

    Aug 2026: a row with `stop_order_ids="["` broke trailing-stop until
    we hardened this parser. Keep it defensive.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except Exception:
            return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    return []


def trailing_stop_for_side(
    side: str,
    close: float,
    current_atr: float,
    atr_mult: float,
    current_stop: float | None,
) -> float | None:
    """Compute the new trailing-stop price for an open position.

    `side` is "long" or "short". The stop only moves in the protective
    direction (long: up, short: down). Returns the new stop in
    instrument price units, or None if the inputs are invalid.

    Pure function — no DB, no broker. Mirrors the formula in
    main.py at ~line 1465 (paper) and ~line 1583 (broker).
    """
    if close <= 0 or current_atr <= 0 or atr_mult <= 0:
        return None
    side = (side or "").lower()
    if side == "long":
        new_stop = close - current_atr * atr_mult
        if current_stop is not None and new_stop <= current_stop:
            return None
        return round(new_stop, 2)
    if side == "short":
        new_stop = close + current_atr * atr_mult
        if current_stop is not None and new_stop >= current_stop:
            return None
        return round(new_stop, 2)
    return None


def merge_stop_ids(existing: Iterable[str], new_stop_id: str | None) -> list[str]:
    """Replace the first element of stop_order_ids with the new stop id.

    Preserves the take-profit id (second element) when present.
    Returns a new list; does not mutate the input.
    """
    ids = list(existing)
    if new_stop_id:
        if ids:
            ids[0] = new_stop_id
        else:
            ids = [new_stop_id]
    return ids

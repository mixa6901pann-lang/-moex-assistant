"""Pure helpers for guarding intraday signal quality.

The intraday monitor pulls 1-minute candles from MOEX ISS and may run while
the exchange is closed or while ISS is lagging the broker price. Without
gates, it can produce proposals whose entry_px is hours/days old or wildly
off the real price. These helpers are the gate logic — extracted as pure
functions so they can be unit-tested without spinning up an async pipeline.

All functions are sync and have no I/O. The async call sites live in
execution/intraday.py.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


def candle_age_minutes(candle_end_str, now=None):
    """Return age of a candle end field in minutes, or None if unparseable.

    candle_end_str is a MOEX ISS timestamp like 2026-08-17 15:18:59.
    Naive strings are treated as UTC. Returns None when the input is empty
    or cannot be parsed; callers should treat None as skip the freshness
    check, not as reject the candle.
    """
    if not candle_end_str:
        return None
    try:
        last_dt = datetime.fromisoformat(candle_end_str)
    except (ValueError, TypeError):
        return None
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    ref = now if now is not None else datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return (ref - last_dt).total_seconds() / 60.0


def price_drift_pct(entry, broker_price):
    """Return abs(entry - broker) / broker * 100.

    Returns +inf when broker_price is non-positive: caller must treat that
    as price check failed, skip the proposal, not as 0% drift.
    """
    if broker_price is None or broker_price <= 0:
        return math.inf
    return abs(entry - broker_price) / broker_price * 100.0

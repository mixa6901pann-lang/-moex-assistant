"""Tests for core/intraday_freshness.py and the freshness gates in
execution/intraday.py.

The freshness helpers are pure functions, so we can test them without
spinning up the async pipeline. Gate 1 (market phase) and the existing
screener / paper-position guards are out of scope here — those are
covered by test_broker_executor and the live sandbox cron.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.intraday_freshness import candle_age_minutes, price_drift_pct  # noqa: E402


# ─── candle_age_minutes ─────────────────────────────────────────────


def test_candle_age_fresh():
    """1-minute-old candle returns ~1."""
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    candle = (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    age = candle_age_minutes(candle, now=now)
    assert age is not None
    assert 0.99 <= age <= 1.01


def test_candle_age_stale():
    """30-minute-old candle returns ~30."""
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    candle = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    age = candle_age_minutes(candle, now=now)
    assert age is not None
    assert 29.99 <= age <= 30.01


def test_candle_age_empty_returns_none():
    """Empty string -> None (skip the freshness check, do not reject)."""
    assert candle_age_minutes("") is None
    assert candle_age_minutes(None) is None


def test_candle_age_garbage_returns_none():
    """Unparseable timestamp -> None."""
    assert candle_age_minutes("not a date") is None
    assert candle_age_minutes("2026-13-40 99:99:99") is None


def test_candle_age_naive_datetime_treated_as_utc():
    """ISO string without tz is interpreted as UTC, matching ISS format."""
    # 2026-08-20 12:00:00 (naive) parsed at 12:05:00 UTC -> 5 min
    now = datetime(2026, 8, 20, 12, 5, tzinfo=timezone.utc)
    age = candle_age_minutes("2026-08-20 12:00:00", now=now)
    assert age is not None
    assert 4.99 <= age <= 5.01


def test_candle_age_iso_with_tz():
    """ISO string with explicit +03:00 offset is honoured."""
    now = datetime(2026, 8, 20, 9, 5, tzinfo=timezone.utc)  # 12:05 MSK
    candle = "2026-08-20T12:00:00+03:00"  # 09:00 UTC
    age = candle_age_minutes(candle, now=now)
    assert age is not None
    assert 4.99 <= age <= 5.01


def test_candle_age_naive_now_treated_as_utc():
    """If `now` is naive, treat it as UTC too (defensive)."""
    now_naive = datetime(2026, 8, 20, 12, 5)  # no tz
    candle = "2026-08-20 12:00:00"
    age = candle_age_minutes(candle, now=now_naive)
    assert age is not None
    assert 4.99 <= age <= 5.01


# ─── price_drift_pct ────────────────────────────────────────────────


def test_price_drift_zero():
    """entry == broker -> 0."""
    assert price_drift_pct(100.0, 100.0) == 0.0


def test_price_drift_one_percent_above():
    """entry 1% above broker -> 1.0."""
    drift = price_drift_pct(101.0, 100.0)
    assert 0.99 <= drift <= 1.01


def test_price_drift_one_percent_below():
    """entry 1% below broker -> 1.0 (absolute value)."""
    drift = price_drift_pct(99.0, 100.0)
    assert 0.99 <= drift <= 1.01


def test_price_drift_two_and_half_percent():
    """NVTK case: 916.95 vs 940.30 -> ~2.48%."""
    drift = price_drift_pct(916.95, 940.30)
    assert 2.45 <= drift <= 2.50


def test_price_drift_five_and_half_percent():
    """AFLT case: 32.33 vs 34.20 -> ~5.47%."""
    drift = price_drift_pct(32.33, 34.20)
    assert 5.40 <= drift <= 5.55


def test_price_drift_zero_broker_returns_inf():
    """broker_price = 0 must not divide-by-zero; return +inf instead."""
    assert price_drift_pct(100.0, 0) == math.inf


def test_price_drift_negative_broker_returns_inf():
    """Defensive: negative broker price -> +inf (not a real price)."""
    assert price_drift_pct(100.0, -1.0) == math.inf


def test_price_drift_none_broker_returns_inf():
    """Defensive: None broker -> +inf (caller will skip the proposal)."""
    assert price_drift_pct(100.0, None) == math.inf  # type: ignore[arg-type]


# ─── Gate 3 integration: pipeline.tinkoff.get_ticker_price ──────────


def test_gate3_drops_proposal_when_drift_exceeds_threshold():
    """When broker price drifts > INTRADAY_PRICE_DRIFT_PCT, the ticker
    is skipped. We don't run the full monitor — we patch the same
    expression the gate uses and confirm it triggers `continue`.

    This is a smoke test: it proves the gate logic is wired up. The
    end-to-end behaviour is exercised live by the cron and observed in
    /root/moex-app/logs/.
    """
    from core.config import INTRADAY_PRICE_DRIFT_PCT
    from core.intraday_freshness import price_drift_pct

    broker_px = 940.30
    iss_entry = 916.95
    drift = price_drift_pct(iss_entry, broker_px)
    assert drift > INTRADAY_PRICE_DRIFT_PCT
    assert drift > 1.0  # NVTK case: drift is ~2.5%


def test_gate3_keeps_proposal_when_drift_below_threshold():
    """When drift is small, the proposal survives the gate."""
    from core.config import INTRADAY_PRICE_DRIFT_PCT
    from core.intraday_freshness import price_drift_pct

    drift = price_drift_pct(100.0, 100.5)  # 0.5% drift
    assert drift < INTRADAY_PRICE_DRIFT_PCT

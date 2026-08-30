"""Tests for core/dividends.py.

Three helpers: `run_update_dividends`, `get_market_price`,
`get_daily_open_price`. All take a `moex` object with two async methods
(`candles_recent`, `last_price`, `dividends`) and use the real db module
via monkeypatch.

No fcntl, no Tinkoff — runs on Windows.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import dividends  # noqa: E402


class FakeMoex:
    """Stand-in for core.moex.MoexClient — records every call."""

    def __init__(self):
        self.candles_calls: list[tuple[str, int, str]] = []
        self.last_price_calls: list[str] = []
        self.dividends_calls: list[str] = []
        # Per-ticker fixtures.
        self.candles_by_ticker: dict[str, list] = {}
        self.last_price_by_ticker: dict[str, float] = {}
        self.dividends_by_ticker: dict[str, list] = {}

    async def candles_recent(self, ticker, count=100, interval="1d"):
        self.candles_calls.append((ticker, count, interval))
        return self.candles_by_ticker.get(ticker, [])

    async def last_price(self, ticker):
        self.last_price_calls.append(ticker)
        return self.last_price_by_ticker.get(ticker)

    async def dividends(self, ticker):
        self.dividends_calls.append(ticker)
        return self.dividends_by_ticker.get(ticker, [])


class FakeDb:
    """Records save_dividends calls."""

    def __init__(self):
        self.saves: list[tuple[str, list]] = []

    async def save_dividends(self, ticker, divs):
        self.saves.append((ticker, divs))


def _patch_db(monkeypatch, fake_db):
    monkeypatch.setattr(dividends, "db", fake_db)


# --- update_dividends -------------------------------------------------------

def test_run_update_dividends_exports():
    assert callable(dividends.run_update_dividends)
    assert inspect.iscoroutinefunction(dividends.run_update_dividends)


def test_run_update_dividends_iterates_config(monkeypatch):
    """Every ticker in core.config.DIVIDEND_TICKERS must be fetched and saved."""
    from core.config import DIVIDEND_TICKERS

    moex = FakeMoex()
    fake_db = FakeDb()
    _patch_db(monkeypatch, fake_db)

    import asyncio
    count = asyncio.run(dividends.run_update_dividends(moex))

    assert count == len(DIVIDEND_TICKERS)
    assert set(moex.dividends_calls) == set(DIVIDEND_TICKERS)
    saved_tickers = {t for t, _ in fake_db.saves}
    assert saved_tickers == set(DIVIDEND_TICKERS)


def test_run_update_dividends_swallows_per_ticker_failure(monkeypatch):
    """One failing ticker must not stop the loop."""
    from core.config import DIVIDEND_TICKERS

    moex = FakeMoex()

    async def flaky_dividends(ticker):
        if ticker == DIVIDEND_TICKERS[0]:
            raise RuntimeError("network down")
        return []

    moex.dividends = flaky_dividends
    fake_db = FakeDb()
    _patch_db(monkeypatch, fake_db)

    import asyncio
    count = asyncio.run(dividends.run_update_dividends(moex))

    # First ticker failed → not counted as processed.
    assert count == len(DIVIDEND_TICKERS) - 1
    # The failed ticker must not be in saves; the rest are saved with empty list.
    saved_tickers = {t for t, _ in fake_db.saves}
    assert DIVIDEND_TICKERS[0] not in saved_tickers
    assert len(saved_tickers) == len(DIVIDEND_TICKERS) - 1


def test_run_update_dividends_returns_zero_on_full_failure(monkeypatch):
    moex = FakeMoex()

    async def always_fail(ticker):
        raise RuntimeError("boom")

    moex.dividends = always_fail
    fake_db = FakeDb()
    _patch_db(monkeypatch, fake_db)

    import asyncio
    assert asyncio.run(dividends.run_update_dividends(moex)) == 0


# --- get_market_price -------------------------------------------------------

def test_get_market_price_exports():
    assert callable(dividends.get_market_price)
    assert inspect.iscoroutinefunction(dividends.get_market_price)


def test_get_market_price_returns_close():
    """Single candle → return its close."""
    moex = FakeMoex()
    moex.candles_by_ticker["SBER"] = [{"close": 100.0}]
    import asyncio
    assert asyncio.run(dividends.get_market_price(moex, "SBER")) == 100.0


def test_get_market_price_uppercases_ticker():
    moex = FakeMoex()
    moex.candles_by_ticker["SBER"] = [{"close": 100.0}]
    import asyncio
    asyncio.run(dividends.get_market_price(moex, "sber"))
    assert moex.candles_calls[0][0] == "SBER"


def test_get_market_price_returns_none_when_no_candles():
    moex = FakeMoex()
    import asyncio
    assert asyncio.run(dividends.get_market_price(moex, "SBER")) is None


def test_get_market_price_falls_back_on_suspicious_jump():
    """When prev/close jump exceeds PRICE_JUMP_THRESHOLD_PCT, fall back to last_price."""
    from core.config import PRICE_JUMP_THRESHOLD_PCT

    threshold = PRICE_JUMP_THRESHOLD_PCT / 100
    # 10% jump — far above default 5% threshold.
    moex = FakeMoex()
    moex.candles_by_ticker["SBER"] = [
        {"close": 100.0},  # prev
        {"close": 110.0},  # current
    ]
    moex.last_price_by_ticker["SBER"] = 109.0

    import asyncio
    price = asyncio.run(dividends.get_market_price(moex, "SBER"))
    assert price == 109.0
    assert "SBER" in moex.last_price_calls


def test_get_market_price_fallback_returns_none_when_last_missing():
    from core.config import PRICE_JUMP_THRESHOLD_PCT

    moex = FakeMoex()
    moex.candles_by_ticker["SBER"] = [{"close": 100.0}, {"close": 110.0}]
    # No last_price configured.
    import asyncio
    assert asyncio.run(dividends.get_market_price(moex, "SBER")) is None


def test_get_market_price_swallows_exception():
    moex = FakeMoex()

    async def boom(ticker, count=100, interval="1d"):
        raise RuntimeError("network")

    moex.candles_recent = boom
    import asyncio
    assert asyncio.run(dividends.get_market_price(moex, "SBER")) is None


# --- get_daily_open_price ---------------------------------------------------

def test_get_daily_open_price_exports():
    assert callable(dividends.get_daily_open_price)
    assert inspect.iscoroutinefunction(dividends.get_daily_open_price)


def test_get_daily_open_price_returns_open():
    moex = FakeMoex()
    moex.candles_by_ticker["SBER"] = [{"open": 95.0, "close": 100.0}]
    import asyncio
    assert asyncio.run(dividends.get_daily_open_price(moex, "SBER")) == 95.0


def test_get_daily_open_price_falls_back_to_last():
    """Missing daily candle → fall back to last_price."""
    moex = FakeMoex()
    moex.last_price_by_ticker["SBER"] = 96.0
    import asyncio
    assert asyncio.run(dividends.get_daily_open_price(moex, "SBER")) == 96.0


def test_get_daily_open_price_returns_none_when_no_open():
    moex = FakeMoex()
    moex.candles_by_ticker["SBER"] = [{"open": None}]
    import asyncio
    assert asyncio.run(dividends.get_daily_open_price(moex, "SBER")) is None


# --- main.py wrappers -------------------------------------------------------

def test_main_uses_thin_wrappers():
    """main.py update_dividends/_market_price/_daily_open_price must delegate to core.dividends."""
    main_path = ROOT / "main.py"
    src = main_path.read_text(encoding="utf-8")
    assert "core.dividends" in src
    # All three must be thin wrappers.
    for wrapper_name, callee in [
        ("def update_dividends(self):", "run_update_dividends(self.moex)"),
        ("def _market_price(self, ticker: str)", "get_market_price(self.moex, ticker)"),
        ("def _daily_open_price(self, ticker: str)", "get_daily_open_price(self.moex, ticker)"),
    ]:
        wrapper_idx = src.index(wrapper_name)
        wrapper_block = src[wrapper_idx:wrapper_idx + 1024]
        assert callee in wrapper_block, \
            f"{wrapper_name} must call {callee}"

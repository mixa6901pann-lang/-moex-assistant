"""Tests for core/morning_screener.py.

The function `run_morning_screener` touches a lot of modules (moex client,
db, run_screener, vk_wall). We test via FakeMoex/FakeVkWall/FakeDb that
record calls and let us assert what was wired up.

No fcntl, no Tinkoff — runs on Windows.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import morning_screener  # noqa: E402


class FakeMoex:
    """Records every call to the moex client."""

    def __init__(self):
        self.candles_calls: list[tuple[str, int]] = []
        self.index_calls: list[tuple[str, str, int]] = []
        self.candles_by_ticker: dict[str, list] = {}

    async def candles_recent(self, ticker, count=100, interval="1d"):
        self.candles_calls.append((ticker, count))
        return self.candles_by_ticker.get(ticker, [])

    async def index_candles(self, index, interval="1d", count=30):
        self.index_calls.append((index, interval, count))
        return [{"close": 3000.0, "open": 2990.0}]

    async def last_price(self, ticker):
        return None


class FakeVkWall:
    """Records morning report calls."""

    def __init__(self):
        self.morning_calls = 0
        self.should_fail = False

    async def post_morning_report(self):
        self.morning_calls += 1
        if self.should_fail:
            raise RuntimeError("VK down")


import core.db as db_mod


class FakeDb:
    """Records save calls so we can verify persistence order."""

    def __init__(self):
        self.candle_saves: list[tuple[list, str, str, str]] = []
        self.screener_saves: list[list] = []

    async def save_candles(self, candles, ticker, board, interval):
        self.candle_saves.append((candles, ticker, board, interval))

    async def save_screener(self, results):
        self.screener_saves.append(results)


def _patch_db(monkeypatch, fake_db: FakeDb):
    """Replace the module-level `db` import inside morning_screener."""
    monkeypatch.setattr(morning_screener, "db", fake_db)


def test_run_morning_screener_exports():
    """Public function must be importable and async."""
    assert callable(morning_screener.run_morning_screener)
    assert inspect.iscoroutinefunction(morning_screener.run_morning_screener)


def test_run_morning_screener_signature():
    """Signature must be (moex, vk_wall) so the thin wrapper in main.py keeps working."""
    sig = inspect.signature(morning_screener.run_morning_screener)
    params = list(sig.parameters.values())
    assert len(params) == 2
    assert params[0].name == "moex"
    assert params[1].name == "vk_wall"


def test_run_morning_screener_returns_count(monkeypatch):
    """Function must return the number of tickers scored."""
    fake_moex = FakeMoex()
    fake_vk = FakeVkWall()
    fake_db = FakeDb()
    _patch_db(monkeypatch, fake_db)

    async def fake_run_screener(fetch, index_candles=None):
        # The fetch closure calls moex.candles_recent for each ticker.
        # We just count those calls.
        return []

    monkeypatch.setattr(
        "strategies.indicators.run_screener", fake_run_screener, raising=True
    )

    import asyncio
    result = asyncio.run(morning_screener.run_morning_screener(fake_moex, fake_vk))
    assert isinstance(result, int)
    assert result == 0  # fake_run_screener returns []


def test_run_morning_screener_persists_candles(monkeypatch):
    """Each watchlist ticker must have its daily candles persisted before scoring."""
    from core.config import WATCHLIST

    fake_moex = FakeMoex()
    fake_vk = FakeVkWall()
    fake_db = FakeDb()
    _patch_db(monkeypatch, fake_db)

    async def fake_run_screener(fetch, index_candles=None):
        return [{"ticker": "TEST", "score": 5}]

    monkeypatch.setattr(
        "strategies.indicators.run_screener", fake_run_screener, raising=True
    )

    import asyncio
    asyncio.run(morning_screener.run_morning_screener(fake_moex, fake_vk))

    # Every watchlist ticker must have its candles saved (TQBR board, 1d interval).
    saved_tickers = {s[1] for s in fake_db.candle_saves}
    assert set(WATCHLIST).issubset(saved_tickers), \
        f"missing tickers: {set(WATCHLIST) - saved_tickers}"
    assert all(s[2] == "TQBR" for s in fake_db.candle_saves)
    assert all(s[3] == "1d" for s in fake_db.candle_saves)


def test_run_morning_screener_fetches_imoex(monkeypatch):
    """IMOEX index candles must be fetched for relative strength scoring."""
    fake_moex = FakeMoex()
    fake_vk = FakeVkWall()
    fake_db = FakeDb()
    _patch_db(monkeypatch, fake_db)

    async def fake_run_screener(fetch, index_candles=None):
        return []

    monkeypatch.setattr(
        "strategies.indicators.run_screener", fake_run_screener, raising=True
    )

    import asyncio
    asyncio.run(morning_screener.run_morning_screener(fake_moex, fake_vk))

    assert len(fake_moex.index_calls) == 1
    assert fake_moex.index_calls[0][0] == "IMOEX"


def test_run_morning_screener_persists_results(monkeypatch):
    """Screener results must be persisted via db.save_screener."""
    fake_moex = FakeMoex()
    fake_vk = FakeVkWall()
    fake_db = FakeDb()
    _patch_db(monkeypatch, fake_db)

    sentinel_results = [{"ticker": "AAPL", "score": 10}]

    async def fake_run_screener(fetch, index_candles=None):
        return sentinel_results

    monkeypatch.setattr(
        "strategies.indicators.run_screener", fake_run_screener, raising=True
    )

    import asyncio
    count = asyncio.run(morning_screener.run_morning_screener(fake_moex, fake_vk))

    assert count == len(sentinel_results)
    assert fake_db.screener_saves == [sentinel_results]


def test_run_morning_screener_swallows_vk_failure(monkeypatch):
    """VK morning post failure must be logged but not crash the screener."""
    fake_moex = FakeMoex()
    fake_vk = FakeVkWall()
    fake_vk.should_fail = True
    fake_db = FakeDb()
    _patch_db(monkeypatch, fake_db)

    async def fake_run_screener(fetch, index_candles=None):
        return []

    monkeypatch.setattr(
        "strategies.indicators.run_screener", fake_run_screener, raising=True
    )

    import asyncio
    # Must not raise.
    asyncio.run(morning_screener.run_morning_screener(fake_moex, fake_vk))


def test_run_morning_screener_swallows_candle_failure(monkeypatch):
    """If one ticker fails to fetch, screener must keep going for the others."""
    from core.config import WATCHLIST

    fake_moex = FakeMoex()

    async def flaky_candles(ticker, count=100, interval="1d"):
        if ticker == WATCHLIST[0]:
            raise RuntimeError("network down")
        return [{"close": 100.0}]

    fake_moex.candles_recent = flaky_candles
    fake_vk = FakeVkWall()
    fake_db = FakeDb()
    _patch_db(monkeypatch, fake_db)

    async def fake_run_screener(fetch, index_candles=None):
        return []

    monkeypatch.setattr(
        "strategies.indicators.run_screener", fake_run_screener, raising=True
    )

    import asyncio
    asyncio.run(morning_screener.run_morning_screener(fake_moex, fake_vk))
    # First ticker failed → not saved. Others succeeded → saved.
    saved_tickers = {s[1] for s in fake_db.candle_saves}
    assert WATCHLIST[0] not in saved_tickers
    assert len(saved_tickers) == len(WATCHLIST) - 1


def test_main_uses_thin_wrapper():
    """main.py morning_screener must be a thin wrapper delegating to core.morning_screener."""
    main_path = ROOT / "main.py"
    src = main_path.read_text(encoding="utf-8")
    assert "core.morning_screener" in src, \
        "main.py must import from core.morning_screener"
    wrapper_idx = src.index("def morning_screener(self):")
    wrapper_block = src[wrapper_idx:wrapper_idx + 1024]
    assert "from core.morning_screener import run_morning_screener" in wrapper_block
    assert "run_morning_screener(self.moex, self.vk_wall)" in wrapper_block

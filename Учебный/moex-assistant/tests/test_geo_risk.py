"""Tests for core/geo_risk.py.

`run_geo_risk_scan` touches the georisk LLM agent, the DB, and (conditionally)
the VK wall. We patch all three at import time and exercise both branches:
high risk → exit positions + alert, and low risk → just persist.
"""
from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import geo_risk  # noqa: E402


@dataclass
class FakeSector:
    sector: str
    direction: int


@dataclass
class FakeGeoResult:
    score: int = 8
    severity: str = "high"
    summary: str = "Crisis in test region"
    affected_sectors: list[dict] = field(default_factory=list)
    trigger_keywords: list[str] = field(default_factory=list)
    news_items: list[dict] = field(default_factory=list)
    overall_direction: int = -1


class FakeAgent:
    """Stands in for `core.georisk_agent.agent`."""

    def __init__(self, result: FakeGeoResult | None):
        self.result = result
        self.scan_calls = 0

    async def scan(self):
        self.scan_calls += 1
        return self.result


class FakeDb:
    """Records all DB calls."""

    def __init__(self):
        self.georisk_saves: list[dict] = []
        self.exit_proposals: list[dict] = []
        self.paper_closes: list[tuple[int, float, str]] = []
        self.open_positions: list[dict] = []
        self.latest_georisk: dict | None = None

    async def save_georisk(self, **kwargs):
        self.georisk_saves.append(kwargs)

    async def save_robot_proposal(self, **kwargs):
        self.exit_proposals.append(kwargs)
        # Return a synthetic id so any downstream code can reference it.
        return len(self.exit_proposals)

    async def close_paper_position(self, pos_id, exit_px, reason):
        self.paper_closes.append((pos_id, exit_px, reason))

    async def get_open_paper_positions(self):
        return list(self.open_positions)

    async def get_latest_georisk(self):
        return self.latest_georisk


class FakeVkWall:
    """Records VK alerts posted."""

    def __init__(self):
        self.alerts: list[tuple[str, str]] = []

    async def post_alert(self, kind, text):
        self.alerts.append((kind, text))


def _patch(monkeypatch, fake_db, fake_agent, fake_vk_wall):
    monkeypatch.setattr(geo_risk, "db", fake_db)
    monkeypatch.setattr(
        "core.georisk_agent.agent", fake_agent, raising=False
    )
    if fake_vk_wall is None:
        return
    monkeypatch.setattr(
        "bot.vk_wall.VkWallPoster", lambda: fake_vk_wall, raising=False
    )


# --- pure helpers ----------------------------------------------------------

def test_sector_for_ticker_known():
    from core.config import TICKER_SECTORS
    # Pick any ticker that has a sector defined.
    sample = next(iter(TICKER_SECTORS))
    assert geo_risk._sector_for_ticker(sample) == TICKER_SECTORS[sample]


def test_sector_for_ticker_unknown_returns_none():
    assert geo_risk._sector_for_ticker("ZZZZ_NOT_A_TICKER") is None


def test_sector_for_ticker_uppercases():
    from core.config import TICKER_SECTORS
    sample = next(iter(TICKER_SECTORS))
    assert geo_risk._sector_for_ticker(sample.lower()) == TICKER_SECTORS[sample]


def test_sector_impact_finds_matching():
    geo = {"affected_sectors": [{"sector": "energy", "direction": -1}]}
    assert geo_risk._sector_impact(geo, "energy") == {"sector": "energy", "direction": -1}


def test_sector_impact_missing_sector():
    geo = {"affected_sectors": [{"sector": "energy", "direction": -1}]}
    assert geo_risk._sector_impact(geo, "tech") is None


def test_sector_impact_no_sector():
    geo = {"affected_sectors": [{"sector": "energy", "direction": -1}]}
    assert geo_risk._sector_impact(geo, None) is None


def test_sector_impact_no_geo():
    assert geo_risk._sector_impact({}, "energy") is None
    assert geo_risk._sector_impact(None, "energy") is None


# --- main entry point -------------------------------------------------------

def test_run_geo_risk_scan_exports():
    assert callable(geo_risk.run_geo_risk_scan)
    assert inspect.iscoroutinefunction(geo_risk.run_geo_risk_scan)


def test_run_geo_risk_scan_signature():
    sig = inspect.signature(geo_risk.run_geo_risk_scan)
    params = list(sig.parameters.values())
    assert len(params) == 2
    assert params[0].name == "market_open_fn"
    assert params[1].name == "market_price_fn"


def test_scan_returns_true_when_result_found(monkeypatch):
    """High-risk result must be persisted, and function returns True."""
    fake_db = FakeDb()
    fake_agent = FakeAgent(result=FakeGeoResult(score=8))
    _patch(monkeypatch, fake_db, fake_agent, FakeVkWall())

    market_open = lambda: True
    market_price = lambda t: 100.0

    import asyncio
    found = asyncio.run(geo_risk.run_geo_risk_scan(market_open, market_price))
    assert found is True
    assert len(fake_db.georisk_saves) == 1
    assert fake_db.georisk_saves[0]["score"] == 8
    assert fake_agent.scan_calls == 1


def test_scan_returns_false_when_no_news(monkeypatch):
    fake_db = FakeDb()
    fake_agent = FakeAgent(result=None)
    _patch(monkeypatch, fake_db, fake_agent, FakeVkWall())

    market_open = lambda: True
    market_price = lambda t: 100.0

    import asyncio
    found = asyncio.run(geo_risk.run_geo_risk_scan(market_open, market_price))
    assert found is False
    assert fake_db.georisk_saves == []
    assert fake_agent.scan_calls == 1


def test_scan_swallows_agent_exception(monkeypatch):
    """If georisk_agent.scan() throws, scan must log a warning and return False."""
    fake_db = FakeDb()

    class BrokenAgent:
        async def scan(self):
            raise RuntimeError("LLM down")

    _patch(monkeypatch, fake_db, BrokenAgent(), FakeVkWall())

    market_open = lambda: True
    market_price = lambda t: 100.0

    import asyncio
    found = asyncio.run(geo_risk.run_geo_risk_scan(market_open, market_price))
    assert found is False


def test_high_risk_exits_long_in_negative_sector(monkeypatch):
    """Long position in a negatively-impacted sector must trigger a paper close."""
    from core.config import TICKER_SECTORS

    # Pick a ticker that has a known sector.
    ticker, sector = next(iter(TICKER_SECTORS.items()))

    fake_db = FakeDb()
    fake_db.open_positions = [
        {
            "id": 42,
            "ticker": ticker,
            "side": "long",
            "entry_px": 90.0,
            "qty": 1,
            "stop_px": 80.0,
            "take_px": 110.0,
        }
    ]
    fake_db.latest_georisk = {
        "score": 8,
        "affected_sectors": [{"sector": sector, "direction": -1}],
        "overall_direction": -1,
    }
    fake_agent = FakeAgent(result=FakeGeoResult(score=8))
    _patch(monkeypatch, fake_db, fake_agent, FakeVkWall())

    market_open = lambda: True

    async def market_price(t):
        return 95.0

    import asyncio
    asyncio.run(geo_risk.run_geo_risk_scan(market_open, market_price))

    # Paper close recorded (since PAPER_TRADING or TINKOFF_SANDBOX is true in sandbox).
    assert len(fake_db.paper_closes) == 1
    assert fake_db.paper_closes[0][0] == 42
    assert fake_db.paper_closes[0][1] == 95.0


def test_high_risk_deferred_when_market_closed(monkeypatch):
    """If market is closed, the intra-day exit must be deferred."""
    from core.config import TICKER_SECTORS
    ticker = next(iter(TICKER_SECTORS))

    fake_db = FakeDb()
    fake_db.open_positions = [
        {"id": 1, "ticker": ticker, "side": "long", "entry_px": 90.0, "qty": 1,
         "stop_px": 80.0, "take_px": 110.0}
    ]
    fake_db.latest_georisk = {"score": 8, "affected_sectors": [], "overall_direction": -1}
    fake_agent = FakeAgent(result=FakeGeoResult(score=8))
    _patch(monkeypatch, fake_db, fake_agent, FakeVkWall())

    market_open = lambda: False
    market_price = lambda t: 100.0

    import asyncio
    asyncio.run(geo_risk.run_geo_risk_scan(market_open, market_price))

    # No paper close — deferred.
    assert fake_db.paper_closes == []
    # But the georisk record itself is still saved.
    assert len(fake_db.georisk_saves) == 1


def test_high_risk_holds_short_in_negative_sector(monkeypatch):
    """Short positions in negatively-impacted sectors profit, so they stay open."""
    from core.config import TICKER_SECTORS
    ticker, sector = next(iter(TICKER_SECTORS.items()))

    fake_db = FakeDb()
    fake_db.open_positions = [
        {"id": 1, "ticker": ticker, "side": "short", "entry_px": 110.0, "qty": 1,
         "stop_px": 120.0, "take_px": 90.0}
    ]
    fake_db.latest_georisk = {
        "score": 8,
        "affected_sectors": [{"sector": sector, "direction": -1}],
        "overall_direction": -1,
    }
    fake_agent = FakeAgent(result=FakeGeoResult(score=8))
    _patch(monkeypatch, fake_db, fake_agent, FakeVkWall())

    market_open = lambda: True
    market_price = lambda t: 100.0

    import asyncio
    asyncio.run(geo_risk.run_geo_risk_scan(market_open, market_price))

    # Short held — no paper close.
    assert fake_db.paper_closes == []


def test_main_uses_thin_wrapper():
    """main.py geo_risk_scan must be a thin wrapper delegating to core.geo_risk."""
    main_path = ROOT / "main.py"
    src = main_path.read_text(encoding="utf-8")
    assert "core.geo_risk" in src
    wrapper_idx = src.index("def geo_risk_scan(self):")
    wrapper_block = src[wrapper_idx:wrapper_idx + 1024]
    assert "from core.geo_risk import run_geo_risk_scan" in wrapper_block
    assert "market_open_fn=_is_market_open" in wrapper_block
    assert "market_price_fn=self._market_price" in wrapper_block

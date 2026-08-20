"""Tests for core/rss_sentiment.py.

The real `RssNewsAggregator` hits the network and the real `sentiment_agent`
calls the LLM. We patch both at import time with fakes that record calls.
"""
from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import rss_sentiment  # noqa: E402


@dataclass
class FakeItem:
    """Stand-in for rss_feed.NewsItem."""

    _hash: str
    title: str
    source: str
    published: datetime | None = None


@dataclass
class FakeWeightedItem:
    """Stand-in for sentiment_agent.WeightedNewsItem."""

    text: str
    published_at: datetime | None
    source: str
    age_minutes: int | None
    weight: float = 1.0


@dataclass
class FakeSentimentResult:
    sentiment: str = "neutral"
    confidence: int = 50
    summary: str = "ok"
    key_topics: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)


class FakeAggregator:
    """Records fetch_unique_for_ticker calls."""

    def __init__(self, items_by_ticker: dict[str, list[FakeItem]] | None = None):
        self.items_by_ticker = items_by_ticker or {}
        self.fetch_calls: list[tuple[str, int]] = []

    async def fetch_unique_for_ticker(self, ticker, max_age_minutes=240, exclude_hashes=None):
        self.fetch_calls.append((ticker, max_age_minutes))
        return self.items_by_ticker.get(ticker, [])


class FakeAgent:
    """Records analyze() calls and returns a canned result."""

    def __init__(self, result: FakeSentimentResult | None = None):
        self.result = result or FakeSentimentResult()
        self.analyze_calls: list[tuple[str, list]] = []

    async def analyze(self, ticker, weighted):
        self.analyze_calls.append((ticker, list(weighted)))
        return self.result


class FakeDb:
    """Records every db call the function makes."""

    def __init__(self):
        self.seen_hashes_calls: list[str] = []
        self.mark_seen_calls: list[dict[str, Any]] = []
        self.sentiment_saves: list[dict[str, Any]] = []

    async def get_seen_rss_hashes(self, ticker):
        self.seen_hashes_calls.append(ticker)
        return set()

    async def mark_rss_hash_seen(self, **kwargs):
        self.mark_seen_calls.append(kwargs)

    async def save_sentiment(self, **kwargs):
        self.sentiment_saves.append(kwargs)


def _patch_modules(monkeypatch, fake_db, fake_aggregator, fake_agent):
    """Patch db + rss_feed + sentiment_agent at import sites inside rss_sentiment."""
    monkeypatch.setattr(rss_sentiment, "db", fake_db)
    fake_feed_module = SimpleNamespace(RssNewsAggregator=lambda: fake_aggregator)
    monkeypatch.setattr("core.rss_feed.RssNewsAggregator", lambda: fake_aggregator)
    monkeypatch.setattr("core.sentiment_agent.agent", fake_agent, raising=False)


def test_run_rss_sentiment_scan_exports():
    """Function must be importable and async."""
    assert callable(rss_sentiment.run_rss_sentiment_scan)
    assert inspect.iscoroutinefunction(rss_sentiment.run_rss_sentiment_scan)


def test_run_rss_sentiment_scan_no_args():
    """Signature must be no-args — pure side effect on the watchlist."""
    sig = inspect.signature(rss_sentiment.run_rss_sentiment_scan)
    assert len(sig.parameters) == 0


def test_scan_processes_all_watchlist_tickers(monkeypatch):
    """Each watchlist ticker must be queried through the aggregator."""
    from core.config import WATCHLIST

    fake_db = FakeDb()
    fake_aggregator = FakeAggregator()
    fake_agent = FakeAgent()
    _patch_modules(monkeypatch, fake_db, fake_aggregator, fake_agent)

    import asyncio
    asyncio.run(rss_sentiment.run_rss_sentiment_scan())

    fetched_tickers = [c[0] for c in fake_aggregator.fetch_calls]
    assert set(fetched_tickers) == set(WATCHLIST)


def test_scan_skips_tickers_with_no_items(monkeypatch):
    """Tickers returning empty lists must NOT trigger LLM analyze or sentiment save."""
    from core.config import WATCHLIST

    # First ticker has news, others do not.
    fake_db = FakeDb()
    items = [
        FakeItem(
            _hash="abc123",
            title="Good news",
            source="finam",
            published=datetime.now(timezone.utc),
        )
    ]
    fake_aggregator = FakeAggregator(
        items_by_ticker={WATCHLIST[0]: items}
    )
    fake_agent = FakeAgent()
    _patch_modules(monkeypatch, fake_db, fake_aggregator, fake_agent)

    import asyncio
    asyncio.run(rss_sentiment.run_rss_sentiment_scan())

    # Only one analyze() call, only one save_sentiment.
    assert len(fake_agent.analyze_calls) == 1
    assert fake_agent.analyze_calls[0][0] == WATCHLIST[0]
    assert len(fake_db.sentiment_saves) == 1


def test_scan_marks_hashes_seen(monkeypatch):
    """Fetched items must be marked as seen via db.mark_rss_hash_seen."""
    from core.config import WATCHLIST

    items = [
        FakeItem(
            _hash=f"hash-{i}",
            title=f"Headline {i}",
            source="finam",
            published=datetime.now(timezone.utc),
        )
        for i in range(3)
    ]
    fake_db = FakeDb()
    fake_aggregator = FakeAggregator(items_by_ticker={WATCHLIST[0]: items})
    fake_agent = FakeAgent()
    _patch_modules(monkeypatch, fake_db, fake_aggregator, fake_agent)

    import asyncio
    asyncio.run(rss_sentiment.run_rss_sentiment_scan())

    assert len(fake_db.mark_seen_calls) == 3
    assert {c["hash"] for c in fake_db.mark_seen_calls} == {"hash-0", "hash-1", "hash-2"}
    assert all(c["ticker"] == WATCHLIST[0] for c in fake_db.mark_seen_calls)


def test_scan_limits_batch_to_five(monkeypatch):
    """More than 5 items must be trimmed to top 5 freshest."""
    from core.config import WATCHLIST

    now = datetime.now(timezone.utc)
    # 10 items with ages 0..9 min — only 5 freshest should reach the agent.
    items = [
        FakeItem(
            _hash=f"h-{i}",
            title=f"Item {i}",
            source="finam",
            published=now,
        )
        for i in range(10)
    ]
    fake_db = FakeDb()
    fake_aggregator = FakeAggregator(items_by_ticker={WATCHLIST[0]: items})
    fake_agent = FakeAgent()
    _patch_modules(monkeypatch, fake_db, fake_aggregator, fake_agent)

    import asyncio
    asyncio.run(rss_sentiment.run_rss_sentiment_scan())

    weighted_batch = fake_agent.analyze_calls[0][1]
    assert len(weighted_batch) == 5
    # All 10 items are still marked as seen (so we don't re-fetch them).
    assert len(fake_db.mark_seen_calls) == 10


def test_scan_persists_sentiment_record(monkeypatch):
    """Aggregated sentiment must be saved with averaged age/weight and source prefix."""
    from core.config import WATCHLIST

    now = datetime.now(timezone.utc)
    items = [
        FakeItem(_hash="h1", title="T1", source="finam", published=now),
        FakeItem(_hash="h2", title="T2", source="finam", published=now),
    ]
    fake_db = FakeDb()
    fake_aggregator = FakeAggregator(items_by_ticker={WATCHLIST[0]: items})
    fake_agent = FakeAgent(result=FakeSentimentResult(
        sentiment="positive", confidence=80, summary="good",
        key_topics=["earnings"], risk_flags=[],
    ))
    _patch_modules(monkeypatch, fake_db, fake_aggregator, fake_agent)

    import asyncio
    asyncio.run(rss_sentiment.run_rss_sentiment_scan())

    save = fake_db.sentiment_saves[0]
    assert save["ticker"] == WATCHLIST[0]
    assert save["sentiment"] == "positive"
    assert save["confidence"] == 80
    assert save["topics"] == ["earnings"]
    assert save["source"].startswith("rss:")
    assert save["news_hash"] is None


def test_scan_swallows_per_ticker_failure(monkeypatch):
    """If aggregator throws for one ticker, others must still be scanned."""
    from core.config import WATCHLIST

    class BrokenAggregator(FakeAggregator):
        async def fetch_unique_for_ticker(self, ticker, max_age_minutes=240, exclude_hashes=None):
            if ticker == WATCHLIST[0]:
                raise RuntimeError("network down")
            return []

    fake_db = FakeDb()
    _patch_modules(monkeypatch, fake_db, BrokenAggregator(), FakeAgent())

    import asyncio
    # Must not raise.
    asyncio.run(rss_sentiment.run_rss_sentiment_scan())


def test_main_uses_thin_wrapper():
    """main.py rss_sentiment_scan must be a thin wrapper delegating to core.rss_sentiment."""
    main_path = ROOT / "main.py"
    src = main_path.read_text(encoding="utf-8")
    assert "core.rss_sentiment" in src
    wrapper_idx = src.index("def rss_sentiment_scan(self):")
    wrapper_block = src[wrapper_idx:wrapper_idx + 1024]
    assert "from core.rss_sentiment import run_rss_sentiment_scan" in wrapper_block
    assert "await run_rss_sentiment_scan()" in wrapper_block

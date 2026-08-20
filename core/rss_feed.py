"""RSS news aggregator for MOEX tickers.

Fetches news from configured RSS feeds, filters by ticker relevance,
and returns structured items for sentiment analysis.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from loguru import logger


@dataclass(frozen=True)
class NewsItem:
    """Single news article from RSS."""

    title: str
    link: str
    source: str
    published: datetime | None
    summary: str | None
    _hash: str

    def __hash__(self) -> int:
        return hash(self._hash)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, NewsItem):
            return self._hash == other._hash
        return False


# RSS feed templates per source.
# Key = source name, value = callable(ticker) -> url or None.
# Some sources are ticker-specific; others return a broad market feed and
# we filter by company/ticker name in the aggregator.
RSS_SOURCES: dict[str, Any] = {
    "rbc": lambda _: "https://rssexport.rbc.ru/rbcnews/news/30/full.rss",
    "finam": lambda t: f"https://www.finam.ru/analysis/newsitem/rss/?ticker={t}",
    "interfax": lambda _: "https://www.interfax.ru/rss.asp",
    "vedomosti": lambda _: "https://www.vedomosti.ru/rss/news",
    "vedomosti_articles": lambda _: "https://www.vedomosti.ru/rss/articles",
    "kommersant": lambda _: "https://www.kommersant.ru/rss/news.xml",
    "investing": lambda _: "https://www.investing.com/rss/news.rss",
    "investing_stock": lambda _: "https://www.investing.com/rss/stock.rss",
    "tradingview": lambda _: "https://www.tradingview.com/feed",
}


class RssNewsAggregator:
    """Fetch and parse RSS feeds for a list of tickers."""

    def __init__(self, seen_hashes: set[str] | None = None) -> None:
        self.seen = seen_hashes or set()

    async def fetch_for_ticker(
        self, ticker: str, max_age_minutes: int = 20, exclude_hashes: set[str] | None = None
    ) -> list[NewsItem]:
        """Fetch fresh news for a single ticker from all configured sources.

        Args:
            ticker: MOEX ticker (e.g. "SBER").
            max_age_minutes: Ignore items older than this.

        Returns:
            List of unique NewsItem objects newer than max_age.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        results: list[NewsItem] = []
        tasks = []

        # Company name aliases for broad-market feed filtering.
        # Keep ticker itself so ticker-specific feeds still work.
        ticker_aliases = {
            "SBER": ["Сбер", "Сбербанк"],
            "GAZP": ["Газпром"],
            "LKOH": ["Лукойл"],
            "NVTK": ["Новатэк"],
            "GMKN": ["Норникель"],
            "CHMF": ["Северсталь"],
            "MAGN": ["ММК", "Магнитогорск"],
            "NLMK": ["НЛМК"],
            "ALRS": ["Алроса"],
            "YNDX": ["Яндекс"],
            "OZON": ["Ozon"],
            "TCSG": ["ТКС", "Тинькофф"],
            "VTBR": ["ВТБ"],
            "ROSN": ["Роснефть"],
            "TATN": ["Татнефть"],
            "SNGS": ["Сургутнефтегаз"],
            "IRAO": ["Интер РАО"],
            "PHOR": ["ФосАгро"],
            "PLZL": ["Полюс"],
            "FIVE": ["X5", "Пятерочка"],
            "MTSS": ["МТС"],
            "RTKM": ["Ростелеком"],
            "AFKS": ["АФК Система"],
            "AFLT": ["Аэрофлот"],
            "MOEX": ["Московская биржа", "Биржа"],
        }
        names = [ticker] + [n.lower() for n in ticker_aliases.get(ticker, [])]

        # Tickers that are Russian depositary receipts / foreign listings.
        # Their RSS feeds (especially TradingView/Investing) mostly emit
        # unrelated US/international headlines, so we skip broad-market
        # sources for them entirely.
        foreign_tickers = {"ASTR", "VKCO", "OKEY", "FIVE", "QIWI", "YNDX"}
        is_foreign = ticker.upper() in foreign_tickers

        for source_name, url_fn in RSS_SOURCES.items():
            url = url_fn(ticker)
            if not url:
                continue
            is_broad = source_name in (
                "rbc", "interfax", "vedomosti", "vedomosti_articles",
                "kommersant", "investing", "investing_stock", "tradingview",
            )
            # Skip broad foreign feeds for DR/foreign tickers to avoid
            # irrelevant US/international headlines dominating the batch.
            if is_foreign and is_broad:
                continue
            tasks.append(self._fetch_one(source_name, url, ticker, cutoff, names if is_broad else None))

        per_source = await asyncio.gather(*tasks, return_exceptions=True)
        for batch in per_source:
            if isinstance(batch, Exception):
                logger.warning(f"RSS fetch error: {batch}")
                continue
            results.extend(batch)

        # Deduplicate across sources by hash and respect external exclusion list
        excluded = exclude_hashes or set()
        unique: dict[str, NewsItem] = {}
        for item in results:
            if item._hash in self.seen or item._hash in excluded:
                continue
            if item._hash not in unique:
                unique[item._hash] = item
        return list(unique.values())

    @staticmethod
    def _clean_title_hash(title: str) -> str:
        """Return a normalized hash of the headline for strict deduplication.

        Strips punctuation, whitespace, case and common filler words so the
        same headline published with different pubDate/summary/link still
        maps to one key.
        """
        import re
        lower = title.lower()
        lower = re.sub(r"[^a-zа-я0-9\s]", " ", lower)
        lower = re.sub(r"\b(по|на|в|с|к|за|от|и|или|но|что|это|который|год|г\.|руб|rub|млрд|млн|тыс|шт|акций|акция|компании|компания|сегодня|вчера|сейчас|уже|еще|был|была|будет|стал|стала|объявил|объявила|сообщил|сообщила|по данным|по словам|из за|из-за|после|ранее)\b", " ", lower)
        words = sorted({w for w in lower.split() if len(w) > 1})
        key = " ".join(words)
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    async def fetch_unique_for_ticker(
        self,
        ticker: str,
        max_age_minutes: int = 20,
        exclude_hashes: set[str] | None = None,
    ) -> list[NewsItem]:
        """Fetch fresh news and collapse near-duplicate headlines across sources."""
        raw = await self.fetch_for_ticker(ticker, max_age_minutes=max_age_minutes, exclude_hashes=exclude_hashes)
        seen_clean_hashes: set[str] = set()
        unique: list[NewsItem] = []
        for item in raw:
            clean_hash = self._clean_title_hash(item.title)
            if clean_hash in seen_clean_hashes:
                continue
            seen_clean_hashes.add(clean_hash)
            unique.append(item)
        return unique

    async def _fetch_one(
        self,
        source: str,
        url: str,
        ticker: str,
        cutoff: datetime,
        names_filter: list[str] | None = None,
    ) -> list[NewsItem]:
        """Fetch and parse a single RSS feed.

        Args:
            names_filter: For broad market feeds, only keep items whose title
                or summary contains the ticker or a company name alias.
        """
        items: list[NewsItem] = []
        body: str | None = None
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                r = await client.get(url, headers={"user-agent": "Mozilla/5.0"})
                r.raise_for_status()
                content_type = r.headers.get("content-type", "")
                if "html" in content_type.lower():
                    logger.debug(f"RSS {source} returned HTML for {ticker}, will try fallback")
                else:
                    body = r.text
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 410):
                logger.debug(f"RSS {source} {e.response.status_code} for {ticker}, will try fallback")
            else:
                logger.warning(f"RSS {source} HTTP {e.response.status_code} for {ticker}, will try fallback")
        except Exception as e:
            logger.debug(f"RSS {source} direct fetch failed for {ticker}: {e}, will try fallback")

        # Try direct XML parse if body looks like RSS/Atom
        if body:
            stripped = body.lstrip("﻿").lstrip()[:200].lower()
            if stripped.startswith("<") and ("rss" in stripped or "feed" in stripped or "channel" in stripped):
                try:
                    root = ET.fromstring(body)
                    channel = root.find("channel")
                    if channel is not None:
                        for entry in channel.findall("item"):
                            item = self._parse_rss_item(entry, source)
                            if item and item.published and item.published >= cutoff:
                                items.append(item)
                    else:
                        ns = {"atom": "http://www.w3.org/2005/Atom"}
                        for entry in root.findall("atom:entry", ns):
                            item = self._parse_atom_entry(entry, source)
                            if item and item.published and item.published >= cutoff:
                                items.append(item)
                except Exception as e:
                    logger.debug(f"RSS {source} XML parse failed for {ticker}: {e}")

        # Fallback to rss2json API for feeds that are blocked directly
        if body is None and not items:
            fallback = await self._fetch_rss2json(url, source, cutoff)
            if fallback:
                logger.debug(f"RSS {source} via rss2json: {len(fallback)} items for {ticker}")
                items = fallback

        # For broad market feeds, apply ticker/company name filter.
        if names_filter:
            lower_names = [n.lower() for n in names_filter]
            filtered: list[NewsItem] = []
            for item in items:
                combined = (item.title or "") + " " + (item.summary or "")
                combined_lower = combined.lower()
                if any(n in combined_lower for n in lower_names):
                    filtered.append(item)
            items = filtered

        logger.debug(f"RSS {source}: {len(items)} fresh items for {ticker}")
        return items

    @staticmethod
    def _parse_rss_item(entry: ET.Element, source: str) -> NewsItem | None:
        title_el = entry.find("title")
        link_el = entry.find("link")
        if title_el is None or title_el.text is None:
            return None
        title = (title_el.text or "").strip()
        link = (link_el.text or "").strip() if link_el is not None else ""
        if not link:
            guid_el = entry.find("guid")
            if guid_el is not None and guid_el.text:
                link = guid_el.text.strip()

        summary = ""
        desc_el = entry.find("description")
        if desc_el is not None and desc_el.text:
            summary = desc_el.text.strip()
        # Some feeds (e.g. RBC) put full article in a namespaced extension.
        if not summary:
            ns_rbc = {"rbc": "https://www.rbc.ru"}
            full_el = entry.find("rbc:full-text", ns_rbc)
            if full_el is not None and full_el.text:
                summary = full_el.text.strip()[:500]

        pub = None
        pub_el = entry.find("pubDate")
        if pub_el is not None and pub_el.text:
            pub = _parse_date(pub_el.text)

        h = hashlib.sha256((source + ":" + title + ":" + link).encode()).hexdigest()[:16]
        return NewsItem(title=title, link=link, source=source, published=pub, summary=summary or None, _hash=h)

    @staticmethod
    def _parse_atom_entry(entry: ET.Element, source: str) -> NewsItem | None:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        title_el = entry.find("atom:title", ns)
        if title_el is None or title_el.text is None:
            return None
        title = (title_el.text or "").strip()

        link_el = entry.find("atom:link", ns)
        link = ""
        if link_el is not None:
            link = (link_el.get("href") or "").strip()

        summary = ""
        summary_el = entry.find("atom:summary", ns)
        if summary_el is not None and summary_el.text:
            summary = summary_el.text.strip()
        else:
            content_el = entry.find("atom:content", ns)
            if content_el is not None and content_el.text:
                summary = content_el.text.strip()[:500]

        pub = None
        pub_el = entry.find("atom:published", ns)
        if pub_el is not None and pub_el.text:
            pub = _parse_date(pub_el.text)
        else:
            updated_el = entry.find("atom:updated", ns)
            if updated_el is not None and updated_el.text:
                pub = _parse_date(updated_el.text)

        h = hashlib.sha256((source + ":" + title + ":" + link).encode()).hexdigest()[:16]
        return NewsItem(title=title, link=link, source=source, published=pub, summary=summary or None, _hash=h)

    async def _fetch_rss2json(
        self,
        url: str,
        source: str,
        cutoff: datetime,
    ) -> list[NewsItem]:
        """Fallback fetch via rss2json API for feeds blocked directly."""
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                api_url = f"https://api.rss2json.com/v1/api.json?rss_url={url}"
                r = await client.get(api_url, headers={"user-agent": "Mozilla/5.0"})
                r.raise_for_status()
                data = r.json()
                if data.get("status") != "ok":
                    return []
                items: list[NewsItem] = []
                for raw in data.get("items", []):
                    item = self._parse_rss2json_item(raw, source)
                    if item and item.published and item.published >= cutoff:
                        items.append(item)
                return items
        except Exception as e:
            logger.debug(f"RSS2JSON fallback for {source} failed: {e}")
            return []

    @staticmethod
    def _parse_rss2json_item(raw: dict[str, Any], source: str) -> NewsItem | None:
        """Convert one rss2json item into a NewsItem."""
        title = (raw.get("title") or "").strip()
        if not title:
            return None
        link = (raw.get("link") or "").strip()
        summary = (raw.get("description") or raw.get("content") or "").strip()
        pub = _parse_date(raw.get("pubDate") or "")
        h = hashlib.sha256((source + ":" + title + ":" + link).encode()).hexdigest()[:16]
        return NewsItem(title=title, link=link, source=source, published=pub, summary=summary or None, _hash=h)

    def mark_seen(self, items: list[NewsItem]) -> None:
        """Remember hashes so they are not re-processed next time."""
        for item in items:
            self.seen.add(item._hash)


def _parse_date(text: str) -> datetime | None:
    """Parse common RSS/Atom date formats."""
    text = text.strip()
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",     # RFC 2822
        "%Y-%m-%dT%H:%M:%S%z",            # ISO with timezone
        "%Y-%m-%dT%H:%M:%SZ",             # ISO UTC
        "%Y-%m-%d %H:%M:%S",              # Simple
        "%d %b %Y %H:%M:%S %z",           # Alternative RFC
    ]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    # Fallback: try ISO parser for mixed formats
    try:
        # Replace Z with +00:00 for fromisoformat
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except Exception:
        pass
    return None

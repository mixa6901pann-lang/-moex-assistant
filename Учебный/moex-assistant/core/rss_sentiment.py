"""RSS sentiment scan — runs every 15 min during market hours.

Was `MoexAssistant.rss_sentiment_scan` in main.py, extracted here as
part of the incremental main.py split (step 5d, 2026-08-20).

Flow:
  1. For each watchlist ticker, fetch RSS items from the last 4h,
     skipping hashes already analyzed (db.get_seen_rss_hashes).
  2. Mark fetched hashes as seen so concurrent runs don't re-analyze.
  3. Build a weighted batch (top 5 freshest) and run sentiment_agent.
  4. Persist aggregated sentiment record with averaged age and weight.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from core import db
from core.config import WATCHLIST


async def run_rss_sentiment_scan() -> int:
    """Run RSS sentiment scan over the watchlist. Returns processed ticker count.

    Mirrors the original `MoexAssistant.rss_sentiment_scan` exactly so the
    sentiment table and per-ticker logs stay byte-identical.
    """
    # Local imports keep the top-level surface small and match the legacy
    # inline imports in main.py.
    from core.rss_feed import RssNewsAggregator
    from core.sentiment_agent import agent as sentiment_agent, WeightedNewsItem

    logger.info("Running RSS sentiment scan")
    aggregator = RssNewsAggregator()
    processed = 0

    for ticker in WATCHLIST:
        try:
            # Load hashes already analyzed for this ticker
            seen_hashes = await db.get_seen_rss_hashes(ticker)
            items = await aggregator.fetch_unique_for_ticker(
                ticker, max_age_minutes=240, exclude_hashes=seen_hashes
            )
            if not items:
                continue

            # Mark all fetched items as seen now so concurrent/future runs skip them
            for item in items:
                await db.mark_rss_hash_seen(
                    hash=item._hash,
                    ticker=ticker,
                    source=item.source,
                    headline=item.title,
                )

            # Build weighted batch for the LLM
            weighted = []
            for item in items:
                age_minutes = None
                if item.published:
                    age_minutes = int(
                        (datetime.now(timezone.utc) - item.published).total_seconds() // 60
                    )
                weighted.append(
                    WeightedNewsItem(
                        text=item.title,
                        published_at=item.published,
                        source=item.source,
                        age_minutes=age_minutes,
                    )
                )

            # Limit batch to top 5 freshest weighted items to keep prompt short
            weighted.sort(key=lambda x: (x.age_minutes or 0))
            weighted = weighted[:5]

            result = await sentiment_agent.analyze(ticker, weighted)

            # Save aggregated sentiment record with averaged age/weight
            freshest = weighted[0]
            avg_age = int(sum((w.age_minutes or 0) for w in weighted) / len(weighted))
            avg_weight = sum(w.weight for w in weighted) / len(weighted)
            await db.save_sentiment(
                ticker=ticker,
                headline=freshest.text,
                sentiment=result.sentiment,
                confidence=result.confidence,
                summary=result.summary,
                topics=list(result.key_topics),
                risk_flags=list(result.risk_flags),
                source=f"rss:{freshest.source}",
                news_hash=None,
                published_at=freshest.published_at.isoformat() if freshest.published_at else None,
                age_minutes=avg_age,
                weight=round(avg_weight, 2),
            )
            logger.info(
                f"Sentiment for {ticker}: {result.sentiment} ({result.confidence}%) — "
                f"batch of {len(weighted)} items, freshest {freshest.age_minutes or '?'}min"
            )
            processed += 1
        except Exception as e:
            logger.warning(f"RSS sentiment failed for {ticker}: {e}")

    return processed

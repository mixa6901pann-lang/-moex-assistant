"""Morning screener — runs ~10:00 MSK before market open.

Was `MoexAssistant.morning_screener` in main.py, extracted here as part
of the incremental main.py split (step 5d, 2026-08-20).

Flow:
  1. Refresh daily candles for each watchlist ticker.
  2. Pull IMOEX candles for relative-strength scoring.
  3. Run `run_screener` over the watchlist with the index as reference.
  4. Persist scored results.
  5. Post the morning report to VK wall (if VK is enabled).
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from core import db
from core.config import VK_ENABLED, WATCHLIST


async def run_morning_screener(moex: Any, vk_wall: Any | None) -> int:
    """Run the morning screener. Returns the number of tickers scored.

    Mirrors the original `MoexAssistant.morning_screener` exactly so the
    daily VK report and screener table stay byte-identical.
    """
    logger.info("Running morning screener")

    # Local import — keeps top-level import surface small and matches the
    # legacy inline import in main.py.
    from strategies.indicators import run_screener

    # Update candle data for watchlist
    for ticker in WATCHLIST:
        try:
            candles = await moex.candles_recent(ticker, count=100)
            await db.save_candles(candles, ticker, "TQBR", "1d")
        except Exception as e:
            logger.warning(f"Failed to fetch {ticker}: {e}")

    # Fetch IMOEX for relative strength
    try:
        index_candles = await moex.index_candles("IMOEX", interval="1d", count=30)
    except Exception as e:
        logger.warning(f"Failed to fetch IMOEX: {e}")
        index_candles = None

    # Run screener
    async def fetch(ticker: str):
        candles = await moex.candles_recent(ticker, count=100)
        return candles

    results = await run_screener(fetch, index_candles=index_candles)
    await db.save_screener(results)

    # Post morning report to VK wall
    if VK_ENABLED and vk_wall is not None:
        try:
            await vk_wall.post_morning_report()
        except Exception as e:
            logger.warning(f"VK morning post failed: {e}")

    logger.info(f"Morning screener done, {len(results)} tickers scored")
    return len(results)

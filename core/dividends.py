"""Dividend updates + market price helpers.

Was part of `MoexAssistant` in main.py, extracted here as part of the
incremental main.py split (step 5d, 2026-08-20).

Three pure helpers:
  - `run_update_dividends`: weekly loop over DIVIDEND_TICKERS, persists
    dividend calendar to db.
  - `get_market_price`: latest close with sanity check against previous
    candle — falls back to last_price if the API returned a suspicious jump.
  - `get_daily_open_price`: today's official open, falls back to last_price.

`_market_price` and `_daily_open_price` are still called from several other
methods in main.py (geo_risk wrapper, evening_paper_check wrapper,
intraday_broker_stop_check), so the MoexAssistant class keeps thin wrappers
that delegate here.
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from core import db
from core.config import DIVIDEND_TICKERS, PRICE_JUMP_THRESHOLD_PCT


async def run_update_dividends(moex: Any) -> int:
    """Update the dividend calendar for each ticker in DIVIDEND_TICKERS.

    Returns the number of tickers processed.
    Mirrors the original `MoexAssistant.update_dividends` exactly.
    """
    logger.info("Updating dividend data")
    processed = 0
    for ticker in DIVIDEND_TICKERS:
        try:
            divs = await moex.dividends(ticker)
            await db.save_dividends(ticker, divs)
            processed += 1
        except Exception as e:
            logger.warning(f"Dividends failed for {ticker}: {e}")
    return processed


async def get_market_price(moex: Any, ticker: str) -> float | None:
    """Fetch latest closing price for a ticker.

    Validates that the returned close is within PRICE_JUMP_THRESHOLD_PCT
    versus the previous candle. A huge jump usually means the API returned
    data for a different instrument or a bad tick — falls back to last_price.
    """
    try:
        candles = await moex.candles_recent(ticker.upper(), count=2)
        if not candles:
            return None
        close = float(candles[-1]["close"])
        if len(candles) > 1:
            prev_close = float(candles[-2].get("close", close))
            if prev_close > 0:
                jump = abs(close - prev_close) / prev_close
                if jump > PRICE_JUMP_THRESHOLD_PCT / 100:
                    logger.warning(
                        f"Suspicious price jump for {ticker}: close={close}, "
                        f"prev={prev_close} ({jump:.1%}); falling back to last_price"
                    )
                    last = await moex.last_price(ticker.upper())
                    if last:
                        return float(last)
                    return None
        return close
    except Exception as e:
        logger.warning(f"Failed to get market price for {ticker}: {e}")
    return None


async def get_daily_open_price(moex: Any, ticker: str) -> float | None:
    """Fetch today's official opening price for a ticker.

    Used by morning_paper_execution to fill queued paper proposals as close
    to real trading as possible. Falls back to last traded price if the
    daily candle is not yet available.
    """
    try:
        candles = await moex.candles_recent(
            ticker.upper(), interval="1d", count=1
        )
        if candles and candles[-1].get("open"):
            return float(candles[-1]["open"])
    except Exception as e:
        logger.warning(f"Failed to get daily open for {ticker}: {e}")
    # Fallback to the current last price if the open candle is missing.
    try:
        return await moex.last_price(ticker.upper())
    except Exception as e:
        logger.warning(f"Failed to get last price for {ticker}: {e}")
    return None

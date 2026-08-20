"""Geo-risk scan — runs every 30 min during market hours.

Was `MoexAssistant.geo_risk_scan` (+ `_maybe_exit_positions_on_georisk`)
in main.py, extracted here as part of the incremental main.py split
(step 5d, 2026-08-20).

Flow:
  1. Run `georisk_agent.scan()` to compute geopolitical risk score.
  2. Persist the result with affected sectors and direction.
  3. If score is high, immediately start exiting affected positions.
  4. Post a critical alert to VK wall (if VK is enabled).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from loguru import logger

from core import db
from core.config import TICKER_SECTORS, VK_ENABLED

# Type alias for the injected market helpers from MoexAssistant.
MarketOpenFn = Callable[[], bool]
MarketPriceFn = Callable[[str], Awaitable[float | None]]


def _sector_for_ticker(ticker: str) -> str | None:
    """Look up the sector a ticker belongs to (e.g. AFLT -> 'transport')."""
    return TICKER_SECTORS.get(ticker.upper())


def _sector_impact(geo: dict, sector: str | None) -> dict | None:
    """Return the affected_sectors entry for a given sector, or None."""
    if not sector or not geo:
        return None
    for s in geo.get("affected_sectors") or []:
        if isinstance(s, dict) and s.get("sector") == sector:
            return s
    return None


async def _maybe_exit_positions_on_georisk(
    geo_score: int,
    market_open_fn: MarketOpenFn,
    market_price_fn: MarketPriceFn,
) -> tuple[int, int]:
    """Exit paper positions in sectors negatively impacted by a high GeoRisk.

    Returns a tuple (exited_count, held_count).
    """
    if not market_open_fn():
        logger.info(
            f"GeoRisk {geo_score}/10 is high but market is closed; deferring to next open"
        )
        return (0, 0)
    open_pos = await db.get_open_paper_positions()
    if not open_pos:
        return (0, 0)

    geo = await db.get_latest_georisk() or {}

    exited = 0
    skipped = 0
    for pos in open_pos:
        try:
            ticker = pos["ticker"]
            side = pos.get("side", "long")
            sector = _sector_for_ticker(ticker)
            impact = _sector_impact(geo, sector)

            # Longs in negatively-impacted sectors are exited.
            # Shorts in negatively-impacted sectors are held (they profit).
            # Everything else is held unless overall direction is strongly bearish.
            should_exit = False
            if side in ("long", "buy") and impact and impact.get("direction", 0) == -1:
                should_exit = True
            elif geo.get("overall_direction", 0) == -1 and side in ("long", "buy"):
                should_exit = True

            if not should_exit:
                skipped += 1
                continue

            # Use the current last price as the exit target for the proposal.
            exit_px = await market_price_fn(ticker)
            if not exit_px or exit_px <= 0:
                logger.warning(
                    f"No price for {ticker}; skipping intra-day geo-risk exit"
                )
                continue
            await db.save_robot_proposal(
                ticker=ticker,
                side=side,
                source="georisk_intraday",
                signal="exit",
                entry_px=pos["entry_px"],
                qty=pos.get("qty", 1),
                stop_px=pos.get("stop_px"),
                take_px=pos.get("take_px"),
                reason=(
                    f"GeoRisk {geo_score}/10 intra-day exit {side} "
                    f"({sector}: {impact.get('direction', 0) if impact else 'market'})"
                ),
                horizon="1d",
                proposal_mode="paper",
            )
            exited += 1
            # Local imports mirror the original lazy-import in main.py.
            from core.config import PAPER_TRADING, TINKOFF_SANDBOX
            if PAPER_TRADING or TINKOFF_SANDBOX:
                await db.close_paper_position(pos["id"], exit_px, "georisk_intraday")
                logger.info(
                    f"Closed paper {pos['side']} {pos['ticker']} at {exit_px:.2f} "
                    f"due to GeoRisk {geo_score}"
                )
            else:
                logger.info(
                    f"Queued intra-day exit proposal for {pos['ticker']} {pos['side']} "
                    f"(GeoRisk {geo_score})"
                )
        except Exception as e:
            logger.warning(f"GeoRisk intra-day exit failed for {pos['ticker']}: {e}")
    logger.warning(
        f"GeoRisk {geo_score}/10 intra-day exit done: exited={exited}, held={skipped}"
    )
    return (exited, skipped)


async def run_geo_risk_scan(
    market_open_fn: MarketOpenFn,
    market_price_fn: MarketPriceFn,
) -> bool:
    """Run the geopolitical risk scan. Returns True if a result was found.

    Mirrors the original `MoexAssistant.geo_risk_scan` exactly so the
    geo-risk table and VK alerts stay byte-identical.
    """
    # Local import keeps top-level surface small and matches legacy main.py.
    from core.georisk_agent import agent as georisk_agent

    logger.info("Running geo-risk scan")
    try:
        result = await georisk_agent.scan()
        if result:
            await db.save_georisk(
                score=result.score,
                severity=result.severity,
                summary=result.summary,
                affected_sectors=result.affected_sectors,
                trigger_keywords=result.trigger_keywords,
                news_items=result.news_items,
                overall_direction=result.overall_direction,
            )
            sector_summary = ", ".join(
                f"{s['sector']}({s['direction']:+d})" for s in result.affected_sectors[:5]
            )
            logger.info(
                f"GeoRisk: {result.score}/10 ({result.severity}, dir={result.overall_direction:+d}) "
                f"— {result.summary[:80]} | sectors: {sector_summary or 'none'}"
            )
            # When risk spikes during the session, immediately start exiting positions.
            await _maybe_exit_positions_on_georisk(
                geo_score=result.score,
                market_open_fn=market_open_fn,
                market_price_fn=market_price_fn,
            )
            # Critical risk alert to VK wall
            if VK_ENABLED and result.score >= 7:
                try:
                    from bot.vk_wall import VkWallPoster
                    wall = VkWallPoster()
                    await wall.post_alert(
                        "GEORISK",
                        f"Геополитический риск {result.score}/10 ({result.severity}). "
                        f"Секторы: {sector_summary or 'все'}. "
                        f"{result.summary[:150]}",
                    )
                except Exception as e:
                    logger.warning(f"VK geo-risk alert failed: {e}")
            return True
        else:
            logger.info("GeoRisk: no relevant news found")
            return False
    except Exception as e:
        logger.warning(f"Geo-risk scan failed: {e}")
        return False

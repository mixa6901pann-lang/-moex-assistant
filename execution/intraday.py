"""Intraday signal pipeline.

Turns 5-minute intraday signals into semi-automatic robot proposals.
"""

from __future__ import annotations

from loguru import logger

from core import db
from core.broker_executor import market_phase
from core.config import (
    INTRADAY_MAX_CANDLE_AGE_MIN,
    INTRADAY_PRICE_DRIFT_PCT,
    SEMI_AUTO_TRADING,
    TINKOFF_SANDBOX,
    TICKER_SECTORS,
    should_auto_trade,
)
import core.config as app_config
from core.intraday_freshness import candle_age_minutes, price_drift_pct
from execution.pipeline import ExecutionPipeline, _sector_for_ticker, _sector_impact


async def run_intraday_monitor(
    moex_client,
    pipeline: ExecutionPipeline,
    intraday_agent,
    resample_1m_to_5m,
    telegram_enabled: bool = False,
    send_proposal_alert=None,
    vk_wall=None,
    vk_enabled: bool = False,
) -> None:
    """Check top screened tickers for 5m intraday signals and queue proposals.

    This mirrors the logic previously in main.py `intraday_monitor`. Intraday
    signals are always informational/semi-auto and never execute automatically.
    """
    logger.info("Running intraday monitor")

    # Gate 1: only run during active trading sessions. Before 09:30 and
    # after the evening close, ISS just returns the most recent historical
    # candles, which used to leak into proposals (AFLT 32.33 vs broker 34.20).
    phase = market_phase()
    if phase not in ("morning_session", "main_session", "evening_session"):
        logger.info(f"Intraday monitor skipped: market phase is {phase}")
        return

    screener = await db.latest_screener(limit=10)
    tickers_to_check = [r["ticker"] for r in screener] if screener else [
        "SBER", "GAZP", "LKOH", "GMKN", "NVTK"
    ]

    for ticker in tickers_to_check:
        try:
            candles_1m = await moex_client.candles_recent(ticker, interval="1m", count=500)
            if not candles_1m:
                continue

            # Gate 2: drop the ticker if the last 1m candle is older than
            # INTRADAY_MAX_CANDLE_AGE_MIN. ISS lags during the evening
            # clear or right after open and returns stale data; we do not
            # want to anchor entry_px to a candle from hours ago.
            age_min = candle_age_minutes(candles_1m[-1].get("end"))
            if age_min is not None and age_min > INTRADAY_MAX_CANDLE_AGE_MIN:
                logger.debug(
                    f"Intraday {ticker} skipped: last candle is {age_min:.1f}m old "
                    f"(> {INTRADAY_MAX_CANDLE_AGE_MIN}m)"
                )
                continue

            candles_5m = resample_1m_to_5m(candles_1m)
            if len(candles_5m) < 30:
                continue

            result = await intraday_agent.analyze(
                ticker=ticker,
                candles_5m=candles_5m,
                order_book=None,
                use_llm=False,
                calibrate=False,
            )

            logger.debug(
                f"Intraday {ticker}: signal={result.signal} dir={result.direction} "
                f"conf={result.confidence} vol_ratio={result.metrics.get('vol_ratio')}"
            )
            if result.signal == "no_signal" or result.direction == "neutral":
                continue
            if result.confidence < 60:
                logger.debug(
                    f"Intraday {ticker} {result.direction} skipped: confidence {result.confidence} < 60"
                )
                continue

            if not (result.entry and result.take):
                logger.debug(
                    f"Intraday {ticker} {result.direction} skipped: missing entry={result.entry} take={result.take}"
                )
                continue

            # Gate 3: cross-check entry_px against the broker live price.
            # ISS 1m candles can lag the real market by minutes, which
            # caused NVTK 916.95 vs broker 940.30 on 2026-08-20. If drift
            # exceeds INTRADAY_PRICE_DRIFT_PCT, drop the signal.
            if result.entry is not None:
                try:
                    broker_px = await pipeline.tinkoff.get_ticker_price(ticker)
                    drift = price_drift_pct(result.entry, broker_px)
                    if drift > INTRADAY_PRICE_DRIFT_PCT:
                        logger.info(
                            f"Intraday {ticker} skipped: entry {result.entry} drifts "
                            f"{drift:.2f}% from broker price {broker_px} "
                            f"(> {INTRADAY_PRICE_DRIFT_PCT}%)"
                        )
                        continue
                except Exception as exc:
                    logger.debug(f"Intraday {ticker}: broker price check failed: {exc}")

            # Skip if already aligned in paper portfolio.
            existing = await db.get_open_paper_position(ticker)
            if existing and existing["side"] == result.direction:
                logger.info(
                    f"Intraday {ticker} {result.direction} skipped: position already open"
                )
                continue

            # GeoRisk-aware filtering: don't fight the macro/geo wind.
            geo = await db.get_latest_georisk()
            sector = _sector_for_ticker(ticker)
            impact = _sector_impact(geo, sector)
            sector_dir = impact.get("direction", 0) if impact else 0
            overall_direction = geo.get("overall_direction", 0) if geo else 0

            if result.direction in ("long", "buy"):
                if sector_dir == -1:
                    logger.info(
                        f"Intraday {ticker} {result.direction} skipped: {sector} negatively impacted by GeoRisk"
                    )
                    continue
                if overall_direction == -1 and sector_dir != 1:
                    logger.info(
                        f"Intraday {ticker} {result.direction} skipped: overall GeoRisk direction is bearish"
                    )
                    continue

            if result.direction in ("short", "sell"):
                if sector_dir == 1:
                    logger.info(
                        f"Intraday {ticker} {result.direction} skipped: {sector} positively impacted by GeoRisk"
                    )
                    continue
                if overall_direction == 1 and sector_dir != -1:
                    logger.info(
                        f"Intraday {ticker} {result.direction} skipped: overall GeoRisk direction is bullish"
                    )
                    continue

            # The pipeline handles sizing, fees, open-position limit and proposal creation.
            proposal_mode = "semi_auto" if (SEMI_AUTO_TRADING or (app_config.AUTO_TRADING_ENABLED and TINKOFF_SANDBOX)) else "paper"
            if proposal_mode == "semi_auto" and should_auto_trade(result.confidence):
                proposal_mode = "auto_trade"

            pipeline_result = await pipeline.run(
                ticker=ticker,
                side=result.direction,
                entry_px=result.entry,
                take_px=result.take,
                stop_px=result.stop,
                atr=None,
                source="intraday",
                signal=result.signal,
                confidence=result.confidence,
                reason=result.reason,
                horizon="intraday",
                hold_days=0,
                atr_mult=1.0,
                use_trailing_stop=False,
                # Intraday signals are informational by default. When the user has
                # enabled auto-trading and we are in Tinkoff sandbox, promote them to
                # semi_auto (or "auto_trade" when AUTO_TRADE is on) so confirmed
                # ideas can execute through the real broker.
                proposal_mode=proposal_mode,
            )

            if not pipeline_result.ok:
                logger.info(
                    f"Intraday {ticker} {result.direction} skipped: {pipeline_result.reason}"
                )
                continue

            # The signal-level take (result.take) is the indicator target.
            # The sizer rewrites take_px to an ATR-based R-multiple target,
            # which is what actually gets stored on the proposal and the
            # broker stop-order. Log both so the operator can see the gap.
            final_take = pipeline_result.sizing.take_px if pipeline_result.sizing else None
            msg = (
                f"{result.signal} {result.direction} | conf={result.confidence}% | "
                f"entry={result.entry or '-'} stop={result.stop or '-'} "
                f"take(indicator)={result.take or '-'} take(ATR)={final_take or '-'} | "
                f"{result.reason}"
            )
            logger.info(f"Intraday signal {ticker}: {msg}")

            proposal_id = pipeline_result.proposal_id
            if telegram_enabled and proposal_id is not None and send_proposal_alert:
                try:
                    await send_proposal_alert(
                        proposal_id=proposal_id,
                        ticker=ticker,
                        side=result.direction,
                        entry_px=result.entry,
                        stop_px=result.stop,
                        take_px=result.take,
                        qty=pipeline_result.sizing.qty if pipeline_result.sizing else 1,
                        source="intraday",
                        reason=result.reason,
                    )
                except Exception as exc:
                    logger.warning(f"Telegram alert failed for intraday {ticker}: {exc}")

            if vk_enabled and vk_wall:
                try:
                    await vk_wall.post_alert(ticker, msg)
                except Exception as e:
                    logger.warning(f"VK alert failed: {e}")

        except Exception as e:
            logger.warning(f"Intraday check failed for {ticker}: {e}")

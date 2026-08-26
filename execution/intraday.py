"""Intraday signal pipeline.

Turns 5-minute intraday signals into semi-automatic robot proposals.
"""

from __future__ import annotations

from loguru import logger

from core import db
from core.broker_executor import market_phase
from core.config import SEMI_AUTO_TRADING, TINKOFF_SANDBOX, TICKER_SECTORS, should_auto_trade
import core.config as app_config

# 26.08.2026: максимальное расхождение между ценой закрытия 5-минутной
# свечи MOEX ISS и last_price брокера Tinkoff, при превышении которого
# proposal получает entry от брокера, а не от ISS.
BROKER_PRICE_DRIFT_THRESHOLD = 0.003  # 0.3 %
from execution.pipeline import ExecutionPipeline, _sector_for_ticker, _sector_impact


async def run_intraday_monitor(
    moex_client,
    pipeline: ExecutionPipeline,
    intraday_agent,
    resample_1m_to_5m,
    tinkoff_client=None,
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

    screener = await db.latest_screener(limit=10)
    tickers_to_check = [r["ticker"] for r in screener] if screener else [
        "SBER", "GAZP", "LKOH", "GMKN", "NVTK"
    ]

    for ticker in tickers_to_check:
        try:
            # In the evening session MOEX ISS stops updating 1m candles and
            # starts returning stale daily data disguised as 1m (candle age
            # gate then drops every signal). 10m candles keep updating
            # throughout the evening extra session and have enough resolution
            # for the agent to find continuations/bounces. 25.08.2026
            # intraday_monitor logged 879 runs with 0 signals during evening
            # until this branch was added.
            phase = market_phase()
            if phase == "evening_session":
                interval = "10m"
                candles_count = 200
            else:
                interval = "1m"
                candles_count = 500

            candles_src = await moex_client.candles_recent(ticker, interval=interval, count=candles_count)
            if not candles_src:
                continue

            if interval == "1m":
                candles_5m = resample_1m_to_5m(candles_src)
            else:
                # 10m candles go straight to the agent — add_indicators is
                # interval-agnostic and only needs enough rows for ADX/VWAP.
                candles_5m = candles_src
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

            # Skip if already aligned in paper portfolio.
            existing = await db.get_open_paper_position(ticker)
            if existing and existing["side"] == result.direction:
                logger.info(
                    f"Intraday {ticker} {result.direction} skipped: position already open"
                )
                continue
            # 26.08.2026: если уже есть открытая позиция в ПРОТИВОПОЛОЖНУЮ
            # сторону — создаём exit-proposal на её закрытие, а новый signal
            # (long/short) идёт своим proposal'ом. broker_executor уже умеет
            # атомарный reverse на исполнении; здесь закрываем зазор в
            # paper-режиме и ускоряем закрытие в sandbox.
            if existing and existing["side"] != result.direction:
                prev_side = existing["side"]
                try:
                    exit_id = await db.save_robot_proposal(
                        ticker=ticker,
                        side=prev_side,
                        source="intraday_reversal",
                        signal="exit",
                        entry_px=existing.get("entry_px"),
                        qty=abs(int(existing.get("qty") or 0)) or None,
                        stop_px=None,
                        take_px=None,
                        confidence=result.confidence,
                        reason=(
                            f"intraday reversal: open {prev_side} on "
                            f"{ticker} conflicts with new {result.direction} signal"
                        ),
                        horizon="intraday",
                        proposal_mode="paper",
                    )
                    logger.info(
                        f"Intraday {ticker} {result.direction} ({result.signal}): "
                        f"created exit proposal {exit_id} to close existing {prev_side}"
                    )
                except Exception as exc:
                    logger.warning(
                        f"Intraday {ticker} reversal exit-proposal failed: {exc}"
                    )

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

            # 26.08.2026: сверяем entry с last_price у Tinkoff. ISS может отдавать
            # устаревший close 5m-свечи (лаг до 5 минут), а брокер видит актуальный
            # стакан. При расхождении >0.3% брокер — источник правды.
            entry_px = result.entry
            if tinkoff_client is not None:
                try:
                    broker_px = await tinkoff_client.get_ticker_price(ticker)
                except Exception as e:
                    logger.debug(f"Intraday {ticker}: broker price fetch failed: {e}")
                    broker_px = None
                if broker_px is not None and broker_px > 0:
                    drift = abs(broker_px - entry_px) / entry_px
                    if drift > BROKER_PRICE_DRIFT_THRESHOLD:
                        logger.info(
                            f"Intraday {ticker} entry drift {drift*100:.2f}% > 0.3% "
                            f"(iss={entry_px:.4f} broker={broker_px:.4f}), using broker price"
                        )
                        entry_px = broker_px

            # The pipeline handles sizing, fees, open-position limit and proposal creation.
            proposal_mode = "semi_auto" if (SEMI_AUTO_TRADING or (app_config.AUTO_TRADING_ENABLED and TINKOFF_SANDBOX)) else "paper"
            if proposal_mode == "semi_auto" and should_auto_trade(result.confidence):
                proposal_mode = "auto_trade"

            pipeline_result = await pipeline.run(
                ticker=ticker,
                side=result.direction,
                entry_px=entry_px,
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

            msg = (
                f"{result.signal} {result.direction} | conf={result.confidence}% | "
                f"entry={result.entry or '-'} stop={result.stop or '-'} take={result.take or '-'} | "
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

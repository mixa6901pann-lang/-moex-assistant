"""Evening TradingAgent pipeline.

Turns daily ReAct decisions into queued proposals for the next session's open.
"""

from __future__ import annotations

from loguru import logger

import core.config as app_config
from core.config import VK_ENABLED, PAPER_TRADING, SEMI_AUTO_TRADING, TINKOFF_SANDBOX, TICKER_SECTORS, should_auto_trade
from core import db
from core.llm import get_last_used_provider
from strategies.indicators import compute_levels
from execution.pipeline import ExecutionPipeline


async def run_evening_trading_decision(
    moex_client,
    market_price_fn,
    pipeline: ExecutionPipeline,
    trading_agent,
    telegram_enabled: bool = False,
    send_proposal_alert=None,
    vk_wall=None,
    vk_enabled: bool = False,
) -> int:
    """Run daily TradingAgent decisions and queue proposals.

    Returns the number of tickers queued.
    """
    logger.info("Running evening trading decision (TradingAgent)")

    geo = await db.get_latest_georisk()
    geo_score = geo["score"] if geo else 0
    geo_high = geo_score >= 7

    if geo_high:
        logger.warning(f"GeoRisk is HIGH ({geo_score}/10) — evaluating exit proposals")
        open_pos = await db.get_open_paper_positions()
        for pos in open_pos:
            ticker = pos["ticker"]
            side = pos.get("side", "long")
            sector = TICKER_SECTORS.get(ticker.upper())
            impact = None
            for s in (geo.get("affected_sectors") or []):
                if isinstance(s, dict) and s.get("sector") == sector:
                    impact = s
                    break

            should_exit = False
            if side in ("long", "buy") and impact and impact.get("direction", 0) == -1:
                should_exit = True
            elif geo.get("overall_direction", 0) == -1 and side in ("long", "buy"):
                should_exit = True

            if not should_exit:
                logger.info(f"Evening geo-risk: keeping {ticker} {side} (sector {sector}, impact {impact.get('direction', 0) if impact else 'none'})")
                continue

            proposal_id = await db.save_robot_proposal(
                ticker=ticker,
                side=side,
                source="georisk",
                signal="exit",
                entry_px=pos["entry_px"],
                qty=pos.get("qty", 1),
                stop_px=pos.get("stop_px"),
                take_px=pos.get("take_px"),
                reason=f"GeoRisk {geo_score}/10 — exit {side} {ticker} at next open (sector {sector})",
                horizon="1d",
                proposal_mode="paper",
            )
            # Persist the original side so a stale exit cannot close a position
            # that has been reversed intraday before the next session open.
            db_conn = await db.get_db()
            await db_conn.execute(
                "UPDATE robot_proposals SET side = ? WHERE id = ?",
                (side, proposal_id),
            )
            await db_conn.commit()
            logger.info(f"Queued exit proposal for {ticker} {side} due to geo-risk {geo_score}")

    # Always run TradingAgent market analysis regardless of GeoRisk. When risk is
    # elevated, new evening proposals are downgraded to paper-only mode.
    if geo_high:
        logger.warning(f"GeoRisk is HIGH ({geo_score}/10); new evening proposals will be queued in paper mode only")

    screener = await db.latest_screener(limit=10)
    if not screener:
        logger.warning("No screener results for evening trading decision")
        return 0

    decisions_made = 0
    llm_provider = get_last_used_provider() or app_config.LLM_PROVIDER

    for r in screener:
        ticker = r["ticker"]
        try:
            decision = await trading_agent.decide(ticker)

            await db.save_prediction(
                ticker=ticker,
                predicted_direction=decision.action,
                predicted_price=decision.price or 0,
                predicted_strength="strong" if decision.confidence >= 70 else "moderate" if decision.confidence >= 50 else "weak",
                higher_tf_trend="unknown",
                signals_used=[],
                reasoning=decision.reasoning,
                source="trading_agent",
                llm_provider=llm_provider,
                environment="sandbox" if TINKOFF_SANDBOX else "paper",
            )

            logger.info(
                f"TradingAgent {ticker}: {decision.action} (conf={decision.confidence}%) — {decision.reasoning[:80]}"
            )

            if decision.action in ("hold", "wait"):
                continue
            if decision.action not in ("buy", "long", "sell", "short"):
                continue

            market_price = await market_price_fn(ticker)
            if market_price is None:
                logger.warning(f"No market price for {ticker}; skipping paper trade")
                continue

            atr = r.get("details", {}).get("atr")
            levels = compute_levels(decision.action, market_price, atr)
            stop_px = levels.get("stop") if levels else None
            take_px = levels.get("take") if levels else None

            side = "long" if decision.action in ("buy", "long") else "short"

            existing = await db.get_open_paper_position(ticker)
            if existing and existing["side"] == side:
                logger.info(f"Evening {ticker} {side} skipped: position already open")
                continue

            proposal_mode = pipeline.resolve_proposal_mode()
            # When auto-trading is enabled and we are connected to Tinkoff sandbox,
            # promote paper proposals to semi_auto so the user can confirm them and
            # they execute through the real broker adapter (sandbox). GeoRisk spikes
            # keep everything in paper-only mode for safety.
            if (
                app_config.AUTO_TRADING_ENABLED
                and TINKOFF_SANDBOX
                and proposal_mode == "paper"
                and not geo_high
            ):
                proposal_mode = "semi_auto"
            # Legacy override: if PAPER_TRADING is explicitly disabled and semi_auto is on.
            if not PAPER_TRADING and SEMI_AUTO_TRADING and not geo_high:
                proposal_mode = "semi_auto"
            if geo_high:
                proposal_mode = "paper"
            if proposal_mode == "semi_auto" and should_auto_trade(decision.confidence):
                proposal_mode = "auto_trade"

            pipeline_result = await pipeline.run(
                ticker=ticker,
                side=side,
                entry_px=market_price,
                take_px=take_px,
                stop_px=stop_px,
                atr=atr,
                source="evening",
                signal=decision.action,
                confidence=decision.confidence,
                reason=decision.reasoning,
                horizon="1d",
                hold_days=1,
                proposal_mode=proposal_mode,
            )

            if not pipeline_result.ok:
                logger.info(f"Evening {ticker} {side} skipped: {pipeline_result.reason}")
                continue

            proposal_id = pipeline_result.proposal_id
            logger.info(
                f"Evening {proposal_mode} proposal saved: id={proposal_id} "
                f"{ticker} {decision.action} qty={pipeline_result.sizing.qty if pipeline_result.sizing else '-'} "
                f"planned={market_price:.2f} take={take_px} conf={decision.confidence}%"
            )

            if telegram_enabled and proposal_mode in ("semi_auto", "auto_trade", "live") and send_proposal_alert:
                try:
                    await send_proposal_alert(
                        proposal_id=proposal_id,
                        ticker=ticker,
                        side=side,
                        entry_px=market_price,
                        stop_px=stop_px,
                        take_px=take_px,
                        qty=pipeline_result.sizing.qty if pipeline_result.sizing else 0,
                        source="trading_agent/evening",
                        reason=decision.reasoning,
                        mode=proposal_mode,
                    )
                except Exception as exc:
                    logger.warning(f"Telegram alert failed for evening {ticker}: {exc}")

            if vk_enabled and proposal_mode in ("semi_auto", "auto_trade", "live") and vk_wall:
                try:
                    await vk_wall.post_alert(
                        ticker,
                        f"{'Предложена' if proposal_mode == 'semi_auto' else 'Авто'} сделка "
                        f"{decision.action.upper()} {ticker} qty={pipeline_result.sizing.qty if pipeline_result.sizing else '-'} "
                        f"planned={market_price:.2f} stop={stop_px} take={take_px}. "
                        f"{'Подтвердите в веб-интерфейсе.' if proposal_mode == 'semi_auto' else ''}",
                    )
                except Exception as exc:
                    logger.warning(f"VK proposal post failed: {exc}")

            decisions_made += 1

        except Exception as e:
            logger.warning(f"TradingAgent failed for {ticker}: {e}")

    logger.info(f"Evening trading decision done: {decisions_made}/{len(screener)} tickers queued")
    return decisions_made

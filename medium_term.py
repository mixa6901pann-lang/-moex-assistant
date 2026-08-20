"""Medium-term prediction pipeline.

Turns 3d/7d directional predictions into robot proposals.
"""

from __future__ import annotations

from loguru import logger

import core.config as app_config
from core.config import PAPER_TRADING, TINKOFF_SANDBOX
from core import db
from execution.pipeline import ExecutionPipeline


DIRECTIONAL = {"long", "short", "buy", "sell"}


async def run_medium_term_proposals(
    moex_client,
    market_price_fn,
    pipeline: ExecutionPipeline,
    guard_new_position,
    telegram_enabled: bool = False,
    send_proposal_alert=None,
) -> int:
    """Generate medium-term proposals from 3d and 7d predictions.

    Returns the total number of proposals created.
    """
    total_created = 0
    handled_tickers: set[str] = set()

    for horizon in (3, 7):
        preds = await db.get_recent_directional_predictions(
            horizon_days=horizon,
            min_strength="moderate",
            since_days=2,
            limit=10,
        )
        if not preds:
            logger.info(f"No medium-term candidates for {horizon}d")
            continue

        created = 0
        for p in preds:
            ticker = p["ticker"]
            direction = p["predicted_direction"]
            if direction not in DIRECTIONAL:
                continue
            side = "long" if direction in ("buy", "long") else "short"

            ok, reason = await guard_new_position(ticker, side)
            if not ok:
                logger.info(f"Medium-term {horizon}d {ticker} skipped: {reason}")
                continue

            # Skip if we already created a proposal for this ticker in an earlier
            # horizon pass of the same run (e.g. 3d already handled the ticker).
            if ticker in handled_tickers:
                logger.info(f"Medium-term {horizon}d {ticker} skipped: already queued for another horizon")
                continue

            # Skip if already have a pending proposal for this ticker/horizon.
            existing = await db.get_robot_proposals(
                status="pending",
                horizon=f"{horizon}d",
                ticker=ticker,
                since_days=7,
                limit=1,
            )
            if existing:
                continue

            # Avoid stacking on the same ticker.
            if await db.get_open_paper_position(ticker):
                continue

            market_price = await market_price_fn(ticker)
            if market_price is None:
                logger.warning(f"No market price for {ticker}; skipping medium-term proposal")
                continue

            # Medium-term levels (3d/7d horizon): 2x ATR stop, 3x ATR target.
            try:
                candles = await moex_client.candles_recent(ticker, interval="D", count=30)
                if candles:
                    from strategies.indicators import df_from_candles, add_indicators
                    df = df_from_candles(candles)
                    df = add_indicators(df)
                    atr = float(df["atr"].iloc[-1]) if "atr" in df.columns else market_price * 0.03
                else:
                    atr = market_price * 0.03
            except Exception:
                atr = market_price * 0.03

            if not atr or atr <= 0:
                atr = market_price * 0.03

            if side == "long":
                stop_px = market_price - atr * 2
                take_px = market_price + atr * 3
            else:
                stop_px = market_price + atr * 2
                take_px = market_price - atr * 3

            # Use the shared helper so sandbox / semi-auto / paper / live modes
            # are resolved consistently with the rest of the app. Never emit
            # "live" proposals when Tinkoff sandbox is active.
            proposal_mode = pipeline.resolve_proposal_mode()
            if proposal_mode == "live" and TINKOFF_SANDBOX:
                proposal_mode = "semi_auto"

            reason_text = (
                f"Среднесрочное предложение на {horizon} дней. "
                f"Основание: прогноз {side.upper()} от {p['source']} "
                f"(уверенность {p.get('predicted_strength')}, {p.get('llm_provider') or 'unknown'}). "
                f"{p.get('reasoning', '')[:120]}"
            )

            pipeline_result = await pipeline.run(
                ticker=ticker,
                side=side,
                entry_px=market_price,
                take_px=round(take_px, 2),
                stop_px=round(stop_px, 2),
                atr=atr,
                source="prediction",
                signal=p.get("predicted_direction"),
                confidence=70 if p.get("predicted_strength") == "strong" else 60,
                reason=reason_text.strip(),
                horizon=f"{horizon}d",
                hold_days=horizon,
                atr_mult=2.0,
                proposal_mode=proposal_mode,
            )

            if not pipeline_result.ok:
                logger.info(
                    f"Medium-term {horizon}d {ticker} skipped: {pipeline_result.reason}"
                )
                continue

            handled_tickers.add(ticker)
            proposal_id = pipeline_result.proposal_id
            created += 1
            logger.info(
                f"Medium-term {horizon}d proposal saved: id={proposal_id} "
                f"{ticker} {side} qty={pipeline_result.sizing.qty if pipeline_result.sizing else '-'} "
                f"entry={market_price:.2f} stop={stop_px:.2f} take={take_px:.2f} mode={proposal_mode}"
            )

            if telegram_enabled and proposal_id is not None and send_proposal_alert:
                try:
                    await send_proposal_alert(
                        proposal_id=proposal_id,
                        ticker=ticker,
                        side=side,
                        entry_px=market_price,
                        stop_px=round(stop_px, 2),
                        take_px=round(take_px, 2),
                        qty=pipeline_result.sizing.qty if pipeline_result.sizing else 0,
                        source="prediction",
                        reason=reason_text,
                        mode=proposal_mode,
                    )
                except Exception as exc:
                    logger.warning(f"Telegram alert failed for medium-term {ticker}: {exc}")

        logger.info(f"Medium-term {horizon}d proposals queued: {created}")
        total_created += created

    return total_created

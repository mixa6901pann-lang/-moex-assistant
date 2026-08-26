"""Morning paper execution for pending robot proposals.

Fills pending paper proposals at the next session's open price with realistic
slippage and ATR recomputation.
"""

from __future__ import annotations

from loguru import logger

from core import db
from core.config import (
    PAPER_STARTING_CAPITAL,
    STOP_LOSS_ATR_MULT,
    TRAILING_STOP_ATR_MULT,
    MAX_OPEN_POSITIONS,
    MAX_POSITION_SIZE_PCT,
    MIN_POSITION_SIZE_PCT,
)
from strategies.indicators import df_from_candles, add_indicators
from strategies.risk import calculate_position
from strategies.fees import apply_slippage
from core.price_reconcile import shift_stop_take_by_fill, describe_shift


async def execute_pending_paper_proposals(
    moex_client,
    daily_open_price_fn,
    guard_new_position,
) -> None:
    """Execute pending paper proposals at today's open price.

    Closes exit proposals, reverses opposite positions, and opens aligned
    positions with realistic slippage.
    """
    pending = await db.get_robot_proposals(status="pending", limit=200, since_days=7)
    if not pending:
        return

    for prop in pending:
        ticker = prop["ticker"]
        side = prop["side"]
        try:
            open_px = await daily_open_price_fn(ticker)
            if open_px is None or open_px <= 0:
                logger.warning(f"No open price for {ticker}; skipping execution")
                continue

            # Exit proposal: close only a position whose side still matches the side
            # recorded on the proposal. A position that has been reversed intraday
            # should not be closed by a stale exit request.
            if prop.get("signal") == "exit":
                existing = await db.get_open_paper_position(ticker)
                if existing and existing["side"] == prop.get("side"):
                    await db.close_paper_position(existing["id"], open_px, "exit_at_open")
                    logger.info(f"Closed {ticker} {existing['side']} at open={open_px:.2f} (exit proposal {prop['id']})")
                elif existing:
                    logger.info(
                        f"Skipping exit proposal {prop['id']} for {ticker}: "
                        f"position side {existing['side']} != proposal side {prop.get('side')}"
                    )
                else:
                    logger.info(f"Exit proposal {prop['id']} for {ticker}: no open position")
                await db.execute_paper_proposal(prop["id"], open_px)
                continue

            # Respect global open-position limit before opening a new one.
            open_positions = await db.get_open_paper_positions()
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                logger.info(
                    f"Skipping queued {ticker} {side}: open position limit reached "
                    f"({MAX_OPEN_POSITIONS})"
                )
                continue

            ok, reason = await guard_new_position(ticker, side)
            if not ok:
                logger.info(f"Rejecting queued {ticker} {side}: {reason}")
                await db.reject_robot_proposal(prop["id"], decided_by="guard")
                continue

            # Recompute ATR and levels from the actual fill price.
            atr = open_px * 0.03
            try:
                candles = await moex_client.candles_recent(ticker, interval="D", count=30)
                if candles:
                    df = df_from_candles(candles)
                    df = add_indicators(df)
                    atr = float(df["atr"].iloc[-1]) if "atr" in df.columns else atr
            except Exception as exc:
                logger.warning(f"Could not compute ATR for {ticker}: {exc}")

            if not atr or atr <= 0:
                atr = open_px * 0.03

            plan = calculate_position(
                ticker, side, open_px, atr, PAPER_STARTING_CAPITAL, atr_mult=STOP_LOSS_ATR_MULT
            )
            planned_qty = max(plan.qty, 1)

            # Cap position value at MAX_POSITION_SIZE_PCT of equity.
            max_position_value = PAPER_STARTING_CAPITAL * (MAX_POSITION_SIZE_PCT / 100)
            if open_px > 0 and planned_qty * open_px > max_position_value:
                planned_qty = max(int(max_position_value / open_px), 1)

            # Also enforce the minimum position value threshold so tiny positions are skipped.
            min_position_value = PAPER_STARTING_CAPITAL * (MIN_POSITION_SIZE_PCT / 100)
            if open_px > 0 and planned_qty * open_px < min_position_value:
                logger.info(
                    f"Skipping queued {ticker} {side}: position value too small "
                    f"({planned_qty * open_px:.2f} RUB < {min_position_value:.2f} RUB)"
                )
                await db.reject_robot_proposal(prop["id"], decided_by="sizing")
                continue

            # If an opposite position exists, close it at the same open price.
            existing = await db.get_open_paper_position(ticker)
            if existing and existing["side"] != side:
                close_reason = "reverse_to_long" if side == "long" else "reverse_to_short"
                await db.close_paper_position(existing["id"], open_px, close_reason)
                logger.info(f"Closed {existing['side']} {ticker} at {open_px} for reversal")
            elif existing and existing["side"] == side:
                logger.info(f"Skipping queued {ticker} {side}: position already open")
                await db.execute_paper_proposal(prop["id"], open_px)
                continue

            # Apply realistic slippage.
            spread_pct = None
            try:
                ob = await moex_client._order_book_from_marketdata(ticker)
                spread_pct = ob.get("spread_pct")
            except Exception as exc:
                logger.debug(f"Could not fetch liquidity proxy for {ticker}: {exc}")
            fill_px = apply_slippage(open_px, side=side, spread_pct=spread_pct)

            # 26.08.2026: stop/take были посчитаны от open_px, но в БД
            # пишем fill_px (с проскальзыванием). При дрейфе >0.3%
            # сдвигаем стоп и тейк на ту же дельту, чтобы R:R не
            # искажался.
            open_stop, open_take, shifted = shift_stop_take_by_fill(
                open_px, fill_px, plan.stop_px, plan.target_px,
            )
            if shifted:
                logger.info(
                    describe_shift(
                        prop["id"], ticker, side,
                        open_px, fill_px,
                        plan.stop_px, plan.target_px,
                        open_stop, open_take,
                        source="paper",
                    )
                )

            await db.open_paper_position(
                ticker=ticker,
                side=side,
                entry_px=fill_px,
                signals_used=(prop.get("reason") or "").split(",") + ["paper_open"],
                stop_px=open_stop,
                take_px=open_take,
                qty=planned_qty,
                initial_atr=atr,
                atr_mult=TRAILING_STOP_ATR_MULT,
            )
            executed = await db.execute_paper_proposal(prop["id"], fill_px)
            if executed:
                logger.info(
                    f"Opened {ticker} {side} qty={planned_qty} at fill={fill_px:.2f} "
                    f"(open={open_px:.2f}) stop={open_stop:.2f} take={open_take:.2f} "
                    f"(proposal {prop['id']})"
                )
        except Exception as e:
            logger.warning(f"Morning execution failed for {ticker}: {e}")

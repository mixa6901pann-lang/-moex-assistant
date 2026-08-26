"""Broker order execution pipeline.

Pure logic for:
- deciding whether a market order can run right now (market phase check),
- running the trading guards (position limits, cash, daily loss),
- actually picking up pending/confirmed proposals and submitting them to Tinkoff.

Originally lived as methods on MoexAssistant in main.py and was extracted
here as part of the incremental main.py split (step 5b, 2026-08-20).

The functions take the tinkoff adapter and config as parameters so they
stay unit-testable without importing Tinkoff directly. The cron-style
loop in main.py just calls `run_broker_order_executor(...)` once per minute.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger

from core import db
from core import config as app_config


def market_phase() -> str:
    """Return a human-readable MOEX market phase for execution decisions.

    Used by can_execute_market_order to gate market orders to continuous
    trading phases. Auction and clearing phases are excluded because
    fills there are unpredictable.
    """
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    hour = now.hour
    minute = now.minute
    weekday = now.weekday()
    total_minutes = hour * 60 + minute
    if weekday >= 5:
        return "closed_weekend"
    if total_minutes < 420:
        return "pre_open"
    if 420 <= total_minutes < 590:
        return "morning_session"
    if 590 <= total_minutes < 600:
        return "morning_clearing"
    if 600 <= total_minutes < 605:
        return "opening_auction"
    if 605 <= total_minutes < 1120:
        return "main_session"
    if 1120 <= total_minutes <= 1125:
        return "closing_auction_pre"
    if 1125 <= total_minutes < 1145:
        return "evening_clearing"
    if 1145 <= total_minutes < 1430:
        return "evening_session"
    return "post_close"


def can_execute_market_order() -> tuple[bool, str]:
    """Return (ok, reason) for submitting a market order right now."""
    phase = market_phase()
    tradable = {"morning_session", "main_session", "evening_session"}
    if phase in tradable:
        return True, ""
    phase_names = {
        "closed_weekend": "рынок закрыт (выходной)",
        "pre_open": "до открытия рынка",
        "morning_clearing": "утренний клиринг",
        "opening_auction": "аукцион открытия",
        "closing_auction_pre": "предзакрытие (аукцион)",
        "evening_clearing": "вечерний клиринг",
        "post_close": "рынок закрыт",
    }
    return False, phase_names.get(phase, phase)


async def check_trading_guards(
    tinkoff: Any,
    ticker: str,
    side: str,
    qty: int,
    entry_px: float,
    guard_new_position: Any,
) -> tuple[bool, str]:
    """Run all trading guards before submitting a real broker order.

    Returns (ok, reason). reason is "" when ok=True. May shrink `qty` if
    the order exceeds MAX_POSITION_SIZE_PCT (does not return False for
    that — just clamps).

    `guard_new_position` is a callable (ticker, side) -> (ok, reason) that
    encapsulates weekday/CBR/sentiment checks. Passed in to keep this
    module independent of main.py.
    """
    if app_config.CIRCUIT_BREAKER_ENABLED:
        return False, "Circuit breaker is ON — real orders disabled"
    if not tinkoff.ready:
        return False, "Tinkoff token not configured"
    can_trade, phase_reason = can_execute_market_order()
    if not can_trade:
        return False, f"Cannot execute market order: {phase_reason}"
    if not ticker:
        return False, "No ticker"

    ok, reason = await guard_new_position(ticker, side)
    if not ok:
        return False, reason

    # Use separate limits for paper (virtual) and broker (sandbox/live) positions
    # so confirmed sandbox orders are not blocked by unrelated paper trades.
    if app_config.PAPER_TRADING:
        open_positions = await db.get_open_paper_positions()
        max_positions = app_config.MAX_OPEN_POSITIONS
    else:
        account_id = await tinkoff.resolve_account_id()
        open_positions = await db.get_open_broker_positions(
            broker="tinkoff", account_id=account_id,
        )
        max_positions = app_config.MAX_BROKER_OPEN_POSITIONS
    if len(open_positions) >= max_positions:
        return False, f"Open position limit reached ({max_positions})"

    # Daily loss guard: compute today's realized PnL % from paper positions.
    try:
        stats = await db.get_paper_stats()
        total_pnl_pct = stats.get("total_pnl_pct", 0.0) or 0.0
        if total_pnl_pct < -app_config.MAX_DAILY_LOSS_PCT:
            return False, (
                f"Daily loss guard: total PnL {total_pnl_pct:.2f}% below "
                f"-{app_config.MAX_DAILY_LOSS_PCT}%"
            )
    except Exception:
        pass

    # Position size guard: cap notional value as % of equity and ensure
    # it is at least one lot.
    try:
        portfolio = await tinkoff.get_portfolio()
        equity = portfolio.total_value_rub or 0
        order_value = entry_px * qty
        if equity > 0 and order_value > 0:
            size_pct = order_value / equity * 100
            if size_pct > app_config.MAX_POSITION_SIZE_PCT:
                max_value = equity * (app_config.MAX_POSITION_SIZE_PCT / 100)
                max_qty = max(1, int(max_value / entry_px))
                # Do not hard-block; shrink the order so it fits the guard.
                qty = max_qty
                order_value = entry_px * qty
                size_pct = order_value / equity * 100
            if size_pct < app_config.MIN_POSITION_SIZE_PCT:
                min_value = equity * (app_config.MIN_POSITION_SIZE_PCT / 100)
                min_qty = int(min_value / entry_px)
                return False, (
                    f"Position size {size_pct:.2f}% of equity below minimum "
                    f"{app_config.MIN_POSITION_SIZE_PCT}%. Min qty ≈ {min_qty} shares"
                )
        # Cash check for longs only; shorts require margin.
        if side in ("long", "buy"):
            if portfolio.cash_rub < order_value:
                return False, (
                    f"Insufficient cash: {portfolio.cash_rub:.2f} RUB available, "
                    f"{order_value:.2f} RUB required"
                )
    except Exception as exc:
        return False, f"Could not verify portfolio/cash: {exc}"

    return True, ""


async def _attach_and_maybe_reattach(
    tinkoff: Any,
    ticker: str,
    side: str,
    lots: int,
    prop: dict,
    account_id: str,
    order_id: str,
) -> int:
    """Attach stop-orders, retry once on failure, optionally re-attach after
    a real fill that diverges from the signal entry.

    Returns the number of stop-orders successfully attached. Logs
    ERROR if no stop could be attached after retry — the position is
    then naked and needs manual intervention.
    """
    from core import stops
    stop_ids = await stops.attach_stop_orders(
        tinkoff, ticker, side, lots, account_id, prop, order_id,
    )
    if not stop_ids and prop.get("stop_px"):
        await asyncio.sleep(2)
        stop_ids = await stops.attach_stop_orders(
            tinkoff, ticker, side, lots, account_id, prop, order_id,
        )
        if stop_ids:
            logger.warning(
                f"Retry _attach_stop_orders for {ticker}: got {len(stop_ids)} ids"
            )
        else:
            logger.error(
                f"No protective stop attached for {ticker} after retry — "
                f"manual intervention needed"
            )
            return 0
    # If the real fill differs from the signal entry by more than
    # STOP_RECALC_THRESHOLD_PCT, the stops we just attached are misaligned
    # with the actual position. Re-place them at the broker.
    fill_px = prop.get("__fill_px")
    entry_px = prop.get("entry_px")
    initial_atr = prop.get("initial_atr")
    atr_mult = (
        app_config.TRAILING_STOP_ATR_MULT
        if prop.get("horizon") in ("3d", "7d")
        else None
    )
    if (
        fill_px
        and entry_px
        and initial_atr
        and atr_mult
        and app_config.STOP_RECALC_THRESHOLD_PCT > 0
        and abs(fill_px - entry_px) / max(entry_px, 1e-6)
            > app_config.STOP_RECALC_THRESHOLD_PCT / 100
    ):
        try:
            await stops.reattach_stop_orders_after_recalc(
                tinkoff, ticker, side, lots, account_id, prop,
            )
        except Exception as exc:
            logger.warning(f"Stop recalc/reattach failed for {ticker}: {exc}")
    return len(stop_ids)


async def run_broker_order_executor(
    tinkoff: Any,
    guard_new_position: Any,
) -> None:
    """Run every minute during market hours.

    Submits real broker market orders for:
    - live proposals when AUTO_TRADING_ENABLED is true,
    - semi_auto proposals after user confirmation (status='confirmed').

    Mirrors the former `MoexAssistant.broker_order_executor` method.
    """
    if app_config.PAPER_TRADING or not tinkoff.ready:
        return

    can_trade, phase_reason = can_execute_market_order()
    if not can_trade:
        logger.info(f"Broker order executor skipped: {phase_reason}")
        return

    logger.info("Running broker order executor")
    try:
        pending = await db.get_robot_proposals(
            status="pending" if app_config.AUTO_TRADING_ENABLED else "confirmed",
            limit=50,
            since_days=1,
        )
        # If auto-trading is on, also pick up confirmed proposals that may
        # have been waiting for execution.
        if app_config.AUTO_TRADING_ENABLED:
            confirmed = await db.get_robot_proposals(
                status="confirmed", limit=50, since_days=1,
            )
            seen = {p["id"] for p in pending}
            for p in confirmed:
                if p["id"] not in seen:
                    pending.append(p)

        account_id = await tinkoff.resolve_account_id()

        logger.info(
            f"broker_order_executor: AUTO_TRADE={app_config.AUTO_TRADE} "
            f"min_conf={app_config.AUTO_TRADE_MIN_CONFIDENCE} pending={len(pending)}"
        )
        for prop in pending:
            # Confidence guard. Two thresholds:
            #   - AUTO_TRADE_MIN_CONFIDENCE — for proposals the robot itself
            #     promoted to "auto_trade" (no manual confirm).
            #   - MANUAL_CONFIRM_MIN_CONFIDENCE — for proposals the user
            #     confirmed in UI; lower because the user explicitly accepted
            #     the risk. NULL confidence = skip, never trade blind.
            conf = prop.get("confidence")
            prop_mode = prop.get("proposal_mode")
            prop_status = prop.get("status")
            if app_config.AUTO_TRADE and prop_mode == "auto_trade":
                min_conf = app_config.AUTO_TRADE_MIN_CONFIDENCE
                if conf is None or conf < min_conf:
                    logger.info(
                        f"Auto-trade skip proposal {prop['id']} {prop['ticker']} "
                        f"{prop['side']}: confidence={conf} < {min_conf}"
                    )
                    continue
            elif prop_mode == "semi_auto" and prop_status == "confirmed":
                min_conf = app_config.MANUAL_CONFIRM_MIN_CONFIDENCE
                if conf is None or conf < min_conf:
                    logger.info(
                        f"Manual-confirm skip proposal {prop['id']} {prop['ticker']} "
                        f"{prop['side']}: confidence={conf} < {min_conf}"
                    )
                    continue

            if prop.get("proposal_mode") == "paper":
                continue
            # auto_trade mode is created only when AUTO_TRADE is on AND
            # confidence >= min_conf, so the proposal is safe to execute
            # without manual confirmation. semi_auto still needs confirm.
            if prop.get("proposal_mode") == "auto_trade":
                pass
            elif prop.get("proposal_mode") == "semi_auto" and prop.get("status") != "confirmed":
                continue

            ticker = prop["ticker"]
            side = prop["side"]
            qty = prop.get("qty") or 1
            entry_px = prop.get("entry_px") or 0

            ok, reason = await check_trading_guards(
                tinkoff, ticker, side, qty, entry_px, guard_new_position,
            )
            if not ok:
                logger.warning(f"Guard blocked broker order for {ticker}: {reason}")
                continue

            # Resolve instrument lot size and round qty to whole lots.
            instr = await tinkoff.find_instrument(ticker)
            if not instr:
                logger.warning(f"Could not resolve instrument for {ticker}")
                continue
            lot = int(instr.get("lot", 1) or 1)
            lots = max(1, int(round(qty / lot)))
            real_qty = lots * lot
            if real_qty != qty:
                logger.info(
                    f"Proposal {prop['id']} {ticker} {side} qty rounded "
                    f"{qty} -> {real_qty} (lot={lot}, lots={lots})"
                )

            # Dedup: if we already have an active broker order for this
            # proposal, do not place another one. This prevents double fills
            # when a previous DB write failed after the broker order was
            # already accepted.
            existing_orders = await db.get_broker_orders_for_proposal(prop["id"])
            active = [o for o in existing_orders if o["status"] in ("pending", "filled", "partial")]
            if active:
                logger.info(
                    f"Proposal {prop['id']} {ticker} {side} already has active "
                    f"broker order {active[0]['order_id']} ({active[0]['status']}), skipping"
                )
                continue

            # Position dedup: skip if we already have an open broker
            # position for this (ticker, side). If an opposite-side
            # position exists, close it at the new entry_px before opening.
            open_positions = await db.get_open_broker_positions(
                broker='tinkoff', account_id=account_id,
            )
            conflict = next(
                (p for p in open_positions
                 if p['ticker'].upper() == ticker.upper() and p['side'] == side),
                None,
            )
            if conflict:
                logger.info(
                    f"Proposal {prop['id']} {ticker} {side} skipped: "
                    f"open broker position {conflict['id']} already exists "
                    f"(qty={conflict['qty']} avg={conflict['avg_entry_px']})"
                )
                await db.reject_robot_proposal(
                    prop['id'], decided_by='system',
                    reject_reason=f"dedup: open {ticker} {side} position already exists",
                )
                continue

            opposite = next(
                (p for p in open_positions
                 if p['ticker'].upper() == ticker.upper() and p['side'] != side),
                None,
            )
            if opposite:
                reverse_reason = (
                    "reverse_to_long" if side == "long" else "reverse_to_short"
                )
                rev_exit_px = entry_px if entry_px > 0 else (
                    opposite.get("avg_entry_px") or 0.0
                )
                rev_entry_px = opposite.get("avg_entry_px") or 0.0
                rev_qty = abs(opposite.get("qty") or 0)
                await db.close_broker_position(
                    ticker=ticker,
                    broker="tinkoff",
                    account_id=account_id,
                    reason=reverse_reason,
                    exit_px=rev_exit_px,
                )
                if rev_entry_px and rev_qty and rev_exit_px:
                    await db.record_journal_entry(
                        ticker=ticker,
                        side=opposite["side"],
                        entry_px=rev_entry_px,
                        qty=rev_qty,
                        exit_px=rev_exit_px,
                        stop_px=opposite.get("stop_px"),
                        target_px=opposite.get("take_px"),
                        reason=reverse_reason,
                        notes=f"reverse on proposal {prop['id']} -> {side}",
                    )
                logger.info(
                    f"Closed opposite {opposite['side']} {ticker} (qty={rev_qty}) "
                    f"at {rev_exit_px} for reversal into {side} "
                    f"(proposal {prop['id']})"
                )

            logger.info(
                f"Submitting {side} market order {ticker}: {lots} lot(s) "
                f"({real_qty} shares) account={account_id}"
            )
            result = await tinkoff.place_market_order(
                ticker=ticker, side=side, lots=lots, account_id=account_id,
            )

            env = "sandbox" if tinkoff.sandbox else "live"
            # Use the real fill price from Tinkoff, not the signal's
            # entry_px — otherwise P&L and avg_entry_px are fiction.
            # Guard against Tinkoff returning a sentinel value (0, None, or
            # a price outside ±50% of the proposal entry).
            raw_px = result.executed_price
            if (
                raw_px is None
                or raw_px <= 0
                or (entry_px and abs(raw_px - entry_px) / max(entry_px, 1e-6) > 0.5)
            ):
                logger.warning(
                    f"Suspicious fill price for {ticker} {side}: "
                    f"executed_price={raw_px}, falling back to entry_px={entry_px}"
                )
                fill_px = entry_px
            else:
                fill_px = raw_px

            # 26.08.2026: пересчёт stop/take по реальной цене исполнения.
            # Пользователь мог подтвердить proposal с опозданием, и к
            # моменту исполнения цена ушла — без сдвига стоп/тейк остаются
            # на старом расстоянии, риск/профит по позиции искажаются.
            # delta = fill_px - planned_entry, дальше stop/take сдвигаем
            # на эту же дельту (абсолютные расстояния сохраняются).
            planned_entry_px = entry_px
            stop_px_for_position = prop.get("stop_px")
            take_px_for_position = prop.get("take_px")
            if (
                fill_px
                and planned_entry_px
                and stop_px_for_position
                and take_px_for_position
            ):
                drift_pct = abs(fill_px - planned_entry_px) / planned_entry_px
                if drift_pct > 0.003:  # 0.3% — тот же порог, что для entry
                    delta = fill_px - planned_entry_px
                    new_stop = round(stop_px_for_position + delta, 4)
                    new_take = round(take_px_for_position + delta, 4)
                    logger.info(
                        f"proposal {prop['id']} {ticker} {side} fill drift "
                        f"{drift_pct*100:.2f}% > 0.3% "
                        f"(planned={planned_entry_px:.4f} fill={fill_px:.4f}); "
                        f"stop {stop_px_for_position:.4f}->{new_stop:.4f}, "
                        f"take {take_px_for_position:.4f}->{new_take:.4f}"
                    )
                    stop_px_for_position = new_stop
                    take_px_for_position = new_take

            if result.status in (
                "EXECUTION_REPORT_STATUS_FILL",
                "EXECUTION_REPORT_STATUS_NEW",
                "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
            ):
                await db.save_broker_order(
                    proposal_id=prop["id"],
                    ticker=ticker,
                    side=side,
                    broker="tinkoff",
                    account_id=account_id,
                    order_id=result.order_id,
                    lots=lots,
                    qty=real_qty,
                    entry_px=fill_px,
                    status="pending" if result.status in (
                        "EXECUTION_REPORT_STATUS_NEW",
                        "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
                    ) else "filled",
                    broker_message=result.message,
                    environment=env,
                )
                if result.status == "EXECUTION_REPORT_STATUS_FILL":
                    await db.mark_proposal_executed(prop["id"], decided_by="broker")
                    atr_mult = (
                        app_config.TRAILING_STOP_ATR_MULT
                        if prop.get("horizon") in ("3d", "7d")
                        else None
                    )
                    await db.update_broker_position(
                        ticker=ticker,
                        side=side,
                        qty=real_qty,
                        lots=lots,
                        entry_px=fill_px,
                        stop_px=stop_px_for_position,
                        take_px=take_px_for_position,
                        initial_atr=prop.get("initial_atr"),
                        atr_mult=atr_mult,
                        account_id=account_id,
                        reason="broker fill",
                    )
                    # Place protective stop-loss / take-profit orders.
                    # Pass the fill_px and drift-shifted stop/take through prop
                    # so the reattach helper can detect divergence from the
                    # signal entry AND post the shifted protective orders
                    # instead of the original (now stale) ones. We override
                    # the original stop_px/take_px keys (not __-prefixed)
                    # because stops.attach_stop_orders reads them directly.
                    prop_with_fill = dict(prop)
                    prop_with_fill["__fill_px"] = fill_px
                    if stop_px_for_position is not None:
                        prop_with_fill["stop_px"] = stop_px_for_position
                    if take_px_for_position is not None:
                        prop_with_fill["take_px"] = take_px_for_position
                    await _attach_and_maybe_reattach(
                        tinkoff, ticker, side, lots,
                        prop_with_fill, account_id, result.order_id,
                    )
                logger.info(
                    f"Broker order accepted: {result.order_id} {result.status} "
                    f"lots_executed={result.lots_executed}"
                )
            else:
                logger.warning(
                    f"Broker order rejected for {ticker}: {result.status} — {result.message}"
                )
                await db.save_broker_order(
                    proposal_id=prop["id"],
                    ticker=ticker,
                    side=side,
                    broker="tinkoff",
                    account_id=account_id,
                    order_id=result.order_id or "",
                    lots=lots,
                    qty=real_qty,
                    entry_px=entry_px,
                    status="rejected",
                    broker_message=result.message,
                    environment=env,
                )
                await db.reject_robot_proposal(
                    prop["id"], decided_by="broker", reject_reason="broker rejected",
                )
    except Exception as e:
        logger.warning(f"Broker order executor failed: {e}")

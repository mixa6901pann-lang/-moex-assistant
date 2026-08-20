"""Evening and intraday routines: stop/take checks, broker reconciliation.

Runs on cron schedules from main.py:
- evening_paper_check (~19:06 MSK)
- evening_broker_check (~19:07 MSK) — fallback stop/take + ATR trailing
- intraday_broker_stop_check (every 60s) — 1m candle limit close
- intraday_broker_reconcile (every 60s) — detect broker-side closes + phantoms

Originally lived as methods on MoexAssistant in main.py and was extracted
here as part of the incremental main.py split (step 5v, 2026-08-20).

Dependencies are passed in as plain objects so this module stays
free of Tinkoff / DB imports beyond the high-level `core.db` calls.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from core import db
from core import config as app_config
from core.broker_executor import can_execute_market_order
from strategies.indicators import df_from_candles, add_indicators


_MSK = timezone(timedelta(hours=3))


# --- helpers -----------------------------------------------------------------

async def _current_atr(moex: Any, ticker: str) -> float | None:
    """Fetch ~30 daily candles and return the latest ATR value, or None."""
    try:
        atr_candles = await moex.candles_recent(ticker, interval="1d", count=30)
        if not atr_candles:
            return None
        df = df_from_candles(atr_candles)
        df = add_indicators(df)
        if "atr" not in df.columns:
            return None
        return float(df["atr"].iloc[-1])
    except Exception:
        return None


def _hit_stop_or_take(
    side: str, stop_px: float | None, take_px: float | None,
    low: float, high: float,
) -> tuple[bool, bool]:
    """Return (hit_stop, hit_take) based on daily low/high."""
    hit_stop = stop_px is not None and (
        (side == "long" and low <= stop_px) or (side == "short" and high >= stop_px)
    )
    hit_take = take_px is not None and (
        (side == "long" and high >= take_px) or (side == "short" and low <= take_px)
    )
    return hit_stop, hit_take


# --- paper -------------------------------------------------------------------

async def run_evening_paper_check(moex: Any) -> None:
    """Run at ~19:06 MSK — check stop/take and close stale paper positions."""
    logger.info("Running evening paper check")
    open_pos = await db.get_open_paper_positions()
    if not open_pos:
        return
    for pos in open_pos:
        try:
            # Use daily candle to know high/low of the day; close alone can
            # overshoot the stop and distort P&L.
            candles = await moex.candles_recent(pos["ticker"], interval="1d", count=1)
            if not candles:
                continue
            candle = candles[-1]
            close = float(candle["close"])
            high = float(candle.get("high", close))
            low = float(candle.get("low", close))
            side = pos["side"]
            stop_px = pos.get("stop_px")
            take_px = pos.get("take_px")
            horizon_tag = next(
                (t for t in (pos.get("trigger_signal") or []) if t.startswith("medium:")),
                None,
            )

            # Trail stop in the direction of profit using the latest ATR.
            atr_mult = pos.get("atr_mult")
            if atr_mult and stop_px:
                current_atr = await _current_atr(moex, pos["ticker"])
                if current_atr and current_atr > 0:
                    if side == "long":
                        new_stop = close - current_atr * atr_mult
                        if new_stop > stop_px:
                            await db.update_paper_position_stop(pos["id"], round(new_stop, 2))
                            logger.info(
                                f"Paper position {pos['id']} {pos['ticker']} trailing stop raised: "
                                f"{stop_px:.2f} -> {new_stop:.2f} (ATR {current_atr:.2f} x {atr_mult})"
                            )
                            stop_px = new_stop
                    else:
                        new_stop = close + current_atr * atr_mult
                        if new_stop < stop_px:
                            await db.update_paper_position_stop(pos["id"], round(new_stop, 2))
                            logger.info(
                                f"Paper position {pos['id']} {pos['ticker']} trailing stop lowered: "
                                f"{stop_px:.2f} -> {new_stop:.2f} (ATR {current_atr:.2f} x {atr_mult})"
                            )
                            stop_px = new_stop

            hit_stop, hit_take = _hit_stop_or_take(side, stop_px, take_px, low, high)

            if hit_stop:
                exit_px = stop_px
                await db.close_paper_position(pos["id"], exit_px, "stop_loss")
                logger.info(
                    f"Paper position {pos['id']} {pos['ticker']} hit stop: "
                    f"daily range {low}-{high}, closed at {exit_px}"
                )
                continue
            if hit_take:
                exit_px = take_px
                await db.close_paper_position(pos["id"], exit_px, "take_profit")
                logger.info(
                    f"Paper position {pos['id']} {pos['ticker']} hit take: "
                    f"daily range {low}-{high}, closed at {exit_px}"
                )
                continue

            # Timeout: intraday/evening positions — 7 days; medium-term — 14 days
            open_dt = datetime.strptime(pos["open_ts"], "%Y-%m-%d %H:%M:%S")
            timeout_days = 14 if horizon_tag else 7
            if (datetime.now() - open_dt).days >= timeout_days:
                await db.close_paper_position(pos["id"], close, "timeout")
                logger.info(
                    f"Paper position {pos['id']} {pos['ticker']} closed by "
                    f"timeout ({timeout_days}d)"
                )
        except Exception as e:
            logger.warning(f"Paper check failed for {pos['ticker']}: {e}")


# --- broker (evening) --------------------------------------------------------

async def run_evening_broker_check(
    tinkoff: Any,
    moex: Any,
    stops: Any,
) -> None:
    """Run at ~19:06 MSK — check stop/take for real broker positions.

    Acts as a fallback in case a Tinkoff stop order failed to fire. Also
    applies trailing-stop logic to medium-term positions.

    `stops` is a namespace-like object with the callables:
      - latest_stop_order_id(ticker, account_id) -> str | None
      - replace_stop_order(tinkoff, ticker, side, lots, account_id,
                           old_stop_order_id, new_stop_price) -> str
      - persist_stop_order_id(ticker, account_id, new_stop_id) -> None
      - cancel_open_stop_orders(tinkoff, ticker, account_id) -> int
    """
    if not tinkoff.ready:
        return
    logger.info("Running evening broker check")
    account_id = await tinkoff.resolve_account_id()
    open_pos = await db.get_open_broker_positions(account_id=account_id)
    if not open_pos:
        return
    for pos in open_pos:
        try:
            candles = await moex.candles_recent(pos["ticker"], interval="1d", count=1)
            if not candles:
                continue
            candle = candles[-1]
            close = float(candle["close"])
            high = float(candle.get("high", close))
            low = float(candle.get("low", close))
            side = pos["side"]
            stop_px = pos.get("stop_px")
            take_px = pos.get("take_px")

            # If a legacy position has no stop level, give it a fresh ATR-based
            # stop so it is protected going forward.
            if stop_px is None:
                current_atr = await _current_atr(moex, pos["ticker"])
                if current_atr and current_atr > 0 and pos.get("avg_entry_px"):
                    if side == "long":
                        stop_px = round(
                            pos["avg_entry_px"] - current_atr * app_config.STOP_LOSS_ATR_MULT, 2,
                        )
                    else:
                        stop_px = round(
                            pos["avg_entry_px"] + current_atr * app_config.STOP_LOSS_ATR_MULT, 2,
                        )
                    await db.update_broker_position_stop(pos["id"], stop_px)
                    logger.info(
                        f"Broker position {pos['id']} {pos['ticker']} assigned default stop: {stop_px}"
                    )

            atr_mult = pos.get("atr_mult")
            if atr_mult and stop_px:
                current_atr = await _current_atr(moex, pos["ticker"])
                if current_atr and current_atr > 0:
                    if side == "long":
                        new_stop = close - current_atr * atr_mult
                        if new_stop > stop_px:
                            await db.update_broker_position_stop(pos["id"], round(new_stop, 2))
                            logger.info(
                                f"Broker position {pos['id']} {pos['ticker']} trailing stop raised: "
                                f"{stop_px:.2f} -> {new_stop:.2f}"
                            )
                            old_stop_id = await stops.latest_stop_order_id(pos["ticker"], account_id)
                            new_id = await stops.replace_stop_order(
                                tinkoff=tinkoff,
                                ticker=pos["ticker"], side=side,
                                lots=abs(int(pos.get("lots") or 0)),
                                account_id=account_id,
                                old_stop_order_id=old_stop_id,
                                new_stop_price=new_stop,
                            )
                            if new_id:
                                await stops.persist_stop_order_id(pos["ticker"], account_id, new_id)
                            stop_px = new_stop
                    else:
                        new_stop = close + current_atr * atr_mult
                        if new_stop < stop_px:
                            await db.update_broker_position_stop(pos["id"], round(new_stop, 2))
                            logger.info(
                                f"Broker position {pos['id']} {pos['ticker']} trailing stop lowered: "
                                f"{stop_px:.2f} -> {new_stop:.2f}"
                            )
                            old_stop_id = await stops.latest_stop_order_id(pos["ticker"], account_id)
                            new_id = await stops.replace_stop_order(
                                tinkoff=tinkoff,
                                ticker=pos["ticker"], side=side,
                                lots=abs(int(pos.get("lots") or 0)),
                                account_id=account_id,
                                old_stop_order_id=old_stop_id,
                                new_stop_price=new_stop,
                            )
                            if new_id:
                                await stops.persist_stop_order_id(pos["ticker"], account_id, new_id)
                            stop_px = new_stop

            hit_stop, hit_take = _hit_stop_or_take(side, stop_px, take_px, low, high)

            if hit_stop or hit_take:
                close_side = "sell" if side in ("long", "buy") else "buy"
                lots = abs(int(pos.get("lots") or 0))
                if lots == 0:
                    continue
                reason = "stop_loss" if hit_stop else "take_profit"
                await stops.cancel_open_stop_orders(tinkoff, pos["ticker"], account_id)
                result = await tinkoff.place_market_order(
                    ticker=pos["ticker"],
                    side=close_side,
                    lots=lots,
                    account_id=account_id,
                )
                if result.status in (
                    "EXECUTION_REPORT_STATUS_FILL",
                    "EXECUTION_REPORT_STATUS_NEW",
                    "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
                ):
                    fill_px = float(stop_px if hit_stop else take_px)
                    await db.close_broker_position(
                        ticker=pos["ticker"],
                        account_id=account_id,
                        reason=reason,
                    )
                    await db.record_journal_entry(
                        ticker=pos["ticker"],
                        side=pos['side'],
                        entry_px=float(pos.get('avg_entry_px') or 0),
                        qty=abs(int(pos.get('qty') or 0)),
                        exit_px=fill_px,
                        stop_px=stop_px,
                        target_px=take_px,
                        reason=reason,
                        notes=f"evening_broker_check order={result.order_id}",
                    )
                    logger.info(
                        f"Broker position {pos['ticker']} closed by {reason}: "
                        f"daily range {low}-{high}, order={result.order_id}"
                    )
                else:
                    logger.warning(
                        f"Failed to close broker position {pos['ticker']} by {reason}: "
                        f"{result.status} — {result.message}"
                    )
        except Exception as e:
            logger.warning(f"Broker check failed for {pos['ticker']}: {e}")
    # Stats summary: helps spot when the daily stop is too tight
    # (audit 17 Aug 2026: 4/5 closes were intraday signals that hit
    # the 1.5*ATR stop on a daily-noise spike within hours of entry).
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        stats = await db.get_broker_close_stats(today)
        if stats and stats.get("total", 0) > 0:
            logger.info(
                f"evening_broker_check stats: "
                f"closed_today={stats['total']} "
                f"stop_loss={stats.get('stop_loss', 0)} "
                f"take_profit={stats.get('take_profit', 0)} "
                f"pnl={stats.get('pnl_rub', 0):+.2f}₽"
            )
    except Exception:
        pass


# --- broker (intraday) -------------------------------------------------------

async def run_intraday_broker_stop_check(
    tinkoff: Any,
    moex: Any,
    stops: Any,
    closing_ids: set,
) -> None:
    """Run every 60s during 7-23 MSK — close broker positions that hit stop/take.

    Mirrors evening_broker_check but operates on 1-minute candles and submits
    a limit order at the exact stop/take price (no slippage). The evening job
    stays as a fallback for default-stops and trailing-stop rebalancing.

    `closing_ids` is an in-memory set that prevents two ticks from closing
    the same position concurrently (the executor and this check may run
    inside the same minute).
    """
    if app_config.PAPER_TRADING:
        return
    if not tinkoff.ready:
        return
    ok, reason = can_execute_market_order()
    if not ok:
        return

    account_id = await tinkoff.resolve_account_id()
    open_pos = await db.get_open_broker_positions(account_id=account_id)
    if not open_pos:
        return

    for pos in open_pos:
        if pos["id"] in closing_ids:
            continue
        closing_ids.add(pos["id"])
        try:
            candles = await moex.candles_recent(pos["ticker"], interval="1m", count=2)
            if not candles:
                continue
            candle = candles[-1]
            high = float(candle.get("high", candle["close"]))
            low = float(candle.get("low", candle["close"]))
            side = pos["side"]
            stop_px = pos.get("stop_px")
            take_px = pos.get("take_px")

            hit_stop, hit_take = _hit_stop_or_take(side, stop_px, take_px, low, high)
            if not (hit_stop or hit_take):
                continue

            close_side = "sell" if side in ("long", "buy") else "buy"
            lots = abs(int(pos.get("lots") or 0))
            if lots == 0:
                continue
            reason_label = "stop_loss" if hit_stop else "take_profit"
            trigger_px = stop_px if hit_stop else take_px
            await stops.cancel_open_stop_orders(tinkoff, pos["ticker"], account_id)
            result = await tinkoff.place_limit_order(
                ticker=pos["ticker"],
                side=close_side,
                lots=lots,
                price=float(trigger_px),
                account_id=account_id,
            )
            if result.status in (
                "EXECUTION_REPORT_STATUS_FILL",
                "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
            ):
                await db.close_broker_position(
                    ticker=pos["ticker"],
                    account_id=account_id,
                    reason=reason_label,
                )
                await db.record_journal_entry(
                    ticker=pos["ticker"],
                    side=pos['side'],
                    entry_px=float(pos.get('avg_entry_px') or 0),
                    qty=abs(int(pos.get('qty') or 0)),
                    exit_px=float(trigger_px),
                    stop_px=stop_px,
                    target_px=take_px,
                    reason=reason_label,
                    notes=f"intraday_broker_stop_check order={result.order_id}",
                )
                logger.info(
                    f"Intraday close {pos['ticker']} {reason_label} @ {trigger_px}: "
                    f"order={result.order_id} status={result.status}"
                )
            elif result.status == "EXECUTION_REPORT_STATUS_NEW":
                # Limit order resting in the book; next tick will retry if it
                # still hasn't filled.
                logger.info(
                    f"Intraday close {pos['ticker']} {reason_label} limit pending @ {trigger_px}"
                )
            else:
                logger.warning(
                    f"Intraday close {pos['ticker']} {reason_label} failed: "
                    f"{result.status} — {result.message}"
                )
        except Exception as e:
            logger.warning(f"Intraday broker check failed for {pos['ticker']}: {e}")
        finally:
            closing_ids.discard(pos["id"])


async def run_intraday_broker_reconcile(tinkoff: Any) -> None:
    """Detect broker-side closes we missed: stop/take orders on the
    exchange fired without going through our limit-order path.

    Runs every 60s. Compares broker_positions against the live portfolio
    from Tinkoff — any tracked open row that is no longer on the broker
    was closed by the exchange (or manually) and needs to be reflected
    in broker_positions.status + journal for accurate P&L.

    Also detects *phantom* closed rows whose open fill never actually
    settled at the broker (audit 17 Aug 2026: 5 closed rows for
    SBER/GAZP/MTSS/VKCO/MGNT all came from fills that did not appear in
    Tinkoff's operation log). Such rows are flagged but kept — they may
    still represent a real entry that the broker just lagged reporting.
    """
    if app_config.PAPER_TRADING or not tinkoff.ready:
        return
    logger.info("Running intraday broker reconcile (open + phantom)")
    try:
        account_id = await tinkoff.resolve_account_id()
        portfolio = await tinkoff.get_portfolio()
        real_keys: set[tuple[str, str]] = set()
        for p in portfolio.positions:
            if p.ticker == "RUB000UTSTOM":
                continue
            side = "short" if int(p.quantity) < 0 else "long"
            real_keys.add((p.ticker.upper(), side))

        # Pull broker operations for the last 7 days so we can detect
        # manual closes by the user (close op exists, but the position
        # has already vanished from the live portfolio).
        ops_from = (datetime.now(_MSK) - timedelta(days=7)).isoformat()
        ops_to = datetime.now(_MSK).isoformat()
        broker_ops = await tinkoff.get_operations(
            account_id=account_id, from_iso=ops_from, to_iso=ops_to,
        )
        ops_by_ticker: dict[str, list[dict]] = {}
        for op in broker_ops:
            tic = (op.get("ticker") or "").upper()
            if not tic:
                continue
            p = op.get("price", {}) or {}
            price = float(p.get("units", 0)) + float(p.get("nano", 0)) / 1e9
            ops_by_ticker.setdefault(tic, []).append({
                "date": op.get("date"),
                "qty": int(op.get("quantity") or 0),
                "price": price,
                "type": op.get("type") or "",
            })

        open_pos = await db.get_open_broker_positions(account_id=account_id)
        for pos in open_pos:
            key = (pos["ticker"].upper(), (pos["side"] or "long").lower())
            if key in real_keys:
                continue
            # Position vanished from the broker — it was closed by an
            # exchange-side stop/take or by the user (manual).
            side_l = (pos.get("side") or "long").lower()
            entry_px = float(pos.get("avg_entry_px") or 0.0)
            stop_px = float(pos.get("stop_px") or 0.0)
            take_px = float(pos.get("take_px") or 0.0)
            qty_pos = abs(int(pos.get("qty") or 0))
            # Try to find a closing op: opposite-sign qty after the
            # position's open timestamp. Use the most recent one.
            open_ts = pos.get("ts") or ""
            close_op_price = 0.0
            close_op_ts = ""
            for op in ops_by_ticker.get(pos["ticker"].upper(), []):
                if abs(op["qty"]) != qty_pos:
                    continue
                # For SHORT, a close is a positive qty (buy back).
                # For LONG, a close is a negative qty (sell).
                if side_l == "short" and op["qty"] <= 0:
                    continue
                if side_l == "long" and op["qty"] >= 0:
                    continue
                if open_ts and op["date"] and op["date"] < open_ts:
                    continue
                if op["date"] >= close_op_ts:
                    close_op_ts = op["date"]
                    close_op_price = op["price"]
            if close_op_price > 0:
                # Real close fill exists. Reason depends on whether a
                # protective stop/take was live at the time.
                prefer_stop = False
                if stop_px and entry_px:
                    if side_l == "short" and stop_px > entry_px:
                        prefer_stop = True
                    elif side_l == "long" and stop_px < entry_px:
                        prefer_stop = True
                if prefer_stop and abs(close_op_price - stop_px) < app_config.STOP_TAKE_MATCH_TOLERANCE_RUB:
                    reason = "broker_stop"
                elif (not prefer_stop) and take_px and abs(close_op_price - take_px) < app_config.STOP_TAKE_MATCH_TOLERANCE_RUB:
                    reason = "broker_take"
                else:
                    reason = "broker_manual"
                exit_px = close_op_price
            else:
                # No close op in broker ops → fall back to protective
                # order inference (was the previous logic).
                prefer_stop = False
                if stop_px and entry_px:
                    if side_l == "short" and stop_px > entry_px:
                        prefer_stop = True
                    elif side_l == "long" and stop_px < entry_px:
                        prefer_stop = True
                if stop_px and (prefer_stop or not take_px):
                    reason = "broker_stop"
                    exit_px = stop_px
                elif take_px:
                    reason = "broker_take"
                    exit_px = take_px
                else:
                    reason = "broker_manual"
                    exit_px = entry_px
            await db.close_broker_position(
                ticker=pos["ticker"],
                account_id=account_id,
                reason=reason,
                exit_px=exit_px,
            )
            entry_px = pos.get("avg_entry_px")
            qty = abs(int(pos.get("qty") or 0))
            if entry_px and exit_px and qty:
                await db.record_journal_entry(
                    ticker=pos["ticker"],
                    side=pos["side"],
                    entry_px=float(entry_px),
                    qty=qty,
                    exit_px=exit_px,
                    stop_px=pos.get("stop_px"),
                    target_px=pos.get("take_px"),
                    reason=reason,
                    notes=f"intraday_broker_reconcile pos_id={pos['id']}",
                )
            logger.info(
                f"Broker closed {pos['ticker']} {pos['side']} qty={qty} "
                f"-> {reason} @ {exit_px}"
            )

        # Phantom detection: closed rows whose open fill never made it to
        # Tinkoff. We compare against the *operations history* (real fills),
        # not the live portfolio — because a legitimately closed row is by
        # definition no longer in the portfolio. Audit 18 Aug 2026: NVTK id=7
        # had real broker fills at 04:01/08:42 but the portfolio-based check
        # marked it _phantom and re-wrote exit_px to a fake take_px.
        try:
            ops = await tinkoff.get_operations(
                account_id=account_id,
                from_iso=(datetime.now(_MSK) - timedelta(days=7)).isoformat(),
                to_iso=datetime.now(_MSK).isoformat(),
            )
            fill_keys: set[tuple[str, str]] = set()
            for op in ops:
                tic = (op.get("ticker") or "").upper()
                if not tic:
                    continue
                qty = int(op.get("quantity") or 0)
                if qty > 0:
                    fill_keys.add((tic, "long"))
                elif qty < 0:
                    fill_keys.add((tic, "short"))
            closed_pos = await db.get_broker_positions(
                account_id=account_id, status="closed", since_days=2,
            )
            for pos in closed_pos:
                side = (pos.get("side") or "long").lower()
                key = (pos["ticker"].upper(), side)
                if key in real_keys:
                    continue
                if pos.get("close_reason", "").endswith("_phantom"):
                    continue
                if key in fill_keys:
                    continue
                new_reason = f"{pos.get('close_reason', 'closed')}_phantom"
                await db.close_broker_position(
                    ticker=pos["ticker"],
                    account_id=account_id,
                    reason=new_reason,
                    exit_px=pos.get("exit_px"),
                )
                logger.warning(
                    f"Phantom position flagged: {pos['ticker']} {pos['side']} "
                    f"qty={pos.get('qty')} reason={new_reason} — broker has no record"
                )
        except Exception as exc:
            logger.warning(f"Phantom detection failed: {exc}")
    except Exception as e:
        logger.warning(f"Intraday broker reconcile failed: {e}")

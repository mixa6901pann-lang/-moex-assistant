"""Stop-order management for broker positions.

Pure async functions (no class). Originally lived as methods on
MoexAssistant in main.py and were extracted here as part of the
incremental main.py split (step 5a, 2026-08-19). They take the broker
adapter and db module as parameters so they stay unit-testable.

This module is concerned only with placing, replacing, and cancelling
Tinkoff stop-orders tied to a broker position. Order execution /
reconciliation lives in core/broker_executor.py and core/evening_routines.py.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger

from core import db
from core.broker_utils import parse_stop_ids


async def attach_stop_orders(
    tinkoff: Any,
    ticker: str,
    side: str,
    lots: int,
    account_id: str,
    prop: dict,
    order_id: str | None = None,
) -> list[str]:
    """Place stop-loss and take-profit stop-orders for an open broker position.

    Made GTC so the orders survive between sessions. Persists the resulting
    stop_order_ids on the broker_orders row when `order_id` is provided.

    Returns the list of stop_order_ids that were placed (order: [stop, take]
    if both succeeded, or just the one that did).
    """
    stop_px = prop.get("stop_px")
    take_px = prop.get("take_px")
    stop_ids: list[str] = []
    if stop_px:
        try:
            so = await tinkoff.place_stop_order(
                ticker=ticker,
                stop_type="stop_loss",
                stop_price=stop_px,
                lots=lots,
                account_id=account_id,
                direction="sell" if side in ("long", "buy") else "buy",
                expiration_type="ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL",
            )
            if so.stop_order_id:
                stop_ids.append(so.stop_order_id)
                logger.info(f"Attached stop-loss {ticker} @ {stop_px} id={so.stop_order_id}")
        except Exception as exc:
            logger.warning(f"Failed to attach stop-loss for {ticker}: {exc}")
    if take_px:
        try:
            so = await tinkoff.place_stop_order(
                ticker=ticker,
                stop_type="take_profit",
                stop_price=take_px,
                lots=lots,
                account_id=account_id,
                direction="sell" if side in ("long", "buy") else "buy",
                expiration_type="ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL",
            )
            if so.stop_order_id:
                stop_ids.append(so.stop_order_id)
                logger.info(f"Attached take-profit {ticker} @ {take_px} id={so.stop_order_id}")
        except Exception as exc:
            logger.warning(f"Failed to attach take-profit for {ticker}: {exc}")
    if stop_ids and order_id:
        await db.update_broker_order_status(
            order_id, status="filled", stop_order_ids=stop_ids
        )
    return stop_ids


async def reattach_stop_orders_after_recalc(
    tinkoff: Any,
    ticker: str,
    side: str,
    lots: int,
    account_id: str,
    prop: dict,
) -> None:
    """After a broker fill at a different price than the signal, the SL/TP
    placed by attach_stop_orders are misaligned with the real position.
    Cancel them and re-attach at the recalculated stop/take, which
    update_broker_position already wrote to broker_positions.

    The DB row is the source of truth — we read fresh stop_px/take_px from
    there, then cancel the prior stop-order ids saved on the most recent
    filled broker_order, and place new ones.
    """
    positions = await db.get_broker_positions(
        account_id=account_id, status="open", since_days=2,
    )
    pos = next((p for p in positions if p["ticker"] == ticker.upper()), None)
    if pos is None:
        logger.warning(f"Reattach skipped: no open broker_position for {ticker}")
        return
    new_stop = pos.get("stop_px")
    new_take = pos.get("take_px")
    if not new_stop and not new_take:
        logger.warning(f"Reattach skipped: no stop/take recorded for {ticker}")
        return

    # Find the most recent filled broker_order for this ticker to get the
    # old stop_order_ids we placed a moment ago.
    orders = await db.get_broker_orders(
        ticker=ticker, status="filled", account_id=account_id, limit=1,
    )
    order = orders[0] if orders else None
    old_stop_ids = parse_stop_ids((order or {}).get("stop_order_ids"))
    cancelled = 0
    for sid in old_stop_ids:
        try:
            await tinkoff.cancel_stop_order(sid, account_id=account_id)
            cancelled += 1
        except Exception as exc:
            logger.warning(f"Failed to cancel stop-order {sid} for {ticker}: {exc}")

    # Build a temporary prop-like dict so we can reuse attach_stop_orders.
    recalc_prop = dict(prop)
    recalc_prop["stop_px"] = new_stop
    recalc_prop["take_px"] = new_take
    new_stop_ids = await attach_stop_orders(
        tinkoff=tinkoff,
        ticker=ticker, side=side, lots=lots,
        prop=recalc_prop, account_id=account_id,
        order_id=(order or {}).get("order_id") or "",
    )
    logger.warning(
        f"Recalculated SL/TP for {ticker} {side}: "
        f"signal={prop.get('entry_px')} fill={pos.get('avg_entry_px')} "
        f"stop={prop.get('stop_px')}->{new_stop} "
        f"take={prop.get('take_px')}->{new_take} "
        f"cancelled={cancelled} new_orders={len(new_stop_ids)}"
    )


async def replace_stop_order(
    tinkoff: Any,
    ticker: str,
    side: str,
    lots: int,
    account_id: str,
    old_stop_order_id: str | None,
    new_stop_price: float,
) -> str:
    """Cancel an existing stop-loss order and place a new one at a new price.

    Returns the new stop_order_id, or empty string on failure.
    Used by evening_broker_check when the trailing stop moves stop_px upward
    (long) or downward (short) — the old GTC stop on the broker must follow.
    """
    if old_stop_order_id:
        try:
            await tinkoff.cancel_stop_order(old_stop_order_id, account_id=account_id)
            logger.info(
                f"Trailing stop: cancelled old stop-order {old_stop_order_id} for {ticker}"
            )
        except Exception as exc:
            logger.warning(
                f"Trailing stop: failed to cancel {old_stop_order_id} for {ticker}: {exc}"
            )
    try:
        so = await tinkoff.place_stop_order(
            ticker=ticker,
            stop_type="stop_loss",
            stop_price=round(new_stop_price, 2),
            lots=abs(lots),
            account_id=account_id,
            direction="sell" if side in ("long", "buy") else "buy",
            expiration_type="ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL",
        )
        if so.stop_order_id:
            logger.info(
                f"Trailing stop: placed new stop-loss {ticker} @ {new_stop_price:.2f} id={so.stop_order_id}"
            )
            return so.stop_order_id
        logger.warning(
            f"Trailing stop: place_stop_order returned empty id for {ticker} @ {new_stop_price:.2f}: {so.message}"
        )
        return ""
    except Exception as exc:
        logger.warning(f"Trailing stop: failed to place new stop for {ticker}: {exc}")
        return ""


async def latest_stop_order_id(ticker: str, account_id: str) -> str | None:
    """Return the stop-loss stop_order_id from the most recent filled broker_order.

    The first element of `stop_order_ids` corresponds to the stop-loss,
    placed first in `attach_stop_orders`.
    """
    try:
        orders = await db.get_broker_orders(
            ticker=ticker, status="filled", account_id=account_id, limit=1,
        )
        if not orders:
            return None
        ids = parse_stop_ids(orders[0].get("stop_order_ids"))
        return ids[0] if ids else None
    except Exception as exc:
        logger.warning(f"latest_stop_order_id({ticker}) failed: {exc}")
        return None


async def persist_stop_order_id(
    ticker: str, account_id: str, new_stop_id: str,
) -> None:
    """Replace the first element (stop-loss) of the latest filled broker_order's
    stop_order_ids with new_stop_id, preserving the take-profit id if present.
    """
    try:
        orders = await db.get_broker_orders(
            ticker=ticker, status="filled", account_id=account_id, limit=1,
        )
        if not orders:
            return
        order = orders[0]
        old_ids = parse_stop_ids(order.get("stop_order_ids"))
        old_ids[0] = new_stop_id
        await db.update_broker_order_status(
            order_id=order["order_id"], status="filled", stop_order_ids=old_ids,
        )
    except Exception as exc:
        logger.warning(f"persist_stop_order_id({ticker}) failed: {exc}")


async def cancel_open_stop_orders(tinkoff: Any, ticker: str, account_id: str) -> int:
    """Cancel every stop-order recorded for the most recent filled broker_order.

    Returns the count of successful cancellations. Used before a manual
    or market close so the GTC stops don't dangle after the position is gone.
    """
    cancelled = 0
    try:
        orders = await db.get_broker_orders(
            ticker=ticker, status="filled", account_id=account_id, limit=1,
        )
        if not orders:
            return 0
        ids = parse_stop_ids(orders[0].get("stop_order_ids"))
        for sid in ids:
            try:
                await tinkoff.cancel_stop_order(sid, account_id=account_id)
                cancelled += 1
            except Exception as exc:
                logger.warning(f"Cancel stop-order {sid} for {ticker} failed: {exc}")
        if cancelled:
            await db.update_broker_order_status(
                order_id=orders[0]["order_id"], status="filled", stop_order_ids=[],
            )
    except Exception as exc:
        logger.warning(f"cancel_open_stop_orders({ticker}) failed: {exc}")
    return cancelled

"""MOEX Assistant — main entry point with scheduled tasks.

Orchestrates: morning screener, intraday monitoring, evening report.
VKontakte only.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

import core.config as app_config
from core.config import BASE_DIR
from bot.vk_bot import VkBotAdapter
from bot.vk_wall import VkWallPoster
from core.config import (
    VK_ENABLED, HEALTH_PORT, HOST, WATCHLIST, WEB_UI_ENABLED,
    LLM_PROVIDER, OLLAMA_URL, OLLAMA_MODEL, MAX_OPEN_POSITIONS, MAX_BROKER_OPEN_POSITIONS,
    PAPER_TRADING, SEMI_AUTO_TRADING, TINKOFF_TOKEN, TINKOFF_SANDBOX,
    MAX_DAILY_LOSS_PCT, CIRCUIT_BREAKER_ENABLED,
    CBR_MEETING_DATES, CBR_SOFT_MODE_ENABLED,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    MAX_POSITION_SIZE_PCT, MIN_POSITION_SIZE_PCT,
    STOP_LOSS_ATR_MULT, TRAILING_STOP_ATR_MULT, STOP_RECALC_THRESHOLD_PCT,
    EVENING_HOUR, EVENING_PREDICTION_MINUTE, EVENING_PAPER_MINUTE,
    EVENING_BROKER_MINUTE, EVENING_MEDIUM_TERM_MINUTE,
    STOP_TAKE_MATCH_TOLERANCE_RUB,
)
from core import db
from core.moex import MoexClient
from core.analyzer import evening_report
from core.trading_agent import agent as trading_agent
from strategies.indicators import df_from_candles, add_indicators, resample_1m_to_5m
from core.intraday_agent import agent as intraday_agent
from brokers.tinkoff_client import TinkoffClient
from execution.pipeline import ExecutionPipeline
from execution.intraday import run_intraday_monitor
from execution.evening import run_evening_trading_decision
from execution.medium_term import run_medium_term_proposals
from execution.paper_execution import execute_pending_paper_proposals

TelegramAdapter = None
if TELEGRAM_BOT_TOKEN:
    from bot.telegram_bot import TelegramAdapter

import uvicorn

# Import the full SPA+API app (desktop.html + mobile + /api/*)
if WEB_UI_ENABLED:
    from api.mobile_api import app as web_app
else:
    from api.web_stub import app as web_app

# Configure loguru: daily rotation, 7-day retention, MSK timezone
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
MSK_TZ = ZoneInfo("Europe/Moscow")


def _msk_format(record):
    t = record["time"]
    if t.tzinfo is None:
        t = t.replace(tzinfo=ZoneInfo("UTC"))
    record["extra"]["msk"] = t.astimezone(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return "{extra[msk]} | {level: <8} | {name}:{function}:{line} - {message}\n{exception}"


logger.remove()
logger.add(sys.stderr, level="INFO", format=_msk_format)
logger.add(
    str(LOG_DIR / "moex_{time:YYYY-MM-DD}.log"),
    rotation="1 day",
    retention="7 days",
    level="INFO",
    encoding="utf-8",
    format=_msk_format,
)


def _is_market_open() -> bool:
    """Return True during any MOEX equities trading session.

    Supported sessions (MSK, Mon-Fri):
    - Morning additional: 07:00 - 09:50
    - Main:               10:00 - 18:45
    - Evening additional: 19:05 - 23:50
    """
    return _market_phase() in ("morning_session", "main_session", "evening_session")


def _market_phase() -> str:
    """Backwards-compatible alias for core.broker_executor.market_phase."""
    from core.broker_executor import market_phase
    return market_phase()


def _can_execute_market_order() -> tuple[bool, str]:
    """Backwards-compatible alias for core.broker_executor.can_execute_market_order."""
    from core.broker_executor import can_execute_market_order
    return can_execute_market_order()


class MoexAssistant:
    """Main orchestrator: schedules and runs all tasks."""

    def _stops_namespace(self):
        """Return a namespace with the 4 stop helpers used by evening_routines."""
        from core import stops as _stops
        return type("NS", (), {
            "latest_stop_order_id": _stops.latest_stop_order_id,
            "replace_stop_order": _stops.replace_stop_order,
            "persist_stop_order_id": _stops.persist_stop_order_id,
            "cancel_open_stop_orders": _stops.cancel_open_stop_orders,
        })()


    """Main orchestrator: schedules and runs all tasks."""

    def __init__(self):
        self.vk_bot = VkBotAdapter()
        self.vk_wall = VkWallPoster()
        self.moex = MoexClient()
        self.tinkoff = TinkoffClient(sandbox=TINKOFF_SANDBOX)
        self.scheduler = AsyncIOScheduler()
        self.telegram = TelegramAdapter() if (TelegramAdapter and TELEGRAM_BOT_TOKEN) else None
        self._telegram_enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
        self.execution_pipeline = ExecutionPipeline(self.moex, self.tinkoff)
        # In-memory lock: prevents two ticks from closing the same broker position
        # at the same time (e.g. when the broker executor and the intraday stop
        # check both run inside the same minute).
        self._closing_ids: set[int] = set()

    async def morning_screener(self):
        """Thin wrapper around core.morning_screener.run_morning_screener."""
        from core.morning_screener import run_morning_screener
        await run_morning_screener(self.moex, self.vk_wall)

    async def rss_sentiment_scan(self):
        """Thin wrapper around core.rss_sentiment.run_rss_sentiment_scan."""
        from core.rss_sentiment import run_rss_sentiment_scan
        await run_rss_sentiment_scan()

    async def intraday_monitor(self):
        """Run every 15 min during trading hours — check for 5m intraday signals."""
        await run_intraday_monitor(
            moex_client=self.moex,
            pipeline=self.execution_pipeline,
            intraday_agent=intraday_agent,
            resample_1m_to_5m=resample_1m_to_5m,
            telegram_enabled=self._telegram_enabled,
            send_proposal_alert=self._send_proposal_alert,
            vk_wall=self.vk_wall,
            vk_enabled=VK_ENABLED,
        )

    async def evening_report_task(self):
        """Run at ~19:00 MSK after main session close — summarize the day."""
        logger.info("Running evening report")

        positions = await db.open_positions()
        closed_trades = await db.closed_trades_today()

        # Get market summary
        try:
            summary = await self.moex.market_summary()
            summary_text = f"Обработано {len(summary)} тикеров"
        except Exception:
            summary_text = ""

        await evening_report(closed_trades, positions, summary_text)

        # Post to VK wall
        if VK_ENABLED:
            try:
                await self.vk_wall.post_evening_report()
            except Exception as e:
                logger.warning(f"VK evening post failed: {e}")

    async def update_dividends(self):
        """Thin wrapper around core.dividends.run_update_dividends."""
        from core.dividends import run_update_dividends
        await run_update_dividends(self.moex)

    async def _market_price(self, ticker: str) -> float | None:
        """Thin wrapper around core.dividends.get_market_price.

        Kept as a method because several other modules inject it as
        `market_price_fn=self._market_price` — see evening_paper_check,
        evening_broker_check, intraday_broker_stop_check.
        """
        from core.dividends import get_market_price
        return await get_market_price(self.moex, ticker)

    async def _daily_open_price(self, ticker: str) -> float | None:
        """Thin wrapper around core.dividends.get_daily_open_price.

        Kept as a method because morning_paper_execution injects it via
        `daily_open_price_fn=self._daily_open_price`.
        """
        from core.dividends import get_daily_open_price
        return await get_daily_open_price(self.moex, ticker)

    async def morning_paper_execution(self):
        """Run at 10:05 MSK — execute queued paper proposals at today's open.

        Delegates to execution.paper_execution so the orchestrator stays thin.
        """
        logger.info("Running morning paper execution")
        await execute_pending_paper_proposals(
            moex_client=self.moex,
            daily_open_price_fn=self._daily_open_price,
            guard_new_position=self._guard_new_position,
        )

    async def evening_trading_decision(self):
        """STUB (since 2026-08-10): вечерний цикл TradingAgent отключён.

        За 10 дней работы (1-10 августа 2026) TradingAgent:
        - 100 прогнозов, из них 97 = hold/wait, 3 = buy/sell.
        - 0 живых proposal в robot_proposals (10 от 1 авг — тестовые, удалены cleanup).
        - 45% сессий падали в max iterations из 5 шагов.
        Вывод: бесполезен, жжёт LLM-кредиты.

        Метод-заглушка сохранён, чтобы не ломать cron-регистрацию ниже.
        Чтобы включить обратно — раскомментировать блок ниже.
        """
        logger.info("evening_trading_decision: STUB (TradingAgent disabled since 2026-08-10)")
        return 0
        # await run_evening_trading_decision(
        #     moex_client=self.moex,
        #     market_price_fn=self._market_price,
        #     pipeline=self.execution_pipeline,
        #     trading_agent=trading_agent,
        #     telegram_enabled=self._telegram_enabled,
        #     send_proposal_alert=self._send_proposal_alert,
        #     vk_wall=self.vk_wall,
        #     vk_enabled=VK_ENABLED,
        # )

    async def _send_proposal_alert(
        self,
        proposal_id: int,
        ticker: str,
        side: str,
        entry_px: float | None,
        stop_px: float | None,
        take_px: float | None,
        qty: int,
        source: str,
        reason: str,
        mode: str = "semi_auto",
    ) -> None:
        """Send a Telegram alert about a new actionable proposal."""
        if not self.telegram or not TELEGRAM_CHAT_ID:
            return
        emoji = "📗" if side in ("long", "buy") else "📕"
        mode_text = "Авто" if mode == "live" else "Подтвердите вручную"
        text = (
            f"{emoji} Новое предложение #{proposal_id}\n"
            f"<b>{ticker}</b> {side.upper()} — {mode_text}\n"
            f"Источник: {source}\n"
            f"Вход: {entry_px or '-'} ₽ | Стоп: {stop_px or '-'} ₽ | Тейк: {take_px or '-'} ₽\n"
            f"Кол-во: {qty}\n"
            f"{reason[:200]}"
        )
        await self.telegram._app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="HTML")

    def _cbr_soft_mode(self) -> tuple[bool, bool, date | None]:
        """Return (is_meeting_day, is_pre_meeting_day, next_meeting_date)."""
        return db.cbr_soft_mode_state()

    async def _guard_new_position(self, ticker: str, side: str) -> tuple[bool, str]:
        """Position-agnostic guard used for paper proposals and real orders.

        Blocks new positions during CBR soft mode and blocks new shorts near a
        dividend cutoff.
        """
        if CBR_SOFT_MODE_ENABLED:
            cbr_meeting, cbr_pre, cbr_date = self._cbr_soft_mode()
            if cbr_meeting or cbr_pre:
                label = "meeting day" if cbr_meeting else "pre-meeting day"
                return False, (
                    f"CBR soft mode ({label}): no new positions around "
                    f"{cbr_date.isoformat() if cbr_date else 'upcoming meeting'}"
                )

        if side in ("short", "sell"):
            try:
                near_cutoff, cutoff_date = await db.is_near_dividend_cutoff(ticker, look_ahead_days=3)
                if near_cutoff:
                    return False, (
                        f"Dividend cutoff for {ticker} on {cutoff_date.isoformat()}: "
                        "short positions blocked"
                    )
            except Exception:
                pass

        return True, ""

    async def _check_trading_guards(self, ticker: str, side: str, qty: int, entry_px: float) -> tuple[bool, str]:
        """Thin wrapper around core.broker_executor.check_trading_guards."""
        from core.broker_executor import check_trading_guards
        return await check_trading_guards(
            self.tinkoff, ticker, side, qty, entry_px, self._guard_new_position,
        )

    async def broker_order_executor(self):
        """Thin wrapper around core.broker_executor.run_broker_order_executor."""
        from core.broker_executor import run_broker_order_executor
        await run_broker_order_executor(
            self.tinkoff, self._guard_new_position,
        )

    # Stop-order helpers below are thin wrappers around core.stops — the
    # real implementations live in core/stops.py so they can be unit-tested
    # without importing Tinkoff. Kept here as instance methods so the many
    # call sites in this file (broker_order_executor, evening_broker_check,
    # intraday_broker_stop_check) do not need to change.

    async def _attach_stop_orders(
        self, ticker, side, lots, prop, account_id, order_id,
    ) -> list[str]:
        from core import stops
        return await stops.attach_stop_orders(
            self.tinkoff, ticker, side, lots, account_id, prop, order_id,
        )

    async def _reattach_stop_orders_after_recalc(
        self, ticker, side, lots, account_id, prop,
    ) -> None:
        from core import stops
        await stops.reattach_stop_orders_after_recalc(
            self.tinkoff, ticker, side, lots, account_id, prop,
        )

    async def _replace_stop_order(
        self, ticker, side, lots, account_id, old_stop_order_id, new_stop_price,
    ) -> str:
        from core import stops
        return await stops.replace_stop_order(
            self.tinkoff, ticker, side, lots, account_id, old_stop_order_id, new_stop_price,
        )

    async def _latest_stop_order_id(self, ticker, account_id) -> str | None:
        from core import stops
        return await stops.latest_stop_order_id(ticker, account_id)

    async def _persist_stop_order_id(self, ticker, account_id, new_stop_id) -> None:
        from core import stops
        await stops.persist_stop_order_id(ticker, account_id, new_stop_id)

    async def _cancel_open_stop_orders(self, ticker, account_id) -> int:
        from core import stops
        return await stops.cancel_open_stop_orders(self.tinkoff, ticker, account_id)

    async def broker_order_poller(self):
        """Run every 5 minutes during market hours — poll pending broker orders."""
        if PAPER_TRADING or not self.tinkoff.ready:
            return

        can_trade, phase_reason = _can_execute_market_order()
        if not can_trade:
            logger.info(f"Broker order poller skipped: {phase_reason}")
            return

        logger.info("Running broker order poller")
        try:
            pending = await db.get_pending_broker_orders(limit=50)
            account_id = await self.tinkoff.resolve_account_id()
            for order in pending:
                state = await self.tinkoff.get_order_state(order["order_id"], account_id=account_id)
                status = state.get("executionReportStatus", "EXECUTION_STATUS_UNSPECIFIED")
                if status == "EXECUTION_STATUS_FILL":
                    await db.update_broker_order_status(order["order_id"], status="filled", broker_message=str(status))
                    await db.mark_proposal_executed(order["proposal_id"], decided_by="broker_poll")
                    prop = await db.get_robot_proposal(order["proposal_id"]) if order.get("proposal_id") else None
                    atr_mult = (
                        TRAILING_STOP_ATR_MULT
                        if prop and prop.get("horizon") in ("3d", "7d")
                        else None
                    )
                    # Pull the actual fill price from the order state and validate
                    # before updating broker_positions. Sentinel detection
                    # mirrors the path used by broker_order_executor.
                    raw_px = None
                    mv = state.get("executedOrderPrice")
                    if mv and isinstance(mv, dict):
                        from brokers.tinkoff_client import _money_value
                        raw_px = _money_value(mv) or None
                    signal_px = (prop.get("entry_px") if prop else None)
                    if (
                        raw_px is None
                        or raw_px <= 0
                        or (signal_px and abs(raw_px - signal_px) / max(signal_px, 1e-6) > 0.5)
                    ):
                        fill_px = signal_px
                    else:
                        fill_px = raw_px
                    await db.update_broker_position(
                        ticker=order["ticker"],
                        side=order["side"],
                        qty=order.get("qty") or 0,
                        lots=order.get("lots") or 0,
                        entry_px=fill_px,
                        stop_px=prop.get("stop_px") if prop else None,
                        take_px=prop.get("take_px") if prop else None,
                        atr_mult=atr_mult,
                        initial_atr=prop.get("initial_atr") if prop else None,
                        account_id=account_id,
                        reason="broker poll fill",
                    )
                    # Re-attach SL/TP at the recalculated prices if the real
                    # fill diverged from the signal. Same threshold as the
                    # executor path.
                    if (
                        fill_px
                        and signal_px
                        and prop
                        and prop.get("initial_atr")
                        and atr_mult
                        and STOP_RECALC_THRESHOLD_PCT > 0
                        and abs(fill_px - signal_px) / max(signal_px, 1e-6)
                            > STOP_RECALC_THRESHOLD_PCT / 100
                    ):
                        try:
                            lots_abs = abs(order.get("lots") or 0)
                            await self._reattach_stop_orders_after_recalc(
                                ticker=order["ticker"],
                                side=order["side"],
                                lots=lots_abs,
                                account_id=account_id,
                                prop=prop,
                            )
                        except Exception as exc:
                            logger.warning(
                                f"Stop recalc/reattach failed for {order['ticker']} (poll): {exc}"
                            )
                    lots = abs(order.get("lots") or 0)
                    if lots and prop:
                        stop_ids = await self._attach_stop_orders(
                            ticker=order["ticker"],
                            side=order["side"],
                            lots=lots,
                            prop=prop,
                            account_id=account_id,
                            order_id=order["order_id"],
                        )
                        if not stop_ids and prop.get("stop_px"):
                            await asyncio.sleep(2)
                            stop_ids = await self._attach_stop_orders(
                                ticker=order["ticker"],
                                side=order["side"],
                                lots=lots,
                                prop=prop,
                                account_id=account_id,
                                order_id=order["order_id"],
                            )
                            if stop_ids:
                                logger.warning(
                                    f"Retry _attach_stop_orders for {order['ticker']} "
                                    f"(broker_order_poller): got {len(stop_ids)} ids"
                                )
                    logger.info(f"Broker order {order['order_id']} filled")
                elif status in ("EXECUTION_STATUS_REJECTED", "EXECUTION_STATUS_CANCELLED"):
                    await db.update_broker_order_status(order["order_id"], status=status.lower().replace("execution_status_", ""), broker_message=str(status))
                    if order.get("proposal_id"):
                        await db.reject_robot_proposal(order["proposal_id"], decided_by="broker_poll")
                    logger.warning(f"Broker order {order['order_id']} {status}")
        except Exception as e:
            logger.warning(f"Broker order poller failed: {e}")

    async def geo_risk_scan(self):
        """Thin wrapper around core.geo_risk.run_geo_risk_scan."""
        from core.geo_risk import run_geo_risk_scan
        await run_geo_risk_scan(
            market_open_fn=_is_market_open,
            market_price_fn=self._market_price,
        )

    async def check_predictions(self):
        """Run at ~19:05 MSK — evaluate pending predictions against actual prices.

        Only directional predictions (buy/long/short/sell) are scored.
        Wait/hold/neutral predictions are left pending because they do not
        express a directional view and would pollute the accuracy metric.
        """
        logger.info("Checking prediction accuracy")
        directional = {"buy", "long", "short", "sell"}
        for horizon in (1, 3, 7):
            pending = await db.get_pending_predictions(horizon)
            if not pending:
                continue
            logger.info(f"Prediction check {horizon}d: {len(pending)} pending")
            for p in pending:
                if p["predicted_direction"] not in directional:
                    logger.debug(
                        f"Prediction {p['id']} {p['ticker']} skipped: non-directional '{p['predicted_direction']}'"
                    )
                    continue
                try:
                    candles = await self.moex.candles_recent(p["ticker"], count=1)
                    if not candles:
                        continue
                    actual = float(candles[-1]["close"])
                    pred_dir = p["predicted_direction"]
                    pred_px = p["predicted_price"]
                    if pred_dir in ("buy", "long"):
                        result = "correct" if actual > pred_px * 1.005 else "wrong"
                    elif pred_dir in ("short", "sell"):
                        result = "correct" if actual < pred_px * 0.995 else "wrong"
                    else:
                        continue
                    await db.update_prediction_result(p["id"], horizon, result, actual)
                    logger.info(f"Prediction {p['id']} {p['ticker']} {horizon}d: {result} (pred {pred_px}, actual {actual})")
                except Exception as e:
                    logger.warning(f"Prediction check failed for {p['ticker']} {horizon}d: {e}")

    async def evening_paper_check(self):
        """Thin wrapper around core.evening_routines.run_evening_paper_check."""
        from core.evening_routines import run_evening_paper_check
        await run_evening_paper_check(self.moex)

    async def evening_broker_check(self):
        """Thin wrapper around core.evening_routines.run_evening_broker_check."""
        from core.evening_routines import run_evening_broker_check
        await run_evening_broker_check(self.tinkoff, self.moex, self._stops_namespace())

    async def intraday_broker_stop_check(self):
        """Thin wrapper around core.evening_routines.run_intraday_broker_stop_check."""
        from core.evening_routines import run_intraday_broker_stop_check
        await run_intraday_broker_stop_check(
            self.tinkoff, self.moex, self._stops_namespace(), self._closing_ids,
        )

    async def intraday_broker_reconcile(self):
        """Thin wrapper around core.evening_routines.run_intraday_broker_reconcile."""
        from core.evening_routines import run_intraday_broker_reconcile
        await run_intraday_broker_reconcile(self.tinkoff)

    async def generate_medium_term_proposals(self):
        """Run after market close — queue 3d and 7d paper proposals.

        Delegates to execution.medium_term so the orchestrator stays thin.
        """
        await run_medium_term_proposals(
            moex_client=self.moex,
            market_price_fn=self._market_price,
            pipeline=self.execution_pipeline,
            guard_new_position=self._guard_new_position,
            telegram_enabled=self._telegram_enabled,
            send_proposal_alert=self._send_proposal_alert,
        )

    async def _close_shorts_before_dividend(self):
        """Run before market open — close any short position whose dividend
        cutoff is today (the last safe trading day before the registry closes).
        """
        logger.info("Checking dividend-driven short closures")
        today = date.today()

        # Paper / virtual positions
        for pos in await db.get_open_paper_positions():
            if pos["side"] != "short":
                continue
            cutoff = await db.dividend_close_cutoff_date(pos["ticker"])
            if cutoff is None or cutoff != today:
                continue
            px = await self._market_price(pos["ticker"])
            if not px:
                continue
            await db.close_paper_position(pos["id"], px, f"dividend_cutoff {cutoff}")
            logger.info(
                f"Closed paper short {pos['ticker']} at {px:.2f} "
                f"before dividend cutoff {cutoff}"
            )

        # Real broker positions
        if PAPER_TRADING or not self.tinkoff.ready:
            return
        try:
            account_id = await self.tinkoff.resolve_account_id()
            for pos in await db.get_open_broker_positions(account_id=account_id):
                if pos["side"] != "short":
                    continue
                cutoff = await db.dividend_close_cutoff_date(pos["ticker"])
                if cutoff is None or cutoff != today:
                    continue
                lots = abs(pos["lots"])
                if lots <= 0:
                    continue
                result = await self.tinkoff.place_market_order(
                    ticker=pos["ticker"], side="buy", lots=lots, account_id=account_id
                )
                if result.status == "EXECUTION_REPORT_STATUS_FILL":
                    await db.close_broker_position(
                        pos["ticker"], account_id=account_id,
                        reason=f"dividend cutoff {cutoff}"
                    )
                    await db.record_journal_entry(
                        ticker=pos["ticker"],
                        side='short',
                        entry_px=float(pos.get('avg_entry_px') or 0),
                        qty=abs(int(pos.get('qty') or lots)),
                        reason=f"dividend cutoff {cutoff}",
                        notes=f"dividend_close order={result.order_id}",
                    )
                    logger.info(
                        f"Closed real short {pos['ticker']} {lots} lots "
                        f"before dividend cutoff {cutoff}"
                    )
                else:
                    logger.warning(
                        f"Failed to close real short {pos['ticker']}: "
                        f"{result.status} — {result.message}"
                    )
        except Exception as exc:
            logger.warning(f"Dividend short closure failed: {exc}")

    async def _cbr_profit_protect(self):
        """During CBR soft mode, move profitable positions to breakeven.

        Runs only on the pre-meeting day and the meeting day itself.
        """
        cbr_meeting, cbr_pre, cbr_date = self._cbr_soft_mode()
        if not (cbr_meeting or cbr_pre):
            return
        logger.info(
            f"CBR profit protection active ({'meeting' if cbr_meeting else 'pre-meeting'} "
            f"day, next meeting {cbr_date})"
        )
        for pos in await db.get_open_paper_positions():
            entry = pos.get("entry_px")
            stop = pos.get("stop_px")
            if not entry:
                continue
            try:
                px = await self._market_price(pos["ticker"])
                if px is None:
                    continue
                if pos["side"] == "long" and px > entry:
                    if stop is None or stop < entry:
                        await db.update_paper_position_stop(pos["id"], round(entry, 2))
                        logger.info(
                            f"CBR protect: long {pos['ticker']} stop -> breakeven {entry:.2f}"
                        )
                elif pos["side"] == "short" and px < entry:
                    if stop is None or stop > entry:
                        await db.update_paper_position_stop(pos["id"], round(entry, 2))
                        logger.info(
                            f"CBR protect: short {pos['ticker']} stop -> breakeven {entry:.2f}"
                        )
            except Exception as exc:
                logger.warning(f"CBR profit protect failed for {pos['ticker']}: {exc}")

    async def cleanup_stale_proposals(self):
        """Retire pending proposals that are too old to act on.

        Runs every 2 hours so the UI does not accumulate intraday and paper
        proposals past their useful lifespan. Newer signals still get their
        fresh proposal; this only retires ones that have been waiting too long.
        """
        try:
            superseded = await db.supersede_stale_pending_proposals()
            if superseded:
                logger.info(f"Superseded {superseded} stale pending proposals")
        except Exception as exc:
            logger.warning(f"Stale proposal cleanup failed: {exc}")

    def setup_schedule(self):
        """Thin wrapper around core.scheduler_setup.setup_schedule."""
        from core.scheduler_setup import setup_schedule as _setup_schedule
        _setup_schedule(self, self.scheduler)

    async def run(self):
        """Start VK bot and scheduler."""
        # Initialize VK bot
        if VK_ENABLED:
            try:
                asyncio.create_task(self.vk_bot.run())
                logger.info("VK bot started")
            except Exception as e:
                logger.warning(f"VK bot init failed: {e}")
        else:
            logger.info("VK bot disabled (VK_ENABLED=false)")

        self.setup_schedule()
        self.scheduler.start()
        logger.info("MOEX Assistant started")

        # Prime dividend calendar at startup so guards have fresh data.
        try:
            await self.update_dividends()
        except Exception as exc:
            logger.warning(f"Startup dividend update failed: {exc}")

        # Log LLM configuration so it is obvious why TradingAgent may fail
        logger.info(f"LLM provider configured: {LLM_PROVIDER}")
        if LLM_PROVIDER == "ollama":
            logger.info(f"Ollama config: {OLLAMA_URL} / model={OLLAMA_MODEL}")
        elif LLM_PROVIDER == "none":
            logger.warning("LLM_PROVIDER is 'none'; TradingAgent will default to hold")

        # Log trading mode
        if PAPER_TRADING:
            logger.info("Trading mode: PAPER (virtual trades)")
        elif SEMI_AUTO_TRADING:
            logger.info("Trading mode: SEMI-AUTO (proposals only, user confirmation required)")
        else:
            logger.info("Trading mode: LIVE (real broker orders)")
        if TINKOFF_TOKEN:
            mode_str = "sandbox" if TINKOFF_SANDBOX else "LIVE"
            logger.info(f"Tinkoff broker adapter: configured ({mode_str})")
        else:
            logger.info("Tinkoff broker adapter: not configured")

        if CBR_SOFT_MODE_ENABLED and CBR_MEETING_DATES:
            sorted_dates = sorted(CBR_MEETING_DATES)
            logger.info(f"CBR soft mode enabled; meetings: {', '.join(d.isoformat() for d in sorted_dates)}")
            cbr_meeting, cbr_pre, cbr_date = self._cbr_soft_mode()
            if cbr_meeting or cbr_pre:
                label = "meeting day" if cbr_meeting else "pre-meeting day"
                logger.warning(f"CBR soft mode ACTIVE today ({label}, meeting {cbr_date}) — new positions blocked, profit protection on")
        else:
            logger.info("CBR soft mode disabled or no meeting dates configured")

        # Load runtime auto-trading toggle from DB (falls back to env default).
        try:
            app_config.AUTO_TRADING_ENABLED = await db.load_auto_trading_enabled(
                default=app_config.AUTO_TRADING_ENABLED_DEFAULT
            )
            logger.info(f"Auto-trading enabled: {app_config.AUTO_TRADING_ENABLED}")
        except Exception as e:
            logger.warning(f"Failed to load auto-trading setting, using env default: {e}")

        # Load runtime auto-trade toggle from DB (falls back to env default).
        try:
            app_config.AUTO_TRADE = await db.load_auto_trade_enabled(
                default=app_config.AUTO_TRADE
            )
            logger.info(
                f"Auto-trade (no-manual-confirm) enabled: {app_config.AUTO_TRADE} "
                f"min_confidence={app_config.AUTO_TRADE_MIN_CONFIDENCE}"
            )
        except Exception as e:
            logger.warning(f"Failed to load auto-trade setting, using env default: {e}")

        # Telegram bot (notifications only, no command handlers required)
        if self.telegram:
            try:
                await self.telegram.init()
                await self.telegram._app.initialize()
                await self.telegram._app.start()
                logger.info("Telegram notification adapter initialized")
            except Exception as e:
                logger.warning(f"Telegram adapter init failed: {e}")
                self.telegram = None

        # Web UI + Health endpoint (FastAPI)
        try:
            web_app.state.assistant = self
            uvicorn_config = uvicorn.Config(web_app, host=HOST, port=HEALTH_PORT, log_level="warning", log_config=None)
            server = uvicorn.Server(uvicorn_config)
            asyncio.create_task(server.serve())
            if WEB_UI_ENABLED:
                logger.info(f"Web UI + Health on :{HEALTH_PORT}")
            else:
                logger.info(f"Web UI stub + Health on :{HEALTH_PORT}")
        except Exception as e:
            logger.warning(f"Web server failed: {e}")

        # Keep alive
        while True:
            await asyncio.sleep(3600)


async def main():
    assistant = MoexAssistant()
    await assistant.run()


def _acquire_singleton_lock() -> int:
    """Acquire an exclusive file lock to prevent two moex instances.

    Returns the lock file descriptor so it can be kept alive for the
    lifetime of the process. If another instance already holds the lock,
    logs a clear error and exits with code 1. On normal exit the OS
    releases the lock automatically when the fd is closed.
    """
    lock_path = "/var/run/moex.lock"
    try:
        fd = open(lock_path, "w")
    except OSError as exc:
        logger.error(f"Cannot open lock file {lock_path}: {exc}")
        sys.exit(1)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.error(
            "Another moex instance already holds the singleton lock. "
            "If you are sure no other instance is running, delete "
            f"{lock_path} and restart."
        )
        fd.close()
        sys.exit(1)
    fd.write(f"{os.getpid()}\n")
    fd.flush()
    logger.info(f"Acquired singleton lock at {lock_path} (pid={os.getpid()})")
    return fd


if __name__ == "__main__":
    _lock_fd = _acquire_singleton_lock()
    asyncio.run(main())

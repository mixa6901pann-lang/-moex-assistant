"""Cron schedule configuration for the bot's recurring tasks.

All times are MSK (Europe/Moscow). Originally lived as
`MoexAssistant.setup_schedule` in main.py and was extracted here as
part of the incremental main.py split (step 5g, 2026-08-20).

The `assistant` argument is the MoexAssistant instance — every scheduled
job is a bound method on it. Reading constants from `core.config` keeps
the cron schedule configurable via environment variables.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from loguru import logger

from core.config import (
    EVENING_HOUR, EVENING_PREDICTION_MINUTE, EVENING_PAPER_MINUTE,
    EVENING_BROKER_MINUTE, EVENING_MEDIUM_TERM_MINUTE,
)


def setup_schedule(assistant: Any, scheduler: Any) -> None:
    """Configure recurring tasks on the given scheduler.

    Mirrors the original `MoexAssistant.setup_schedule` method exactly
    so the cron behavior does not change.
    """
    tz = "Europe/Moscow"
    logger.info(f"Scheduler timezone: {tz}; system local time: {datetime.now().isoformat()}")
    # Morning screener at 10:00 MSK
    scheduler.add_job(assistant.morning_screener, "cron", hour=10, minute=0, timezone=tz)

    # Execute queued paper proposals at the official open (10:05 MSK)
    scheduler.add_job(assistant.morning_paper_execution, "cron", hour=10, minute=5, timezone=tz)

    # Intraday monitor every 15 min during all Tinkoff sessions (07:00-18:45 MSK)
    scheduler.add_job(assistant.intraday_monitor, "cron", minute="*/15", hour="7-18", timezone=tz)

    # RSS sentiment scan every 15 min during market hours
    scheduler.add_job(assistant.rss_sentiment_scan, "cron", minute="*/15", hour="7-23", timezone=tz)

    # Geo-risk scan every 30 min during market hours
    scheduler.add_job(assistant.geo_risk_scan, "cron", minute="*/30", hour="7-23", timezone=tz)

    # Evening trading decision at 18:45 MSK (before market close, so exits can still be executed)
    scheduler.add_job(assistant.evening_trading_decision, "cron", hour=18, minute=45, timezone=tz)

    # Evening report at 19:00 MSK
    scheduler.add_job(assistant.evening_report_task, "cron", hour=EVENING_HOUR, minute=0, timezone=tz)

    # Prediction accuracy check at 19:05 MSK
    scheduler.add_job(
        assistant.check_predictions, "cron",
        hour=EVENING_HOUR, minute=EVENING_PREDICTION_MINUTE, timezone=tz,
    )

    # Paper trading check at 19:06 MSK (stop/take/trail for existing positions)
    scheduler.add_job(
        assistant.evening_paper_check, "cron",
        hour=EVENING_HOUR, minute=EVENING_PAPER_MINUTE, timezone=tz,
    )

    # Broker position check at 19:07 MSK (fallback stop/take close for real positions)
    scheduler.add_job(
        assistant.evening_broker_check, "cron",
        hour=EVENING_HOUR, minute=EVENING_BROKER_MINUTE, timezone=tz,
    )

    # Intraday broker stop/take check every minute during trading hours.
    # Uses 1-minute candles and limit orders so positions close the moment a
    # stop or take level is touched, not hours later at 19:07.
    scheduler.add_job(assistant.intraday_broker_stop_check, "cron", minute="*/1", hour="7-23", timezone=tz)
    scheduler.add_job(assistant.intraday_broker_reconcile, "cron", minute="*/1", hour="7-23", timezone=tz)

    # Medium-term proposals from predictions at 19:10 MSK
    scheduler.add_job(
        assistant.generate_medium_term_proposals, "cron",
        hour=EVENING_HOUR, minute=EVENING_MEDIUM_TERM_MINUTE, timezone=tz,
    )

    # Real broker order executor: checks confirmed/auto proposals every minute
    # during all Tinkoff sessions (morning additional, main, evening additional).
    # The can_execute_market_order guard inside the job blocks execution
    # outside tradable phases (auctions, clearing, post-close).
    scheduler.add_job(assistant.broker_order_executor, "cron", minute="*/1", hour="7-23", timezone=tz)

    # Real broker order status poller: updates fill status of pending orders.
    scheduler.add_job(assistant.broker_order_poller, "cron", minute="*/5", hour="7-23", timezone=tz)

    # Dividend update weekly on Saturday at 12:00 MSK
    scheduler.add_job(assistant.update_dividends, "cron", day_of_week="sat", hour=12, minute=0, timezone=tz)

    # Close shorts at dividend cutoff before the registry close date.
    scheduler.add_job(assistant._close_shorts_before_dividend, "cron", hour=9, minute=30, timezone=tz)

    # CBR profit protection: twice on pre-meeting / meeting days.
    scheduler.add_job(assistant._cbr_profit_protect, "cron", hour=9, minute=45, timezone=tz)
    scheduler.add_job(assistant._cbr_profit_protect, "cron", hour=13, minute=0, timezone=tz)

    # Retire stale pending proposals so the UI does not show ancient ideas.
    # Runs every 2 hours at :07 to avoid the :00 cluster with the broker executor.
    scheduler.add_job(assistant.cleanup_stale_proposals, "cron", hour="*/2", minute=7, timezone=tz)

    logger.info("Schedule configured")

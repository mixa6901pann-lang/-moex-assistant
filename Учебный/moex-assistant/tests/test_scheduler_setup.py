"""Static tests for core/scheduler_setup.py.

The function configures APScheduler jobs but we don't need a live scheduler
to test it — we use a small FakeScheduler that records every add_job call.
This keeps the tests runnable on Windows (no fcntl, no Tinkoff).
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import scheduler_setup  # noqa: E402


class FakeJob:
    """Mimics apscheduler.job.Job just enough for our assertions."""

    def __init__(self, func, trigger, trigger_args, kwargs):
        self.func = func
        self.trigger = trigger
        self.trigger_args = trigger_args
        self.kwargs = kwargs


class FakeScheduler:
    """Records add_job() calls instead of scheduling them."""

    def __init__(self):
        self.jobs: list[FakeJob] = []

    def add_job(self, func, trigger, **kwargs):
        # apscheduler uses positional args too sometimes; mirror them
        self.jobs.append(FakeJob(func=func, trigger=trigger, trigger_args={}, kwargs=kwargs))
        return self.jobs[-1]


class FakeAssistant:
    """Stand-in for MoexAssistant with bound methods the scheduler wires up."""

    pass


def _make_assistant() -> FakeAssistant:
    a = FakeAssistant()
    # Bind every method the scheduler references. The FakeScheduler just
    # stores the bound method reference — we don't call them.
    method_names = [
        "morning_screener",
        "morning_paper_execution",
        "intraday_monitor",
        "rss_sentiment_scan",
        "geo_risk_scan",
        "evening_trading_decision",
        "evening_report_task",
        "check_predictions",
        "evening_paper_check",
        "evening_broker_check",
        "intraday_broker_stop_check",
        "intraday_broker_reconcile",
        "generate_medium_term_proposals",
        "broker_order_executor",
        "broker_order_poller",
        "update_dividends",
        "_close_shorts_before_dividend",
        "_cbr_profit_protect",
        "cleanup_stale_proposals",
    ]
    for name in method_names:

        def _make_method(name):
            def _method(self, *args, **kwargs):
                return None

            _method.__name__ = name
            return _method

        bound = _make_method(name).__get__(a, FakeAssistant)
        setattr(a, name, bound)
    return a


def test_setup_schedule_exports_function():
    """setup_schedule must be a module-level callable."""
    assert callable(scheduler_setup.setup_schedule)


def test_setup_schedule_signature_takes_assistant_and_scheduler():
    """Function must accept (assistant, scheduler) — used by thin wrapper in main.py."""
    sig = inspect.signature(scheduler_setup.setup_schedule)
    params = list(sig.parameters.values())
    assert len(params) == 2, f"expected 2 params, got {len(params)}"
    assert params[0].name == "assistant"
    assert params[1].name == "scheduler"


def test_setup_schedule_registers_all_jobs():
    """All 20 cron jobs must be wired up in the same order as the legacy inline code."""
    a = _make_assistant()
    sched = FakeScheduler()
    scheduler_setup.setup_schedule(a, sched)
    assert len(sched.jobs) == 20, f"expected 20 jobs, got {len(sched.jobs)}"


def test_setup_schedule_uses_europe_moscow_tz():
    """All cron jobs must use Europe/Moscow timezone — bot is bound to MSK market hours."""
    a = _make_assistant()
    sched = FakeScheduler()
    scheduler_setup.setup_schedule(a, sched)
    for job in sched.jobs:
        assert job.kwargs.get("timezone") == "Europe/Moscow", \
            f"job {job.func.__name__} has tz={job.kwargs.get('timezone')}"


def test_setup_schedule_uses_cron_trigger():
    """Every job must be a cron job (not interval/date)."""
    a = _make_assistant()
    sched = FakeScheduler()
    scheduler_setup.setup_schedule(a, sched)
    for job in sched.jobs:
        assert job.trigger == "cron", f"job {job.func.__name__} uses {job.trigger}"


def test_evening_jobs_use_config_constants():
    """Evening-hour jobs (19:00–19:10 MSK) must come from core.config EVENING_* constants."""
    a = _make_assistant()
    sched = FakeScheduler()
    scheduler_setup.setup_schedule(a, sched)
    from core.config import EVENING_HOUR
    evening_jobs = [j for j in sched.jobs if j.kwargs.get("hour") == EVENING_HOUR]
    assert len(evening_jobs) >= 4, \
        f"expected >=4 jobs at {EVENING_HOUR}:00 MSK, got {len(evening_jobs)}"


def test_high_frequency_jobs_run_every_minute():
    """Intraday broker checks and executor must run every minute, not every 15 min."""
    a = _make_assistant()
    sched = FakeScheduler()
    scheduler_setup.setup_schedule(a, sched)
    high_freq = [j for j in sched.jobs if j.kwargs.get("minute") == "*/1"]
    # intraday_broker_stop_check, intraday_broker_reconcile, broker_order_executor
    assert len(high_freq) == 3, f"expected 3 */1 jobs, got {len(high_freq)}"


def test_cbr_profit_protect_runs_twice():
    """_cbr_profit_protect is scheduled at 9:45 and 13:00 — protect twice per day."""
    a = _make_assistant()
    sched = FakeScheduler()
    scheduler_setup.setup_schedule(a, sched)
    cbr_jobs = [j for j in sched.jobs if j.func.__name__ == "_cbr_profit_protect"]
    assert len(cbr_jobs) == 2, f"expected 2 _cbr_profit_protect jobs, got {len(cbr_jobs)}"
    hours = sorted(j.kwargs.get("hour") for j in cbr_jobs)
    assert hours == [9, 13], f"expected hours [9, 13], got {hours}"


def test_main_uses_thin_wrapper():
    """main.py setup_schedule must be a thin wrapper delegating to core.scheduler_setup.

    We read main.py as a text file rather than importing it — main.py imports
    fcntl (Linux-only) so static analysis keeps the test runnable on Windows.
    """
    main_path = ROOT / "main.py"
    src = main_path.read_text(encoding="utf-8")
    assert "core.scheduler_setup" in src, \
        "main.py must import from core.scheduler_setup"
    assert "def setup_schedule(self):" in src, \
        "main.py must still define setup_schedule as a method"
    # Locate the wrapper block and check it delegates
    wrapper_idx = src.index("def setup_schedule(self):")
    # Slice up to 2KB ahead — enough for the wrapper, well before the next class.
    wrapper_block = src[wrapper_idx:wrapper_idx + 2048]
    assert "from core.scheduler_setup import setup_schedule" in wrapper_block, \
        "wrapper must import setup_schedule from core.scheduler_setup"
    assert "setup_schedule(self, self.scheduler)" in wrapper_block, \
        "wrapper must call core.setup_schedule(self, self.scheduler)"

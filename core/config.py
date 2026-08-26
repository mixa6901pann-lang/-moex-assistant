"""Application configuration loaded from environment variables."""

import os
from datetime import date
from pathlib import Path

# Paths
BASE_DIR = Path(__file__).resolve().parent.parent

from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env")

DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "moex.db"

# VKontakte
VK_ACCESS_TOKEN = os.getenv("VK_ACCESS_TOKEN", "")
VK_GROUP_ID = os.getenv("VK_GROUP_ID", "")
VK_ENABLED = os.getenv("VK_ENABLED", "false").lower() == "true"

# Telegram notifications (optional)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# LLM (optional, leave empty to skip)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "none").lower()  # anthropic | ollama | gemini | yandex | none
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma3:4b")
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "180.0"))

# Google Gemini (free tier via Google AI Studio)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"

# YandexGPT (works from Russia without VPN)
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "")
YANDEX_MODEL = os.getenv("YANDEX_MODEL", "yandexgpt-lite")

# Provider fallback order used by call_llm_with_fallback.
# Providers without configured keys will be skipped automatically.
LLM_FALLBACK_ORDER = [
    p.strip()
    for p in os.getenv("LLM_FALLBACK_ORDER", "anthropic,gemini,yandex,ollama").split(",")
    if p.strip()
]

# Risk management
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "2.0"))  # max risk % of equity per trade
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
MAX_BROKER_OPEN_POSITIONS = int(os.getenv("MAX_BROKER_OPEN_POSITIONS", "5"))
MAX_POSITION_SIZE_PCT = float(os.getenv("MAX_POSITION_SIZE_PCT", "5.0"))  # max position value % of equity
MIN_POSITION_SIZE_PCT = float(os.getenv("MIN_POSITION_SIZE_PCT", "0.5"))  # ignore positions below this % of equity
STOP_LOSS_ATR_MULT = float(os.getenv("STOP_LOSS_ATR_MULT", "1.5"))
# Trailing-stop ATR multiplier for medium-term paper/broker positions.
# Initial stop uses STOP_LOSS_ATR_MULT; this governs how stop trails in profit.
TRAILING_STOP_ATR_MULT = float(os.getenv("TRAILING_STOP_ATR_MULT", "1.8"))
# Risk/reward ratio used by calculate_position() to set the take-profit distance.
TARGET_RR = float(os.getenv("TARGET_RR", "1.5"))
# Recalculate stop/take (and re-attach SL/TP at the broker) when the real
# fill price differs from the signal price by more than this percent.
# 0 disables recalculation entirely (use stored stop/take as-is).
STOP_RECALC_THRESHOLD_PCT = float(os.getenv("STOP_RECALC_THRESHOLD_PCT", "0.5"))
MIN_AVG_VOLUME = int(os.getenv("MIN_AVG_VOLUME", "100000"))  # average daily volume threshold for liquidity filter

# Intraday signal filter
INTRADAY_MIN_FACTORS = int(os.getenv("INTRADAY_MIN_FACTORS", "2"))        # minimum confirming factors for a directional signal
INTRADAY_MIN_VOLUME_RATIO = float(os.getenv("INTRADAY_MIN_VOLUME_RATIO", "0.5"))  # min vol_ratio for any directional signal
INTRADAY_COOLDOWN_MINUTES = int(os.getenv("INTRADAY_COOLDOWN_MINUTES", "30"))       # cooldown between repeated signals for same ticker

# Health / web UI endpoint
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))
# Bind address: 127.0.0.1 by default so the port is not exposed to the internet.
# Use 0.0.0.0 only on a trusted local network.
HOST = os.getenv("HOST", "127.0.0.1")

# Web UI (disable by default until the SPA is production-ready)
WEB_UI_ENABLED = os.getenv("WEB_UI_ENABLED", "false").lower() == "true"

# API key for protecting write / data endpoints when Web UI is enabled.
# Set a strong random value in .env; if empty, write endpoints are blocked.
API_KEY = os.getenv("API_KEY", "")

# CodeAct — execute Python code blocks from LLM responses.
# Disabled by default because running LLM-generated code on a server is dangerous.
# Enable only for local experiments after reviewing the sandbox.
CODEACT_ENABLED = os.getenv("CODEACT_ENABLED", "false").lower() == "true"

# Default watchlist for screener and monitoring
WATCHLIST = [
    "SBER", "GAZP", "LKOH", "GMKN", "NVTK", "ROSN", "TATN",
    "PLZL", "MGNT", "MTSS", "VTBR", "ALRS", "CHMF",
    "NLMK", "OZON", "SNGS", "MOEX",
    "IRAO",
    "VKCO", "SOFL", "ASTR", "RTKM", "RTKMP", "AFLT",
]

# Sector mapping for watchlist tickers. Used by GeoRisk and correlation guards.
# Keys are Russian sector names that GeoRisk affected_sectors should reference.
TICKER_SECTORS: dict[str, str] = {
    # Banking
    "SBER": "банки",
    "VTBR": "банки",
    # Oil & gas
    "GAZP": "нефть",
    "LKOH": "нефть",
    "ROSN": "нефть",
    "TATN": "нефть",
    "SNGS": "нефть",
    "NVTK": "нефть",
    # Metals & mining
    "GMKN": "металлы",
    "NLMK": "металлы",
    "CHMF": "металлы",
    "POLY": "металлы",
    "RUAL": "металлы",
    "ALRS": "металлы",
    # Gold
    "PLZL": "золото",
    # Tech / IT
    "YNDX": "it",
    "OZON": "it",
    "VKCO": "it",
    "SOFL": "it",
    "ASTR": "it",
    # Telecom
    "MTSS": "телеком",
    "RTKM": "телеком",
    "RTKMP": "телеком",
    # Retail
    "MGNT": "ретейл",
    "FIVE": "ретейл",
    # Exchange
    "MOEX": "биржа",
    # Utilities / energy
    "IRAO": "энергетика",
    # Airlines
    "AFLT": "авиа",
}

# Tickers that typically pay dividends (for dividend calendar updates)
DIVIDEND_TICKERS = [
    "SBER", "GAZP", "LKOH", "GMKN", "NVTK", "ROSN", "TATN",
    "MGNT", "PLZL", "CHMF", "NLMK", "IRAO",
]

# MOEX API
MOEX_ISS_BASE = "https://iss.moex.com/iss"
MOEX_REQUEST_DELAY = 0.3  # seconds between requests to be polite

# Broker commission settings used by fee estimator.
# DEFAULT_BROKER_COMMISSION_PCT is the round-trip commission as a percent of
# turnover. For T-Bank Trader tariff 0.05% per side means 0.10% round-trip.
DEFAULT_BROKER_COMMISSION_PCT = float(os.getenv("DEFAULT_BROKER_COMMISSION_PCT", "0.1"))

# Minimum net profit % after fees required for a signal to be considered
# worth executing. With 10k RUB capital and 0.1% round-trip commission,
# a 0.5% gross move leaves ~0.4% net.
MIN_PROFIT_PCT_AFTER_FEES = float(os.getenv("MIN_PROFIT_PCT_AFTER_FEES", "0.5"))

# Overnight carry fee (per day) for margin/leveraged positions. T-Bank "Trader"
# tariff charges a fixed daily fee depending on position notional. Used by
# estimate_trade_costs when hold_days > 0. Set to 0 to disable.
OVERNIGHT_CARRY_FEE_PCT = float(os.getenv("OVERNIGHT_CARRY_FEE_PCT", "0.0"))

# Circuit breaker: when true, all new real-order requests are rejected.
# Can be toggled at runtime via the Web UI / API to stop the robot quickly.
CIRCUIT_BREAKER_ENABLED = os.getenv("CIRCUIT_BREAKER_ENABLED", "false").lower() == "true"

# Slippage settings for paper trading to approximate real fills.
# Base slippage for liquid stocks; illiquid stocks get extra slippage from spread.
DEFAULT_SLIPPAGE_PCT = float(os.getenv("DEFAULT_SLIPPAGE_PCT", "0.05"))
ILLIQUID_SLIPPAGE_PCT = float(os.getenv("ILLIQUID_SLIPPAGE_PCT", "0.15"))
ILLIQUID_SPREAD_PCT = float(os.getenv("ILLIQUID_SPREAD_PCT", "0.10"))

# Tinkoff Invest API broker adapter
# Read .env.tinkoff as well so token can live in a separate git-ignored file.
load_dotenv(BASE_DIR / ".env.tinkoff", override=True)
TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN", "")
TINKOFF_ACCOUNT_ID = os.getenv("TINKOFF_ACCOUNT_ID", "")

# Fallback: read token from a restricted file outside version control.
# The .secrets/ folder is git-ignored and has 700 permissions.
if not TINKOFF_TOKEN:
    _token_path = BASE_DIR / ".secrets" / "tinkoff_token.txt"
    try:
        TINKOFF_TOKEN = _token_path.read_text(encoding="utf-8").strip()
    except Exception:
        TINKOFF_TOKEN = ""

# Use Tinkoff sandbox for real-order testing. Set to false only when you are
# ready to trade real money. Sandbox still requires a Tinkoff token.
TINKOFF_SANDBOX = os.getenv("TINKOFF_SANDBOX", "true").lower() == "true"

# Retry transient Tinkoff sandbox errors (HTTP 500/503). Live endpoint rarely
# needs this, but sandbox occasionally returns internal errors.
TINKOFF_MAX_RETRIES = int(os.getenv("TINKOFF_MAX_RETRIES", "3"))

# Trading mode: paper = virtual trades only; semi-auto = robot proposes, user
# confirms before any real order; live = robot executes automatically.
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"
SEMI_AUTO_TRADING = os.getenv("SEMI_AUTO_TRADING", "true").lower() == "true"

# Daily loss guard: when realized P&L drops below this negative threshold (% of
# equity), new live/semi-auto orders are blocked until manually reset.
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "5.0"))

# Virtual paper account size. Changing this does not resize existing positions
# automatically — reset paper_positions if you want a clean test.
PAPER_STARTING_CAPITAL = float(os.getenv("PAPER_STARTING_CAPITAL", "50000"))

# Sandbox starting capital for Tinkoff paper broker testing.
SANDBOX_STARTING_CAPITAL = float(os.getenv("SANDBOX_STARTING_CAPITAL", "50000"))

# Master switch for automatic position opening. Can be toggled at runtime via
# the Web UI / API. When false, the robot only creates proposals and waits for
# user confirmation (semi-automatic mode).
AUTO_TRADING_ENABLED_DEFAULT = os.getenv("AUTO_TRADING_ENABLED", "true").lower() == "true"
AUTO_TRADING_ENABLED = AUTO_TRADING_ENABLED_DEFAULT  # mutable runtime cache

# Auto-trade: когда True, broker_order_executor исполняет pending-proposal'ы
# сразу, без ручного подтверждения (только для sandbox/теста). Чтобы не
# путать с AUTO_TRADING_ENABLED — это про режим доверия роботу, а не про
# допустимость live-ордеров в принципе. По умолчанию False для безопасности.
AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"
AUTO_TRADE_MIN_CONFIDENCE = int(os.getenv("AUTO_TRADE_MIN_CONFIDENCE", "70"))
# 26.08.2026: separate MANUAL_CONFIRM_MIN_CONFIDENCE=55 from auto-trade=65.
# Применяется к proposal'ам со статусом confirmed (пользователь нажал
# «Исполнить» в UI), когда AUTO_TRADE выключен. AUTO_TRADE_MIN_CONFIDENCE
# остаётся для auto_trade-режима (без подтверждения).
MANUAL_CONFIRM_MIN_CONFIDENCE = int(os.getenv("MANUAL_CONFIRM_MIN_CONFIDENCE", "55"))


def should_auto_trade(confidence: int | None) -> bool:
    """Return True when a proposal qualifies for the auto-trade (no-confirm) mode.

    Used by proposal creators to promote semi_auto / paper proposals to a
    dedicated "auto_trade" mode that broker_order_executor picks up without
    waiting for manual confirmation. Disabled when AUTO_TRADE is off or
    when confidence is below the configured threshold.
    """
    if not AUTO_TRADE:
        return False
    if confidence is None:
        return False
    return int(confidence) >= AUTO_TRADE_MIN_CONFIDENCE

# Central Bank of Russia key-rate meeting dates.
# Comma-separated ISO dates, e.g. "2026-07-25,2026-09-12,2026-10-24,2026-12-12".
# On the meeting day and one trading day before, the robot blocks new positions
# and tightens profit protection for open positions.
CBR_MEETING_DATES_RAW = os.getenv("CBR_MEETING_DATES", "")
CBR_MEETING_DATES: set[date] = set()
for _d in CBR_MEETING_DATES_RAW.split(","):
    _d = _d.strip()
    if not _d:
        continue
    try:
        CBR_MEETING_DATES.add(date.fromisoformat(_d))
    except ValueError:
        pass

CBR_SOFT_MODE_ENABLED = os.getenv("CBR_SOFT_MODE_ENABLED", "true").lower() == "true"


# GeoRisk thresholds used when turning the risk score into directional signals.
GEORISK_EXIT_THRESHOLD = int(os.getenv("GEORISK_EXIT_THRESHOLD", "7"))   # score >= N -> bearish, exit affected longs
GEORISK_BULLISH_THRESHOLD = int(os.getenv("GEORISK_BULLISH_THRESHOLD", "2"))  # score <= N -> bullish tailwind for longs

# Evening cron schedule (MSK). Used by main.py setup_scheduler() to register
# report, prediction check, paper close, broker close, and medium-term proposals.
EVENING_HOUR = int(os.getenv("EVENING_HOUR", "19"))
EVENING_PREDICTION_MINUTE = int(os.getenv("EVENING_PREDICTION_MINUTE", "5"))
EVENING_PAPER_MINUTE = int(os.getenv("EVENING_PAPER_MINUTE", "6"))
EVENING_BROKER_MINUTE = int(os.getenv("EVENING_BROKER_MINUTE", "7"))
EVENING_MEDIUM_TERM_MINUTE = int(os.getenv("EVENING_MEDIUM_TERM_MINUTE", "10"))

# Suspicious price jump threshold (%). When 1m close diverges from previous by
# more than this, fall back to /last_price to avoid stale MOEX candle issues.
PRICE_JUMP_THRESHOLD_PCT = float(os.getenv("PRICE_JUMP_THRESHOLD_PCT", "5.0"))

# Tolerance for matching close_op_price to stop_px / take_px (RUB).
# Tinkoff fill prices can drift by a few kopeks; this lets us attribute the
# close to "broker_stop" / "broker_take" instead of "broker_manual".
STOP_TAKE_MATCH_TOLERANCE_RUB = float(os.getenv("STOP_TAKE_MATCH_TOLERANCE_RUB", "0.05"))

# Intraday freshness gates — protect against stale MOEX ISS 1m candles and
# stale prices from the broker. Without these, intraday_monitor can produce
# proposals whose entry_px is hours/days old or wildly off the real price
# (observed 2026-08-20: AFLT entry 32.33 vs broker 34.20 at 07:00; NVTK entry
# 916.95 vs broker 940.30 at 13:45).
INTRADAY_MAX_CANDLE_AGE_MIN = int(os.getenv("INTRADAY_MAX_CANDLE_AGE_MIN", "5"))
INTRADAY_PRICE_DRIFT_PCT = float(os.getenv("INTRADAY_PRICE_DRIFT_PCT", "1.0"))

# When True, check_predictions logs evaluations but does not write results
# to the predictions table. Lets us stop accumulating low-signal daily
# accuracy rows without losing the ability to flip back on (e.g. for the
# sentiment correlation study).
PREDICTIONS_DRY_RUN = os.getenv("PREDICTIONS_DRY_RUN", "true").lower() in ("true", "1", "yes")

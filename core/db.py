"""SQLite storage for candles, trade journal, and screening results.

Uses aiosqlite for async I/O. Schema is simple — one table per concern.
"""

from __future__ import annotations

import json
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any

import asyncio

import aiosqlite

from core.config import DB_PATH, DATA_DIR, PAPER_STARTING_CAPITAL, CBR_MEETING_DATES

_db_conn: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()


def _to_json(value: Any) -> Any:
    """Recursively convert numpy types to native Python for JSON serialization."""
    import numpy as np
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {k: _to_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_json(v) for v in value]
    return value


SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    ticker     TEXT NOT NULL,
    board      TEXT NOT NULL DEFAULT 'TQBR',
    interval   TEXT NOT NULL,
    ts         TEXT NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    volume     INTEGER,
    value      REAL,
    PRIMARY KEY (ticker, board, interval, ts)
);

CREATE TABLE IF NOT EXISTS journal (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    ticker     TEXT NOT NULL,
    side       TEXT NOT NULL,          -- 'long' or 'short'
    entry_px   REAL NOT NULL,
    stop_px    REAL,
    target_px  REAL,
    qty        INTEGER NOT NULL,
    reason     TEXT,                   -- why we entered
    exit_px    REAL,
    exit_ts    TEXT,
    pnl        REAL,
    notes      TEXT
);

CREATE TABLE IF NOT EXISTS screener_results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    ticker     TEXT NOT NULL,
    score      REAL NOT NULL,
    signals    TEXT,                   -- JSON list of signal names
    details    TEXT                    -- JSON with indicator values
);

CREATE TABLE IF NOT EXISTS dividends (
    ticker     TEXT NOT NULL,
    ex_date    TEXT NOT NULL,
    dividend   REAL,
    currency   TEXT,
    PRIMARY KEY (ticker, ex_date)
);

CREATE TABLE IF NOT EXISTS sentiment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    ticker      TEXT NOT NULL,
    source      TEXT,                   -- rss source name or 'manual'
    headline    TEXT NOT NULL,
    sentiment   TEXT NOT NULL,          -- bullish | bearish | neutral
    confidence  INTEGER NOT NULL DEFAULT 0,
    summary     TEXT,
    topics      TEXT,                   -- JSON list
    risk_flags  TEXT,                   -- JSON list
    news_hash   TEXT,                   -- hash of the news item for dedup
    published_at TEXT,                  -- original publication time (ISO)
    age_minutes INTEGER,                -- age of the news when analyzed
    weight      REAL DEFAULT 1.0        -- age-based decay weight
);

CREATE TABLE IF NOT EXISTS rss_seen_hashes (
    hash       TEXT PRIMARY KEY,
    ticker     TEXT NOT NULL,
    source     TEXT,
    headline   TEXT,
    seen_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_candles_ticker ON candles(ticker, interval);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal(ts);
CREATE INDEX IF NOT EXISTS idx_screener_run ON screener_results(run_ts);
CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    ticker          TEXT NOT NULL,
    predicted_direction TEXT NOT NULL,
    predicted_price REAL NOT NULL,
    predicted_strength TEXT NOT NULL,
    higher_tf_trend TEXT,
    signals_used    TEXT,
    reasoning       TEXT,              -- LLM/agent reasoning
    source          TEXT DEFAULT 'analysis',  -- analysis | trading_agent | screener
    environment     TEXT DEFAULT 'paper',       -- paper | sandbox | live
    result_1d       TEXT DEFAULT 'pending',
    result_3d       TEXT DEFAULT 'pending',
    result_7d       TEXT DEFAULT 'pending',
    actual_price_1d REAL,
    actual_price_3d REAL,
    actual_price_7d REAL
);
CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON predictions(ticker, ts);
CREATE INDEX IF NOT EXISTS idx_predictions_pending ON predictions(ts);

CREATE TABLE IF NOT EXISTS paper_positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    open_ts     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    close_ts    TEXT,
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL,
    entry_px    REAL NOT NULL,
    exit_px     REAL,
    stop_px     REAL,
    take_px     REAL,
    qty         INTEGER DEFAULT 1,
    pnl_pct     REAL,
    pnl_rub     REAL,
    status      TEXT DEFAULT 'open',
    trigger_signal TEXT,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_paper_open ON paper_positions(ticker, status);

CREATE TABLE IF NOT EXISTS georisk_scores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    score       INTEGER NOT NULL DEFAULT 0,
    severity    TEXT NOT NULL DEFAULT 'low',
    summary     TEXT,
    affected_sectors TEXT,  -- JSON list
    trigger_keywords TEXT,  -- JSON list
    overall_direction INTEGER DEFAULT 0,
    news_items  TEXT        -- JSON list
);
CREATE INDEX IF NOT EXISTS idx_georisk_ts ON georisk_scores(ts);

CREATE TABLE IF NOT EXISTS macro_indicators (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    usd_rub     REAL,
    eur_rub     REAL,
    brent       REAL,
    cbr_rate    REAL
);
CREATE INDEX IF NOT EXISTS idx_macro_ts ON macro_indicators(ts);

CREATE TABLE IF NOT EXISTS intraday_backtest (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    ticker      TEXT NOT NULL,
    signal_ts   TEXT NOT NULL,
    signal      TEXT NOT NULL,
    direction   TEXT NOT NULL,
    confidence  INTEGER NOT NULL,
    entry_px    REAL,
    stop_px     REAL,
    take_px     REAL,
    open_px     REAL,
    close_px    REAL,
    horizon_candles INTEGER NOT NULL,
    future_px   REAL,
    return_pct  REAL,
    result      TEXT NOT NULL,
    signals_used TEXT,
    reason      TEXT,
    llm_used    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_intraday_backtest_ticker ON intraday_backtest(ticker, run_ts);
CREATE INDEX IF NOT EXISTS idx_intraday_backtest_signal ON intraday_backtest(signal, run_ts);

CREATE TABLE IF NOT EXISTS robot_proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL,          -- long | short
    source      TEXT NOT NULL,            -- intraday | evening
    signal      TEXT,
    entry_px    REAL,
    qty         INTEGER,
    stop_px     REAL,
    take_px     REAL,
    confidence  INTEGER,
    reason      TEXT,
    fee_rub     REAL,
    net_profit_pct REAL,
    horizon     TEXT,                    -- intraday | 1d | 3d | 7d
    proposal_mode TEXT DEFAULT 'semi_auto',  -- semi_auto | paper | auto_trade | live
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed | rejected | executed | superseded
    decided_at  TEXT,
    decided_by  TEXT,
    reject_reason TEXT,                 -- why user rejected the proposal
    exec_entry_px REAL,                 -- actual fill price (usually next-day open)
    exec_ts     TEXT,                   -- when the morning job executed the proposal
    initial_atr REAL,                   -- ATR at signal time, used for trailing-stop
    atr_mult    REAL                    -- configured stop-loss / trailing ATR multiplier
);
CREATE INDEX IF NOT EXISTS idx_robot_proposals_status ON robot_proposals(status, ts);
CREATE INDEX IF NOT EXISTS idx_robot_proposals_ticker ON robot_proposals(ticker, status);

CREATE TABLE IF NOT EXISTS broker_orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    proposal_id INTEGER,
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL,            -- long | short
    broker      TEXT NOT NULL DEFAULT 'tinkoff',
    account_id  TEXT,
    order_id    TEXT,
    stop_order_ids TEXT,                 -- JSON list of stop-order IDs
    lots        INTEGER,
    qty         INTEGER,
    entry_px    REAL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | filled | partial | rejected | cancelled
    broker_message TEXT,
    FOREIGN KEY (proposal_id) REFERENCES robot_proposals(id)
);
CREATE INDEX IF NOT EXISTS idx_broker_orders_status ON broker_orders(status, ts);
CREATE INDEX IF NOT EXISTS idx_broker_orders_ticker ON broker_orders(ticker, status);

CREATE TABLE IF NOT EXISTS broker_positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    ticker      TEXT NOT NULL,
    broker      TEXT NOT NULL DEFAULT 'tinkoff',
    account_id  TEXT,
    side        TEXT NOT NULL,            -- long | short
    qty         INTEGER NOT NULL,         -- signed shares
    lots        INTEGER NOT NULL,         -- signed lots
    avg_entry_px REAL,
    stop_px     REAL,
    take_px     REAL,
    initial_atr REAL,
    atr_mult    REAL,
    exit_px     REAL,
    status      TEXT NOT NULL DEFAULT 'open',
    close_reason TEXT,
    UNIQUE(ticker, broker, account_id)
);
CREATE INDEX IF NOT EXISTS idx_broker_positions_open ON broker_positions(ticker, status);

CREATE TABLE IF NOT EXISTS user_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


async def get_db() -> aiosqlite.Connection:
    """Return shared DB connection, creating it lazily if needed."""
    global _db_conn
    if _db_conn is not None:
        return _db_conn
    async with _db_lock:
        if _db_conn is not None:
            return _db_conn
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(str(DB_PATH), timeout=60.0)
        db.row_factory = aiosqlite.Row
        # WAL journal mode allows concurrent readers + one writer without
        # blocking the bot during evening_broker_check + reconcile + executor.
        # Safe to set on every connection; SQLite returns the existing mode.
        # executescript is required because execute() silently swallows the
        # result row that PRAGMA journal_mode returns.
        await db.executescript(
            "PRAGMA journal_mode=WAL;\n"
            "PRAGMA synchronous=NORMAL;\n"
            "PRAGMA busy_timeout=60000;\n"
        )
        await db.executescript(SCHEMA)
        await _migrate_db(db)
        _db_conn = db
        return _db_conn


def is_db_connected() -> bool:
    """Return True if a shared DB connection is currently open."""
    return _db_conn is not None


async def _migrate_db(db: aiosqlite.Connection) -> None:
    """Add columns that appeared in newer schema versions without dropping data."""
    await _ensure_column(db, "predictions", "reasoning", "TEXT")
    await _ensure_column(db, "predictions", "source", "TEXT DEFAULT 'analysis'")
    await _ensure_column(db, "predictions", "llm_provider", "TEXT")
    await _ensure_column(db, "sentiment", "news_hash", "TEXT")
    await _ensure_column(db, "sentiment", "published_at", "TEXT")
    await _ensure_column(db, "sentiment", "age_minutes", "INTEGER")
    await _ensure_column(db, "sentiment", "weight", "REAL DEFAULT 1.0")

    # Paper positions stop/take levels (added in newer schema)
    await _ensure_column(db, "paper_positions", "stop_px", "REAL")
    await _ensure_column(db, "paper_positions", "take_px", "REAL")

    # Trailing-stop ATR state for paper positions
    await _ensure_column(db, "paper_positions", "initial_atr", "REAL")
    await _ensure_column(db, "paper_positions", "atr_mult", "REAL")

    # Broker positions stop/take and trailing-stop ATR state
    await _ensure_column(db, "broker_positions", "stop_px", "REAL")
    await _ensure_column(db, "broker_positions", "take_px", "REAL")
    await _ensure_column(db, "broker_positions", "initial_atr", "REAL")
    await _ensure_column(db, "broker_positions", "atr_mult", "REAL")

    # Geo-risk extra fields (added to support directional sector filtering)
    await _ensure_column(db, "georisk_scores", "overall_direction", "INTEGER DEFAULT 0")
    await _ensure_column(db, "georisk_scores", "news_items", "TEXT")

    # Indexes for new columns (created here because columns may not exist yet
    # when the main SCHEMA script runs on an older database).
    await db.execute("CREATE INDEX IF NOT EXISTS idx_sentiment_ticker_ts ON sentiment(ticker, ts)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_sentiment_hash ON sentiment(news_hash)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_rss_seen_hash ON rss_seen_hashes(hash)")

    # Robot proposal horizon/mode columns for existing databases
    await _ensure_column(db, "robot_proposals", "horizon", "TEXT")
    await _ensure_column(db, "robot_proposals", "proposal_mode", "TEXT DEFAULT 'semi_auto'")
    await _ensure_column(db, "robot_proposals", "initial_atr", "REAL")
    await _ensure_column(db, "robot_proposals", "atr_mult", "REAL")

    # Paper-proposal execution tracking
    await _ensure_column(db, "robot_proposals", "exec_entry_px", "REAL")
    await _ensure_column(db, "robot_proposals", "exec_ts", "TEXT")

    # Broker orders table may already exist from older migrations
    await db.execute("""
        CREATE TABLE IF NOT EXISTS broker_orders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            proposal_id INTEGER,
            ticker      TEXT NOT NULL,
            side        TEXT NOT NULL,
            broker      TEXT NOT NULL DEFAULT 'tinkoff',
            account_id  TEXT,
            order_id    TEXT,
            stop_order_ids TEXT,
            lots        INTEGER,
            qty         INTEGER,
            entry_px    REAL,
            status      TEXT NOT NULL DEFAULT 'pending',
            broker_message TEXT,
            FOREIGN KEY (proposal_id) REFERENCES robot_proposals(id)
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_broker_orders_status ON broker_orders(status, ts)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_broker_orders_ticker ON broker_orders(ticker, status)")

    # Broker positions table for tracking real-account exposure
    await db.execute("""
        CREATE TABLE IF NOT EXISTS broker_positions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            ticker      TEXT NOT NULL,
            broker      TEXT NOT NULL DEFAULT 'tinkoff',
            account_id  TEXT,
            side        TEXT NOT NULL,
            qty         INTEGER NOT NULL,
            lots        INTEGER NOT NULL,
            avg_entry_px REAL,
            status      TEXT NOT NULL DEFAULT 'open',
            close_reason TEXT,
            UNIQUE(ticker, broker, account_id)
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_broker_positions_open ON broker_positions(ticker, status)")

    # Robot proposals: track reject reason on existing databases
    await _ensure_column(db, "robot_proposals", "reject_reason", "TEXT")

    # Broker positions: store the actual exit price when the broker closed
    # the position (stop/take on the exchange, manual close, etc.). Used by
    # intraday_broker_reconcile and the API discrepancy payload.
    await _ensure_column(db, "broker_positions", "exit_px", "REAL")

    # Broker orders: track exchange/sandbox environment
    await _ensure_column(db, "broker_orders", "environment", "TEXT DEFAULT 'unknown'")

    # Predictions: track whether they were made in paper, sandbox or live mode
    await _ensure_column(db, "predictions", "environment", "TEXT DEFAULT 'paper'")

    # Intraday backtest table/indexes for existing databases
    await db.execute("""
        CREATE TABLE IF NOT EXISTS intraday_backtest (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_ts      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            ticker      TEXT NOT NULL,
            signal_ts   TEXT NOT NULL,
            signal      TEXT NOT NULL,
            direction   TEXT NOT NULL,
            confidence  INTEGER NOT NULL,
            entry_px    REAL,
            stop_px     REAL,
            take_px     REAL,
            open_px     REAL,
            close_px    REAL,
            horizon_candles INTEGER NOT NULL,
            future_px   REAL,
            return_pct  REAL,
            result      TEXT NOT NULL,
            signals_used TEXT,
            reason      TEXT,
            llm_used    INTEGER DEFAULT 0
        )
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS idx_intraday_backtest_ticker ON intraday_backtest(ticker, run_ts)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_intraday_backtest_signal ON intraday_backtest(signal, run_ts)")

    # broker_positions: relax UNIQUE constraint so a ticker can have multiple
    # closed records (history) while still enforcing one open per (ticker, side).
    # Old constraint UNIQUE(ticker, broker, account_id) blocked any re-entry
    # after a close, which is exactly the wrong behavior for a trading bot.
    await _migrate_broker_positions_unique(db)

    await db.commit()


async def _migrate_broker_positions_unique(db: aiosqlite.Connection) -> None:
    """Recreate broker_positions with UNIQUE(ticker, broker, account_id, side, status).

    Safe to call repeatedly: if the constraint is already up to date, it's a no-op.
    Collapses any existing duplicate rows by keeping the most recent one per
    (ticker, broker, account_id, side, status) tuple.
    """
    # Check whether the new UNIQUE constraint is already in place.
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='broker_positions'"
    )
    row = await cursor.fetchone()
    create_sql = row[0] if row else ""
    if "UNIQUE(ticker, broker, account_id, side, status)" in create_sql.replace(" ", ""):
        return  # already migrated

    # 1) Build the new table with the right UNIQUE.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS _new_broker_positions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            ticker      TEXT NOT NULL,
            broker      TEXT NOT NULL DEFAULT 'tinkoff',
            account_id  TEXT,
            side        TEXT NOT NULL,
            qty         INTEGER NOT NULL,
            lots        INTEGER NOT NULL,
            avg_entry_px REAL,
            stop_px     REAL,
            take_px     REAL,
            initial_atr REAL,
            atr_mult    REAL,
            status      TEXT NOT NULL DEFAULT 'open',
            close_reason TEXT,
            exit_px     REAL,
            UNIQUE(ticker, broker, account_id, side, status)
        )
    """)
    # 2) Copy data, collapsing duplicate keys by keeping the highest id (most recent).
    await db.execute("""
        INSERT OR IGNORE INTO _new_broker_positions
            (id, ts, ticker, broker, account_id, side, qty, lots,
             avg_entry_px, stop_px, take_px, initial_atr, atr_mult,
             status, close_reason, exit_px)
        SELECT id, ts, ticker, broker, account_id, side, qty, lots,
               avg_entry_px, stop_px, take_px, initial_atr, atr_mult,
               status, close_reason, exit_px
        FROM broker_positions
        WHERE id IN (
            SELECT MAX(id) FROM broker_positions
            GROUP BY ticker, broker, account_id, side, status
        )
    """)
    # 3) Drop old, rename new, restore index.
    await db.execute("DROP TABLE broker_positions")
    await db.execute("ALTER TABLE _new_broker_positions RENAME TO broker_positions")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_broker_positions_open ON broker_positions(ticker, status)")


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    existing = {r["name"] for r in rows}
    if column not in existing:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        await db.commit()


async def close_db() -> None:
    """Close the shared DB connection (for graceful shutdown)."""
    global _db_conn
    if _db_conn is not None:
        await _db_conn.close()
        _db_conn = None


# ── Candles ───────────────────────────────────────────────────


async def save_candles(candles: list[dict], ticker: str, board: str, interval: str) -> int:
    """Upsert candle rows. Returns number of rows inserted."""
    if not candles:
        return 0
    db = await get_db()
    rows = []
    for c in candles:
        ts = c.get("begin") or c.get("end") or c.get("open")
        rows.append((ticker, board, interval, ts, c.get("open"), c.get("high"),
                     c.get("low"), c.get("close"), c.get("volume"), c.get("value")))
    await db.executemany(
        """INSERT OR REPLACE INTO candles
           (ticker, board, interval, ts, open, high, low, close, volume, value)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    await db.commit()
    return len(rows)


async def load_candles(
    ticker: str, interval: str = "1d", board: str = "TQBR", limit: int = 500
) -> list[dict]:
    """Load candles from DB, newest first."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT ts, open, high, low, close, volume, value
           FROM candles
           WHERE ticker=? AND board=? AND interval=?
           ORDER BY ts DESC LIMIT ?""",
        (ticker, board, interval, limit),
    )
    rows = await cursor.fetchall()
    return [
        {"ts": r[0], "open": r[1], "high": r[2], "low": r[3],
         "close": r[4], "volume": r[5], "value": r[6]}
        for r in reversed(rows)  # oldest first for analysis
    ]


# ── Journal ───────────────────────────────────────────────────


async def add_trade(
    ticker: str,
    side: str,
    entry_px: float,
    qty: int,
    stop_px: float | None = None,
    target_px: float | None = None,
    reason: str | None = None,
) -> int:
    """Record a new trade entry. Returns row id."""
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO journal (ticker, side, entry_px, stop_px, target_px, qty, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ticker, side, entry_px, stop_px, target_px, qty, reason),
    )
    await db.commit()
    return cursor.lastrowid


async def close_trade(trade_id: int, exit_px: float, notes: str | None = None) -> None:
    """Mark a trade as closed with exit price and PnL."""
    db = await get_db()
    # Fetch entry to compute PnL
    cursor = await db.execute("SELECT side, entry_px, qty FROM journal WHERE id=?", (trade_id,))
    row = await cursor.fetchone()
    if not row:
        return
    side, entry_px, qty = row[0], row[1], row[2]
    pnl = (exit_px - entry_px) * qty if side == "long" else (entry_px - exit_px) * qty
    await db.execute(
        """UPDATE journal SET exit_px=?, exit_ts=datetime('now','localtime'), pnl=?, notes=?
           WHERE id=?""",
        (exit_px, pnl, notes, trade_id),
    )
    await db.commit()


async def open_positions() -> list[dict]:
    """Get all currently open (unclosed) trades."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT id, ts, ticker, side, entry_px, stop_px, target_px, qty, reason
           FROM journal WHERE exit_px IS NULL ORDER BY ts DESC"""
    )
    rows = await cursor.fetchall()
    return [
        {"id": r[0], "ts": r[1], "ticker": r[2], "side": r[3],
         "entry_px": r[4], "stop_px": r[5], "target_px": r[6],
         "qty": r[7], "reason": r[8]}
        for r in rows
    ]


# ── Screener results ──────────────────────────────────────────


async def save_screener(results: list[dict]) -> None:
    """Save a batch of screener results."""
    if not results:
        return
    db = await get_db()
    rows = [
        (r["ticker"], r["score"], json.dumps(_to_json(r.get("signals", []))), json.dumps(_to_json(r.get("details", {}))))
        for r in results
    ]
    await db.executemany(
        """INSERT INTO screener_results (ticker, score, signals, details)
           VALUES (?, ?, ?, ?)""",
        rows,
    )
    await db.commit()


async def latest_screener(limit: int = 20) -> list[dict]:
    """Get most recent screener run results."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT run_ts, ticker, score, signals, details
           FROM screener_results
           WHERE run_ts = (SELECT MAX(run_ts) FROM screener_results)
           ORDER BY score DESC LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        {"run_ts": r[0], "ticker": r[1], "score": r[2],
         "signals": json.loads(r[3]) if r[3] else [],
         "details": json.loads(r[4]) if r[4] else {}}
        for r in rows
    ]


async def latest_screener_ts() -> str | None:
    """Return the run_ts of the most recent screener run, or None."""
    db = await get_db()
    cursor = await db.execute("SELECT MAX(run_ts) FROM screener_results")
    row = await cursor.fetchone()
    return row[0] if row and row[0] else None


# ── Dividends ────────────────────────────────────────────────


async def save_dividends(ticker: str, divs: list[dict]) -> None:
    if not divs:
        return
    db = await get_db()
    rows = [
        (d.get("ticker", ticker), d.get("registry_close_date", ""), d.get("dividend"), d.get("currency", "RUB"))
        for d in divs
        if d.get("registry_close_date")
    ]
    if not rows:
        return
    await db.executemany(
        "INSERT OR REPLACE INTO dividends (ticker, ex_date, dividend, currency) VALUES (?, ?, ?, ?)",
        rows,
    )
    await db.commit()


async def closed_trades_today() -> list[dict]:
    """Get trades closed today."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT id, ticker, side, entry_px, exit_px, pnl
           FROM journal
           WHERE date(ts) = date('now') AND exit_px IS NOT NULL"""
    )
    rows = await cursor.fetchall()
    return [
        {"id": r[0], "ticker": r[1], "side": r[2],
         "entry_px": r[3], "exit_px": r[4], "pnl": r[5]}
        for r in rows
    ]


async def upcoming_dividends(days: int = 14) -> list[dict]:
    """Dividends with ex_date in the next N days."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT ticker, ex_date, dividend, currency FROM dividends
           WHERE date(ex_date) BETWEEN date('now') AND date('now', ?)
           ORDER BY ex_date""",
        (f"+{days} days",),
    )
    rows = await cursor.fetchall()
    return [{"ticker": r[0], "ex_date": r[1], "dividend": r[2], "currency": r[3]} for r in rows]


async def dividend_close_cutoff_date(ticker: str) -> date | None:
    """Return the last safe trading day for a short before dividend cutoff.

    registry_close_date is the date you must be in the register to receive the
    dividend. We close shorts one trading day before that date.
    """
    db = await get_db()
    cursor = await db.execute(
        """SELECT ex_date FROM dividends
           WHERE ticker = ? AND date(ex_date) >= date('now')
           ORDER BY ex_date ASC LIMIT 1""",
        (ticker.upper(),),
    )
    row = await cursor.fetchone()
    if not row or not row[0]:
        return None
    try:
        registry_close = date.fromisoformat(row[0])
    except ValueError:
        return None
    # Move back one trading day (skip weekends).
    cutoff = registry_close - timedelta(days=1)
    while cutoff.weekday() >= 5:
        cutoff -= timedelta(days=1)
    return cutoff


async def is_near_dividend_cutoff(ticker: str, look_ahead_days: int = 3) -> tuple[bool, date | None]:
    """Return (True, cutoff_date) if a short should not be opened/held."""
    cutoff = await dividend_close_cutoff_date(ticker)
    if cutoff is None:
        return False, None
    today = date.today()
    # Count trading days between today and cutoff inclusive.
    trading_days = 0
    d = today
    while d <= cutoff:
        if d.weekday() < 5:
            trading_days += 1
        d += timedelta(days=1)
    return trading_days <= look_ahead_days, cutoff


async def tickers_with_upcoming_dividend_cutoff(look_ahead_days: int = 3) -> list[dict]:
    """Return tickers whose dividend cutoff is within look_ahead_days."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT ticker, ex_date, dividend, currency FROM dividends
           WHERE date(ex_date) >= date('now')
           ORDER BY ex_date"""
    )
    rows = await cursor.fetchall()
    result = []
    today = date.today()
    for r in rows:
        try:
            registry_close = date.fromisoformat(r[1])
        except ValueError:
            continue
        cutoff = registry_close - timedelta(days=1)
        while cutoff.weekday() >= 5:
            cutoff -= timedelta(days=1)
        if cutoff < today:
            continue
        trading_days = 0
        d = today
        while d <= cutoff:
            if d.weekday() < 5:
                trading_days += 1
            d += timedelta(days=1)
        if trading_days <= look_ahead_days:
            result.append({"ticker": r[0], "cutoff_date": cutoff.isoformat(), "registry_close_date": registry_close.isoformat(), "dividend": r[2], "currency": r[3]})
    return result


def _previous_trading_day(d: date) -> date:
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return prev


def cbr_soft_mode_state(meeting_dates: set[date] | None = None) -> tuple[bool, bool, date | None]:
    """Return (is_meeting_day, is_pre_meeting_day, next_meeting_date).

    A "pre-meeting day" is the last trading day before the CBR meeting.
    On those days the robot blocks new positions and tightens profit protection.
    """
    dates = meeting_dates or CBR_MEETING_DATES
    today = date.today()
    if today in dates:
        return True, False, today
    future = sorted([d for d in dates if d >= today])
    if not future:
        return False, False, None
    next_meeting = future[0]
    return False, today == _previous_trading_day(next_meeting), next_meeting


# ── Sentiment ────────────────────────────────────────────────


async def save_sentiment(
    ticker: str,
    headline: str,
    sentiment: str,
    confidence: int,
    summary: str,
    topics: list[str],
    risk_flags: list[str],
    source: str = "rss",
    news_hash: str | None = None,
    published_at: str | None = None,
    age_minutes: int | None = None,
    weight: float = 1.0,
) -> int:
    """Save a sentiment analysis result. Returns row id."""
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO sentiment
           (ticker, source, headline, sentiment, confidence, summary, topics, risk_flags,
            news_hash, published_at, age_minutes, weight)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker, source, headline, sentiment, confidence, summary,
         json.dumps(topics), json.dumps(risk_flags), news_hash, published_at,
         age_minutes, weight),
    )
    await db.commit()
    return cursor.lastrowid


async def get_seen_rss_hashes(ticker: str | None = None) -> set[str]:
    """Return hashes of RSS items already processed.

    If ticker is provided, only return hashes for that ticker.
    Otherwise return all hashes from the last 7 days.
    """
    db = await get_db()
    if ticker:
        cursor = await db.execute(
            """SELECT hash FROM rss_seen_hashes
               WHERE ticker = ? AND datetime(seen_at) > datetime('now', '-7 days')""",
            (ticker,),
        )
    else:
        cursor = await db.execute(
            """SELECT hash FROM rss_seen_hashes
               WHERE datetime(seen_at) > datetime('now', '-7 days')"""
        )
    rows = await cursor.fetchall()
    return {r[0] for r in rows}


async def get_recent_rss_headlines(ticker: str, hours: int = 4, limit: int = 10) -> list[dict]:
    """Return recent RSS headlines for a ticker from the local cache.

    Used by TradingAgent and analyzer to avoid slow live RSS fetching.
    """
    db = await get_db()
    cursor = await db.execute(
        """SELECT hash, ticker, source, headline, seen_at
           FROM rss_seen_hashes
           WHERE ticker = ? AND datetime(seen_at) > datetime('now', ?)
           ORDER BY seen_at DESC
           LIMIT ?""",
        (ticker, f"-{hours} hours", limit),
    )
    rows = await cursor.fetchall()
    return [
        {
            "hash": r[0],
            "ticker": r[1],
            "source": r[2],
            "headline": r[3],
            "seen_at": r[4],
        }
        for r in rows
    ]


async def mark_rss_hash_seen(
    hash: str, ticker: str, source: str, headline: str
) -> None:
    """Mark an RSS item as processed to avoid re-analysis."""
    db = await get_db()
    await db.execute(
        """INSERT OR IGNORE INTO rss_seen_hashes (hash, ticker, source, headline)
           VALUES (?, ?, ?, ?)""",
        (hash, ticker, source, headline),
    )
    await db.commit()


async def get_latest_sentiment(ticker: str, max_age_hours: int = 24) -> dict | None:
    """Get the most recent sentiment record for a ticker if it is fresh enough."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT ts, headline, sentiment, confidence, summary, topics, risk_flags,
                  news_hash, published_at, age_minutes, weight
           FROM sentiment
           WHERE ticker = ? AND datetime(ts) > datetime('now', ?)
           ORDER BY ts DESC LIMIT 1""",
        (ticker, f"-{max_age_hours} hours"),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "ts": row[0],
        "headline": row[1],
        "sentiment": row[2],
        "confidence": row[3],
        "summary": row[4],
        "topics": json.loads(row[5]) if row[5] else [],
        "risk_flags": json.loads(row[6]) if row[6] else [],
        "news_hash": row[7],
        "published_at": row[8],
        "age_minutes": row[9],
        "weight": row[10],
    }


async def get_watchlist_sentiment(tickers: list[str], max_age_hours: int = 24) -> dict[str, dict]:
    """Return latest sentiment for each ticker in the list."""
    db = await get_db()
    placeholders = ",".join("?" * len(tickers))
    cursor = await db.execute(
        f"""SELECT ticker, ts, headline, source, sentiment, confidence, summary,
                   topics, risk_flags, news_hash, published_at, age_minutes, weight
            FROM sentiment
            WHERE ticker IN ({placeholders})
              AND datetime(ts) > datetime('now', ?)
              AND id IN (
                  SELECT MAX(id) FROM sentiment
                  WHERE ticker IN ({placeholders})
                    AND datetime(ts) > datetime('now', ?)
                  GROUP BY ticker
              )""",
        (*tickers, f"-{max_age_hours} hours", *tickers, f"-{max_age_hours} hours"),
    )
    rows = await cursor.fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out[r[0]] = {
            "ts": r[1],
            "headline": r[2],
            "source": r[3],
            "sentiment": r[4],
            "confidence": r[5],
            "summary": r[6],
            "topics": json.loads(r[7]) if r[7] else [],
            "risk_flags": json.loads(r[8]) if r[8] else [],
            "news_hash": r[9],
            "published_at": r[10],
            "age_minutes": r[11],
            "weight": r[12],
        }
    return out


# ── Predictions diary ─────────────────────────────────────────


async def save_prediction(
    ticker: str,
    predicted_direction: str,
    predicted_price: float,
    predicted_strength: str,
    higher_tf_trend: str | None = None,
    signals_used: list[str] | None = None,
    reasoning: str | None = None,
    source: str = "analysis",
    llm_provider: str | None = None,
    environment: str = "paper",
) -> int:
    """Save a new prediction. Returns row id.

    If a prediction for the same ticker, source, environment and calendar day already exists,
    overwrite it so the diary does not accumulate duplicate rows for repeated
    analysis of the same ticker.
    """
    db = await get_db()
    cursor = await db.execute(
        """SELECT id FROM predictions
           WHERE ticker = ? AND source = ? AND environment = ? AND date(ts) = date('now')
           ORDER BY ts DESC LIMIT 1""",
        (ticker, source, environment),
    )
    existing = await cursor.fetchone()
    if existing:
        await db.execute(
            """UPDATE predictions
               SET predicted_direction = ?, predicted_price = ?, predicted_strength = ?,
                   higher_tf_trend = ?, signals_used = ?, reasoning = ?, llm_provider = ?, environment = ?, ts = datetime('now', 'localtime'),
                   result_1d = 'pending', result_3d = 'pending', result_7d = 'pending',
                   actual_price_1d = NULL, actual_price_3d = NULL, actual_price_7d = NULL
               WHERE id = ?""",
            (predicted_direction, predicted_price, predicted_strength, higher_tf_trend,
             json.dumps(signals_used or []), reasoning, llm_provider, environment, existing[0]),
        )
        await db.commit()
        return existing[0]

    cursor = await db.execute(
        """INSERT INTO predictions
           (ticker, predicted_direction, predicted_price, predicted_strength, higher_tf_trend, signals_used, reasoning, source, llm_provider, environment)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker, predicted_direction, predicted_price, predicted_strength, higher_tf_trend, json.dumps(signals_used or []), reasoning, source, llm_provider, environment),
    )
    await db.commit()
    return cursor.lastrowid


async def save_intraday_backtest(records: list[dict]) -> int:
    """Bulk-save intraday backtest records. Returns number of rows inserted."""
    if not records:
        return 0
    db = await get_db()
    rows = [
        (
            r["ticker"],
            r["signal_ts"],
            r["signal"],
            r["direction"],
            r["confidence"],
            r.get("entry_px"),
            r.get("stop_px"),
            r.get("take_px"),
            r.get("open_px"),
            r.get("close_px"),
            r["horizon_candles"],
            r.get("future_px"),
            r.get("return_pct"),
            r["result"],
            json.dumps(r.get("signals_used") or []),
            r.get("reason"),
            1 if r.get("llm_used") else 0,
        )
        for r in records
    ]
    await db.executemany(
        """INSERT INTO intraday_backtest
           (ticker, signal_ts, signal, direction, confidence,
            entry_px, stop_px, take_px, open_px, close_px,
            horizon_candles, future_px, return_pct, result,
            signals_used, reason, llm_used)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    await db.commit()
    return len(rows)


def _trading_days_ago(n: int, from_dt: datetime | None = None) -> str:
    """Return ISO date string that is n trading days ago in Europe/Moscow time.

    Trading days = Monday-Friday (MOEX is closed on weekends and holidays
    are ignored for simplicity; this is close enough for a hobby project).
    Uses Europe/Moscow local time so predictions made during Moscow trading
    hours are evaluated on the correct next trading day.
    """
    dt = from_dt or datetime.now()
    days_back = 0
    trading = 0
    while trading < n:
        days_back += 1
        candidate = dt - timedelta(days=days_back)
        if candidate.weekday() < 5:  # Monday=0 .. Friday=4
            trading += 1
    return (dt - timedelta(days=days_back)).strftime("%Y-%m-%d")


async def get_pending_predictions(horizon_days: int) -> list[dict]:
    """Get predictions that are due for a horizon check and still pending.

    horizon_days is interpreted as trading days (Mon-Fri) so a prediction
    made on Friday gets its 1-day check on Monday, not Saturday.
    """
    db = await get_db()
    col = f"result_{horizon_days}d"
    cutoff = _trading_days_ago(horizon_days)
    cursor = await db.execute(
        f"""SELECT id, ts, ticker, predicted_direction, predicted_price, predicted_strength,
                  higher_tf_trend, signals_used, reasoning, source, llm_provider
           FROM predictions
           WHERE date(ts) <= ?
             AND {col} = 'pending'""",
        (cutoff,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "ts": r[1], "ticker": r[2],
            "predicted_direction": r[3], "predicted_price": r[4],
            "predicted_strength": r[5], "higher_tf_trend": r[6],
            "signals_used": json.loads(r[7]) if r[7] else [],
            "reasoning": r[8], "source": r[9], "llm_provider": r[10],
        }
        for r in rows
    ]


async def update_prediction_result(
    pred_id: int, horizon_days: int, result: str, actual_price: float
) -> None:
    """Update the result for a given horizon."""
    db = await get_db()
    col_result = f"result_{horizon_days}d"
    col_price = f"actual_price_{horizon_days}d"
    await db.execute(
        f"UPDATE predictions SET {col_result}=?, {col_price}=? WHERE id=?",
        (result, actual_price, pred_id),
    )
    await db.commit()


async def get_prediction_stats(
    ticker: str | None = None,
    since_days: int = 30,
    group_by_provider: bool = False,
    source: str | None = None,
    environment: str | None = None,
) -> dict:
    """Compute accuracy stats for predictions. Optionally group by LLM provider, source or environment."""
    db = await get_db()
    where = "datetime(ts) > datetime('now', ?)"
    params = [f"-{since_days} days"]
    if ticker:
        where += " AND ticker=?"
        params.append(ticker)
    if source:
        where += " AND source=?"
        params.append(source)
    if environment:
        where += " AND environment=?"
        params.append(environment)

    base_params = list(params)

    async def _pct(col: str, extra_where: str = "", extra_params: list[Any] | None = None) -> float:
        q_params = base_params + (extra_params or [])
        cursor = await db.execute(
            f"""SELECT COUNT(CASE WHEN {col}='correct' THEN 1 END),
                       COUNT(CASE WHEN {col} IN ('correct','wrong') THEN 1 END)
               FROM predictions WHERE {where} {extra_where}""",
            q_params,
        )
        row = await cursor.fetchone()
        correct = row[0] or 0
        total = row[1] or 0
        return round(correct / total * 100, 1) if total else 0.0

    stats = {
        "correct_1d_pct": await _pct("result_1d"),
        "correct_3d_pct": await _pct("result_3d"),
        "correct_7d_pct": await _pct("result_7d"),
    }

    # total count: count unique ticker-day rows so duplicates do not inflate stats
    cursor = await db.execute(
        f"""SELECT COUNT(*) FROM (
                SELECT ticker, date(ts) FROM predictions WHERE {where} GROUP BY ticker, date(ts)
            )""",
        base_params,
    )
    row = await cursor.fetchone()
    stats["total"] = row[0] or 0

    if group_by_provider:
        cursor = await db.execute(
            f"""SELECT provider, COUNT(*) as total
                FROM (
                    SELECT COALESCE(llm_provider, 'unknown') as provider
                    FROM predictions
                    WHERE {where}
                    GROUP BY ticker, date(ts), provider
                )
                GROUP BY provider""",
            base_params,
        )
        by_provider: dict[str, dict] = {}
        for row in await cursor.fetchall():
            provider = row[0]
            by_provider[provider] = {
                "total": row[1],
                "correct_1d_pct": await _pct(
                    "result_1d", "AND COALESCE(llm_provider, 'unknown') = ?", [provider]
                ),
                "correct_3d_pct": await _pct(
                    "result_3d", "AND COALESCE(llm_provider, 'unknown') = ?", [provider]
                ),
                "correct_7d_pct": await _pct(
                    "result_7d", "AND COALESCE(llm_provider, 'unknown') = ?", [provider]
                ),
            }
        stats["by_provider"] = by_provider

    return stats


async def get_proposal_source_stats(since_days: int = 30) -> list[dict]:
    """Compute performance stats for robot proposals grouped by source.

    Returns one row per source with counts by status and estimated win rate
    for executed proposals where a result can be inferred.
    """
    db = await get_db()
    cursor = await db.execute(
        """SELECT source,
                  COUNT(*) as total,
                  COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending,
                  COUNT(CASE WHEN status = 'confirmed' THEN 1 END) as confirmed,
                  COUNT(CASE WHEN status = 'rejected' THEN 1 END) as rejected,
                  COUNT(CASE WHEN status = 'executed' THEN 1 END) as executed,
                  COUNT(CASE WHEN status = 'superseded' THEN 1 END) as superseded,
                  AVG(CASE WHEN status = 'executed' AND net_profit_pct IS NOT NULL
                           THEN net_profit_pct END) as avg_profit_pct,
                  SUM(CASE WHEN status = 'executed' AND net_profit_pct > 0 THEN 1 ELSE 0 END) as profitable,
                  COUNT(CASE WHEN status = 'executed' AND net_profit_pct IS NOT NULL THEN 1 END) as with_result
           FROM robot_proposals
           WHERE datetime(ts) > datetime('now', ?)
           GROUP BY source
           ORDER BY total DESC""",
        (f"-{since_days} days",),
    )
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        source = r[0]
        total = r[1] or 0
        executed = r[5] or 0
        with_result = r[9] or 0
        profitable = r[8] or 0
        avg_profit = r[7]
        win_rate = round(profitable / with_result * 100, 1) if with_result else None
        result.append({
            "source": source,
            "total": total,
            "pending": r[2] or 0,
            "confirmed": r[3] or 0,
            "rejected": r[4] or 0,
            "executed": executed,
            "superseded": r[6] or 0,
            "avg_profit_pct": round(avg_profit, 2) if avg_profit is not None else None,
            "win_rate_pct": win_rate,
            "profitable_count": profitable,
            "with_result_count": with_result,
        })
    return result


async def get_predictions(
    tickers: list[str] | None = None,
    since_days: int = 30,
    limit: int = 50,
    source: str | None = None,
    environment: str | None = None,
) -> list[dict]:
    """Get recent predictions with optional ticker, source and environment filter.

    Returns only the newest prediction per ticker per calendar day so the
    diary table does not show duplicated rows for repeated analysis.
    """
    db = await get_db()
    where = "datetime(ts) > datetime('now', ?)"
    params: list[Any] = [f"-{since_days} days"]
    if tickers:
        placeholders = ",".join("?" * len(tickers))
        where += f" AND ticker IN ({placeholders})"
        params.extend(tickers)
    if source:
        where += " AND source = ?"
        params.append(source)
    if environment:
        where += " AND environment = ?"
        params.append(environment)
    cursor = await db.execute(
        f"""SELECT id, ts, ticker, predicted_direction, predicted_price, predicted_strength,
                  higher_tf_trend, signals_used, reasoning, source, llm_provider, environment, result_1d, result_3d, result_7d,
                  actual_price_1d, actual_price_3d, actual_price_7d
           FROM predictions
           WHERE {where}
             AND id IN (
                 SELECT MAX(id) FROM predictions
                 WHERE {where}
                 GROUP BY ticker, date(ts)
             )
           ORDER BY ts DESC LIMIT ?""",
        (*params, *params, limit),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "ts": r[1], "ticker": r[2],
            "predicted_direction": r[3], "predicted_price": r[4],
            "predicted_strength": r[5], "higher_tf_trend": r[6],
            "signals_used": json.loads(r[7]) if r[7] else [],
            "reasoning": r[8], "source": r[9], "llm_provider": r[10],
            "environment": r[11],
            "result_1d": r[12], "result_3d": r[13], "result_7d": r[14],
            "actual_price_1d": r[15], "actual_price_3d": r[16], "actual_price_7d": r[17],
        }
        for r in rows
    ]


# ── Medium-term proposals from predictions ──────────────────


async def get_recent_directional_predictions(
    horizon_days: int,
    min_strength: str = "moderate",
    since_days: int = 2,
    limit: int = 20,
) -> list[dict]:
    """Return recent directional predictions suitable for medium-term proposals.

    Only returns predictions with predicted_direction in {buy,long,sell,short}
    and predicted_strength >= min_strength that do not already have an open
    proposal for the same ticker and horizon.
    """
    strength_order = {"weak": 0, "moderate": 1, "strong": 2}
    min_score = strength_order.get(min_strength, 1)
    db = await get_db()
    cutoff = _trading_days_ago(since_days)
    cursor = await db.execute(
        f"""SELECT id, ts, ticker, predicted_direction, predicted_price,
                  predicted_strength, higher_tf_trend, signals_used, reasoning,
                  source, llm_provider, result_1d, result_3d, result_7d
           FROM predictions
           WHERE date(ts) >= ?
             AND predicted_direction IN ('buy','long','sell','short')
             AND result_{horizon_days}d = 'pending'
           ORDER BY ts DESC""",
        (cutoff,),
    )
    rows = await cursor.fetchall()
    out = []
    seen_tickers: set[str] = set()
    for r in rows:
        pred = {
            "id": r[0], "ts": r[1], "ticker": r[2],
            "predicted_direction": r[3], "predicted_price": r[4],
            "predicted_strength": r[5], "higher_tf_trend": r[6],
            "signals_used": json.loads(r[7]) if r[7] else [],
            "reasoning": r[8], "source": r[9], "llm_provider": r[10],
            "result_1d": r[11], "result_3d": r[12], "result_7d": r[13],
        }
        score = strength_order.get(pred["predicted_strength"], 0)
        if score < min_score:
            continue
        key = pred["ticker"]
        if key in seen_tickers:
            continue
        seen_tickers.add(key)
        out.append(pred)
        if len(out) >= limit:
            break
    return out


# ── Paper trading ───────────────────────────────────────────


async def open_paper_position(
    ticker: str,
    side: str,
    entry_px: float,
    signals_used: list[str] | None = None,
    stop_px: float | None = None,
    take_px: float | None = None,
    qty: int = 1,
    initial_atr: float | None = None,
    atr_mult: float | None = None,
) -> int:
    """Open a new virtual paper position. Returns row id."""
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO paper_positions
           (ticker, side, entry_px, stop_px, take_px, qty, trigger_signal, initial_atr, atr_mult)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker, side, entry_px, stop_px, take_px, qty,
         json.dumps(signals_used or []), initial_atr, atr_mult),
    )
    await db.commit()
    return cursor.lastrowid


async def open_medium_term_paper_position(
    ticker: str,
    side: str,
    entry_px: float,
    horizon: str,
    signals_used: list[str] | None = None,
    stop_px: float | None = None,
    take_px: float | None = None,
    qty: int = 1,
    initial_atr: float | None = None,
    atr_mult: float | None = None,
) -> int:
    """Open a medium-term virtual paper position tagged with horizon."""
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO paper_positions
           (ticker, side, entry_px, stop_px, take_px, qty, trigger_signal, initial_atr, atr_mult)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ticker, side, entry_px, stop_px, take_px, qty,
         json.dumps((signals_used or []) + [f"medium:{horizon}"]), initial_atr, atr_mult),
    )
    await db.commit()
    return cursor.lastrowid


async def close_paper_position(
    pos_id: int, exit_px: float, reason: str
) -> None:
    """Close a paper position and compute P&L.

    Uses the provided exit_px. If exit_px is missing or non-positive,
    keeps the position open so it is not closed at a bogus price.
    """
    if not exit_px or exit_px <= 0:
        return
    db = await get_db()
    cursor = await db.execute(
        "SELECT side, entry_px, qty FROM paper_positions WHERE id=?", (pos_id,)
    )
    row = await cursor.fetchone()
    if not row:
        return
    side, entry_px, qty = row[0], row[1], row[2]
    pnl_pct = ((exit_px - entry_px) / entry_px * 100) if side == "long" else ((entry_px - exit_px) / entry_px * 100)
    pnl_rub = pnl_pct / 100 * qty * entry_px
    await db.execute(
        """UPDATE paper_positions
           SET status='closed', close_ts=datetime('now','localtime'), exit_px=?, pnl_pct=?, pnl_rub=?, close_reason=?
           WHERE id=?""",
        (exit_px, round(pnl_pct, 2), round(pnl_rub, 2), reason, pos_id),
    )
    await db.commit()


async def update_paper_position_stop(pos_id: int, new_stop_px: float | None) -> None:
    """Update the trailing stop price for an open paper position.

    Does nothing if the new stop is invalid or the position is already closed.
    """
    if new_stop_px is None or new_stop_px <= 0:
        return
    db = await get_db()
    await db.execute(
        """UPDATE paper_positions
           SET stop_px=?
           WHERE id=? AND status='open'""",
        (new_stop_px, pos_id),
    )
    await db.commit()


async def get_open_paper_position(ticker: str) -> dict | None:
    """Return the single open position for a ticker, or None."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT id, open_ts, ticker, side, entry_px, stop_px, take_px, qty, trigger_signal,
                  initial_atr, atr_mult
           FROM paper_positions WHERE ticker=? AND status='open'""",
        (ticker,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "open_ts": row[1], "ticker": row[2], "side": row[3],
        "entry_px": row[4], "stop_px": row[5], "take_px": row[6], "qty": row[7],
        "trigger_signal": json.loads(row[8]) if row[8] else [],
        "initial_atr": row[9], "atr_mult": row[10],
    }


async def get_open_paper_positions() -> list[dict]:
    """Return all open paper positions."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT id, open_ts, ticker, side, entry_px, stop_px, take_px, qty, trigger_signal,
                  initial_atr, atr_mult
           FROM paper_positions WHERE status='open' ORDER BY open_ts DESC"""
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "open_ts": r[1], "ticker": r[2], "side": r[3],
            "entry_px": r[4], "stop_px": r[5], "take_px": r[6], "qty": r[7],
            "trigger_signal": json.loads(r[8]) if r[8] else [],
            "initial_atr": r[9], "atr_mult": r[10],
        }
        for r in rows
    ]


async def get_paper_positions(status: str | None = None, limit: int = 50) -> list[dict]:
    """Return paper positions filtered by status."""
    db = await get_db()
    where = ""
    params: list[Any] = []
    if status:
        where = "WHERE status=?"
        params.append(status)
    cursor = await db.execute(
        f"""SELECT id, open_ts, close_ts, ticker, side, entry_px, exit_px, stop_px, take_px, qty,
                  pnl_pct, pnl_rub, status, close_reason
           FROM paper_positions {where}
           ORDER BY open_ts DESC LIMIT ?""",
        (*params, limit),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "open_ts": r[1], "close_ts": r[2], "ticker": r[3],
            "side": r[4], "entry_px": r[5], "exit_px": r[6], "stop_px": r[7], "take_px": r[8],
            "qty": r[9], "pnl_pct": r[10], "pnl_rub": r[11], "status": r[12], "close_reason": r[13],
        }
        for r in rows
    ]


async def get_paper_position_by_id(pos_id: int) -> dict | None:
    """Return a single paper position by id, or None if not found."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT id, open_ts, close_ts, ticker, side, entry_px, exit_px, stop_px, take_px, qty,
                  pnl_pct, pnl_rub, status, close_reason
           FROM paper_positions WHERE id=?""",
        (pos_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "open_ts": row[1], "close_ts": row[2], "ticker": row[3],
        "side": row[4], "entry_px": row[5], "exit_px": row[6], "stop_px": row[7], "take_px": row[8],
        "qty": row[9], "pnl_pct": row[10], "pnl_rub": row[11], "status": row[12], "close_reason": row[13],
    }


async def get_paper_stats(since_days: int = 30) -> dict:
    """Compute virtual portfolio stats."""
    db = await get_db()
    since = f"-{since_days} days"
    starting_capital = float(PAPER_STARTING_CAPITAL)

    cursor = await db.execute(
        """SELECT COUNT(*),
               SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END),
               SUM(pnl_rub),
               AVG(pnl_pct)
           FROM paper_positions
           WHERE status='closed' AND datetime(open_ts) > datetime('now', ?)""",
        (since,),
    )
    row = await cursor.fetchone()
    total_trades = row[0] or 0
    win_count = row[1] or 0
    loss_count = row[2] or 0
    total_pnl_rub = row[3] or 0.0
    avg_pnl_pct = row[4] or 0.0

    cursor = await db.execute(
        "SELECT COUNT(*) FROM paper_positions WHERE status='open'",
    )
    open_count = (await cursor.fetchone())[0] or 0

    current_capital = starting_capital + (total_pnl_rub or 0.0)
    total_return_pct = ((current_capital / starting_capital) - 1) * 100 if starting_capital else 0.0

    return {
        "starting_capital": starting_capital,
        "current_capital": round(current_capital, 2),
        "total_return_pct": round(total_return_pct, 2),
        "total_trades": total_trades,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate": round(win_count / total_trades * 100, 1) if total_trades else 0.0,
        "avg_pnl_pct": round(avg_pnl_pct, 2),
        "open_positions": open_count,
    }


# ── GeoRisk scores ────────────────────────────────────────────


async def clear_predictions() -> int:
    """Delete all prediction rows. Returns number of rows removed."""
    db = await get_db()
    cursor = await db.execute("DELETE FROM predictions")
    await db.commit()
    return cursor.rowcount


async def clear_predictions_environment(environment: str) -> int:
    """Delete prediction rows for a specific environment. Returns number of rows removed."""
    db = await get_db()
    cursor = await db.execute("DELETE FROM predictions WHERE environment = ?", (environment,))
    await db.commit()
    return cursor.rowcount


async def save_georisk(
    score: int,
    severity: str,
    summary: str,
    affected_sectors: list[str],
    trigger_keywords: list[str],
    overall_direction: int = 0,
    news_items: list[dict] | None = None,
) -> None:
    """Save a geo-risk assessment."""
    db = await get_db()
    await db.execute(
        """INSERT INTO georisk_scores (score, severity, summary, affected_sectors, trigger_keywords, overall_direction, news_items)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            score,
            severity,
            summary,
            json.dumps(affected_sectors),
            json.dumps(trigger_keywords),
            overall_direction,
            json.dumps(news_items or []),
        ),
    )
    await db.commit()


async def get_latest_georisk() -> dict | None:
    """Return the most recent geo-risk assessment."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT ts, score, severity, summary, affected_sectors, trigger_keywords, overall_direction, news_items
           FROM georisk_scores ORDER BY ts DESC LIMIT 1"""
    )
    row = await cursor.fetchone()
    if not row:
        return None
    def _col(name, default=None):
        return row[name] if name in row.keys() else default
    return {
        "ts": row["ts"],
        "score": row["score"],
        "severity": row["severity"],
        "summary": row["summary"],
        "affected_sectors": json.loads(row["affected_sectors"] or "[]"),
        "trigger_keywords": json.loads(row["trigger_keywords"] or "[]"),
        "overall_direction": _col("overall_direction", 0) or 0,
        "news_items": json.loads(_col("news_items", "[]") or "[]"),
    }


# ── Macro indicators ──────────────────────────────────────────


async def save_macro_indicators(
    usd_rub: float | None,
    eur_rub: float | None,
    brent: float | None,
    cbr_rate: float | None,
) -> None:
    """Persist a macro snapshot."""
    db = await get_db()
    await db.execute(
        """INSERT INTO macro_indicators (usd_rub, eur_rub, brent, cbr_rate)
           VALUES (?, ?, ?, ?)""",
        (usd_rub, eur_rub, brent, cbr_rate),
    )
    await db.commit()


async def get_latest_macro(max_age_minutes: int = 120) -> dict | None:
    """Return the most recent macro snapshot if it is fresh enough."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT ts, usd_rub, eur_rub, brent, cbr_rate
           FROM macro_indicators
           WHERE datetime(ts) > datetime('now', ?)
           ORDER BY ts DESC LIMIT 1""",
        (f"-{max_age_minutes} minutes",),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "ts": row[0],
        "usd_rub": row[1],
        "eur_rub": row[2],
        "brent": row[3],
        "cbr_rate": row[4],
    }


# ── Robot proposals ───────────────────────────────────────────


async def supersede_duplicate_pending_proposals(
    ticker: str,
    side: str,
    source: str,
    horizon: str,
    proposal_mode: str,
    exclude_id: int | None = None,
) -> int:
    """Mark earlier pending proposals with the same (ticker, side, source, horizon, mode) as superseded.

    Called from save_robot_proposal so that fresh signals automatically retire older
    pending ones for the same idea. Returns the number of rows superseded.
    """
    db = await get_db()
    sql = """UPDATE robot_proposals
             SET status = 'superseded',
                 decided_by = 'system:supersede_dup',
                 decided_at = datetime('now','localtime')
             WHERE status = 'pending'
               AND ticker = ? AND side = ? AND source = ?
               AND horizon = ? AND proposal_mode = ?"""
    params: list = [ticker, side, source, horizon, proposal_mode]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    cursor = await db.execute(sql, params)
    await db.commit()
    return cursor.rowcount


async def supersede_stale_pending_proposals(
    intraday_max_age_hours: int = 6,
    paper_max_age_days: int = 3,
) -> int:
    """Mark stale pending proposals as superseded by age.

    Rules:
    - Intraday/georisk_intraday older than `intraday_max_age_hours` are superseded
      because their entry prices are no longer reachable within the same session.
    - Paper proposals older than `paper_max_age_days` are superseded; the executor
      already filters by age, so this just keeps the proposals UI honest.

    Returns the number of rows superseded.
    """
    db = await get_db()
    cursor = await db.execute(
        """UPDATE robot_proposals
           SET status = 'superseded',
               decided_by = 'system:stale_intraday',
               decided_at = datetime('now','localtime')
           WHERE status = 'pending'
             AND source IN ('intraday', 'georisk_intraday')
             AND datetime(ts) < datetime('now', ?)""",
        (f"-{intraday_max_age_hours} hours",),
    )
    intraday_updated = cursor.rowcount
    cursor = await db.execute(
        """UPDATE robot_proposals
           SET status = 'superseded',
               decided_by = 'system:stale_paper',
               decided_at = datetime('now','localtime')
           WHERE status = 'pending'
             AND proposal_mode = 'paper'
             AND datetime(ts) < datetime('now', ?)""",
        (f"-{paper_max_age_days} days",),
    )
    paper_updated = cursor.rowcount
    await db.commit()
    return intraday_updated + paper_updated


async def save_robot_proposal(
    ticker: str,
    side: str,
    source: str,
    signal: str | None = None,
    entry_px: float | None = None,
    qty: int | None = None,
    stop_px: float | None = None,
    take_px: float | None = None,
    confidence: int | None = None,
    reason: str | None = None,
    fee_rub: float | None = None,
    net_profit_pct: float | None = None,
    horizon: str = "1d",
    proposal_mode: str = "semi_auto",
    initial_atr: float | None = None,
    atr_mult: float | None = None,
) -> int:
    """Save a new robot proposal. Returns row id.

    Any earlier pending proposal with the same (ticker, side, source, horizon, mode)
    is automatically superseded, so a fresh signal retires its predecessor instead
    of stacking on top of it.
    """
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO robot_proposals
           (ticker, side, source, signal, entry_px, qty, stop_px, take_px,
            confidence, reason, fee_rub, net_profit_pct, horizon, proposal_mode,
            status, initial_atr, atr_mult)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (ticker, side, source, signal, entry_px, qty, stop_px, take_px,
         confidence, reason, fee_rub, net_profit_pct, horizon, proposal_mode,
         initial_atr, atr_mult),
    )
    new_id = cursor.lastrowid
    await db.commit()
    # Dedup: retire the previous pending for the same idea, if any.
    await supersede_duplicate_pending_proposals(
        ticker=ticker,
        side=side,
        source=source,
        horizon=horizon,
        proposal_mode=proposal_mode,
        exclude_id=new_id,
    )
    return new_id


async def get_robot_proposals(
    status: str | None = None,
    limit: int = 50,
    since_days: int = 7,
    horizon: str | None = None,
    proposal_mode: str | None = None,
    ticker: str | None = None,
) -> list[dict]:
    """Return robot proposals, optionally filtered by status, horizon, mode and ticker.

    The time window looks at the confirmation/decision timestamp for confirmed
    proposals because a proposal may be created days before the user confirms it.
    Pending and other statuses still use the creation timestamp.
    """
    db = await get_db()
    # Confirmed proposals can be decided long after creation; use decided_at for them.
    if status == "confirmed":
        where = "(decided_at IS NOT NULL AND datetime(decided_at) > datetime('now', ?))"
    else:
        where = "datetime(ts) > datetime('now', ?)"
    params: list[Any] = [f"-{since_days} days"]
    if status:
        where += " AND status = ?"
        params.append(status)
    if horizon:
        where += " AND horizon = ?"
        params.append(horizon)
    if proposal_mode:
        where += " AND proposal_mode = ?"
        params.append(proposal_mode)
    if ticker:
        where += " AND ticker = ?"
        params.append(ticker)
    cursor = await db.execute(
        f"""SELECT id, ts, ticker, side, source, signal, entry_px, qty, stop_px, take_px,
                  confidence, reason, fee_rub, net_profit_pct, status, decided_at, decided_by,
                  reject_reason, horizon, proposal_mode, exec_entry_px, exec_ts
           FROM robot_proposals
           WHERE {where}
           ORDER BY ts DESC LIMIT ?""",
        (*params, limit),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "ts": r[1], "ticker": r[2], "side": r[3], "source": r[4],
            "signal": r[5], "entry_px": r[6], "qty": r[7], "stop_px": r[8], "take_px": r[9],
            "confidence": r[10], "reason": r[11], "fee_rub": r[12], "net_profit_pct": r[13],
            "status": r[14], "decided_at": r[15], "decided_by": r[16], "reject_reason": r[17],
            "horizon": r[18], "proposal_mode": r[19], "exec_entry_px": r[20], "exec_ts": r[21],
        }
        for r in rows
    ]


async def get_unique_pending_robot_proposals(limit: int = 50) -> list[dict]:
    """Return the most recent pending proposal per ticker.

    Older pending proposals for the same ticker are marked 'superseded'
    so the user only sees one actionable idea per ticker.
    """
    db = await get_db()
    # Keep only the newest pending row per ticker, supersede older ones.
    await db.execute(
        """UPDATE robot_proposals
           SET status = 'superseded', decided_by = 'system', decided_at = datetime('now','localtime')
           WHERE id IN (
               SELECT older.id FROM robot_proposals older
               JOIN robot_proposals newest
                 ON older.ticker = newest.ticker
                AND older.status = 'pending'
                AND newest.status = 'pending'
                AND older.id != newest.id
                AND datetime(older.ts) < datetime(newest.ts)
           )"""
    )
    await db.commit()
    rows = await get_robot_proposals(status="pending", limit=limit, since_days=30)
    return rows


async def get_pending_robot_proposals(limit: int = 50) -> list[dict]:
    """Return pending proposals that need user decision."""
    return await get_robot_proposals(status="pending", limit=limit, since_days=30)


async def get_robot_proposal(proposal_id: int) -> dict | None:
    """Return a single robot proposal by id."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT id, ts, ticker, side, source, signal, entry_px, qty, stop_px, take_px,
                  confidence, reason, fee_rub, net_profit_pct, status, decided_at, decided_by,
                  reject_reason, horizon, proposal_mode, exec_entry_px, exec_ts
           FROM robot_proposals
           WHERE id = ?""",
        (proposal_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0], "ts": row[1], "ticker": row[2], "side": row[3], "source": row[4],
        "signal": row[5], "entry_px": row[6], "qty": row[7], "stop_px": row[8], "take_px": row[9],
        "confidence": row[10], "reason": row[11], "fee_rub": row[12], "net_profit_pct": row[13],
        "status": row[14], "decided_at": row[15], "decided_by": row[16], "reject_reason": row[17],
        "horizon": row[18], "proposal_mode": row[19], "exec_entry_px": row[20], "exec_ts": row[21],
    }


async def get_pending_paper_proposals(limit: int = 50, max_age_days: int = 3) -> list[dict]:
    """Return pending paper proposals queued for next-day execution.

    Filters out proposals older than max_age_days so stale weekend/over-holiday
    orders do not accumulate indefinitely.
    """
    return await get_robot_proposals(
        status="pending",
        proposal_mode="paper",
        limit=limit,
        since_days=max_age_days,
    )


async def execute_paper_proposal(proposal_id: int, exec_entry_px: float) -> bool:
    """Mark a paper proposal as executed and record the real fill price."""
    db = await get_db()
    cursor = await db.execute(
        """UPDATE robot_proposals
           SET status='executed', exec_entry_px=?, exec_ts=datetime('now')
           WHERE id=? AND status='pending'""",
        (exec_entry_px, proposal_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def _set_proposal_status(
    proposal_id: int,
    status: str,
    decided_by: str | None = None,
    reject_reason: str | None = None,
) -> bool:
    """Mark a proposal as confirmed/rejected/executed. Returns True if row existed."""
    db = await get_db()
    if reject_reason is not None and status == "rejected":
        cursor = await db.execute(
            """UPDATE robot_proposals
               SET status = ?, decided_at = datetime('now','localtime'), decided_by = ?, reject_reason = ?
               WHERE id = ?""",
            (status, decided_by or "user", reject_reason, proposal_id),
        )
    else:
        cursor = await db.execute(
            """UPDATE robot_proposals
               SET status = ?, decided_at = datetime('now','localtime'), decided_by = ?
               WHERE id = ?""",
            (status, decided_by or "user", proposal_id),
        )
    await db.commit()
    return cursor.rowcount > 0


async def confirm_robot_proposal(
    proposal_id: int,
    decided_by: str | None = None,
    proposal_mode: str | None = None,
) -> bool:
    """User confirmed the proposal (still needs broker execution).

    Optionally change the execution mode (paper/semi_auto/live) at confirmation time.
    """
    db = await get_db()
    if proposal_mode is not None:
        await db.execute(
            """UPDATE robot_proposals
               SET proposal_mode = ?
               WHERE id = ?""",
            (proposal_mode, proposal_id),
        )
    return await _set_proposal_status(proposal_id, "confirmed", decided_by)


async def reject_robot_proposal(proposal_id: int, decided_by: str | None = None, reject_reason: str | None = None) -> bool:
    """User rejected the proposal."""
    return await _set_proposal_status(proposal_id, "rejected", decided_by, reject_reason=reject_reason)


async def mark_proposal_executed(proposal_id: int, decided_by: str | None = None) -> bool:
    """Broker order was placed."""
    return await _set_proposal_status(proposal_id, "executed", decided_by)


async def delete_stale_robot_proposals(
    max_age_hours: int = 24,
    keep_statuses: tuple[str, ...] = ("confirmed", "rejected", "executed", "superseded"),
    clear_all_pending: bool = False,
) -> dict[str, int]:
    """Delete old api_ticker proposals and pending proposals.

    Useful for cleaning up test/analysis noise and stale intraday signals.
    When clear_all_pending is True, remove all pending proposals regardless of age.
    """
    db = await get_db()
    statuses_to_keep = ",".join(f"'{s}'" for s in keep_statuses)

    # Delete api_ticker source proposals regardless of age
    cursor = await db.execute(
        "DELETE FROM robot_proposals WHERE source = 'api_ticker'"
    )
    api_ticker_deleted = cursor.rowcount

    if clear_all_pending:
        # Delete every pending proposal (regardless of age or source)
        cursor = await db.execute(
            f"""DELETE FROM robot_proposals
                WHERE status = 'pending'
                  AND status NOT IN ({statuses_to_keep})"""
        )
    else:
        # Delete pending proposals older than max_age_hours
        cursor = await db.execute(
            f"""DELETE FROM robot_proposals
                WHERE status = 'pending'
                  AND datetime(ts) < datetime('now', ?)
                  AND status NOT IN ({statuses_to_keep})""",
            (f"-{max_age_hours} hours",),
        )
    stale_pending_deleted = cursor.rowcount

    await db.commit()
    return {
        "api_ticker": api_ticker_deleted,
        "stale_pending": stale_pending_deleted,
    }


async def save_broker_order(
    proposal_id: int | None,
    ticker: str,
    side: str,
    broker: str,
    account_id: str,
    order_id: str,
    lots: int,
    qty: int,
    entry_px: float,
    status: str = "pending",
    broker_message: str = "",
    stop_order_ids: list[str] | None = None,
    environment: str = "unknown",
) -> int:
    """Persist a broker order record."""
    db = await get_db()
    import json
    cursor = await db.execute(
        """INSERT INTO broker_orders
           (proposal_id, ticker, side, broker, account_id, order_id, lots, qty,
            entry_px, status, broker_message, stop_order_ids, environment)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            proposal_id,
            ticker.upper(),
            side,
            broker,
            account_id,
            order_id,
            lots,
            qty,
            entry_px,
            status,
            broker_message,
            json.dumps(stop_order_ids or []),
            environment,
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def update_broker_order_status(
    order_id: str,
    status: str,
    broker_message: str = "",
    stop_order_ids: list[str] | None = None,
) -> bool:
    """Update status/message of a broker order."""
    db = await get_db()
    import json
    cursor = await db.execute(
        """UPDATE broker_orders
           SET status=?, broker_message=?,
               stop_order_ids=COALESCE(?, stop_order_ids)
           WHERE order_id=?""",
        (status, broker_message, json.dumps(stop_order_ids) if stop_order_ids is not None else None, order_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def get_broker_orders(
    status: str | None = None,
    limit: int = 50,
    ticker: str | None = None,
    account_id: str | None = None,
) -> list[dict]:
    """Return broker orders, optionally filtered by status / ticker / account.

    Each row is enriched with:
    - The originating robot proposal's source, confidence and proposal_mode
      (LEFT JOIN robot_proposals — manual closes have no proposal_id and the
      extra fields stay NULL).
    - The proposal's stop_px / take_px and exec_entry_px (for stop/take closes).
    - The realised P&L (pnl_rub / pnl_pct) computed by FIFO-matching close rows
      against the most-recently-filled open row of the opposite normalised side.

    Trades executed by Tinkoff directly (stop/take/geo-risk/dividend) do NOT
    create a corresponding close row in `broker_orders`, so their P&L stays
    NULL in this table — those events are visible only via `broker_positions`.
    """
    db = await get_db()
    where = "WHERE 1=1"
    params: list[Any] = []
    if status:
        where += " AND bo.status = ?"
        params.append(status)
    if ticker:
        where += " AND bo.ticker = ?"
        params.append(ticker.upper())
    if account_id:
        where += " AND bo.account_id = ?"
        params.append(account_id)
    cursor = await db.execute(
        f"""SELECT bo.id, bo.ts, bo.proposal_id, bo.ticker, bo.side, bo.broker,
                   bo.account_id, bo.order_id, bo.stop_order_ids, bo.lots, bo.qty,
                   bo.entry_px, bo.status, bo.broker_message, bo.environment,
                   rp.source AS p_source, rp.confidence AS p_confidence,
                   rp.proposal_mode AS p_proposal_mode,
                   rp.stop_px AS p_stop_px, rp.take_px AS p_take_px,
                   rp.exec_entry_px AS p_exec_entry_px,
                   rp.signal AS p_signal, rp.horizon AS p_horizon, rp.qty AS p_qty,
                   bp.close_reason AS bp_close_reason,
                   bp.avg_entry_px AS bp_avg_entry_px
            FROM broker_orders bo
            LEFT JOIN robot_proposals rp ON rp.id = bo.proposal_id
            LEFT JOIN (
                SELECT ticker, broker, account_id, side, close_reason, avg_entry_px
                FROM broker_positions
                WHERE status = 'closed'
                  AND (ticker, broker, account_id, side, ts) IN (
                      SELECT ticker, broker, account_id, side, MAX(ts)
                      FROM broker_positions
                      WHERE status = 'closed'
                      GROUP BY ticker, broker, account_id, side
                  )
            ) bp ON bp.ticker = bo.ticker
                AND bp.broker = bo.broker AND bp.account_id = bo.account_id
                AND bp.side = CASE
                    WHEN bo.side IN ('long','buy') THEN 'long'
                    WHEN bo.side IN ('short','sell') THEN 'short'
                    ELSE bo.side END
            {where}
            ORDER BY bo.ts DESC
            LIMIT ?""",
        params + [limit],
    )
    rows = await cursor.fetchall()
    orders = [
        {
            "id": r[0], "ts": r[1], "proposal_id": r[2], "ticker": r[3], "side": r[4],
            "broker": r[5], "account_id": r[6], "order_id": r[7], "stop_order_ids": r[8],
            "lots": r[9], "qty": r[10], "entry_px": r[11], "status": r[12],
            "broker_message": r[13], "environment": r[14],
            "proposal_source": r[15], "proposal_confidence": r[16],
            "proposal_proposal_mode": r[17],
            "proposal_stop_px": r[18], "proposal_take_px": r[19],
            "proposal_exec_entry_px": r[20],
            "proposal_signal": r[21], "proposal_horizon": r[22],
            "proposal_qty": r[23],
            "bp_close_reason": r[24],
            "bp_avg_entry_px": r[25],
            # Defaults for fields the UI will render:
            "exit_px": None,
            "pnl_rub": None,
            "pnl_pct": None,
            "open_px": None,
            "stop_px": None,
            "take_px": None,
            "close_reason": None,
        }
        for r in rows
    ]

    # FIFO: walk in chronological order (oldest first) and match closes
    # against the earliest still-open position of the same ticker with the
    # opposite normalised side. side normalisation: long/buy → long,
    # short/sell → short.
    def _norm_side(s: str) -> str:
        s = (s or "").lower()
        return "long" if s in ("long", "buy") else "short" if s in ("short", "sell") else s

    open_lots: dict[tuple[str, str], list[dict]] = {}
    for o in sorted(orders, key=lambda x: x["ts"] or ""):
        is_open = o["proposal_id"] is not None and o["status"] == "filled"
        if is_open:
            pos_side = _norm_side(o["side"])
            qty = o["qty"] or 0
            if qty > 0 and o["entry_px"]:
                key = (o["ticker"].upper(), pos_side)
                open_lots.setdefault(key, []).append({
                    "qty": qty,
                    "entry_px": float(o["entry_px"]),
                    "ts": o["ts"],
                    "proposal_id": o["proposal_id"],
                    "stop_px": o["proposal_stop_px"],
                    "take_px": o["proposal_take_px"],
                })
            o["open_px"] = float(o["entry_px"]) if o["entry_px"] else None
            o["stop_px"] = o["proposal_stop_px"]
            o["take_px"] = o["proposal_take_px"]
        elif o["proposal_id"] is None and o["status"] == "filled":
            # Manual close path (api_sandbox_close_position writes proposal_id=NULL
            # and entry_px = close_px). Match by ticker + opposite side.
            pos_side_for_close = "long" if _norm_side(o["side"]) == "short" else "short"
            key = (o["ticker"].upper(), pos_side_for_close)
            stack = open_lots.get(key)
            if stack and o["entry_px"]:
                lot = stack[0]
                remain = o["qty"] or 0
                close_px = float(o["entry_px"])
                while remain > 0 and stack:
                    head = stack[0]
                    take = min(remain, head["qty"])
                    pnl_px = (close_px - head["entry_px"]) if pos_side_for_close == "long" else (head["entry_px"] - close_px)
                    head_qty = head["qty"]
                    contribution = pnl_px * take
                    if o["pnl_rub"] is None:
                        o["pnl_rub"] = 0.0
                        o["pnl_pct"] = 0.0
                    o["pnl_rub"] += contribution
                    if head_qty > 0 and head["entry_px"]:
                        o["pnl_pct"] = (o["pnl_pct"] or 0.0) + pnl_px / head["entry_px"] * 100 * (take / head_qty)
                    head["qty"] -= take
                    remain -= take
                    if head["qty"] <= 0:
                        stack.pop(0)
                if not stack:
                    open_lots.pop(key, None)
                o["exit_px"] = close_px
                o["open_px"] = lot["entry_px"] if lot else None
                o["stop_px"] = lot["stop_px"] if lot else None
                o["take_px"] = lot["take_px"] if lot else None
                o["close_reason"] = "manual_close"
        elif o["status"] == "filled" and o["proposal_id"] is not None:
            # Filled via Tinkoff stop/take/geo/dividend mechanism — no separate
            # broker_orders row was created for the close. Stop/take still come
            # from the original proposal so the UI can show them.
            o["stop_px"] = o["proposal_stop_px"]
            o["take_px"] = o["proposal_take_px"]
            # If we have a matching closed broker_positions row, surface its
            # close reason. (broker_positions has no exit_px column, so we
            # don't have an exit price for Tinkoff-driven closes — that
            # information would need a separate log table to track.)
            if o["bp_close_reason"]:
                o["close_reason"] = o["bp_close_reason"]
        # pending / partial / rejected: nothing extra

    # Drop the helper fields so they don't pollute the public response.
    for o in orders:
        o.pop("bp_close_reason", None)
        o.pop("bp_avg_entry_px", None)
        o.pop("proposal_stop_px", None)
        o.pop("proposal_take_px", None)
        o.pop("proposal_signal", None)
        o.pop("proposal_horizon", None)
        o.pop("proposal_qty", None)

    return orders


async def get_pending_broker_orders(limit: int = 50) -> list[dict]:
    """Return broker orders awaiting fill."""
    return await get_broker_orders(status="pending", limit=limit)


async def count_broker_orders_by_status(environment: str | None = None) -> dict[str, int]:
    """Return count of broker orders grouped by status, optionally filtered by environment."""
    db = await get_db()
    where = "WHERE 1=1"
    params: list[Any] = []
    if environment:
        where += " AND environment = ?"
        params.append(environment)
    cursor = await db.execute(
        f"""SELECT status, COUNT(*) FROM broker_orders {where} GROUP BY status""",
        params,
    )
    rows = await cursor.fetchall()
    # Ensure all expected keys exist so the UI always gets a consistent object.
    counts = {"pending": 0, "filled": 0, "partial": 0, "rejected": 0, "cancelled": 0}
    for status, count in rows:
        if status in counts:
            counts[status] = count
    return counts


async def summarize_broker_trades(environment: str = "sandbox", limit: int = 500) -> dict:
    """Return realised-P&L summary sourced from the journal table.

    Closed trades come from `journal` (one row per closed idea, with
    realised entry/exit/qty/pnl). Open positions come from
    `broker_positions WHERE status='open' AND broker='tinkoff'`.

    This is the same source of truth as `/api/sandbox/trades`, so the
    summary tile and the table stay in sync. We previously derived
    closed trades via FIFO on `broker_orders`, but the backfill script
    writes historical deals straight into `journal` (they never pass
    through the broker_orders pipeline), so the FIFO view missed those.
    """
    db = await get_db()
    # Closed trades from journal
    cur = await db.execute(
        """SELECT id, COALESCE(exit_ts, ts) AS ts, ticker, side,
                  entry_px, exit_px, qty, pnl, reason
           FROM journal
           ORDER BY COALESCE(exit_ts, ts) DESC
           LIMIT ?""",
        (limit,),
    )
    closed_rows = await cur.fetchall()
    closed_trades: list[dict] = []
    for r in closed_rows:
        entry = float(r[4]) if r[4] is not None else None
        exit_px = float(r[5]) if r[5] is not None else None
        qty = int(r[6]) if r[6] is not None else 0
        pnl = float(r[7]) if r[7] is not None else None
        closed_trades.append({
            "ticker": r[2],
            "side": r[3] or "long",
            "qty": qty,
            "open_px": entry,
            "close_px": exit_px,
            "close_ts": r[1],
            "realised_pnl_rub": round(pnl, 2) if pnl is not None else 0.0,
            "reason": r[8] or "",
        })

    # Open positions from broker_positions
    cur = await db.execute(
        """SELECT id, ts, ticker, side, qty, avg_entry_px, stop_px, take_px
           FROM broker_positions
           WHERE status='open' AND broker='tinkoff'
           ORDER BY ts DESC"""
    )
    open_pos_rows = await cur.fetchall()
    open_positions: list[dict] = []
    for r in open_pos_rows:
        raw_qty = int(r[4]) if r[4] is not None else 0
        if raw_qty == 0:
            continue
        # broker_positions stores qty as negative for short positions in some
        # legacy rows. Use absolute value for the user-facing count.
        qty = abs(raw_qty)
        open_positions.append({
            "ticker": r[2],
            "side": r[3] or "long",
            "qty": qty,
            "avg_px": round(float(r[5]), 4) if r[5] is not None else None,
            "proposal_id": None,
        })

    total_pnl = round(sum(t["realised_pnl_rub"] for t in closed_trades), 2)
    wins = sum(1 for t in closed_trades if t["realised_pnl_rub"] > 0)
    losses = sum(1 for t in closed_trades if t["realised_pnl_rub"] < 0)
    breakeven = sum(1 for t in closed_trades if t["realised_pnl_rub"] == 0)
    win_rate = round(100 * wins / len(closed_trades), 1) if closed_trades else 0.0

    by_ticker: dict[str, dict] = {}
    for t in closed_trades:
        slot = by_ticker.setdefault(t["ticker"], {
            "ticker": t["ticker"], "trades": 0, "wins": 0,
            "pnl_rub": 0.0, "best_rub": None, "worst_rub": None,
        })
        slot["trades"] += 1
        if t["realised_pnl_rub"] > 0:
            slot["wins"] += 1
        slot["pnl_rub"] = round(slot["pnl_rub"] + t["realised_pnl_rub"], 2)
        if slot["best_rub"] is None or t["realised_pnl_rub"] > slot["best_rub"]:
            slot["best_rub"] = t["realised_pnl_rub"]
        if slot["worst_rub"] is None or t["realised_pnl_rub"] < slot["worst_rub"]:
            slot["worst_rub"] = t["realised_pnl_rub"]

    return {
        "environment": environment,
        "closed_count": len(closed_trades),
        "open_count": len(open_positions),
        "winning_trades": wins,
        "losing_trades": losses,
        "breakeven_trades": breakeven,
        "total_pnl_rub": total_pnl,
        "win_rate_pct": win_rate,
        "by_ticker": sorted(by_ticker.values(), key=lambda x: -x["pnl_rub"]),
        "open_positions": open_positions,
        "trades": closed_trades,
    }





async def get_broker_orders_for_proposal(proposal_id: int) -> list[dict]:
    """Return all broker_orders rows for a given proposal_id, newest first.

    Used by the executor to skip a proposal that already has an active order
    (pending/filled/partial) — prevents duplicate sends after a transient DB
    error or a missed status update.
    """
    db = await get_db()
    cursor = await db.execute(
        """SELECT id, ts, proposal_id, ticker, side, broker, account_id, order_id,
                  stop_order_ids, lots, qty, entry_px, status, broker_message, environment
           FROM broker_orders
           WHERE proposal_id = ?
           ORDER BY id DESC""",
        (proposal_id,),
    )
    rows = await cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in rows]


# ── Broker positions ────────────────────────────────────────────


async def get_open_broker_positions(
    broker: str = "tinkoff",
    account_id: str | None = None,
    include_closed: bool = False,
) -> list[dict]:
    """Return tracked broker positions.

    By default only `status='open'` rows are returned. Pass
    `include_closed=True` to also get recently closed ones (used by the API
    to surface broker-side closes that we reconciled ourselves).
    """
    db = await get_db()
    where = "broker = ?"
    params: list[Any] = [broker]
    if not include_closed:
        where += " AND status = 'open'"
    if account_id is not None:
        where += " AND account_id = ?"
        params.append(account_id)
    cursor = await db.execute(
        f"""SELECT id, ts, ticker, side, qty, lots, avg_entry_px, stop_px, take_px,
                   initial_atr, atr_mult, broker, account_id,
                   status, close_reason, exit_px
            FROM broker_positions WHERE {where} ORDER BY ts DESC""",
        params,
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "ts": r[1], "ticker": r[2], "side": r[3],
            "qty": r[4], "lots": r[5], "avg_entry_px": r[6],
            "stop_px": r[7], "take_px": r[8],
            "initial_atr": r[9], "atr_mult": r[10],
            "broker": r[11], "account_id": r[12],
            "status": r[13], "close_reason": r[14], "exit_px": r[15],
        }
        for r in rows
    ]


async def get_broker_positions(
    broker: str = "tinkoff",
    account_id: str | None = None,
    status: str | None = None,
    since_days: int | None = None,
) -> list[dict]:
    """Return broker_positions filtered by status and (optionally) ts window.

    `status` accepts a single value like 'open' or 'closed' (None = all).
    `since_days` limits to rows updated within the last N days (uses `ts`).
    Used by phantom-position reconciliation to scan recently closed rows.
    """
    db = await get_db()
    where = "broker = ?"
    params: list[Any] = [broker]
    if status is not None:
        where += " AND status = ?"
        params.append(status)
    if account_id is not None:
        where += " AND account_id = ?"
        params.append(account_id)
    if since_days is not None and since_days > 0:
        where += " AND ts >= datetime('now', ?)"
        params.append(f"-{int(since_days)} days")
    cursor = await db.execute(
        f"""SELECT id, ts, ticker, side, qty, lots, avg_entry_px, stop_px, take_px,
                   initial_atr, atr_mult, broker, account_id,
                   status, close_reason, exit_px
            FROM broker_positions WHERE {where} ORDER BY ts DESC""",
        params,
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": r[0], "ts": r[1], "ticker": r[2], "side": r[3],
            "qty": r[4], "lots": r[5], "avg_entry_px": r[6],
            "stop_px": r[7], "take_px": r[8],
            "initial_atr": r[9], "atr_mult": r[10],
            "broker": r[11], "account_id": r[12],
            "status": r[13], "close_reason": r[14], "exit_px": r[15],
        }
        for r in rows
    ]


async def update_broker_position(
    ticker: str,
    side: str,
    qty: int,
    lots: int,
    entry_px: float | None = None,
    stop_px: float | None = None,
    take_px: float | None = None,
    initial_atr: float | None = None,
    atr_mult: float | None = None,
    broker: str = "tinkoff",
    account_id: str | None = None,
    reason: str | None = None,
    exit_px: float | None = None,
) -> None:
    """Update tracked broker position after a fill.

    Positive side ('long'/'buy') increases exposure, negative side ('short'/'sell')
    decreases it. When the net quantity reaches zero the position is marked closed.
    `exit_px` is recorded when the closing fill is known (broker_order_poller
    observing a stop-order fill on the exchange); close_broker_position will
    also COALESCE-fill it if a later caller passes the same price.

    If `entry_px` is provided together with `initial_atr` and `atr_mult`, the
    stop/take are recalculated from the actual fill price using the standard
    ATR formula (long: entry - atr*mult / entry + atr*mult*target_rr).
    This corrects stops/targets that were calculated from the signal/limit
    price if the broker filled at a different price (audit 17 Aug 2026).
    Explicit `stop_px`/`take_px` passed by the caller win over recalculation.
    """
    db = await get_db()
    cursor = await db.execute(
        """SELECT id, qty, lots, avg_entry_px, stop_px, take_px, initial_atr, atr_mult
           FROM broker_positions
           WHERE ticker = ? AND broker = ? AND account_id = ? AND side = ? AND status = 'open'""",
        (ticker.upper(), broker, account_id or "", side),
    )
    row = await cursor.fetchone()
    sign = 1 if side in ("long", "buy") else -1
    delta_qty = sign * qty
    delta_lots = sign * lots
    if row is None:
        # No open row exists. Insert the new fill as a fresh open position.
        # If explicit stop_px/take_px weren't passed, compute them from the
        # fill price when initial_atr/atr_mult are available.
        cur_atr = initial_atr
        cur_mult = atr_mult
        cur_stop = stop_px
        cur_take = take_px
        if entry_px and cur_atr and cur_mult and (cur_stop is None or cur_take is None):
            try:
                from core.config import TARGET_RR
                risk = cur_atr * cur_mult
                target = risk * TARGET_RR
                if side in ("long", "buy"):
                    if cur_stop is None:
                        cur_stop = entry_px - risk
                    if cur_take is None:
                        cur_take = entry_px + target
                else:
                    if cur_stop is None:
                        cur_stop = entry_px + risk
                    if cur_take is None:
                        cur_take = entry_px - target
            except Exception:
                pass
        cur = await db.execute(
            """INSERT OR IGNORE INTO broker_positions
               (ticker, broker, account_id, side, qty, lots, avg_entry_px,
                stop_px, take_px, initial_atr, atr_mult, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (
                ticker.upper(), broker, account_id or "", side,
                delta_qty, delta_lots, entry_px,
                cur_stop, cur_take, cur_atr, cur_mult,
            ),
        )
        if cur.lastrowid == 0:
            # Another row with the same UNIQUE key already exists. Re-select
            # the open row for this (ticker, side) and fall through to the
            # update branch so we net into it instead of crashing.
            cursor = await db.execute(
                """SELECT id, qty, lots, avg_entry_px, stop_px, take_px, initial_atr, atr_mult
                   FROM broker_positions
                   WHERE ticker = ? AND broker = ? AND account_id = ? AND side = ? AND status = 'open'""",
                (ticker.upper(), broker, account_id or "", side),
            )
            row = await cursor.fetchone()
            if row is None:
                # No open row at all (e.g. the matching row is closed). The
                # legacy closed row is blocking the INSERT. Drop it and retry
                # so the new broker fill is actually recorded as an open
                # position — otherwise the trade exists at the broker but is
                # invisible to reconcile/stop_check and shows up as a phantom
                # in the UI (audit 14 Aug 2026).
                await db.execute(
                    """DELETE FROM broker_positions
                       WHERE ticker = ? AND broker = ? AND account_id = ?
                         AND side = ? AND status = 'closed'""",
                    (ticker.upper(), broker, account_id or "", side),
                )
                cur = await db.execute(
                    """INSERT INTO broker_positions
                       (ticker, broker, account_id, side, qty, lots, avg_entry_px,
                        stop_px, take_px, initial_atr, atr_mult, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
                    (
                        ticker.upper(), broker, account_id or "", side,
                        delta_qty, delta_lots, entry_px,
                        cur_stop, cur_take, cur_atr, cur_mult,
                    ),
                )
                await db.commit()
                return
        else:
            await db.commit()
            return
    new_qty = row[1] + delta_qty
    new_lots = row[2] + delta_lots
    if new_qty == 0:
        await db.execute(
            """UPDATE broker_positions
               SET status = 'closed', qty = 0, lots = 0, close_reason = ?,
                   exit_px = COALESCE(?, exit_px)
               WHERE id = ?""",
            (reason or "closed", exit_px, row[0]),
        )
    else:
        # Recompute stop/take from the real fill price when ATR info is on
        # hand. COALESCE means an explicit caller-supplied stop/take wins.
        cur_atr = initial_atr if initial_atr is not None else row[6]
        cur_mult = atr_mult if atr_mult is not None else row[7]
        cur_stop = stop_px
        cur_take = take_px
        if entry_px and cur_atr and cur_mult and (cur_stop is None or cur_take is None):
            try:
                from core.config import TARGET_RR
                risk = cur_atr * cur_mult
                target = risk * TARGET_RR
                if side in ("long", "buy"):
                    if cur_stop is None:
                        cur_stop = entry_px - risk
                    if cur_take is None:
                        cur_take = entry_px + target
                else:
                    if cur_stop is None:
                        cur_stop = entry_px + risk
                    if cur_take is None:
                        cur_take = entry_px - target
            except Exception:
                pass
        await db.execute(
            """UPDATE broker_positions
               SET qty = ?, lots = ?, avg_entry_px = COALESCE(?, avg_entry_px),
                   stop_px = COALESCE(?, stop_px),
                   take_px = COALESCE(?, take_px),
                   initial_atr = COALESCE(?, initial_atr),
                   atr_mult = COALESCE(?, atr_mult)
               WHERE id = ?""",
            (new_qty, new_lots, entry_px, cur_stop, cur_take, cur_atr, cur_mult, row[0]),
        )
    await db.commit()


async def update_broker_position_stop(
    pos_id: int, new_stop_px: float | None
) -> None:
    """Update the trailing stop price for an open broker position."""
    if new_stop_px is None or new_stop_px <= 0:
        return
    db = await get_db()
    await db.execute(
        """UPDATE broker_positions
           SET stop_px=?
           WHERE id=? AND status='open'""",
        (new_stop_px, pos_id),
    )
    await db.commit()


async def close_broker_position(
    ticker: str,
    broker: str = "tinkoff",
    account_id: str | None = None,
    reason: str = "manual",
    exit_px: float | None = None,
) -> None:
    """Mark a tracked broker position as closed and record the exit price.

    Idempotent: if the row is already closed (e.g. broker_order_poller flipped
    it via update_broker_position before the exit price was known) we backfill
    exit_px instead of dropping the row. The previous implementation ran
    `DELETE … WHERE status='closed'` before the open→closed UPDATE, which
    silently lost the position record when both states already coexisted in the
    table (audit 17 Aug 2026: SBER/GAZP/MTSS/VKCO/MGNT all had exit_px=NULL).
    """
    db = await get_db()
    # Flip the open row to closed (if any). COALESCE preserves any
    # close_reason / exit_px that update_broker_position may have written
    # earlier (e.g. broker poll observed the stop fill before this call).
    await db.execute(
        """UPDATE broker_positions
           SET status = 'closed',
               close_reason = COALESCE(?, close_reason),
               exit_px = COALESCE(?, exit_px)
           WHERE ticker = ? AND broker = ? AND account_id = ? AND status = 'open'""",
        (reason, exit_px, ticker.upper(), broker, account_id or ""),
    )
    # Backfill exit_px on an already-closed row sharing the same key. This is
    # the path that fires when broker_order_poller or update_broker_position
    # closed the row first with exit_px=NULL and the evening/intraday stop
    # check arrives with the real fill price a moment later.
    if exit_px is not None:
        await db.execute(
            """UPDATE broker_positions
               SET exit_px = COALESCE(?, exit_px),
                   close_reason = COALESCE(?, close_reason)
               WHERE ticker = ? AND broker = ? AND account_id = ? AND status = 'closed'""",
            (exit_px, reason, ticker.upper(), broker, account_id or ""),
        )
    await db.commit()


async def record_journal_entry(
    ticker: str,
    side: str,
    entry_px: float,
    qty: int,
    exit_px: float | None = None,
    stop_px: float | None = None,
    target_px: float | None = None,
    reason: str = "",
    notes: str = "",
) -> int | None:
    """Insert a journal row for a closed broker trade. Returns the new
    row id, or None if the inputs were insufficient. PnL is computed as
    (exit - entry) for long, (entry - exit) for short, multiplied by qty."""
    if not entry_px or not qty:
        return None
    pnl = None
    if exit_px is not None:
        sign = 1 if side in ("long", "buy") else -1
        pnl = round(sign * (exit_px - entry_px) * abs(qty), 2)
    db = await get_db()
    cur = await db.execute(
        """INSERT INTO journal
           (ts, ticker, side, entry_px, stop_px, target_px, qty, reason, exit_px, exit_ts, pnl, notes)
           VALUES (datetime('now','localtime'), ?, ?, ?, ?, ?, ?, ?, ?, datetime('now','localtime'), ?, ?)""",
        (
            ticker.upper(), side, entry_px, stop_px, target_px, abs(qty),
            reason, exit_px, pnl, notes,
        ),
    )
    await db.commit()
    return cur.lastrowid


async def get_broker_close_stats(date_str: str) -> dict:
    """Return stop_loss/take_profit/total/pnl summary for broker closes on the
    given date (YYYY-MM-DD). Joins broker_positions with journal so P&L is
    summed even when broker_positions.exit_px is NULL."""
    db = await get_db()
    cursor = await db.execute(
        """SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN bp.close_reason LIKE 'stop_loss%' THEN 1 ELSE 0 END) AS stop_loss,
              SUM(CASE WHEN bp.close_reason LIKE 'take_profit%' THEN 1 ELSE 0 END) AS take_profit,
              SUM(CASE WHEN bp.close_reason LIKE 'broker_stop%' THEN 1 ELSE 0 END) AS broker_stop,
              SUM(CASE WHEN bp.close_reason LIKE 'broker_take%' THEN 1 ELSE 0 END) AS broker_take,
              COALESCE(SUM(j.pnl), 0) AS pnl_rub
           FROM broker_positions bp
           LEFT JOIN journal j
             ON j.ticker = bp.ticker AND j.side = bp.side
            AND date(j.ts) = date(bp.ts)
           WHERE bp.status = 'closed'
             AND date(bp.ts) = date(?)""",
        (date_str,),
    )
    row = await cursor.fetchone()
    if not row:
        return {"total": 0, "pnl_rub": 0.0}
    return {
        "total": int(row[0] or 0),
        "stop_loss": int(row[1] or 0),
        "take_profit": int(row[2] or 0),
        "broker_stop": int(row[3] or 0),
        "broker_take": int(row[4] or 0),
        "pnl_rub": float(row[5] or 0),
    }


async def purge_phantom_broker_positions(
    broker: str,
    account_id: str,
    real_keys: set[tuple[str, str]],
) -> int:
    """Close `broker_positions` rows whose (ticker, side) is missing from
    `real_keys`. Used by the sandbox reconciliation flow: the broker is the
    source of truth, so phantom DB rows are marked closed rather than deleted
    (keeps the journal). Returns the number of rows closed."""
    db = await get_db()
    # Include leftover 'phantom_reconcile' rows from a prior partial reconcile
    # so they get cleaned up too. The two-step attempt could leave these behind
    # if it hit the same UNIQUE constraint.
    cursor = await db.execute(
        """SELECT id, ticker, side FROM broker_positions
           WHERE status IN ('open', 'phantom_reconcile')
             AND broker = ? AND account_id = ?""",
        (broker, account_id),
    )
    rows = await cursor.fetchall()
    phantom_ids = [r[0] for r in rows if (r[1].upper(), (r[2] or "long").lower()) not in real_keys]
    if phantom_ids:
        # Phantom rows (broker has no matching position) are DELETEd outright
        # rather than marked closed, because the UNIQUE constraint on
        # (ticker, broker, account_id, side, status) means a real closed row
        # from a prior stop_loss / take_profit would block the UPDATE.
        # Deletion is safe: these rows never had a real fill at the broker,
        # so they are not part of any historical P&L.
        placeholders = ",".join("?" for _ in phantom_ids)
        await db.execute(
            f"""DELETE FROM broker_positions WHERE id IN ({placeholders})""",
            phantom_ids,
        )
        await db.commit()
    return len(phantom_ids)


async def clear_broker_positions_for_account(account_id: str) -> int:
    """Wipe all broker_positions rows for the given account. Used by the
    sandbox reset endpoint right before the account gets recreated."""
    db = await get_db()
    cursor = await db.execute(
        "DELETE FROM broker_positions WHERE account_id = ?", (account_id,)
    )
    await db.commit()
    return cursor.rowcount or 0


async def clear_journal_for_environment(environment: str) -> int:
    """Wipe journal rows whose `notes` mentions the given environment.

    The journal table does not have an environment column, so we match on
    `notes LIKE '%<env>%'`. Sandbox trades are recorded with notes like
    'evening_broker_check', 'intraday_broker_reconcile' or 'backfill from
    Tinkoff ops'; we conservatively match any 'sandbox' substring in notes
    plus our backfill prefix."""
    db = await get_db()
    like = f"%{environment}%"
    cursor = await db.execute(
        """DELETE FROM journal
           WHERE notes LIKE ?
              OR notes LIKE '%Tinkoff ops%'
              OR notes LIKE '%evening_broker_check%'
              OR notes LIKE '%intraday_broker_reconcile%'
              OR notes LIKE '%manual_close%'""",
        (like,),
    )
    await db.commit()
    return cursor.rowcount or 0


async def clear_broker_orders_for_environment(environment: str) -> int:
    """Wipe broker_orders rows for the given environment ('sandbox' / 'live')."""
    db = await get_db()
    cursor = await db.execute(
        "DELETE FROM broker_orders WHERE environment = ?", (environment,)
    )
    await db.commit()
    return cursor.rowcount or 0


# ── User settings ─────────────────────────────────────────────


async def get_setting(key: str, default: str | None = None) -> str | None:
    """Read a single user setting by key."""
    db = await get_db()
    cursor = await db.execute("SELECT value FROM user_settings WHERE key=?", (key,))
    row = await cursor.fetchone()
    return row[0] if row else default


async def set_setting(key: str, value: str) -> None:
    """Upsert a user setting."""
    db = await get_db()
    await db.execute(
        """INSERT INTO user_settings (key, value, updated_at)
           VALUES (?, ?, datetime('now','localtime'))
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                          updated_at=excluded.updated_at""",
        (key, value),
    )
    await db.commit()


async def load_auto_trading_enabled(default: bool = True) -> bool:
    """Load the auto-trading toggle from DB, seeding the default if missing."""
    val = await get_setting("auto_trading_enabled")
    if val is None:
        await set_setting("auto_trading_enabled", "true" if default else "false")
        return default
    return val.lower() == "true"


async def save_auto_trading_enabled(enabled: bool) -> None:
    """Persist the auto-trading toggle to DB."""
    await set_setting("auto_trading_enabled", "true" if enabled else "false")


async def load_auto_trade_enabled(default: bool = False) -> bool:
    """Load the auto-trade toggle from DB, seeding the default if missing.

    Auto-tradecontrols whether the broker_order_executor picks up
    pending proposals without manual confirmation. Default is false
    so the robot stays in semi-auto mode out of the box.
    """
    val = await get_setting("auto_trade_enabled")
    if val is None:
        await set_setting("auto_trade_enabled", "true" if default else "false")
        return default
    return val.lower() == "true"


async def save_auto_trade_enabled(enabled: bool) -> None:
    """Persist the auto-trade toggle to DB."""
    await set_setting("auto_trade_enabled", "true" if enabled else "false")

"""Backtest интрадей-сигналов MOEX Assistant на 5-минутных свечах.

Собирает 5-минутные свечи из минутных (1m), затем прогоняет технический
скоринг IntradayAgent и проверяет, куда цена ушла через 30 и 60 минут
внутри той же сессии.

Запуск:
    python scripts/intraday_backtest_5m.py --tickers SBER,GAZP --threshold 60
    python scripts/intraday_backtest_5m.py --save --llm --max-workers 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

import sys

# Allow running from project root or scripts/ dir
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from core.config import WATCHLIST
from core.intraday_agent import IntradayAgent
from core.moex import MoexClient


MIN_SESSION_CANDLES = 30
WARMUP_CANDLES = 29
DEFAULT_HORIZONS = [6, 12]  # 30m and 60m in 5m candles


def _resample_1m_to_5m(candles_1m: list[dict]) -> list[dict]:
    """Build 5-minute OHLCV candles from 1-minute MOEX candles."""
    if not candles_1m:
        return []

    df = pd.DataFrame(candles_1m)
    if "begin" in df.columns:
        df = df.rename(columns={"begin": "ts"})
    elif "end" in df.columns and "ts" not in df.columns:
        df = df.rename(columns={"end": "ts"})
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.set_index("ts").sort_index()

    for col in ("open", "high", "low", "close", "volume", "value"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Resample to 5-minute buckets
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    if "value" in df.columns:
        agg["value"] = "sum"

    resampled = df.resample("5min").agg(agg)
    resampled = resampled.dropna(subset=["open", "high", "low", "close"])

    records = []
    for ts, row in resampled.iterrows():
        record = {"begin": ts.strftime("%Y-%m-%d %H:%M:%S")}
        for col in ("open", "high", "low", "close", "volume", "value"):
            if col in row.index and pd.notna(row[col]):
                record[col] = float(row[col])
        records.append(record)

    return records


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest intraday signals on 5m candles")
    parser.add_argument(
        "--tickers",
        type=str,
        default=",".join(WATCHLIST),
        help="Comma-separated tickers (default: WATCHLIST)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1500,
        help="1m candles to fetch per ticker (default: 1500). Roughly 3-4 trading days.",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=50,
        help="Minimum confidence to include signal (default: 50)",
    )
    parser.add_argument(
        "--horizons",
        type=str,
        default=",".join(str(h) for h in DEFAULT_HORIZONS),
        help="Forward horizons in 5m candles, comma-separated (default: 6,12)",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Enable LLM cascade (default: deterministic technical scoring only)",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save results to intraday_backtest_5m table in the database",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=5,
        help="Concurrent MOEX requests (default: 5)",
    )
    parser.add_argument(
        "--stop-mult",
        type=float,
        default=None,
        help="Override ATR multiplier for stop-loss (default: use agent levels)",
    )
    parser.add_argument(
        "--take-mult",
        type=float,
        default=None,
        help="Override ATR multiplier for take-profit (default: use agent levels)",
    )
    return parser.parse_args()


def _split_sessions(candles: list[dict]) -> list[list[dict]]:
    """Split candles into trading sessions by ts.date()."""
    if not candles:
        return []
    sessions: dict[str, list[dict]] = defaultdict(list)
    for c in candles:
        ts = c.get("begin") or c.get("ts")
        if not ts:
            continue
        try:
            day = str(ts)[:10]
        except Exception:
            continue
        sessions[day].append(c)
    return [session for session in sessions.values() if len(session) >= MIN_SESSION_CANDLES]


def _daily_ohlc_up_to(session: list[dict], idx: int) -> dict[str, float]:
    """Build daily OHLC using only candles 0..idx (no peeking)."""
    slice_ = session[: idx + 1]
    opens = [c["open"] for c in slice_ if c.get("open") is not None]
    highs = [c["high"] for c in slice_ if c.get("high") is not None]
    lows = [c["low"] for c in slice_ if c.get("low") is not None]
    closes = [c["close"] for c in slice_ if c.get("close") is not None]
    return {
        "open": float(opens[0]),
        "high": float(max(highs)) if highs else float(closes[-1]),
        "low": float(min(lows)) if lows else float(closes[-1]),
        "close": float(closes[-1]),
    }


def _classify_result(direction: str, current_close: float, exit_px: float) -> str:
    if direction == "long":
        return "correct" if exit_px > current_close else "wrong"
    if direction == "short":
        return "correct" if exit_px < current_close else "wrong"
    return "wrong"


def _override_levels(
    direction: str,
    close: float,
    atr: float | None,
    stop: float | None,
    take: float | None,
    stop_mult: float | None,
    take_mult: float | None,
) -> tuple[float | None, float | None]:
    """Return stop/take levels, optionally overriding ATR multipliers."""
    if atr is None or atr <= 0:
        return stop, take

    effective_stop = stop
    effective_take = take

    if direction == "long":
        if stop_mult is not None:
            effective_stop = round(close - atr * stop_mult, 2)
        if take_mult is not None:
            effective_take = round(close + atr * take_mult, 2)
    elif direction == "short":
        if stop_mult is not None:
            effective_stop = round(close + atr * stop_mult, 2)
        if take_mult is not None:
            effective_take = round(close - atr * take_mult, 2)

    return effective_stop, effective_take


def _first_touch_exit(
    direction: str,
    entry: float | None,
    stop: float | None,
    take: float | None,
    window: list[dict],
) -> tuple[float, str]:
    """Return exit price and reason based on first touched stop/take level.

    If neither stop nor take is hit within the window, returns the closing
    price of the last candle and reason 'hold'.
    """
    if not window:
        return 0.0, "no_data"

    # Use entry as current close if levels are missing
    reference = entry if entry is not None else float(window[0].get("close", 0))

    for candle in window:
        high = float(candle.get("high", 0))
        low = float(candle.get("low", 0))

        if direction == "long":
            if stop is not None and low <= stop:
                return stop, "stop_loss"
            if take is not None and high >= take:
                return take, "take_profit"
        elif direction == "short":
            if stop is not None and high >= stop:
                return stop, "stop_loss"
            if take is not None and low <= take:
                return take, "take_profit"

    return float(window[-1].get("close", reference)), "hold"


async def _run_backtest_for_ticker(
    agent: IntradayAgent,
    candles: list[dict],
    ticker: str,
    horizons: list[int],
    threshold: int,
    use_llm: bool,
    stop_mult: float | None,
    take_mult: float | None,
) -> list[dict]:
    if not candles:
        print(f"[{ticker}] нет 5-минутных данных")
        return []

    sessions = _split_sessions(candles)
    records: list[dict] = []
    max_horizon = max(horizons)

    for session in sessions:
        if len(session) < MIN_SESSION_CANDLES + max_horizon + 1:
            continue
        for i in range(WARMUP_CANDLES, len(session) - max_horizon):
            daily_ohlc = _daily_ohlc_up_to(session, i)
            result = await agent.analyze(
                ticker=ticker,
                candles_5m=session[: i + 1],
                order_book=None,
                daily_ohlc=daily_ohlc,
                use_llm=use_llm,
                calibrate=False,
            )
            if result.signal == "no_signal":
                continue
            if result.direction == "neutral":
                continue
            if result.confidence < threshold:
                continue

            current = session[i]
            current_close = float(current["close"])
            for h in horizons:
                future_window = session[i + 1 : i + h + 1]
                stop_px, take_px = _override_levels(
                    result.direction,
                    current_close,
                    result.atr,
                    result.stop,
                    result.take,
                    stop_mult,
                    take_mult,
                )

                exit_px, exit_reason = _first_touch_exit(
                    direction=result.direction,
                    entry=result.entry,
                    stop=stop_px,
                    take=take_px,
                    window=future_window,
                )
                future_close = float(future_window[-1]["close"])
                outcome = _classify_result(result.direction, current_close, exit_px)
                return_pct = (exit_px / current_close - 1) * 100

                records.append({
                    "ticker": ticker.upper(),
                    "signal_ts": str(current.get("begin") or current.get("ts")),
                    "signal": result.signal,
                    "direction": result.direction,
                    "confidence": result.confidence,
                    "entry_px": result.entry,
                    "stop_px": stop_px,
                    "take_px": take_px,
                    "open_px": current["open"],
                    "close_px": current_close,
                    "horizon_candles": h,
                    "future_px": future_close,
                    "exit_px": exit_px,
                    "exit_reason": exit_reason,
                    "return_pct": return_pct,
                    "result": outcome,
                    "signals_used": result.signals_used,
                    "reason": result.reason,
                    "llm_used": use_llm,
                })
    return records


async def _fetch_all(
    client: MoexClient,
    tickers: list[str],
    count: int,
    max_workers: int,
) -> list[list[dict]]:
    sem = asyncio.Semaphore(max_workers)

    async def _fetch(ticker: str) -> list[dict]:
        async with sem:
            candles_1m = await client.candles_recent(ticker.upper(), interval="1m", count=count)
            return _resample_1m_to_5m(candles_1m)

    return await asyncio.gather(*[_fetch(t) for t in tickers])


def _fmt_pct(n: float) -> str:
    return f"{n:.1f}%"


def _accuracy(correct: int, wrong: int) -> float:
    total = correct + wrong
    return correct / total * 100 if total else 0.0


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _print_report(records: list[dict], horizons: list[int], threshold: int) -> None:
    print("=" * 70)
    print("BACKTEST: ИНТРАДЕЙ-СИГНАЛЫ (5-минутные свечи)")
    print("=" * 70)
    print(f"Всего сигналов: {len(records)} | confidence >= {threshold} | LLM: {'да' if records and records[0].get('llm_used') else 'нет'}")
    print()

    if not records:
        print("Нет сигналов для отчёта.")
        return

    for h in horizons:
        h_records = [r for r in records if r["horizon_candles"] == h]
        print(f"--- Горизонт {h*5} минут ({h} свечей) ---")
        if not h_records:
            print("  нет данных")
            print()
            continue

        total_correct = sum(1 for r in h_records if r["result"] == "correct")
        total_wrong = len(h_records) - total_correct
        returns = [r["return_pct"] for r in h_records]
        print(
            f"  Всего сигналов: {len(h_records):3d} | "
            f"точность {_accuracy(total_correct, total_wrong):5.1f}% "
            f"(correct={total_correct}, wrong={total_wrong})"
        )
        print(
            f"  Средняя доходность: {_avg(returns):+.3f}% | "
            f"медиана: {median(returns):+.3f}% | "
            f"min: {min(returns):+.3f}% | max: {max(returns):+.3f}%"
        )

        # Exit reasons
        print("  По причине выхода:")
        for reason in ("stop_loss", "take_profit", "hold"):
            subset = [r for r in h_records if r.get("exit_reason") == reason]
            if not subset:
                continue
            c = sum(1 for r in subset if r["result"] == "correct")
            w = len(subset) - c
            print(
                f"    {reason:14s}: {len(subset):3d} сигналов, "
                f"точность {_accuracy(c, w):5.1f}% (c={c}, w={w}), "
                f"avg ret={_avg([r['return_pct'] for r in subset]):+.3f}%"
            )
        print()

        # By signal type
        print("  По типу сигнала:")
        for signal in ("bounce_up", "bounce_down", "continuation"):
            subset = [r for r in h_records if r["signal"] == signal]
            if not subset:
                print(f"    {signal:16s}: нет данных")
                continue
            c = sum(1 for r in subset if r["result"] == "correct")
            w = len(subset) - c
            print(
                f"    {signal:16s}: {len(subset):3d} сигналов, "
                f"точность {_accuracy(c, w):5.1f}% (c={c}, w={w}), "
                f"avg ret={_avg([r['return_pct'] for r in subset]):+.3f}%"
            )

        # By direction
        print("  По направлению:")
        for direction in ("long", "short"):
            subset = [r for r in h_records if r["direction"] == direction]
            if not subset:
                continue
            c = sum(1 for r in subset if r["result"] == "correct")
            w = len(subset) - c
            print(
                f"    {direction:6s}: {len(subset):3d} сигналов, "
                f"точность {_accuracy(c, w):5.1f}% (c={c}, w={w})"
            )

        # Confidence buckets
        print("  По уверенности:")
        buckets = [
            ("<50", lambda conf: conf < 50),
            ("50-69", lambda conf: 50 <= conf < 70),
            ("70-79", lambda conf: 70 <= conf < 80),
            (">=80", lambda conf: conf >= 80),
        ]
        for label, pred in buckets:
            subset = [r for r in h_records if pred(r["confidence"])]
            if not subset:
                print(f"    {label:8s}: нет данных")
                continue
            c = sum(1 for r in subset if r["result"] == "correct")
            w = len(subset) - c
            print(
                f"    {label:8s}: {len(subset):3d} сигналов, "
                f"точность {_accuracy(c, w):5.1f}% (c={c}, w={w})"
            )
        print()

    # Win/loss ratio
    print("--- Соотношение прибыли и убытка ---")
    positive_returns = [r["return_pct"] for r in records if r["return_pct"] > 0]
    negative_returns = [r["return_pct"] for r in records if r["return_pct"] < 0]
    avg_win = _avg(positive_returns)
    avg_loss = _avg(negative_returns)
    print(f"  Средний плюс: {avg_win:+.3f}% | Средний минус: {avg_loss:+.3f}%")
    print(f"  Плюсовых сделок: {len(positive_returns)} | Минусовых: {len(negative_returns)}")
    if avg_loss != 0:
        print(f"  Win/Loss ratio: {abs(avg_win / avg_loss):.2f}")
    print()

    # Per-ticker stats
    print("--- Топ тикеров по количеству сигналов ---")
    ticker_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0, "correct": 0})
    for r in records:
        ticker_stats[r["ticker"]]["total"] += 1
        if r["result"] == "correct":
            ticker_stats[r["ticker"]]["correct"] += 1
    rows = [
        (ticker, s["total"], _accuracy(s["correct"], s["total"] - s["correct"]))
        for ticker, s in ticker_stats.items()
        if s["total"] >= 5
    ]
    rows.sort(key=lambda x: x[1], reverse=True)
    for ticker, total, pct in rows[:15]:
        print(f"  {ticker}: {total} сигналов, точность {pct:.1f}%")
    print()

    # Distribution
    print("--- Распределение сигналов ---")
    signal_counts: dict[str, int] = defaultdict(int)
    for r in records:
        signal_counts[r["signal"]] += 1
    for signal, count in sorted(signal_counts.items(), key=lambda x: x[1], reverse=True):
        print(f"  {signal}: {count}")
    print()

    # Samples
    print("--- Последние 10 сигналов ---")
    for r in records[-10:]:
        print(
            f"  {r['ticker']} @ {r['signal_ts']} | {r['signal']} {r['direction']} "
            f"conf={r['confidence']} | h={r['horizon_candles']} "
            f"ret={r['return_pct']:+.3f}% result={r['result']}"
        )
    print()


async def main() -> None:
    args = _parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    horizons = sorted({int(h.strip()) for h in args.horizons.split(",") if h.strip()})
    if not horizons:
        horizons = DEFAULT_HORIZONS

    agent = IntradayAgent()
    client = MoexClient()
    all_records: list[dict] = []

    try:
        print(f"Загружаю 1-минутные свечи для {len(tickers)} тикеров и собираю 5-минутки...")
        raw_data = await _fetch_all(client, tickers, args.count, args.max_workers)
        total_5m = sum(len(d) for d in raw_data)
        print(f"Загружено и агрегировано {total_5m} 5-минутных свечей. Прогоняю backtest...")

        for ticker, candles in zip(tickers, raw_data):
            if not candles:
                continue
            recs = await _run_backtest_for_ticker(
                agent, candles, ticker, horizons, args.threshold, args.llm,
                stop_mult=args.stop_mult, take_mult=args.take_mult,
            )
            all_records.extend(recs)
    finally:
        await client.close()

    _print_report(all_records, horizons, args.threshold)

    if args.save and all_records:
        from core import db
        inserted = await db.save_intraday_backtest(all_records)
        print(f"Сохранено записей в БД: {inserted}")


if __name__ == "__main__":
    asyncio.run(main())

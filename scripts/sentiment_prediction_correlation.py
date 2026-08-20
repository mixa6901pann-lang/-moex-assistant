"""Анализ корреляции новостного сентимента с точностью прогнозов.

Связывает таблицы `sentiment` и `predictions`, оценивает:
- Совпадает ли направление sentiment (bullish/bearish) с predicted_direction.
- Как влияет возраст новости (age_minutes) и вес (weight) на result_1d/3d/7d.
- Общую точность прогнозов при наличии свежих новостей vs без них.

Запуск:
    python scripts/sentiment_prediction_correlation.py
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

# Allow running from project root or scripts/ dir
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "moex.db"

# Date when the latest sentiment-signal changes were deployed.
# Used to compare accuracy "before" vs "after" the fix.
CUTOVER_DATE = datetime(2026, 6, 26, 22, 19)


def _fmt_pct(n: float) -> str:
    return f"{n:.1f}%"


async def analyze() -> None:
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row

    # 1. Ensure required columns exist
    cursor = await db.execute("PRAGMA table_info(sentiment)")
    sentiment_cols = {r["name"] for r in await cursor.fetchall()}
    if "weight" not in sentiment_cols or "age_minutes" not in sentiment_cols:
        print(
            "Таблица sentiment не содержит колонок weight/age_minutes. "
            "Сначала обнови схему (core/db.py)."
        )
        await db.close()
        return

    # 2. Load latest sentiment per ticker and predictions separately,
    # then match each prediction to the freshest sentiment that existed before it.
    cursor = await db.execute(
        """
        SELECT
            id, ticker, ts, sentiment, confidence, age_minutes, weight, headline, source
        FROM sentiment
        WHERE datetime(ts) > datetime('now', '-90 days')
        ORDER BY ts DESC
        """
    )
    sentiment_rows = await cursor.fetchall()

    # Build per-ticker list of sentiment records (newest first)
    sentiments_by_ticker: dict[str, list[dict]] = defaultdict(list)
    for r in sentiment_rows:
        sentiments_by_ticker[r["ticker"]].append({
            "ts": r["ts"],
            "sentiment": r["sentiment"],
            "confidence": r["confidence"],
            "age_minutes": r["age_minutes"],
            "weight": r["weight"],
            "headline": r["headline"],
            "source": r["source"],
        })

    cursor = await db.execute(
        """
        SELECT
            id,
            ticker,
            ts AS pred_ts,
            predicted_direction,
            predicted_strength,
            result_1d,
            result_3d,
            result_7d
        FROM predictions
        WHERE datetime(ts) > datetime('now', '-90 days')
          AND predicted_direction IN ('up', 'down', 'long', 'short', 'bullish', 'bearish', 'buy', 'sell')
        ORDER BY ts DESC
        """
    )
    pred_rows = await cursor.fetchall()

    if not pred_rows:
        print("Нет прогнозов за последние 90 дней.")
        await db.close()
        return

    # Normalize directions
    def _norm_dir(d: str | None) -> str | None:
        if not d:
            return None
        d = d.lower()
        if d in ("up", "long", "bullish", "buy"):
            return "bullish"
        if d in ("down", "short", "bearish", "sell"):
            return "bearish"
        return None

    def _find_matching_sentiment(ticker: str, pred_ts: str) -> dict | None:
        """Return freshest sentiment for ticker that is not newer than the prediction."""
        for s in sentiments_by_ticker.get(ticker, []):
            if s["ts"] <= pred_ts:
                return s
        return None

    # 3. Aggregate stats
    horizon_cols = {
        "1d": "result_1d",
        "3d": "result_3d",
        "7d": "result_7d",
    }

    def _is_after_cutover(pred_ts: str) -> bool:
        try:
            ts = datetime.fromisoformat(pred_ts.replace("Z", "+00:00"))
        except Exception:
            return False
        return ts >= CUTOVER_DATE

    # buckets: all, with_sentiment, sentiment_agrees, sentiment_disagrees
    totals: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"correct": 0, "wrong": 0, "pending": 0})
    )
    totals_after: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"correct": 0, "wrong": 0, "pending": 0})
    )
    last7_totals: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"correct": 0, "wrong": 0, "pending": 0})
    )
    by_weight: dict[str, list[tuple[float, str]]] = {h: [] for h in horizon_cols}
    by_age: dict[str, list[tuple[int | None, str]]] = {h: [] for h in horizon_cols}
    by_confidence: dict[str, list[tuple[int, str]]] = {h: [] for h in horizon_cols}

    # Track sentiment metadata per bucket so we can report average confidence/weight/age
    bucket_metadata: dict[str, dict[str, dict[str, list[Any]]]] = defaultdict(
        lambda: {h: {"weights": [], "confidences": [], "ages": []} for h in horizon_cols}
    )
    bucket_metadata_after: dict[str, dict[str, dict[str, list[Any]]]] = defaultdict(
        lambda: {h: {"weights": [], "confidences": [], "ages": []} for h in horizon_cols}
    )
    bucket_metadata_last7: dict[str, dict[str, dict[str, list[Any]]]] = defaultdict(
        lambda: {h: {"weights": [], "confidences": [], "ages": []} for h in horizon_cols}
    )

    ticker_stats: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: {h: {"correct": 0, "wrong": 0, "pending": 0} for h in horizon_cols}
    )

    for r in pred_rows:
        pred_dir = _norm_dir(r["predicted_direction"])
        ticker = r["ticker"]
        matched = _find_matching_sentiment(ticker, r["pred_ts"])
        sent = matched["sentiment"] if matched else None
        sent_dir = _norm_dir(sent)
        weight = matched["weight"] if matched else 1.0
        age = matched["age_minutes"] if matched else None
        conf = matched["confidence"] if matched else 0

        has_sentiment = sent_dir is not None
        agrees = has_sentiment and pred_dir == sent_dir
        disagrees = has_sentiment and pred_dir != sent_dir

        after_cutover = _is_after_cutover(r["pred_ts"])
        within_last7 = False
        try:
            pred_dt = datetime.fromisoformat(r["pred_ts"].replace("Z", "+00:00"))
            within_last7 = pred_dt >= CUTOVER_DATE - timedelta(days=7)
        except Exception:
            pass

        for horizon, col in horizon_cols.items():
            res = r[col] or "pending"
            if res not in ("correct", "wrong"):
                continue

            totals["all"][horizon][res] += 1
            ticker_stats[ticker][horizon][res] += 1

            if after_cutover:
                totals_after["all"][horizon][res] += 1
            if within_last7:
                last7_totals["all"][horizon][res] += 1

            def _append_metadata(container: dict[str, dict[str, dict[str, list[Any]]]], bucket: str) -> None:
                if has_sentiment:
                    container[bucket][horizon]["weights"].append(weight)
                    container[bucket][horizon]["confidences"].append(conf)
                    if age is not None:
                        container[bucket][horizon]["ages"].append(age)

            if has_sentiment:
                totals["with_sentiment"][horizon][res] += 1
                by_weight[horizon].append((weight, res))
                by_age[horizon].append((age, res))
                by_confidence[horizon].append((conf, res))
                _append_metadata(bucket_metadata, "with_sentiment")
                if agrees:
                    totals["sentiment_agrees"][horizon][res] += 1
                    _append_metadata(bucket_metadata, "sentiment_agrees")
                elif disagrees:
                    totals["sentiment_disagrees"][horizon][res] += 1
                    _append_metadata(bucket_metadata, "sentiment_disagrees")

                if after_cutover:
                    totals_after["with_sentiment"][horizon][res] += 1
                    _append_metadata(bucket_metadata_after, "with_sentiment")
                    if agrees:
                        totals_after["sentiment_agrees"][horizon][res] += 1
                        _append_metadata(bucket_metadata_after, "sentiment_agrees")
                    elif disagrees:
                        totals_after["sentiment_disagrees"][horizon][res] += 1
                        _append_metadata(bucket_metadata_after, "sentiment_disagrees")
                if within_last7:
                    last7_totals["with_sentiment"][horizon][res] += 1
                    _append_metadata(bucket_metadata_last7, "with_sentiment")
                    if agrees:
                        last7_totals["sentiment_agrees"][horizon][res] += 1
                        _append_metadata(bucket_metadata_last7, "sentiment_agrees")
                    elif disagrees:
                        last7_totals["sentiment_disagrees"][horizon][res] += 1
                        _append_metadata(bucket_metadata_last7, "sentiment_disagrees")
            else:
                totals["without_sentiment"][horizon][res] += 1
                if after_cutover:
                    totals_after["without_sentiment"][horizon][res] += 1
                if within_last7:
                    last7_totals["without_sentiment"][horizon][res] += 1

    # 4. Print report
    print("=" * 70)
    print("АНАЛИЗ: ВЛИЯНИЕ НОВОСТНОГО СЕНТИМЕНТА НА ТОЧНОСТЬ ПРОГНОЗОВ")
    print("=" * 70)
    print(f"Всего прогнозов за 90 дней: {len(pred_rows)}")
    print(f"Дата обновления логики сентимента: {CUTOVER_DATE.isoformat()}")
    print()

    def _pct_bucket(bucket: dict[str, int]) -> float:
        total = bucket["correct"] + bucket["wrong"]
        if total == 0:
            return 0.0
        return bucket["correct"] / total * 100

    def _avg(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    def _print_bucket_section(
        title: str,
        data: dict[str, dict[str, dict[str, int]]],
        horizons: dict[str, str],
        meta: dict[str, dict[str, dict[str, list[Any]]]] | None = None,
    ) -> None:
        print(f"--- {title} ---")
        for horizon in horizons:
            print(f"  Горизонт {horizon}:")
            for bucket_name in [
                "all",
                "without_sentiment",
                "with_sentiment",
                "sentiment_agrees",
                "sentiment_disagrees",
            ]:
                b = data.get(bucket_name, {}).get(horizon, {"correct": 0, "wrong": 0})
                total = b["correct"] + b["wrong"]
                line = (
                    f"    {bucket_name:22s}: {total:3d} прогнозов, "
                    f"точность {_pct_bucket(b):5.1f}% "
                    f"(correct={b['correct']}, wrong={b['wrong']})"
                )
                if meta and bucket_name in ("with_sentiment", "sentiment_agrees", "sentiment_disagrees"):
                    m = meta.get(bucket_name, {}).get(horizon, {"weights": [], "confidences": [], "ages": []})
                    avg_w = _avg(m["weights"])
                    avg_c = _avg(m["confidences"])
                    avg_a = _avg([float(a) for a in m["ages"]]) if m["ages"] else 0.0
                    line += f" | avg conf={avg_c:.1f}, weight={avg_w:.2f}, age={avg_a:.0f}min"
                print(line)
            print()

    _print_bucket_section("Все данные (90 дней)", totals, horizon_cols, bucket_metadata)
    if any(totals_after[b][h]["correct"] + totals_after[b][h]["wrong"] for b in totals_after for h in horizon_cols):
        _print_bucket_section("После обновления логики сентимента", totals_after, horizon_cols, bucket_metadata_after)
    _print_bucket_section("Последние 7 дней", last7_totals, horizon_cols, bucket_metadata_last7)

    # 5. Weighted accuracy correlation
    print("--- Корреляция по весу новости (weight) ---")
    for horizon in horizon_cols:
        pairs = by_weight[horizon]
        if not pairs:
            print(f"  {horizon}: недостаточно данных")
            continue
        high_w = [p for p in pairs if p[0] >= 0.75]
        mid_w = [p for p in pairs if 0.25 <= p[0] < 0.75]
        low_w = [p for p in pairs if p[0] < 0.25]
        for label, subset in [("высокий (>=0.75)", high_w), ("средний (0.25-0.75)", mid_w), ("низкий (<0.25)", low_w)]:
            correct = sum(1 for _, r in subset if r == "correct")
            total = len(subset)
            pct = correct / total * 100 if total else 0
            print(f"  {horizon} {label:20s}: {total:3d} прогнозов, точность {pct:5.1f}%")
        print()

    # 6. Age correlation
    print("--- Корреляция по возрасту новости (age_minutes) ---")
    for horizon in horizon_cols:
        pairs = [(a, r) for a, r in by_age[horizon] if a is not None]
        if not pairs:
            print(f"  {horizon}: недостаточно данных")
            continue
        fresh = [p for p in pairs if p[0] <= 60]
        recent = [p for p in pairs if 60 < p[0] <= 360]
        stale = [p for p in pairs if 360 < p[0] <= 1440]
        very_stale = [p for p in pairs if p[0] > 1440]
        for label, subset in [
            ("<=1 час", fresh),
            ("1-6 часов", recent),
            ("6-24 часа", stale),
            (">24 часов", very_stale),
        ]:
            correct = sum(1 for _, r in subset if r == "correct")
            total = len(subset)
            pct = correct / total * 100 if total else 0
            print(f"  {horizon} {label:15s}: {total:3d} прогнозов, точность {pct:5.1f}%")
        print()

    # 7. Confidence correlation
    print("--- Корреляция по уверенности LLM (confidence) ---")
    for horizon in horizon_cols:
        pairs = by_confidence[horizon]
        if not pairs:
            print(f"  {horizon}: недостаточно данных")
            continue
        high = [p for p in pairs if p[0] >= 70]
        mid = [p for p in pairs if 40 <= p[0] < 70]
        low = [p for p in pairs if p[0] < 40]
        for label, subset in [("высокая (>=70)", high), ("средняя (40-69)", mid), ("низкая (<40)", low)]:
            correct = sum(1 for _, r in subset if r == "correct")
            total = len(subset)
            pct = correct / total * 100 if total else 0
            print(f"  {horizon} {label:18s}: {total:3d} прогнозов, точность {pct:5.1f}%")
        print()

    # 8. Per-ticker top movers
    print("--- Топ тикеров по количеству прогнозов (7d) ---")
    ticker_rows = []
    for ticker, stats in ticker_stats.items():
        b = stats["7d"]
        total = b["correct"] + b["wrong"]
        if total >= 3:
            pct = b["correct"] / total * 100
            ticker_rows.append((ticker, total, pct))
    ticker_rows.sort(key=lambda x: x[2], reverse=True)
    for ticker, total, pct in ticker_rows[:10]:
        print(f"  {ticker}: {total} прогнозов, точность 7d {pct:.1f}%")
    print()

    # 9. Raw sample rows for manual inspection
    print("--- Примеры связанных записей (последние 10) ---")
    samples = []
    for r in pred_rows:
        matched = _find_matching_sentiment(r["ticker"], r["pred_ts"])
        if matched:
            samples.append((r, matched))
        if len(samples) >= 10:
            break
    for r, s in samples:
        print(
            f"  {r['ticker']} @ {r['pred_ts']}: "
            f"pred={r['predicted_direction']}, "
            f"sent={s['sentiment']}({s['confidence']}%), "
            f"age={s['age_minutes']}min, weight={s['weight']:.2f}, "
            f"1d={r['result_1d']}, 3d={r['result_3d']}, 7d={r['result_7d']}"
        )
    print()

    await db.close()


if __name__ == "__main__":
    asyncio.run(analyze())

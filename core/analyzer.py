"""LLM-powered analysis for trade context and news sentiment.

Supports Anthropic (Claude) and local Ollama models.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.llm import call_llm, call_llm_with_fallback, SYSTEM_PROMPT, render_system_prompt, sanitize_for_llm
from core.sentiment_agent import agent as sentiment_agent


async def analyze_ticker(
    ticker: str,
    price_data: dict[str, Any],
    signals: list[str],
    news: str = "",
    macro: dict[str, Any] | None = None,
    bounce_prediction: dict[str, Any] | None = None,
    latest_sentiment: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Get LLM analysis of a specific ticker situation plus Gemma critique."""
    direction = price_data.get("direction")
    strength = price_data.get("strength")
    recommendation = price_data.get("recommendation")
    reason = price_data.get("reason", "")
    warnings = price_data.get("warnings", [])

    if not recommendation:
        score = price_data.get("score", 50)
        if score is None:
            score = 50
        if score >= 70:
            recommendation = "ЛОНГ (сильно)"
        elif score >= 55:
            recommendation = "ЛОНГ (умеренно)"
        elif score <= 30:
            recommendation = "ШОРТ (сильно)"
        elif score <= 45:
            recommendation = "ШОРТ (умеренно)"
        else:
            recommendation = "НЕЙТРАЛЬНО"

    details = price_data.get("details") or {}
    for key in ("close", "rsi", "macd_hist", "atr", "adx", "vol_ratio",
                "bb_pct", "sma_20", "sma_50", "div_yield", "di_plus", "di_minus",
                "fib_levels", "bounce", "rs_vs_index", "avg_volume_20d"):
        if price_data.get(key) is None and details.get(key) is not None:
            price_data[key] = details[key]

    # Sanitize news to avoid triggering cloud LLM safety filters
    safe_news = sanitize_for_llm(news.strip()) if news else ""

    # Sentiment analysis from news (for LLM context — math already includes it)
    sentiment_block = ""
    if safe_news:
        try:
            sres = await sentiment_agent.analyze(ticker, [safe_news])
            sentiment_block = (
                f"\nНовостной сентимент: {sres.sentiment} (уверенность {sres.confidence}%). "
                f"Темы: {', '.join(sres.key_topics) or 'нет'}. "
                f"Риски: {', '.join(sres.risk_flags) or 'нет'}. "
                f"Резюме: {sres.summary}"
            )
        except Exception:
            pass

    # Build prompt: news-first when present so weak local LLM sees it
    news_block = ""
    if safe_news:
        news_block = (
            f"ВНИМАНИЕ — важная новость по {ticker}: {safe_news[:300]}{'…' if len(safe_news) > 300 else ''}\n"
            f"Сентимент: {sentiment_block.strip() if sentiment_block else 'не определён'}.\n"
            f"Примечание: текущая математическая рекомендация уже учитывает этот сентимент. "
            f"Опиши, как именно новость влияет на ситуацию, и подтверди или оспорь итоговую рекомендацию.\n\n"
        )

    macro_block = ""
    if macro:
        macro_block = (
            f"Макро: USD/RUB {macro.get('usd_rub', '?')}, EUR/RUB {macro.get('eur_rub', '?')}, "
            f"Brent ${macro.get('brent', '?')}, ставка ЦБ {macro.get('cbr_rate', '?')}%, "
            f"макро-фон {'бычий' if macro.get('macro_bullish') else 'осторожный'}.\n"
        )

    ob = price_data.get("order_book") or {}
    ob_block = ""
    if ob and not ob.get("error"):
        imbalance = ob.get("imbalance") or 0
        if imbalance:
            imbalance_word = "покупатели" if imbalance > 0.1 else "продавцы" if imbalance < -0.1 else "нейтрально"
        else:
            imbalance_word = ob.get("trade_activity") or "нейтрально"
        activity_raw = ob.get("trade_activity") or "нет данных"
        activity_word = (
            activity_raw.replace("pokupki", "покупки")
            .replace("prodazhi", "продажи")
            .replace("neutralno", "нейтрально")
        )
        ob_block = (
            f"Стакан/ликвидность: спред {ob.get('spread', '?')} ₽ ({ob.get('spread_pct', '?')}%), "
            f"лучший bid {ob.get('best_bid', '?')} / ask {ob.get('best_ask', '?')}, "
            f"объём сегодня {ob.get('voltoday', '?')} лот, "
            f"сделок {ob.get('numtrades', '?')}, "
            f"активность в потоке сделок {activity_word} "
            f"(перекос по стакану {imbalance_word}).\n"
        )

    user = f"""{news_block}Анализ {ticker}. Текущая рекомендация: {recommendation} ({direction}, {strength}). Причина: {reason}. Предупреждения: {'; '.join(warnings) if warnings else 'нет'}.

{macro_block}{ob_block}Данные:
Цена: {price_data.get('close', '?')} ₽
RSI: {price_data.get('rsi', '?')}
MACD: {price_data.get('macd_hist', '?')}
ATR: {price_data.get('atr', '?')}
ADX: {price_data.get('adx', '?')}
Объём: {price_data.get('vol_ratio', '?')}x
BB%: {price_data.get('bb_pct', '?')}
SMA20/50: {price_data.get('sma_20', '?')}/{price_data.get('sma_50', '?')}
Тренд: {price_data.get('higher_tf_trend', '?')}
Дивдоход: {price_data.get('div_yield', '?')}%
Сигналы: {', '.join(signals) if signals else 'нет'}

Дай кратко:
1. Как новость влияет на рекомендацию (если есть)
2. Оценка ситуации, обязательно учитывая ликвидность и активность в стакане/потоке сделок
3. Уровни поддержки и сопротивления"""

    # Use dynamic system prompt with live portfolio injection
    # (inspired by hack-moex leader architecture)
    dynamic_prompt = await render_system_prompt()
    analysis = await call_llm_with_fallback(dynamic_prompt, user, max_tokens=384)

    # Optional second-opinion critic via local Ollama (Gemma) — cheap sanity check.
    # Runs in parallel so it does not block the primary analysis.
    critic_task = asyncio.create_task(
        _critic_review(
            ticker, recommendation, direction, strength, reason, warnings, signals,
            price_data, analysis, bounce_prediction=bounce_prediction, latest_sentiment=latest_sentiment,
        )
    )

    # Await the critic with a short timeout so a slow/broken local LLM never stalls the API.
    try:
        critic_text = await asyncio.wait_for(critic_task, timeout=40.0)
    except asyncio.TimeoutError:
        critic_text = "[Критик (Gemma) не успел ответить за 40 секунд]"
    except Exception as e:
        critic_text = f"[Критик недоступен: {e}]"

    return analysis, critic_text


async def _critic_review(
    ticker: str,
    recommendation: str,
    direction: str | None,
    strength: str | None,
    reason: str,
    warnings: list[str],
    signals: list[str],
    price_data: dict[str, Any],
    analysis: str,
    bounce_prediction: dict[str, Any] | None = None,
    latest_sentiment: dict[str, Any] | None = None,
) -> str:
    """Run a lightweight local LLM as a critic to sanity-check the primary analysis."""
    from core.llm import call_llm

    system = """Ты — критик и риск-менеджер. Твоя задача — проверить анализ другого аналитика.
Отвечай очень кратко (2-4 предложения). Скажи:
1. Согласен ли ты (ДА / НЕТ / ЧАСТИЧНО).
2. Если не согласен — главный риск или ошибка.
3. Уровень уверенности, который ты бы поставил (высокая / средняя / низкая), и почему.
Не повторяй весь анализ — только критические замечания.

Дополнительно сверься с двумя независимыми источниками:
- Bounce-предиктор: если он даёт вероятность ≥40% отскока ПРОТИВ основной рекомендации — это красный флаг.
- Новостной сентимент из RSS: если он противоречит рекомендации (bullish vs шорт / bearish vs лонг) — укажи это."""

    bounce_block = ""
    if bounce_prediction:
        bp = bounce_prediction
        if bp.get("probability", 0) >= 20:
            bounce_block = (
                f"- Bounce-предиктор: вероятность отскока {bp.get('probability', '?')}% "
                f"направление {bp.get('direction', '?')} "
                f"(уверенность {bp.get('confidence_label', '?')}); факторы: {', '.join(bp.get('factors', [])) or 'нет'}.\n"
            )
        else:
            bounce_block = "- Bounce-предиктор: слабый сигнал (вероятность < 20%), не доминирует.\n"

    sentiment_block = ""
    if latest_sentiment:
        sentiment_block = (
            f"- Новостной сентимент (RSS, последние 24ч): {latest_sentiment.get('sentiment', '?')} "
            f"(уверенность {latest_sentiment.get('confidence', 0)}%). "
            f"Риски: {', '.join(latest_sentiment.get('risk_flags', []) or []) or 'нет'}. "
            f"Резюме: {latest_sentiment.get('summary', 'нет')}.\n"
        )

    user = f"""Акция: {ticker}
Математические данные:
- Рекомендация модели: {recommendation} ({direction}, {strength})
- RSI: {price_data.get('rsi', '?')}, MACD: {price_data.get('macd_hist', '?')}, ADX: {price_data.get('adx', '?')}
- Тренд: {price_data.get('higher_tf_trend', '?')}
- Предупреждения: {'; '.join(warnings) if warnings else 'нет'}
- Сигналы: {', '.join(signals) if signals else 'нет'}
{bounce_block}{sentiment_block}
Анализ другого аналитика:
{analysis}

Проверь этот анализ."""

    try:
        return await call_llm_with_fallback(system, user, max_tokens=384)
    except Exception as e:
        return f"[Критик недоступен: {e}]"


async def morning_report(screener_results: list[dict], positions: list[dict]) -> str:
    """Generate morning watchlist report."""
    top = screener_results[:10]
    lines = []
    for r in top:
        d = r.get("details", {})
        lines.append(
            f"• {r['ticker']}: {d.get('close', '?')}₽ | "
            f"RSI {d.get('rsi', '?')} | Сигналы: {', '.join(r.get('signals', [])) or 'нет'} | "
            f"Очков: {r['score']}"
        )
    screener_text = "\n".join(lines)

    pos_lines = []
    for p in positions:
        pos_lines.append(f"• {p['ticker']} {p['side']} @ {p['entry_px']}₽ | стоп {p.get('stop_px', '?')}")
    positions_text = "\n".join(pos_lines) if pos_lines else "Нет открытых позиций"

    user = f"""Сформируй утреннюю сводку.

ТОП-10:
{screener_text}

Позиции:
{positions_text}

Выдели 2-3 идеи на сегодня."""

    return await call_llm(SYSTEM_PROMPT, user, max_tokens=2048)


async def explain_anomaly(ticker: str, price_data: dict, volume_data: dict, news: str = "") -> str:
    """Ask LLM to explain unusual price/volume behavior."""
    user = f"""{ticker}: аномалия.
Цена: {price_data.get('close')}₽ ({price_data.get('change_pct')}%)
Объём: {volume_data.get('volume', '?')} (средний {volume_data.get('avg_volume', '?')}, x{volume_data.get('vol_ratio', '?')})
{('Новости: ' + news[:800]) if news else 'Новостей нет.'}

Причины движения?"""

    return await call_llm(SYSTEM_PROMPT, user, max_tokens=512)


async def evening_report(trades_closed: list[dict], positions: list[dict], market_summary: str = "") -> str:
    """Generate evening P&L summary."""
    trade_lines = []
    for t in trades_closed:
        trade_lines.append(f"• {t['ticker']} {t['side']}: вход {t['entry_px']} → выход {t.get('exit_px', '?')}, PnL {t.get('pnl', '?')}₽")
    trades_text = "\n".join(trade_lines) if trade_lines else "Сделок не было"

    user = f"""Вечерний отчёт.

Сделки:
{trades_text}

Позиции:
{positions}

{('Рынок: ' + market_summary) if market_summary else ''}

Оценка дня."""

    return await call_llm(SYSTEM_PROMPT, user, max_tokens=1024)

"""Smart directional signal logic for long/short recommendations.

Uses weighted scoring of technical indicators and market context
to produce confident trading directions with reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class DirectionAdvice:
    """Structured trading recommendation."""
    direction: str          # 'long', 'short', 'neutral'
    strength: str           # 'weak', 'moderate', 'strong'
    reason: str             # human-readable explanation
    risk_reward: float | None
    stop_pct: float | None  # recommended stop distance in %
    warnings: list[str]     # e.g. dividend risk, macro filter
    signals_used: list[str] # which raw signals contributed
    bull_score: int = 0     # bullish evidence points
    bear_score: int = 0     # bearish evidence points


# Blue-chip tickers where shorting is discouraged near dividends
BLUE_CHIPS = {
    "SBER", "GAZP", "LKOH", "GMKN", "NVTK", "ROSN", "TATN",
    "YNDX", "PLZL", "MGNT", "MTSS", "VTBR", "ALRS", "CHMF",
    "NLMK", "POLY", "SNGS", "MOEX", "RUAL", "AFKS", "PIKK", "IRAO", "PHOR",
}


def _bullish_score(signals: list[str], rsi: float, macd_hist: float, bb_pct: float, adx: float | None = None, di_plus: float | None = None, di_minus: float | None = None, higher_tf_trend: str | None = None) -> int:
    """Score bullish evidence 0-100."""
    score = 0
    if "RSI_oversold" in signals:
        score += 30
    if "MACD_bullish_cross" in signals:
        score += 25
    if "BB_lower_touch" in signals:
        score += 20
    if "UPTREND" in signals:
        score += 20
    if "VOLUME_spike" in signals:
        score += 15
    if "GAP_down" in signals:
        score += 10  # gap down often bounces
    if "HAMMER" in signals:
        score += 15  # reversal hammer at bottom
    if "BULLISH_ENGULFING" in signals:
        score += 20  # strong bullish reversal
    if any(s.startswith("FIB_bounce") for s in signals):
        score += 12  # bounce off Fibonacci support
    # RSI deep oversold bonus
    if rsi < 20:
        score += 15
    elif rsi < 30:
        score += 10
    # MACD momentum bonus
    if macd_hist > 0:
        score += 10
    # Bollinger extreme bonus
    if bb_pct < 0.02:
        score += 10
    # ADX trend filter: boost if strong bullish trend
    if adx is not None and adx > 25:
        if "ADX_bullish_trend" in signals:
            score += 15
        elif "ADX_bearish_trend" in signals:
            score = max(score, 0)  # prevent negative; bear trend simply neutralises bullish
    # Multi-timeframe filter
    if higher_tf_trend == "UPTREND":
        score += 15  # aligned with weekly uptrend
    elif higher_tf_trend == "DOWNTREND":
        score = max(score - 20, 0)  # long signal against weekly downtrend
    return min(score, 100)


def _bearish_score(signals: list[str], rsi: float, macd_hist: float, bb_pct: float, adx: float | None = None, di_plus: float | None = None, di_minus: float | None = None, higher_tf_trend: str | None = None) -> int:
    """Score bearish evidence 0-100."""
    score = 0
    if "RSI_overbought" in signals:
        score += 30
    if "MACD_bearish_cross" in signals:
        score += 25
    if "BB_upper_touch" in signals:
        score += 20
    if "DOWNTREND" in signals:
        score += 20
    if "VOLUME_spike" in signals:
        score += 15
    if "GAP_up" in signals:
        score += 10
    if "HANGING_MAN" in signals:
        score += 15  # reversal hanging man at top
    if "BEARISH_ENGULFING" in signals:
        score += 20  # strong bearish reversal
    if any(s.startswith("FIB_reject") for s in signals):
        score += 10  # rejection at Fibonacci resistance
    # RSI deep overbought bonus
    if rsi > 80:
        score += 15
    elif rsi > 70:
        score += 10
    # MACD momentum bonus
    if macd_hist < 0:
        score += 10
    # Bollinger extreme bonus
    if bb_pct > 0.98:
        score += 10
    # ADX trend filter: boost if strong bearish trend
    if adx is not None and adx > 25:
        if "ADX_bearish_trend" in signals:
            score += 15
        elif "ADX_bullish_trend" in signals:
            score = max(score, 0)  # prevent negative; bull trend simply neutralises bearish
    # Multi-timeframe filter
    if higher_tf_trend == "DOWNTREND":
        score += 15  # aligned with weekly downtrend
    elif higher_tf_trend == "UPTREND":
        score = max(score - 20, 0)  # short signal against weekly uptrend
    return min(score, 100)


def _conflicting(signals: list[str]) -> bool:
    """Check if bullish and bearish signals appear together."""
    bullish = {
        "RSI_oversold", "MACD_bullish_cross", "BB_lower_touch", "UPTREND", "GAP_down",
        "ADX_bullish_trend", "HAMMER", "BULLISH_ENGULFING",
    }
    bearish = {
        "RSI_overbought", "MACD_bearish_cross", "BB_upper_touch", "DOWNTREND", "GAP_up",
        "ADX_bearish_trend", "HANGING_MAN", "BEARISH_ENGULFING",
    }
    # Add fib signals dynamically
    bullish.update({s for s in signals if s.startswith("FIB_bounce_")})
    bearish.update({s for s in signals if s.startswith("FIB_reject_")})
    has_bull = any(s in bullish for s in signals)
    has_bear = any(s in bearish for s in signals)
    return has_bull and has_bear


def recommend_direction(
    ticker: str,
    signals: list[str],
    score: float,
    rsi: float,
    macd_hist: float,
    bb_pct: float,
    sma_20: float,
    sma_50: float,
    close: float,
    vol_ratio: float,
    macro_bullish: bool = True,
    adx: float | None = None,
    di_plus: float | None = None,
    di_minus: float | None = None,
    higher_tf_trend: str | None = None,
    div_yield: float | None = None,
    sentiment: str | None = None,
    sentiment_confidence: int = 0,
) -> DirectionAdvice:
    """Produce a smart long/short/neutral recommendation with reasoning."""
    bull = _bullish_score(signals, rsi, macd_hist, bb_pct, adx, di_plus, di_minus, higher_tf_trend)
    bear = _bearish_score(signals, rsi, macd_hist, bb_pct, adx, di_plus, di_minus, higher_tf_trend)
    warnings: list[str] = []

    # News-sentiment adjustment: weak filter, never overrides TA
    if sentiment and sentiment_confidence > 0:
        sentiment = sentiment.lower()
        conf = sentiment_confidence / 100.0

        # Sentiment is a minor confirmation. Cap impact so technical analysis stays primary.
        max_boost = 10  # was 25: too aggressive, overrode technical signals
        max_penalty = 8  # was 15: reduce opposite-side suppression
        trend_conflict_multiplier = 0.5  # halve impact when sentiment fights weekly trend

        trend_aligned = (
            (sentiment == "bullish" and higher_tf_trend == "UPTREND")
            or (sentiment == "bearish" and higher_tf_trend == "DOWNTREND")
        )
        boost = min(conf * max_boost, max_boost) if trend_aligned else min(conf * max_boost * trend_conflict_multiplier, max_boost)
        penalty = min(conf * max_penalty, max_penalty) if trend_aligned else min(conf * max_penalty * trend_conflict_multiplier, max_penalty)

        if sentiment == "bullish":
            # Add boost only if technical side already has moderate evidence (>=35)
            if bull >= 35:
                bull = min(bull + boost, 100)
            if bear >= 20:
                bear = max(bear - penalty, 0)
            warnings.append(f"Новостной сентимент bullish ({sentiment_confidence}%) — небольшое подтверждение лонг-сигнала")
        elif sentiment == "bearish":
            if bear >= 35:
                bear = min(bear + boost, 100)
            if bull >= 20:
                bull = max(bull - penalty, 0)
            warnings.append(f"Новостной сентимент bearish ({sentiment_confidence}%) — небольшое подтверждение шорт-сигнала")
        elif sentiment == "neutral":
            # Neutral news slightly reduces confidence on both sides (uncertainty)
            dampen = min(conf * 6, 6)
            bull = max(bull - dampen, 0)
            bear = max(bear - dampen, 0)
            warnings.append(f"Новостной сентимент нейтральный ({sentiment_confidence}%) — снижает уверенность")

    # Fundamental filter: dividend yield / overvaluation
    if div_yield is not None:
        if div_yield > 7:
            warnings.append(f"Дивдоход {div_yield:.1f}% — высокий, возможно недооценена")
        elif div_yield < 1 and rsi > 60:
            warnings.append(f"Дивдоход {div_yield:.1f}% — низкий, акция может быть переоценена")
        elif div_yield < 2:
            warnings.append(f"Дивдоход {div_yield:.1f}% — ниже среднего по рынку")

    # Overvaluation guard
    if div_yield is not None and rsi > 70 and div_yield < 1:
        warnings.append("RSI > 70 + дивдоход < 1% — акция выглядит перекупленной, избегайте лонгов")
        bull = max(bull - 30, 0)

    # Trend context: daily ADX + weekly trend synthesized
    if adx is not None:
        adx_dir = None
        if "ADX_bullish_trend" in signals:
            adx_dir = "up"
        elif "ADX_bearish_trend" in signals:
            adx_dir = "down"
        if adx < 20:
            warnings.append("ADX < 20 — рынок в боковике, сигналы менее надёжны")
        elif adx > 25 and adx_dir:
            weekly = higher_tf_trend or "NEUTRAL"
            if weekly == "NEUTRAL":
                warnings.append(f"ADX {adx:.1f} — сильный {'восходящий' if adx_dir == 'up' else 'нисходящий'} тренд")
            elif (adx_dir == "up" and weekly == "UPTREND") or (adx_dir == "down" and weekly == "DOWNTREND"):
                warnings.append(f"ADX {adx:.1f} — сильный {'восходящий' if adx_dir == 'up' else 'нисходящий'} тренд, недельный тренд подтверждает")
            else:
                warnings.append(f"ADX {adx:.1f} — дневной {'восходящий' if adx_dir == 'up' else 'нисходящий'} импульс, но недельный тренд противоположный. Конфликт таймфреймов!")
    if higher_tf_trend:
        if higher_tf_trend == "UPTREND":
            warnings.append("Недельный тренд восходящий")
        elif higher_tf_trend == "DOWNTREND":
            warnings.append("Недельный тренд нисходящий")
        else:
            warnings.append("Недельный тренд нейтральный — ждите пробоя")

    # Macro filter: avoid longs in bear market unless very strong
    if not macro_bullish and bull < 70:
        warnings.append("Рынок в целом слабый — будьте осторожны с лонгами")

    # Short filter: discourage shorts on blue chips
    if ticker in BLUE_CHIPS and bear > 0:
        if rsi < 60:
            warnings.append(f"{ticker} — голубая фишка, шорт рискованен")
            bear = max(bear - 25, 0)
        if rsi < 70 and "DOWNTREND" not in signals:
            warnings.append("Нет сильного нисходящего тренда для шорта")
            bear = max(bear - 15, 0)

    # Determine direction
    direction = "neutral"
    strength = "weak"
    if _conflicting(signals):
        # Conflicting signals: pick winner only if clear
        if bull > bear + 30:
            direction = "long"
            strength = "moderate" if bull < 70 else "strong"
        elif bear > bull + 30:
            direction = "short"
            strength = "moderate" if bear < 70 else "strong"
    else:
        # No conflict — pick the stronger side
        if bull >= 60 and bull > bear:
            direction = "long"
            strength = "strong" if bull >= 80 else "moderate"
        elif bear >= 60 and bear > bull:
            direction = "short"
            strength = "strong" if bear >= 80 else "moderate"
        elif bull > bear + 20:
            direction = "long"
            strength = "weak"
        elif bear > bull + 20:
            direction = "short"
            strength = "weak"

    # Higher timeframe cap: trading against the weekly trend is always weak
    if direction == "long" and higher_tf_trend == "DOWNTREND":
        if strength in ("strong", "moderate"):
            strength = "weak"
        warnings.append("⚠️ Лонг против нисходящего недельного тренда — контртренд, повышенный риск")
    elif direction == "short" and higher_tf_trend == "UPTREND":
        if strength in ("strong", "moderate"):
            strength = "weak"
        warnings.append("⚠️ Шорт против восходящего недельного тренда — контртренд, повышенный риск")

    # Build reason after strength finalization
    if direction == "long":
        reason = _build_reason_long(signals, rsi, bb_pct, vol_ratio, strength, higher_tf_trend)
    elif direction == "short":
        reason = _build_reason_short(signals, rsi, bb_pct, vol_ratio, strength, higher_tf_trend)
    else:
        if _conflicting(signals):
            reason = (
                f"Сигналы противоречат друг другу (бычьих очков: {bull}, медвежьих: {bear}). "
                "Лучше подождать ясности."
            )
        else:
            reason = (
                f"Нет чёткого направления (бычьих очков: {bull}, медвежьих: {bear}). "
                "RSI, MACD и тренд не дают уверенного сигнала."
            )

    # Calculate rough risk/reward and stop distance
    rr = None
    stop_pct = None
    if direction in ("long", "short"):
        # Typical ATR-based stop ~2-3%, target ~4-6%
        stop_pct = 2.5
        target_pct = 5.0 if strength == "strong" else 4.0
        rr = round(target_pct / stop_pct, 1)

    # Build signals_used list, including sentiment marker when active
    _signals_used = [s for s in signals if s in {
            "RSI_oversold", "RSI_overbought",
            "MACD_bullish_cross", "MACD_bearish_cross",
            "BB_lower_touch", "BB_upper_touch",
            "UPTREND", "DOWNTREND",
            "ADX_strong_trend", "ADX_weak_trend",
            "ADX_bullish_trend", "ADX_bearish_trend",
            "DOJI", "HAMMER", "HANGING_MAN",
            "BULLISH_ENGULFING", "BEARISH_ENGULFING",
            "VOLUME_spike", "GAP_up", "GAP_down",
            "FIB_bounce_38.2", "FIB_bounce_50", "FIB_bounce_61.8",
            "FIB_reject_38.2", "FIB_reject_50", "FIB_reject_61.8",
        }]
    if sentiment and sentiment_confidence > 0:
        _signals_used.append(f"SENTIMENT_{sentiment.upper()}({sentiment_confidence})")

    return DirectionAdvice(
        direction=direction,
        strength=strength,
        reason=reason,
        risk_reward=rr,
        stop_pct=stop_pct,
        warnings=warnings,
        signals_used=_signals_used,
        bull_score=bull,
        bear_score=bear,
    )


def _build_reason_long(signals: list[str], rsi: float, bb_pct: float, vol_ratio: float, strength: str, higher_tf_trend: str | None = None) -> str:
    parts: list[str] = []
    if "RSI_oversold" in signals:
        parts.append(f"RSI перепродан ({rsi:.1f})")
    if "MACD_bullish_cross" in signals:
        parts.append("MACD дал бычий пересечение")
    if "BB_lower_touch" in signals:
        parts.append("цена у нижней границы Bollinger")
    if "UPTREND" in signals:
        parts.append("восходящий тренд")
    if "ADX_bullish_trend" in signals:
        parts.append("ADX подтверждает сильный восходящий тренд")
    if "HAMMER" in signals:
        parts.append("свеча «молот» — возможный разворот вверх")
    if "BULLISH_ENGULFING" in signals:
        parts.append("бычье поглощение — сильный разворотный сигнал")
    for s in signals:
        if s.startswith("FIB_bounce_"):
            parts.append(f"отскок от уровня Фибоначчи {s.split('_')[-1]}%")
    if "DOJI" in signals:
        parts.append("доджи — рынок нерешителен, возможен разворот")
    if "VOLUME_spike" in signals:
        parts.append("всплеск объёма")
    if "GAP_down" in signals:
        parts.append("даун-гэп (возможен отскок)")
    if not parts:
        parts.append(f"RSI {rsi:.1f}, близость к нижней Bollinger ({bb_pct:.2f})")
    reason = "; ".join(parts)
    if higher_tf_trend == "DOWNTREND":
        return f"Контртрендовый лонг (недельный тренд нисходящий): {reason}."
    if strength == "strong":
        return f"Сильные бычьи сигналы: {reason}."
    elif strength == "moderate":
        return f"Умеренные бычьи сигналы: {reason}."
    else:
        return f"Слабые бычьи признаки: {reason}."


def _build_reason_short(signals: list[str], rsi: float, bb_pct: float, vol_ratio: float, strength: str, higher_tf_trend: str | None = None) -> str:
    parts: list[str] = []
    if "RSI_overbought" in signals:
        parts.append(f"RSI перекуплен ({rsi:.1f})")
    if "MACD_bearish_cross" in signals:
        parts.append("MACD дал медвежье пересечение")
    if "BB_upper_touch" in signals:
        parts.append("цена у верхней границы Bollinger")
    if "DOWNTREND" in signals:
        parts.append("нисходящий тренд")
    if "ADX_bearish_trend" in signals:
        parts.append("ADX подтверждает сильный нисходящий тренд")
    if "HANGING_MAN" in signals:
        parts.append("свеча «повешенный» — возможный разворот вниз")
    if "BEARISH_ENGULFING" in signals:
        parts.append("медвежье поглощение — сильный разворотный сигнал")
    for s in signals:
        if s.startswith("FIB_reject_"):
            parts.append(f"отбой от уровня Фибоначчи {s.split('_')[-1]}%")
    if "DOJI" in signals:
        parts.append("доджи — рынок нерешителен, возможен разворот")
    if "VOLUME_spike" in signals:
        parts.append("всплеск объёма")
    if "GAP_up" in signals:
        parts.append("ап-гэп (возможен откат)")
    if not parts:
        parts.append(f"RSI {rsi:.1f}, близость к верхней Bollinger ({bb_pct:.2f})")
    reason = "; ".join(parts)
    if higher_tf_trend == "UPTREND":
        return f"Контртрендовый шорт (недельный тренд восходящий): {reason}."
    if strength == "strong":
        return f"Сильные медвежьи сигналы: {reason}."
    elif strength == "moderate":
        return f"Умеренные медвежьи сигналы: {reason}."
    else:
        return f"Слабые медвежьи признаки: {reason}."


def format_direction_emoji(direction: str, strength: str, low_liquidity: bool = False) -> str:
    """Return a visual badge for the direction. Low liquidity turns the badge yellow and appends a warning."""
    strength_ru = {
        "strong": "(сильно)",
        "moderate": "(умеренно)",
        "weak": "(слабо)",
    }.get(strength, "(слабо)")

    if low_liquidity:
        if direction == "long":
            return f"🟡 ЛОНГ {strength_ru} ⚠️ низкая ликвидность"
        elif direction == "short":
            return f"🟡 ШОРТ {strength_ru} ⚠️ низкая ликвидность"
        else:
            return "🟡 ЖДАТЬ ⚠️ низкая ликвидность"

    if direction == "long":
        return f"🟢 ЛОНГ {strength_ru}"
    elif direction == "short":
        return f"🔴 ШОРТ {strength_ru}"
    else:
        return "⚪ ЖДАТЬ"

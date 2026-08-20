"""NewsSentimentAgent — анализ сентимента новостей для конкретного тикера.

Принимает на вход список новостных текстов, отправляет в LLM
и возвращает структурированную оценку: сентимент, уверенность,
ключевые темы и краткое резюме.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.llm import call_llm, sanitize_for_llm


@dataclass(frozen=True)
class SentimentResult:
    """Результат анализа сентимента новостей."""

    sentiment: str  # bullish | bearish | neutral
    confidence: int  # 0–100
    summary: str
    key_topics: list[str]
    risk_flags: list[str]


@dataclass(frozen=True)
class WeightedNewsItem:
    """News item with age-based weight for decayed impact scoring."""

    text: str
    published_at: datetime | None
    source: str = ""
    age_minutes: int | None = None
    weight: float = 1.0


SYSTEM_PROMPT = """Ты — аналитик новостного сентимента российского рынка акций (Мосбиржа).
Твоя задача — оценить, как поданные новости влияют на котировку конкретного тикера.

Правила:
- Смотри только на факты и конкретику, а не на заголовки-соус.
- Учитывай: санкции, дивиденды, отчётность, сделки M&A, смена руководства, регуляторные решения ЦБ/ФАС.
- ОБЯЗАТЕЛЬНО отсекай нерелевантные новости. Если новость не про этот тикер или про другую компанию с похожим названием — верни sentiment=neutral и низкую уверенность.
- Сентимент: bullish (позитив), bearish (негатив), neutral (нейтрально или противоречиво).
- Уверенность: 0–100. Высокая (80–100) только если новости:
  • прямо про указанный тикер,
  • однозначны,
  • подтверждены фактами (не слухи),
  • их 3 и более.
  Низкая (20–40), если:
  • новость одна,
  • это слух или неофициальная информация,
  • новость старше 6 часов,
  • новость косвенно связана с тикером.
- Если несколько новостей, оцени их совокупное влияние. Более свежие новости важнее устаревших.

Формат ответа — строго JSON, без markdown-блоков:
{
  "sentiment": "bullish|bearish|neutral",
  "confidence": 0-100,
  "summary": "1-2 предложения суть",
  "key_topics": ["тема1", "тема2"],
  "risk_flags": ["риск1", "риск2"]
}"""


def _news_weight_from_age(age_minutes: int) -> float:
    """Decay weight based on news age in minutes.

    Fresh news keeps full weight; older news is progressively discounted.
    Anything older than 72 hours is considered stale and gets near-zero weight.
    """
    if age_minutes < 0:
        return 1.0
    if age_minutes <= 60:
        return 1.0
    if age_minutes <= 360:
        return 0.75
    if age_minutes <= 1440:
        return 0.5
    if age_minutes <= 4320:
        return 0.25
    return 0.05


def _format_age(age_minutes: int | None) -> str:
    """Human-readable age label for the prompt."""
    if age_minutes is None:
        return "время публикации неизвестно"
    if age_minutes < 60:
        return f"{age_minutes} минут назад"
    if age_minutes < 1440:
        return f"{age_minutes // 60} часов назад"
    return f"{age_minutes // 1440} дней назад"


class NewsSentimentAgent:
    """Агент анализа новостного сентимента по тикеру."""

    async def analyze(
        self,
        ticker: str,
        news_items: list[str] | list[WeightedNewsItem],
    ) -> SentimentResult:
        """Проанализировать список новостей для тикера.

        Args:
            ticker: Тикер Мосбиржи (например, "SBER").
            news_items: Список строк (заголовки/тексты) или WeightedNewsItem.

        Returns:
            SentimentResult с оценкой.
        """
        if not news_items:
            return SentimentResult(
                sentiment="neutral",
                confidence=0,
                summary="Новостей не предоставлено.",
                key_topics=[],
                risk_flags=[],
            )

        normalized = self._normalize_items(news_items)
        if not normalized:
            return SentimentResult(
                sentiment="neutral",
                confidence=0,
                summary="Нет свежих новостей для анализа.",
                key_topics=[],
                risk_flags=[],
            )

        # Deduplicate similar news items to avoid one story dominating the score
        normalized = self._deduplicate_items(normalized)

        # Build prompt block with age/weight context
        trimmed: list[str] = []
        for i, item in enumerate(normalized, 1):
            text = sanitize_for_llm(item.text.strip())
            if len(text) > 800:
                text = text[:800] + "…"
            age_label = _format_age(item.age_minutes)
            weight_label = f"вес {item.weight:.0%}"
            trimmed.append(f"{i}. [{age_label}, {weight_label}] {text}")

        news_block = "\n".join(trimmed)
        user = f"""Тикер: {ticker.upper()}

Новости ({len(trimmed)} шт.):
{news_block}

Оцени совокупное влияние этих новостей на тикер {ticker.upper()}.
Учитывай, что более свежие новости важнее. Верни только JSON."""

        raw = await call_llm(SYSTEM_PROMPT, user, max_tokens=512)
        parsed = self._parse(raw)
        return self._calibrate_confidence(parsed, normalized)

    @staticmethod
    def _normalize_items(
        news_items: list[str] | list[WeightedNewsItem],
    ) -> list[WeightedNewsItem]:
        """Convert mixed input into weighted news items, skipping stale items."""
        now = datetime.now(timezone.utc)
        result: list[WeightedNewsItem] = []
        for item in news_items:
            if isinstance(item, WeightedNewsItem):
                weighted = item
            else:
                weighted = WeightedNewsItem(text=str(item), published_at=None)

            # Compute age if we have a publication timestamp
            if weighted.age_minutes is None and weighted.published_at is not None:
                try:
                    delta = now - weighted.published_at
                    weighted = WeightedNewsItem(
                        text=weighted.text,
                        published_at=weighted.published_at,
                        source=weighted.source,
                        age_minutes=int(delta.total_seconds() // 60),
                        weight=weighted.weight,
                    )
                except Exception:
                    pass

            # Apply default weight from age if not explicitly set
            if weighted.weight == 1.0 and weighted.age_minutes is not None:
                weighted = WeightedNewsItem(
                    text=weighted.text,
                    published_at=weighted.published_at,
                    source=weighted.source,
                    age_minutes=weighted.age_minutes,
                    weight=_news_weight_from_age(weighted.age_minutes),
                )

            # Skip items older than 72 hours unless explicitly kept
            if weighted.age_minutes is not None and weighted.age_minutes > 4320:
                continue
            result.append(weighted)
        return result

    @staticmethod
    def _deduplicate_items(items: list[WeightedNewsItem]) -> list[WeightedNewsItem]:
        """Remove near-duplicate news items based on normalized text content."""
        seen: set[str] = set()
        unique: list[WeightedNewsItem] = []
        for item in items:
            # Normalize: lowercase, remove punctuation/spaces, keep first 120 chars
            normalized_text = re.sub(r"[^a-zа-я0-9]", "", item.text.lower())[:120]
            if normalized_text in seen:
                continue
            seen.add(normalized_text)
            unique.append(item)
        return unique

    @staticmethod
    def _calibrate_confidence(
        result: SentimentResult,
        items: list[WeightedNewsItem],
    ) -> SentimentResult:
        """Cap confidence based on data quality to avoid overconfident LLM outputs."""
        if result.sentiment == "neutral":
            # Neutral sentiment should rarely drive trading decisions
            return SentimentResult(
                sentiment="neutral",
                confidence=min(result.confidence, 40),
                summary=result.summary,
                key_topics=result.key_topics,
                risk_flags=result.risk_flags,
            )

        # Calibrate based on number of unique news items
        n = len(items)
        if n == 1:
            cap = 60
        elif n == 2:
            cap = 70
        elif n == 3:
            cap = 80
        else:
            cap = 90

        # Penalize low average weight (older/stale news)
        avg_weight = sum(item.weight for item in items) / n if n > 0 else 1.0
        if avg_weight < 0.5:
            cap = min(cap, 50)
        elif avg_weight < 0.75:
            cap = min(cap, 70)

        return SentimentResult(
            sentiment=result.sentiment,
            confidence=min(result.confidence, cap),
            summary=result.summary,
            key_topics=result.key_topics,
            risk_flags=result.risk_flags,
        )

    @staticmethod
    def _parse(raw: str) -> SentimentResult:
        """Извлечь JSON из ответа LLM, с защитой от markdown-блоков."""
        text = raw.strip()
        # Убираем markdown-обёртку ```json ... ``` если есть
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

        # Пробуем найти JSON-объект в тексте
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            text = match.group(0)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Fallback — если LLM не вернул JSON
            sentiment = "neutral"
            lowered = raw.lower()
            if any(w in lowered for w in ("bullish", "позитив", "рост", "покупать", "long")):
                sentiment = "bullish"
            elif any(w in lowered for w in ("bearish", "негатив", "падение", "продавать", "short")):
                sentiment = "bearish"
            return SentimentResult(
                sentiment=sentiment,
                confidence=50,
                summary=raw[:300],
                key_topics=[],
                risk_flags=["parse_error"],
            )

        sentiment = data.get("sentiment", "neutral")
        if sentiment not in ("bullish", "bearish", "neutral"):
            sentiment = "neutral"

        confidence = data.get("confidence", 50)
        try:
            confidence = int(confidence)
        except Exception:
            confidence = 50
        confidence = max(0, min(100, confidence))

        key_topics = data.get("key_topics") or []
        if isinstance(key_topics, str):
            key_topics = [key_topics]

        risk_flags = data.get("risk_flags") or []
        if isinstance(risk_flags, str):
            risk_flags = [risk_flags]

        return SentimentResult(
            sentiment=sentiment,
            confidence=confidence,
            summary=data.get("summary", "—")[:500],
            key_topics=list(key_topics)[:5],
            risk_flags=list(risk_flags)[:5],
        )

    def to_dict(self, result: SentimentResult) -> dict[str, Any]:
        """Сериализовать результат в dict для JSON-ответа API."""
        return {
            "sentiment": result.sentiment,
            "confidence": result.confidence,
            "summary": result.summary,
            "key_topics": result.key_topics,
            "risk_flags": result.risk_flags,
        }


# Глобальный экземпляр для удобного импорта
agent = NewsSentimentAgent()

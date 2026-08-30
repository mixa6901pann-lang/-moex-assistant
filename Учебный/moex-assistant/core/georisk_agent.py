"""GeoRiskAgent — геополитический риск-сканер.

Сканирует RSS-ленты политики, фильтрует по ключевым словам
и выставляет geo-risk score 0-10 через LLM.

Возвращает секторные направленные импульсы (direction/impact/confidence)
и общий market direction, чтобы торговая логика могла не просто блокировать
заявки, а активно помогать принимать решения (лонг/шорт).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from loguru import logger

from core.llm import call_llm
from core.config import GEORISK_EXIT_THRESHOLD, GEORISK_BULLISH_THRESHOLD


@dataclass(frozen=True)
class GeoRiskResult:
    """Результат оценки геополитического риска."""

    score: int  # 0-10
    severity: str  # low | medium | high | critical
    summary: str
    affected_sectors: list[dict]  # [{sector, direction, impact, confidence}]
    trigger_keywords: list[str]  # какие слова сработали
    overall_direction: int = 0  # -1 = bearish, 0 = neutral, +1 = bullish
    news_items: list[dict] = None  # исходные новости с релевантностью


# RSS-источники геополитики и макро-новостей.
# Mix of Russian-language and international feeds for early detection of shocks.
GEO_RSS_FEEDS = [
    # Russian / CIS
    ("tass", "https://tass.ru/rss/v2.xml"),
    ("lenta", "https://lenta.ru/rss/news"),
    ("ria", "https://ria.ru/export/rss2/archive/index.xml"),
    ("interfax", "https://www.interfax.ru/rss.asp"),
    ("vedomosti", "https://www.vedomosti.ru/rss/news"),
    ("vedomosti_articles", "https://www.vedomosti.ru/rss/articles"),
    ("kommersant", "https://www.kommersant.ru/rss/news.xml"),
    # International
    ("ft", "https://www.ft.com/news?format=rss"),
    ("guardian", "https://www.theguardian.com/world/rss"),
    ("economist", "https://www.economist.com/international/rss.xml"),
    ("aljazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("oilprice", "https://oilprice.com/rss/main"),
    ("marketwatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
]

# Ключевые слова для фильтрации геополитических, макро- и рыночных новостей.
# Слова на русском и английском; вхождение проверяется без учёта регистра.
GEO_KEYWORDS = {
    # Санкции и торговые войны
    "санкц", "sanction", "embargo", "ban", "restrict",
    "торговая война", "trade war", "tariff", "пошлина",
    # Военные конфликты и угрозы
    "военн", "войн", "war", "conflict", "military", "attack", "strike",
    "украин", "ukraine", "russia", "росси", "кремль",
    "иран", "iran", "ормуз", "hormuz", "israel", "израиль", "палестин", "gaza", "газа",
    "китай", "china", "тайвань", "taiwan", "северная корея", "north korea",
    "ядерн", "nuclear", "ракет", "missile", "баллист", "drone", "беспилотник",
    "террор", "terror", "hostage", "заложник", "extremist", "экстремист",
    # Энергетика и сырьё
    "нефт", "oil", "gas", "газ", "opec", "opec+", "production cut", "нефтяной шок",
    " LNG", "СПГ", "алюминий", "aluminum", "никель", "nickel", "медь", "copper",
    "уран", "uranium", "золото", "gold", "редкоземельные", "rare earth",
    "potolok cen", "price cap", "эмбарго", "embargo",
    # Макро и финансовая стабильность
    "цб", "центральный банк", "central bank", "ecb", "fed", "банк англии",
    "ставка", "rate hike", "rate cut", "key rate", "interest rate",
    "инфляц", "inflation", "гиперинфляц", "hyperinflation",
    "рецесс", "recession", "дефолт", "default", "банкротство", "bankruptcy",
    "кризис", "crisis", "обвал", "crash", "flash crash", "market sell-off",
    "банковский кризис", "bank run", "санац", "банк лишился лицензии",
    "валютный кризис", "девальвац", "devaluation", "обесценивание",
    "quantitative easing", "quantitative tightening",
    # Валюта, долг и рейтинги
    "курс рубл", "ruble", "rouble",
    "евробонд", "eurobond", "внешний долг", "external debt",
    "офз", "ОФЗ",
    "moody's", "fitch", "s&p", "credit rating", "downgrade", "upgrade", "рейтинг",
    "asset freeze", "заморозка активов", "арест активов",
    # Военные угрозы — расширение
    "мобилизац", "mobilization", "призыв", "conscription", "draft",
    "взрыв", "explosion", "диверси", "sabotage",
    # Энергетика и логистика — расширение
    "трубопровод", "pipeline", "северный поток", "nord stream",
    "черное море", "black sea", "зерно", "grain", "пшеница", "wheat",
    # Регуляторы и рынки
    "биржа закрыта", "exchange closed", "торги приостановлены", "trading halted",
    "circuit breaker", "валютный контроль", "capital control", "вывод капитала",
    # Природные и техногенные катастрофы
    "пандем", "pandemic", "эпидем", "epidemic", "локдаун", "lockdown",
    "землетрясен", "earthquake", "наводнен", "flood", "торнадо", "tornado",
    "ураган", "hurricane", "пожар", "fire", "авария на аэс", "nuclear accident",
    "разлив нефт", "oil spill", "климат", "climate disaster",
    # Киберугрозы
    "кибератак", "cyberattack", "хакер", "hacker", "утечка данных", "data breach",
}


def _matches_geo_keywords(text: str) -> bool:
    """Проверить, содержит ли текст геополитические ключевые слова."""
    lower = text.lower()
    return any(kw in lower for kw in GEO_KEYWORDS)


def _normalize_sector_name(name: str) -> str:
    """Привести название сектора к единому виду для сопоставления с TICKER_SECTORS."""
    return re.sub(r"[^а-яa-z0-9]", "", name.lower().strip())


# Английские синонимы, которые LLM любит выдавать вместо русских названий секторов.
_SECTOR_ALIASES: dict[str, str] = {
    "metals": "металлы",
    "metal": "металлы",
    "oil": "нефть",
    "oilgas": "нефть",
    "oilandgas": "нефть",
    "gas": "нефть",
    "bank": "банки",
    "banks": "банки",
    "gold": "золото",
    "retail": "ретейл",
    "telecom": "телеком",
    "telecommunications": "телеком",
    "energy": "энергетика",
    "utilities": "энергетика",
    "exchange": "биржа",
    "airlines": "авиа",
    "aviation": "авиа",
    "tech": "it",
    "technology": "it",
    "it": "it",
}


@dataclass
class _RawItem:
    title: str
    summary: str
    source: str
    pub_date: datetime | None
    market_relevance: float = 0.0


class GeoRiskAgent:
    """Агент оценки геополитического риска."""

    SYSTEM_PROMPT = """Ты — геополитический аналитик рынка акций Мосбиржи.
Твоя задача — оценить совокупный геополитический риск и сказать, в какие
секторы лучше заходить, а из каких выходить.

Шкала риска:
0-2 — низкий: спокойная обстановка, можно искать лонги
3-4 — умеренный: риски локальные, позиции уменьшай
5-6 — повышенный: реальные угрозы, волатильность вероятна
7-8 — высокий: серьёзные угрозы, рынок может просесть, выходи из рискованных лонгов
9-10 — критический: шоковые события, ожидай сильного движения, уходи в кэш/шорт защитные секторы

Формат ответа — строго JSON:
{
  "score": 0-10,
  "severity": "low|medium|high|critical",
  "summary": "1-2 предложения: что произошло и как это влияет на рынок",
  "overall_direction": -1 или 0 или +1,
  "affected_sectors": [
    {"sector": "нефть", "direction": -1, "impact": 0.8, "confidence": 8},
    {"sector": "банки", "direction": -1, "impact": 0.6, "confidence": 7},
    {"sector": "золото", "direction": +1, "impact": 0.7, "confidence": 8}
  ],
  "trigger_keywords": ["санкции", "Иран"]
}

Правила для affected_sectors:
- Названия секторов используй только из этого списка: банки, нефть, металлы, золото, it, телеком, ретейл, биржа, энергетика, авиа.
- direction: -1 (сектору плохо, цены падают), +1 (сектору хорошо, цены растут), 0 (нейтрально).
- impact: от 0.1 (слабое влияние) до 1.0 (сильное влияние).
- confidence: от 1 до 10, насколько ты уверен в направлении.
- overall_direction: общее направление рынка (-1 медвежье, 0 нейтрально, +1 бычье).

Примеры:
- Новости о санкциях против нефти → нефть direction=-1, банки direction=-1, золото direction=+1.
- Новости о перемирии/снятии санкций → рынок overall_direction=+1, пострадавшие секторы direction=+1.
- Новости о военной эскалации → overall_direction=-1, защитные активы (золото) direction=+1."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._llm_sem = asyncio.Semaphore(3)  # limit concurrent Ollama calls

    async def scan(self, max_items: int = 5) -> GeoRiskResult | None:
        """Сканировать RSS и вернуть оценку риска.

        Args:
            max_items: максимум новостей для анализа (чтобы не перегружать LLM).

        Returns:
            GeoRiskResult или None если новостей нет.
        """
        items = await self._fetch_geo_news()
        if not items:
            return None

        # Only score the freshest subset to avoid overwhelming the local LLM.
        items.sort(key=lambda x: x.pub_date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        relevance_candidates = items[:30]

        # Score market relevance for each item so low-signal noise does not dilute real risk.
        scored = await asyncio.gather(*[self._score_relevance(i) for i in relevance_candidates])
        relevance_candidates = [item for item, _ in scored]

        MIN_RELEVANCE = 4.0
        items = [i for i in relevance_candidates if i.market_relevance >= MIN_RELEVANCE]
        if not items:
            logger.info(f"GeoRisk: all {len(scored)} news items scored below relevance threshold {MIN_RELEVANCE}")
            return None

        # Берём max_items свежих новостей, уже отфильтрованных по релевантности
        items = items[:max_items]

        # Проверяем, не анализировали ли мы уже этот набор
        fingerprint = "|".join(sorted({i.title for i in items}))
        if fingerprint in self._seen:
            return None
        self._seen.add(fingerprint)

        return await self._analyze(items)

    async def _fetch_geo_news(self) -> list[_RawItem]:
        """Получить геополитические новости из RSS."""
        items: list[_RawItem] = []
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            tasks = [self._fetch_one(client, name, url) for name, url in GEO_RSS_FEEDS]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, list):
                    items.extend(res)
                elif isinstance(res, Exception):
                    logger.warning(f"Geo RSS fetch failed: {res}")
        return items

    async def _fetch_one(self, client: httpx.AsyncClient, name: str, url: str) -> list[_RawItem]:
        """Получить и отфильтровать одну RSS-ленту."""
        body: str | None = None
        try:
            r = await client.get(url, headers={"user-agent": "Mozilla/5.0"}, timeout=20.0)
            r.raise_for_status()
            content_type = r.headers.get("content-type", "")
            if "html" in content_type.lower():
                logger.debug(f"Geo RSS {name} returned HTML, will try fallback")
            else:
                body = r.text
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 410):
                logger.debug(f"Geo RSS {name} {e.response.status_code}, will try fallback")
            else:
                logger.warning(f"Geo RSS {name} HTTP {e.response.status_code}, will try fallback")
        except Exception as e:
            logger.debug(f"Geo RSS {name} direct fetch failed: {e}, will try fallback")

        # Try direct XML parse if body looks like RSS/Atom
        if body:
            stripped = body.lstrip("﻿").lstrip()[:200].lower()
            if stripped.startswith("<") and ("rss" in stripped or "feed" in stripped or "channel" in stripped):
                return self._parse_rss(body, name)

        # Fallback to rss2json API for feeds that are blocked directly
        if body is None:
            return await self._fetch_rss2json(client, name, url)
        return []

    @staticmethod
    def _parse_rss(xml_text: str, source: str) -> list[_RawItem]:
        """Парсить RSS XML и отфильтровать по ключевым словам."""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            logger.debug(f"RSS parse error for {source}")
            return []

        # Handle RSS 2.0 and Atom namespaces
        ns = {"dc": "http://purl.org/dc/elements/1.1/"}
        channel = root.find("channel")
        if channel is not None:
            entries = channel.findall("item")
        else:
            entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")

        results = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

        for entry in entries:
            title_el = entry.find("title")
            title = (title_el.text or "").strip() if title_el is not None else ""

            desc_el = entry.find("description") or entry.find("{http://www.w3.org/2005/Atom}summary")
            summary = (desc_el.text or "").strip() if desc_el is not None else ""

            # Only keep items that match geo keywords
            combined = f"{title} {summary}"
            if not _matches_geo_keywords(combined):
                continue

            # Parse publication date
            pub_date = None
            for tag in ("pubDate", "{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated"):
                el = entry.find(tag)
                if el is not None and el.text:
                    try:
                        from email.utils import parsedate_to_datetime
                        pub_date = parsedate_to_datetime(el.text)
                        break
                    except Exception:
                        try:
                            pub_date = datetime.fromisoformat(el.text.replace("Z", "+00:00"))
                            break
                        except Exception:
                            pass

            # Skip very old items
            if pub_date and pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)
            if pub_date and pub_date < cutoff:
                continue

            results.append(_RawItem(title=title, summary=summary, source=source, pub_date=pub_date))

        return results

    async def _fetch_rss2json(
        self,
        client: httpx.AsyncClient,
        name: str,
        url: str,
    ) -> list[_RawItem]:
        """Fallback fetch geo RSS via rss2json API."""
        try:
            api_url = f"https://api.rss2json.com/v1/api.json?rss_url={url}"
            r = await client.get(api_url, headers={"user-agent": "Mozilla/5.0"}, timeout=20.0)
            r.raise_for_status()
            data = r.json()
            if data.get("status") != "ok":
                return []
            return self._parse_rss2json(data, name)
        except Exception as e:
            logger.debug(f"Geo RSS2JSON fallback for {name} failed: {e}")
            return []

    def _parse_rss2json(self, data: dict[str, Any], source: str) -> list[_RawItem]:
        """Convert rss2json response into _RawItems with geo filtering."""
        results: list[_RawItem] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        for raw in data.get("items", []):
            title = (raw.get("title") or "").strip()
            summary = (raw.get("description") or raw.get("content") or "").strip()
            combined = f"{title} {summary}"
            if not _matches_geo_keywords(combined):
                continue

            pub_date = None
            raw_date = raw.get("pubDate") or ""
            if raw_date:
                try:
                    from email.utils import parsedate_to_datetime
                    pub_date = parsedate_to_datetime(raw_date)
                except Exception:
                    try:
                        pub_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                    except Exception:
                        pass
            if pub_date and pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)
            if pub_date and pub_date < cutoff:
                continue

            results.append(_RawItem(title=title, summary=summary, source=source, pub_date=pub_date))
        return results

    async def _analyze(self, items: list[_RawItem]) -> GeoRiskResult:
        """Отправить отфильтрованные новости в LLM для оценки."""
        if not items:
            return GeoRiskResult(
                score=0,
                severity="low",
                summary="Новостей нет.",
                affected_sectors=[],
                trigger_keywords=[],
                overall_direction=0,
            )

        # Geopolitical topics trigger YandexGPT's safety filter, so this agent
        # always routes through the local Ollama/Gemma instance.
        provider = "ollama"

        lines = []
        for i, item in enumerate(items, 1):
            text = item.title
            if item.summary:
                text += f". {item.summary[:200]}"
            lines.append(f"{i}. [{item.source}] {text}")
        news_block = "\n".join(lines)

        user = f"""Новости ({len(items)} шт., за последние 48 часов):
{news_block}

Оцени совокупный геополитический риск для российского рынка акций и верни JSON
с секторными направлениями и overall_direction."""

        raw = await call_llm(self.SYSTEM_PROMPT, user, max_tokens=512, provider=provider)
        return self._parse(raw, items)

    RELEVANCE_PROMPT = """Ты — фильтр рыночной релевантности для российского рынка акций.

Для поданной новости оцени, насколько она может повлиять на котировки акций на Мосбирже.

Шкала:
0 — совсем не влияет (культура, спорт, бытовые события, социальные льготы)
1-3 — косвенно/локально (региональная политика, мелкие инциденты)
4-6 — заметно (секторальные санкции, цены на сырьё, ключевая макро, военные события с экономическим следствием)
7-8 — сильно (эскалация конфликта, глобальный энергетический шок, резкие санкции)
9-10 — критично (шок, способный двигать всем рынком)

Верни только JSON:
{\"relevance\": 0-10, \"rationale\": \"1 предложение\"}"""

    async def _score_relevance(self, item: _RawItem) -> tuple[_RawItem, float]:
        """Estimate market relevance of a single news item via LLM."""
        text = item.title
        if item.summary:
            text += f". {item.summary[:250]}"
        user = f"""Новость:
[{item.source}] {text}

Оцени рыночную релевантность для Мосбиржи."""
        try:
            async with self._llm_sem:
                raw = await call_llm(self.RELEVANCE_PROMPT, user, max_tokens=128, provider="ollama")
            text_clean = raw.strip()
            # Strip markdown code fences if present
            if text_clean.startswith("```"):
                text_clean = text_clean.split("\n", 1)[1]
            if text_clean.endswith("```"):
                text_clean = text_clean.rsplit("\n", 1)[0]
            text_clean = text_clean.strip()
            match = re.search(r"\{.*\}", text_clean, re.DOTALL)
            if match:
                text_clean = match.group(0)
            data = json.loads(text_clean)
            relevance = float(data.get("relevance", 0))
        except Exception as e:
            logger.debug(f"Relevance scoring failed for {item.source}: {e}")
            relevance = 5.0
        item.market_relevance = max(0.0, min(10.0, relevance))
        return item, item.market_relevance

    @staticmethod
    async def _llm_available() -> bool:
        """Проверить, настроен ли хотя бы один LLM-провайдер."""
        from core.config import ANTHROPIC_API_KEY, GEMINI_API_KEY, LLM_PROVIDER
        return LLM_PROVIDER in ("ollama",) or bool(ANTHROPIC_API_KEY or GEMINI_API_KEY)

    @staticmethod
    def _direction_from_text(text: str) -> int:
        """Эвристика: определить направление по неструктурированному тексту."""
        lowered = text.lower()
        if any(w in lowered for w in ("рост", "расти", "быч", "bull", "позитив", "снятие санкц", "улучш")):
            return 1
        if any(w in lowered for w in ("паден", "падают", "медвеж", "bear", "негатив", "санкц", "эскалац", "кризис")):
            return -1
        return 0

    @staticmethod
    def _compute_overall_direction(score: int) -> int:
        """Перевести итоговый score в общее направление рынка."""
        if score >= GEORISK_EXIT_THRESHOLD:
            return -1
        if score <= GEORISK_BULLISH_THRESHOLD:
            return 1
        return 0

    @staticmethod
    def _parse(raw: str, items: list[_RawItem]) -> GeoRiskResult:
        """Извлечь JSON из ответа LLM."""
        import re

        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            text = match.group(0)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Fallback: estimate from text
            lowered = raw.lower()
            score = 3
            if any(w in lowered for w in ("критическ", "critical", "шок", "collapse")):
                score = 9
            elif any(w in lowered for w in ("высок", "high", "серьёзн", "serious")):
                score = 7
            elif any(w in lowered for w in ("умеренн", "moderate", "повышен")):
                score = 5

            fallback_dir = GeoRiskAgent._direction_from_text(raw)
            if fallback_dir == 0:
                fallback_dir = -1 if score >= 7 else 0

            news_items = [
                {
                    "title": i.title,
                    "source": i.source,
                    "pub_date": i.pub_date.isoformat() if i.pub_date else None,
                    "market_relevance": i.market_relevance,
                }
                for i in items
            ]
            return GeoRiskResult(
                score=score,
                severity="medium" if score >= 5 else "low",
                summary=raw[:300],
                affected_sectors=[],
                trigger_keywords=[i.title for i in items][:3],
                overall_direction=fallback_dir,
                news_items=news_items,
            )

        score = data.get("score", 3)
        try:
            score = int(score)
        except Exception:
            score = 3
        score = max(0, min(10, score))

        sev = data.get("severity", "low")
        if sev not in ("low", "medium", "high", "critical"):
            sev = "low"

        summary_text = str(data.get("summary", "—"))[:500]

        sectors = data.get("affected_sectors") or []
        if isinstance(sectors, str):
            sectors = [sectors]

        # Some small LLMs wrap the real JSON inside the summary field. If the top-level
        # response has no useful sector data, try to extract a nested JSON object.
        if not sectors and summary_text.strip().startswith("```"):
            inner_match = re.search(r"\{.*\}", summary_text, re.DOTALL)
            if inner_match:
                try:
                    inner = json.loads(inner_match.group(0))
                    if isinstance(inner.get("affected_sectors"), list):
                        sectors = inner["affected_sectors"]
                    if inner.get("summary"):
                        summary_text = str(inner["summary"])[:500]
                    if inner.get("severity") in ("low", "medium", "high", "critical"):
                        sev = inner["severity"]
                    inner_score = inner.get("score")
                    if inner_score is not None:
                        try:
                            score = max(0, min(10, int(inner_score)))
                        except Exception:
                            pass
                except Exception:
                    pass

        parsed_sectors: list[dict] = []
        allowed = {"банки", "нефть", "металлы", "золото", "it", "телеком", "ретейл", "биржа", "энергетика", "авиа"}

        for s in sectors:
            if isinstance(s, dict):
                name = str(s.get("sector") or "").strip()
                direction = s.get("direction", 0)
                try:
                    direction = int(direction)
                except Exception:
                    direction = 0
                if direction not in (-1, 0, 1):
                    direction = 0
                impact = s.get("impact", 0.5)
                try:
                    impact = float(impact)
                except Exception:
                    impact = 0.5
                confidence = s.get("confidence", 5)
                try:
                    confidence = int(confidence)
                except Exception:
                    confidence = 5
            elif isinstance(s, str):
                name = s.strip()
                # No explicit direction in legacy string format — infer from total score.
                direction = -1 if score >= GEORISK_EXIT_THRESHOLD else 0
                impact = 0.5
                confidence = 5
            else:
                continue

            if not name:
                continue
            norm = _normalize_sector_name(name)
            matched = next((a for a in allowed if _normalize_sector_name(a) == norm), None)
            if matched is None:
                matched = _SECTOR_ALIASES.get(norm)
            if matched is None:
                # If the name looks like a plausible sector, keep it normalized.
                matched = name.lower().strip()
            parsed_sectors.append({
                "sector": matched,
                "direction": max(-1, min(1, direction)),
                "impact": max(0.0, min(1.0, impact)),
                "confidence": max(1, min(10, confidence)),
            })

        # Deduplicate sectors by normalized name, keep strongest absolute impact.
        seen: dict[str, dict] = {}
        for s in parsed_sectors:
            key = _normalize_sector_name(s["sector"])
            if key not in seen or abs(s["impact"]) > abs(seen[key]["impact"]):
                seen[key] = s
        parsed_sectors = list(seen.values())[:5]

        keywords = data.get("trigger_keywords") or []
        if isinstance(keywords, str):
            keywords = [keywords]

        overall = data.get("overall_direction")
        if overall is None:
            overall = GeoRiskAgent._compute_overall_direction(score)
        try:
            overall = int(overall)
        except Exception:
            overall = 0
        if overall not in (-1, 0, 1):
            overall = GeoRiskAgent._compute_overall_direction(score)

        news_items = [
            {
                "title": i.title,
                "source": i.source,
                "pub_date": i.pub_date.isoformat() if i.pub_date else None,
                "market_relevance": i.market_relevance,
            }
            for i in items
        ]

        return GeoRiskResult(
            score=score,
            severity=sev,
            summary=summary_text,
            affected_sectors=parsed_sectors,
            trigger_keywords=list(keywords)[:5],
            overall_direction=overall,
            news_items=news_items,
        )


# Глобальный экземпляр
agent = GeoRiskAgent()

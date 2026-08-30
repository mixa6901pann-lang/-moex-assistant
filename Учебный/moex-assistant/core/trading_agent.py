"""TradingAgent — ReAct-style autonomous trading agent.

Architecture (inspired by hack-moex leader):
  1. Observe market state (prices, news, geo-risk)
  2. Think: what data do I need?
  3. Act: call tools (get_price, get_news, calculate_indicator)
  4. Observe results
  5. Repeat until confident, then decide: buy / sell / hold / wait

Uses OpenRouter (Claude Sonnet 4.6) for heavy reasoning.
Local Gemma3:4b can be used for simple observations.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Awaitable

from loguru import logger

from core.llm import call_llm
from core.codeact import execute_python


@dataclass(frozen=True)
class TradingDecision:
    """Final decision from the agent."""

    action: str  # buy | sell | hold | wait
    ticker: str
    confidence: int  # 0-100
    reasoning: str
    price: float | None = None
    qty: int = 1


# Available tools that LLM can call
tools_registry: dict[str, Callable[..., Awaitable[str]]] = {}


def register_tool(name: str):
    """Decorator to register a tool in the agent's toolkit."""
    def decorator(func: Callable[..., Awaitable[str]]):
        tools_registry[name] = func
        return func
    return decorator


# ── Tool implementations ──────────────────────────────────────

@register_tool("get_price")
async def tool_get_price(ticker: str) -> str:
    """Fetch latest price and OHLCV for a ticker."""
    from core.moex import MoexClient
    moex = MoexClient()
    try:
        candles = await moex.candles_recent(ticker.upper(), interval="1d", count=30)
        if not candles:
            return f"[Нет данных для {ticker}]"
        last = candles[-1]
        prev = candles[-2] if len(candles) > 1 else last
        change = (float(last["close"]) - float(prev["close"])) / float(prev["close"]) * 100 if prev else 0
        return (
            f"{ticker.upper()}: close={last['close']}, open={last['open']}, "
            f"high={last['high']}, low={last['low']}, volume={last['volume']}, "
            f"change={change:.2f}%"
        )
    except Exception as e:
        return f"[Ошибка: {e}]"


@register_tool("get_indicator")
async def tool_get_indicator(ticker: str, indicator: str) -> str:
    """Calculate a technical indicator for a ticker."""
    from strategies.indicators import df_from_candles, add_indicators
    from core.moex import MoexClient
    moex = MoexClient()
    try:
        candles = await moex.candles_recent(ticker.upper(), interval="1d", count=100)
        if not candles:
            return f"[Нет данных для {ticker}]"
        df = df_from_candles(candles)
        df = add_indicators(df)
        last = df.iloc[-1]
        val = last.get(indicator.lower())
        if val is None or str(val) == "nan":
            return f"[Индикатор {indicator} недоступен. Доступные: rsi, macd_hist, atr, adx, bb_pct, sma_20, sma_50, vol_ratio]"
        return f"{ticker.upper()} {indicator.upper()} = {float(val):.4f}"
    except Exception as e:
        return f"[Ошибка: {e}]"


@register_tool("get_news")
async def tool_get_news(ticker: str) -> str:
    """Fetch latest news headlines for a ticker.

    Uses the local RSS cache first; only falls back to live fetching
    if nothing fresh is cached, so the agent stays fast.
    """
    from core import db as _db
    try:
        cached = await _db.get_recent_rss_headlines(ticker.upper(), hours=4, limit=5)
        if cached:
            lines = [f"- [{n['source']}] {n['headline']}" for n in cached]
            return f"Новости по {ticker} (из кэша):\n" + "\n".join(lines)
    except Exception:
        pass

    from core.rss_feed import RssNewsAggregator
    agg = RssNewsAggregator()
    try:
        items = await agg.fetch_for_ticker(ticker.upper(), max_age_minutes=120)
        if not items:
            return f"[Новостей по {ticker} за 2 часа не найдено]"
        lines = [f"- {item.title}" for item in items[:5]]
        return f"Новости по {ticker}:\n" + "\n".join(lines)
    except Exception as e:
        return f"[Ошибка: {e}]"


def _sanitize_geo_summary(text: str) -> str:
    """Remove geopolitically-sensitive words that trigger LLM safety filters.

    Local models (Gemma) and cloud models (YandexGPT) may refuse to discuss
    armed-conflict keywords. We keep the meaning but use neutral market terms.
    """
    replacements = {
        r"Украин[аеы]?": "регион Восточной Европы",
        r"украин[аеы]?": "регион Восточной Европы",
        r"военн[а-я]* обстановк[а-я]*": "региональная напряжённость",
        r"военн[а-я]* конфликт[а-я]*": "региональный конфликт",
        r"войн[аеыуой]*": "военные действия",
        r"потер[ийьи]*": "операционные риски",
        r"мобилизац[а-я]*": "кадровое перераспределение",
        r"Крым[а-я]*": "регион Чёрного моря",
        r"Донбас[с-я]*": "восточный регион",
        r"Газ[а-я]*": "сектор безопасности",
    }
    import re
    out = text
    for pattern, repl in replacements.items():
        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)
    return out


@register_tool("get_georisk")
async def tool_get_georisk() -> str:
    """Fetch current geopolitical risk score."""
    from core import db
    geo = await db.get_latest_georisk()
    if not geo:
        return "[Нет данных о геополитическом риске]"
    summary = _sanitize_geo_summary(geo["summary"]) if geo.get("summary") else ""
    return (
        f"GeoRisk: {geo['score']}/10 ({geo['severity']}). "
        f"{summary[:200]}. "
        f"Секторы: {', '.join(geo['affected_sectors']) or 'нет'}"
    )


@register_tool("search_web")
async def tool_search_web(query: str) -> str:
    """Search RSS feeds for information matching the query."""
    from core.rss_feed import RssNewsAggregator
    from core.georisk_agent import GeoRiskAgent

    keywords = [w.lower() for w in query.split() if len(w) > 3]
    if not keywords:
        keywords = [query.lower()]

    # 1. Search financial RSS for ticker-related news
    agg = RssNewsAggregator()
    financial_hits = []
    for ticker in ["SBER", "GAZP", "LKOH", "NVTK", "ROSN", "YNDX"]:
        if ticker.lower() in query.lower():
            try:
                items = await agg.fetch_for_ticker(ticker, max_age_minutes=360)
                for item in items:
                    if any(kw in item.title.lower() for kw in keywords):
                        financial_hits.append(f"[{ticker}] {item.title}")
            except Exception:
                pass

    # 2. Search geo-political RSS
    geo_agent = GeoRiskAgent()
    geo_items = await geo_agent._fetch_geo_news()
    geo_hits = []
    for item in geo_items:
        if any(kw in (item.title + " " + item.summary).lower() for kw in keywords):
            geo_hits.append(f"[GEO] {item.title}")

    all_hits = list(dict.fromkeys(financial_hits + geo_hits))[:10]
    if not all_hits:
        return f"[По запросу '{query}' ничего не найдено в RSS-лентах]"
    return "Найдено в RSS:\n" + "\n".join(all_hits)


@register_tool("execute_python")
async def tool_execute_python(code: str) -> str:
    """Execute Python code for custom calculations."""
    return execute_python(code)


# ── ReAct loop ────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — автономный торговый агент Мосбиржи.

Твоя задача: проанализировать ситуацию и принять одно торговое решение.

Правила:
1. Торгуй ТОЛЬКО разрешёнными тикерами.
2. Если не уверен — говори "hold" или "wait".
3. Покупай по ASK, продавай по BID.
4. Учитывай геополитический риск — при высоком риске не открывай новые позиции.
5. Обосновывай решение конкретно.
6. НЕ проси пользователя предоставить данные — используй инструменты сам.

Доступные инструменты:
- get_price(ticker) — цена и свечи
- get_indicator(ticker, indicator) — индикатор (rsi, macd_hist, atr, adx, bb_pct, sma_20, sma_50, vol_ratio)
- get_news(ticker) — новости
- get_georisk() — геополитический риск
- search_web(query) — поиск по RSS-лентам (новости + геополитика)
- execute_python(code) — выполнить Python-код

Формат вызова инструмента (строго, один инструмент на строку):
ДЕЙСТВИЕ: get_price("SBER")
ДЕЙСТВИЕ: get_indicator("SBER", "rsi")
ДЕЙСТВИЕ: get_georisk()

Формат итогового решения:
РЕШЕНИЕ: {"action": "buy|sell|hold|wait", "ticker": "SBER", "confidence": 75, "reasoning": "почему", "price": 280.5, "qty": 1}

Текущие позиции и портфель уже включены в системный промпт."""


class TradingAgent:
    """ReAct-style agent that iterates observation-thought-action."""

    def __init__(self, max_iterations: int = 5) -> None:
        self.max_iterations = max_iterations

    async def decide(self, ticker: str, provider: str | None = None) -> TradingDecision:
        """Run the ReAct loop for a single ticker and return a decision.

        Args:
            provider: override LLM provider for this call (e.g. "ollama" for testing).

        Notes:
            If this call creates the shared DB connection (typical for one-off
            scripts/tests), it is closed on exit so the process terminates
            cleanly. In a long-running server the connection stays open.
        """
        from core.llm import render_system_prompt
        from core import db as _db

        db_was_open = _db.is_db_connected()

        dynamic_prompt = await render_system_prompt()
        system = f"{SYSTEM_PROMPT}\n\n{dynamic_prompt}"

        context = f"Нужно принять решение по тикеру {ticker.upper()}."
        observations: list[str] = []

        log_path = Path("logs/react_trace.jsonl")
        log_path.parent.mkdir(parents=True, exist_ok=True)

        def _log(phase: str, payload: dict):
            try:
                entry = {
                    "ts": datetime.now().isoformat(),
                    "ticker": ticker.upper(),
                    "step": step,
                    "phase": phase,
                    **payload,
                }
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass

        fallback_used = False

        for step in range(1, self.max_iterations + 1):
            # Build prompt with all previous observations
            obs_block = "\n".join(f"Наблюдение {i}: {o}" for i, o in enumerate(observations, 1))
            user = f"{context}\n\n{obs_block}\n\nПодумай, какие данные нужны. Если хвати данных — дай РЕШЕНИЕ."

            _log("prompt", {"system": system, "user": user})
            response = await call_llm(system, user, max_tokens=512, provider=provider)
            _log("response", {"text": response})

            # Abort early if LLM is disabled or Ollama is unreachable
            if response.startswith("[LLM отключён]"):
                logger.warning(f"LLM disabled for {ticker}; aborting ReAct loop")
                break
            if response.startswith("[Ollama"):
                logger.warning(f"Ollama error for {ticker}: {response}")
                break

            # Safety-filter fallback: if the cloud model (YandexGPT) refuses the topic,
            # retry once with the local uncensored model and restart the loop.
            if (
                not fallback_used
                and provider != "ollama"
                and any(phrase in response.lower() for phrase in ("не могу обсуждать", "давайте поговорим", "can't discuss", "не могу помочь"))
            ):
                logger.warning(f"TradingAgent {ticker}: LLM refused, falling back to ollama")
                provider = "ollama"
                observations.clear()
                fallback_used = True
                continue

            # Check if final decision is present
            decision = self._parse_decision(response)
            if decision:
                _log("decision", {"action": decision.action, "confidence": decision.confidence, "reasoning": decision.reasoning, "price": decision.price})
                logger.info(f"TradingAgent decided: {decision.action} {decision.ticker} (conf={decision.confidence})")
                if not db_was_open and _db.is_db_connected():
                    try:
                        await _db.close_db()
                    except Exception:
                        pass
                return decision

            # Extract tool calls
            tool_calls = self._extract_actions(response)
            _log("tool_calls", {"calls": [[name, args] for name, args in tool_calls]})
            if not tool_calls:
                # No tool calls and no decision — agent is unsure
                logger.info(f"TradingAgent step {step}: no action, waiting")
                observations.append("Агент не запросил данные и не принял решение.")
                _log("observation", {"text": "Агент не запросил данные и не принял решение."})
                continue

            # Execute tools
            for tool_name, args in tool_calls:
                if tool_name not in tools_registry:
                    observations.append(f"[Инструмент {tool_name} не найден]")
                    _log("observation", {"tool": tool_name, "text": f"[Инструмент {tool_name} не найден]"})
                    continue
                try:
                    result = await tools_registry[tool_name](**args)
                    observations.append(result)
                    _log("observation", {"tool": tool_name, "args": args, "text": result})
                except Exception as e:
                    observations.append(f"[Ошибка {tool_name}: {e}]")
                    _log("observation", {"tool": tool_name, "args": args, "text": f"[Ошибка {tool_name}: {e}]"})

        # Fallback if max iterations reached without decision
        logger.warning(f"TradingAgent max iterations reached for {ticker}, defaulting to hold")
        decision = TradingDecision(action="hold", ticker=ticker, confidence=0, reasoning="Не удалось принять решение за 5 шагов")

        # In one-off script usage close the DB connection we created so the
        # process exits promptly. The server keeps its own connection alive.
        if not db_was_open and _db.is_db_connected():
            try:
                await _db.close_db()
            except Exception:
                pass
        return decision

    @staticmethod
    def _parse_decision(text: str) -> TradingDecision | None:
        """Extract final JSON decision from response."""
        match = re.search(r'РЕШЕНИЕ:\s*(\{.*\})', text, re.DOTALL)
        if not match:
            # Also try just a JSON block
            match = re.search(r'\{[^}]*"action"[^}]*\}', text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(1))
            return TradingDecision(
                action=data.get("action", "hold"),
                ticker=data.get("ticker", ""),
                confidence=int(data.get("confidence", 0)),
                reasoning=data.get("reasoning", ""),
                price=float(data["price"]) if data.get("price") else None,
                qty=int(data.get("qty", 1)),
            )
        except Exception:
            return None

    @staticmethod
    def _extract_actions(text: str) -> list[tuple[str, dict[str, Any]]]:
        """Extract tool calls like ДЕЙСТВИЕ: get_price("SBER") or get_price(SBER).

        Supports: get_price(TICKER), get_indicator(TICKER, "rsi"),
        get_indicator(ticker="TICKER", indicator="rsi"), get_news(TICKER).
        """
        results = []
        for line in text.splitlines():
            m = re.search(r'ДЕЙСТВИЕ:\s*(\w+)\s*\((.*)\)', line)
            if not m:
                continue
            tool_name = m.group(1)
            args_raw = m.group(2).strip()
            args: dict[str, Any] = {}

            # 1. key=value pairs
            kv_pattern = re.findall(r'(\w+)\s*=\s*"([^"]*)"', args_raw)
            for k, v in kv_pattern:
                args[k] = v

            if args:
                results.append((tool_name, args))
                continue

            # 2. positional arguments: split by comma, strip quotes
            raw_args = [a.strip().strip('"').strip("'") for a in args_raw.split(",") if a.strip()]
            if not raw_args:
                results.append((tool_name, args))
                continue

            if tool_name == "get_price":
                args = {"ticker": raw_args[0]}
            elif tool_name == "get_news":
                args = {"ticker": raw_args[0]}
            elif tool_name == "get_indicator":
                args = {"ticker": raw_args[0]}
                if len(raw_args) > 1:
                    args["indicator"] = raw_args[1]
            elif tool_name == "execute_python":
                args = {"code": args_raw}
            results.append((tool_name, args))
        return results


# Global instance
agent = TradingAgent()

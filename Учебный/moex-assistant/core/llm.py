"""Low-level LLM client — wraps Anthropic Claude and Ollama.

Used by both analyzer.py and sentiment_agent.py to avoid circular imports.
Supports dynamic Jinja2 system prompts with live portfolio injection
(inspired by hack-moex leader Alexander-Panov/ai-trader).
"""

from __future__ import annotations

import httpx
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from loguru import logger

from core.config import (
    ANTHROPIC_API_KEY,
    LLM_PROVIDER,
    OLLAMA_URL,
    OLLAMA_MODEL,
    OLLAMA_NUM_CTX,
    OLLAMA_TIMEOUT,
    WATCHLIST,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GEMINI_URL,
    YANDEX_API_KEY,
    YANDEX_FOLDER_ID,
    YANDEX_MODEL,
    LLM_FALLBACK_ORDER,
    CODEACT_ENABLED,
)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"


class GeminiError(Exception):
    """Raised when Gemini API returns a quota or safety error."""
    pass

_WHITELIST_STR = ", ".join(WATCHLIST)

# Legacy static prompt for backward compatibility
STATIC_SYSTEM_PROMPT = ("""Ты — финансовый аналитик Мосбиржи. Отвечай кратко, по делу.
Учитывай: Т+1, вечерняя сессия, дивидендные отсечки, гэпы, санкционные риски, низкую ликвидность 2-3 эшелона.
Используй техническую терминологию, но объясняй суть.

Ты можешь анализировать и рекомендовать торговлю ТОЛЬКО по следующим тикерам: {_WHITELIST_STR}.
Не упоминай и не рекомендуй другие акции.""".format(_WHITELIST_STR=_WHITELIST_STR))

# Backward-compat alias used by analyzer.py / sentiment_agent.py
SYSTEM_PROMPT = STATIC_SYSTEM_PROMPT

# Jinja2 environment for dynamic prompts
_TPL_DIR = Path(__file__).resolve().parent / "prompt_templates"
_jinja_env = Environment(loader=FileSystemLoader(str(_TPL_DIR))) if _TPL_DIR.exists() else None

# Track which provider actually served the last LLM call (used for prediction logging).
_last_used_provider: str | None = None


def get_last_used_provider() -> str | None:
    """Return the provider that succeeded on the most recent LLM call."""
    return _last_used_provider


def _is_error_response(text: str) -> bool:
    """Heuristic: provider error markers start with '[' and end with ']'."""
    if not text:
        return True
    text = text.strip()
    return text.startswith("[") and text.endswith("]")


def _is_refusal_response(text: str) -> bool:
    """Detect when a provider's safety filter blocks the topic."""
    if not text:
        return False
    lowered = text.lower()
    return any(phrase in lowered for phrase in (
        "не могу обсуждать",
        "давайте поговорим",
        "can't discuss",
        "cannot discuss",
        "не могу помочь",
        "i can't help",
        "i'm sorry",
        "извините",
        "я не могу",
        "i cannot",
        "не в состоянии",
    ))


def sanitize_for_llm(text: str) -> str:
    """Replace politically-sensitive words that trigger cloud LLM safety filters.

    Used on news snippets and GeoRisk summaries before sending them to the LLM.
    Keeps the market meaning while avoiding broad-topic refusals.
    """
    import re
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
        r"иран[а-я]*": "Ближний Восток",
        r"израил[а-я]*": "Ближний Восток",
        r"палестин[а-я]*": "Ближний Восток",
        r"газ[а-я]*": "сектор безопасности",
    }
    out = text
    for pattern, repl in replacements.items():
        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)
    return out


async def render_system_prompt() -> str:
    """Render dynamic system prompt with live portfolio state.

    Falls back to STATIC_SYSTEM_PROMPT if Jinja2 templates unavailable.
    """
    if _jinja_env is None:
        return STATIC_SYSTEM_PROMPT

    try:
        template = _jinja_env.get_template("system.j2")
    except Exception:
        return STATIC_SYSTEM_PROMPT

    # Lazy imports to avoid circular deps
    import asyncio
    from core import db
    from core.moex import MoexClient

    moex = MoexClient()

    # Fetch portfolio and quotes in parallel
    positions, quotes_raw, cash, equity = [], {}, 0.0, 0.0

    try:
        open_paper = await db.get_open_paper_positions()
        for pos in open_paper:
            positions.append({
                "ticker": pos["ticker"],
                "qty": pos.get("qty", 1),
                "side": pos.get("side", "long"),
                "unrealized_pnl": float(pos.get("unrealized_pnl", 0) or 0),
            })
    except Exception:
        pass

    try:
        stats = await db.get_paper_stats(since_days=30)
        cash = float(stats.get("available_cash", 0) or 0)
        equity = float(stats.get("total_equity", cash) or cash)
    except Exception:
        pass

    # Fetch live quotes for top 5 holdings + liquid tickers
    tickers_to_quote = list({p["ticker"] for p in positions})[:5]
    tickers_to_quote += ["SBER", "GAZP", "LKOH", "GMKN", "NVTK"]
    tickers_to_quote = list(dict.fromkeys(tickers_to_quote))  # dedup, keep order

    async def _quote(ticker: str):
        try:
            candles = await moex.candles_recent(ticker, interval="1h", count=1)
            if candles:
                c = candles[-1]
                return {
                    "ticker": ticker,
                    "bid": float(c.get("low", c.get("close", 0))),
                    "ask": float(c.get("high", c.get("close", 0))),
                }
        except Exception:
            pass
        return None

    quote_results = await asyncio.gather(*[_quote(t) for t in tickers_to_quote])
    quotes = [q for q in quote_results if q]

    return template.render(
        datetime=datetime.now().strftime("%Y-%m-%d %H:%M"),
        whitelist=_WHITELIST_STR,
        positions=positions,
        quotes=quotes,
        cash=cash,
        equity=equity,
    )


def _extract_code_blocks(text: str) -> list[str]:
    """Extract Python code blocks from LLM response."""
    import re
    blocks = []
    # Match ```python ... ``` blocks
    for m in re.finditer(r"```python\n(.*?)\n```", text, re.DOTALL):
        blocks.append(m.group(1))
    # Also match plain ``` ... ``` blocks that look like Python (contain '=', 'def', 'import')
    for m in re.finditer(r"```\n(.*?)\n```", text, re.DOTALL):
        candidate = m.group(1)
        if any(kw in candidate for kw in ("=", "def ", "import ", "print(", "for ", "if ", "math.")):
            blocks.append(candidate)
    return blocks


async def call_llm(system: str, user: str, max_tokens: int = 512, provider: str | None = None) -> str:
    """Route to chosen LLM provider.

    Args:
        provider: override provider for this call only.
                  Use 'gemini' for cloud, 'ollama' for local testing.

    After receiving the response, scans for Python code blocks and
    executes them via CodeAct (if present), appending the output.
    """
    global _last_used_provider
    prov = (provider or LLM_PROVIDER).lower()
    if prov == "none":
        return "[LLM отключён]"
    if prov == "ollama":
        response = await _call_ollama(system, user, max_tokens)
    elif prov == "gemini":
        response = await _call_gemini(system, user, max_tokens)
    elif prov == "yandex":
        response = await _call_yandexgpt(system, user, max_tokens)
    else:
        response = await _call_claude(system, user, max_tokens)

    # Record which provider actually produced the response.
    _last_used_provider = prov

    # Detect safety-filter refusals even on direct calls so callers can handle them.
    if _is_refusal_response(response):
        logger.warning(f"{prov} returned a safety-filter refusal")

    # --- CodeAct integration (disabled by default for safety) ---
    if CODEACT_ENABLED:
        code_blocks = _extract_code_blocks(response)
        if code_blocks:
            from core.codeact import execute_python
            extra = "\n\n[CodeAct результат:\n"
            for i, code in enumerate(code_blocks, 1):
                result = execute_python(code)
                extra += f"--- Блок {i} ---\n{result}\n"
            extra += "]"
            response = response + extra
    return response


async def call_llm_with_fallback(
    system: str,
    user: str,
    max_tokens: int = 512,
    providers: list[str] | None = None,
) -> str:
    """Try providers in order until one returns a valid response.

    Treats both technical errors and safety-filter refusals as failures,
    so a censored cloud model automatically falls back to the next provider.
    Falls back to LLM_FALLBACK_ORDER env var if providers not supplied.
    Updates _last_used_provider to the provider that succeeded.
    """
    global _last_used_provider
    order = [p.lower() for p in (providers or LLM_FALLBACK_ORDER) if p]
    tried: list[str] = []

    for prov in order:
        response = await call_llm(system, user, max_tokens, provider=prov)
        if _is_refusal_response(response):
            logger.warning(f"{prov} refused the prompt; trying next provider")
            tried.append(f"{prov} (refusal)")
            continue
        if not _is_error_response(response):
            _last_used_provider = prov
            return response
        tried.append(prov)

    _last_used_provider = None
    return f"[Все LLM-провайдеры недоступны: {', '.join(tried)}]"


async def _call_yandexgpt(system: str, user: str, max_tokens: int = 512) -> str:
    """Call YandexGPT Foundation Models API."""
    if not YANDEX_API_KEY:
        return "[YANDEX_API_KEY не задан]"
    if not YANDEX_FOLDER_ID:
        return "[YANDEX_FOLDER_ID не задан]"

    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/{YANDEX_MODEL}",
        "completionOptions": {
            "stream": False,
            "temperature": 0.1,
            "maxTokens": str(max_tokens),
        },
        "messages": [
            {"role": "system", "text": system},
            {"role": "user", "text": user},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, headers=headers, json=payload)
            if r.status_code == 401:
                return "[YandexGPT: неверный Api-Key]"
            if r.status_code == 429:
                return "[YandexGPT: превышен лимит запросов]"
            r.raise_for_status()
            data = r.json()
            alternatives = data.get("result", {}).get("alternatives", [])
            if not alternatives:
                return "[YandexGPT: пустой ответ]"
            return alternatives[0].get("message", {}).get("text", "[YandexGPT: нет текста]")
    except httpx.TimeoutException:
        return "[YandexGPT timeout — попробуйте позже]"
    except Exception as e:
        return f"[YandexGPT error: {e}]"


async def _call_ollama(system: str, user: str, max_tokens: int = 512) -> str:
    """Call local Ollama API."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {
            "num_predict": max_tokens,
            "temperature": 0.1,
            "num_ctx": OLLAMA_NUM_CTX,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            r = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
            return data.get("message", {}).get("content", "[пустой ответ]")
    except httpx.ConnectError:
        return f"[Ollama недоступен: {OLLAMA_URL}]"
    except httpx.TimeoutException:
        return "[Ollama timeout — попробуйте позже]"
    except Exception as e:
        return f"[Ollama error: {e}]"


async def _call_claude(system: str, user: str, max_tokens: int = 512) -> str:
    """Call Claude API directly via httpx."""
    if not ANTHROPIC_API_KEY:
        return "[ANTHROPIC_API_KEY не задан]"

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(ANTHROPIC_API_URL, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        return data["content"][0]["text"]


async def _call_gemini(system: str, user: str, max_tokens: int = 512) -> str:
    """Call Google Gemini API (Generative Language API, free tier)."""
    if not GEMINI_API_KEY:
        return "[GEMINI_API_KEY не задан]"

    url = f"{GEMINI_URL}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.1,
        },
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, json=payload)
        if r.status_code == 429:
            raise GeminiError("Gemini: 429 Quota exceeded")
        r.raise_for_status()
        data = r.json()

    candidates = data.get("candidates", [])
    if not candidates:
        return "[Gemini: пустой ответ]"
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts) or "[Gemini: нет текста]"

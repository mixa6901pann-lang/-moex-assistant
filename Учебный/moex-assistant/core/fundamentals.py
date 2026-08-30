"""Fundamental metrics for Russian stocks via MOEX API.

Provides dividend yield, market cap, and valuation scoring.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from core.moex import MoexClient


@dataclass
class Fundamentals:
    """Container for fundamental data."""
    ticker: str
    price: float | None
    market_cap: float | None  # in rubles
    last_dividend: float | None
    div_yield: float | None  # in percent
    div_registry_date: str | None


def compute_div_yield(last_dividend: float | None, price: float | None) -> float | None:
    """Compute annual dividend yield in percent."""
    if last_dividend and price and price > 0:
        return round(last_dividend / price * 100, 2)
    return None


def is_blue_chip(market_cap: float | None) -> bool:
    """Rough classification: > 1T RUB is blue chip territory."""
    return market_cap is not None and market_cap > 1_000_000_000_000


def fundamental_score(div_yield: float | None, market_cap: float | None) -> dict:
    """Score fundamentals 0-100. Higher = more attractive for longs.

    Returns dict with score and warnings.
    """
    score = 50.0
    warnings: list[str] = []

    # Dividend yield scoring
    if div_yield is not None:
        if div_yield > 7:
            score += 20
            warnings.append(f"Дивдоход {div_yield:.1f}% — высокий, возможно недооценена")
        elif div_yield > 4:
            score += 10
        elif div_yield < 1:
            score -= 20
            warnings.append(f"Дивдоход {div_yield:.1f}% — низкий, акция возможно переоценена")
        elif div_yield < 2:
            score -= 10

    # Market cap / size scoring (large caps = stability)
    if market_cap is not None:
        if market_cap > 1_000_000_000_000:  # > 1T
            score += 5
        elif market_cap < 50_000_000_000:  # < 50B = small cap
            score -= 10
            warnings.append("Мелкая компания — высокая волатильность")

    return {"score": round(max(0, min(100, score)), 1), "warnings": warnings}


def is_overvalued(div_yield: float | None, rsi: float | None) -> tuple[bool, str | None]:
    """Check if stock looks overvalued. Returns (is_overvalued, reason)."""
    if div_yield is not None and div_yield < 1 and rsi is not None and rsi > 70:
        return True, f"RSI {rsi:.1f} + дивдоход {div_yield:.1f}% — акция выглядит перекупленной"
    return False, None


async def fetch_fundamentals(ticker: str) -> Fundamentals:
    """Fetch dividends and market cap for a ticker."""
    client = MoexClient()

    # Get latest price and market cap
    price = None
    market_cap = None
    try:
        payload = await client._get(
            f"engines/stock/markets/shares/boards/TQBR/securities/{ticker}",
            params={"marketdata.columns": "LAST,ISSUECAPITALIZATION"},
        )
        md = client._parse_table(payload, "marketdata")
        if md:
            price = float(md[0]["LAST"]) if md[0].get("LAST") else None
            market_cap = float(md[0]["ISSUECAPITALIZATION"]) if md[0].get("ISSUECAPITALIZATION") else None
    except Exception:
        pass

    # Fallback price
    if price is None:
        price = await client.last_price(ticker)

    # Get dividends (filter out older than 2 years)
    last_dividend = None
    registry_date = None
    try:
        payload = await client._get(f"securities/{ticker}/dividends")
        divs = client._parse_table(payload, "dividends")
        cutoff = datetime.now() - timedelta(days=730)
        recent_divs = [
            d for d in divs
            if d.get("registryclosedate") and datetime.strptime(d["registryclosedate"], "%Y-%m-%d") > cutoff
        ]
        if recent_divs:
            latest = recent_divs[-1]
            last_dividend = float(latest["value"]) if latest.get("value") else None
            registry_date = latest.get("registryclosedate")
    except Exception:
        pass

    await client.close()

    div_yield = compute_div_yield(last_dividend, price)

    return Fundamentals(
        ticker=ticker,
        price=price,
        market_cap=market_cap,
        last_dividend=last_dividend,
        div_yield=div_yield,
        div_registry_date=registry_date,
    )

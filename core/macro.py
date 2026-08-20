"""Macroeconomic indicators for the Russian market.

Fetches USD/RUB, EUR/RUB, Brent oil and the Central Bank key rate
from public sources and caches them in SQLite.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

import httpx
from loguru import logger

from core import db
from core.config import MOEX_ISS_BASE


# Public endpoints (no API keys required)
BRENT_URL = "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F"


_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}


async def _fetch_json(url: str, timeout: float = 15.0) -> dict | None:
    """Fetch JSON from a public URL."""
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_HTTP_HEADERS) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.warning(f"Macro fetch failed for {url}: {e}")
        return None


async def _fetch_text(url: str, timeout: float = 15.0) -> str | None:
    """Fetch raw text from a public URL."""
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_HTTP_HEADERS) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.text
    except Exception as e:
        logger.warning(f"Macro fetch failed for {url}: {e}")
        return None


def _parse_moex_last(payload: dict) -> float | None:
    """Extract price from MOEX marketdata block.

    Falls back through LAST → MARKETPRICE → CLOSEPRICE because currency
    instruments sometimes publish only settlement/market price after hours.
    """
    block = payload.get("marketdata", {})
    columns = block.get("columns", [])
    data = block.get("data", [])
    if not data:
        return None
    for field in ("LAST", "MARKETPRICE", "CLOSEPRICE", "WAPRICE"):
        try:
            idx = columns.index(field)
            val = data[0][idx]
            if val is not None:
                return float(val)
        except Exception:
            continue
    return None


async def fetch_usd_rub() -> float | None:
    """USD/RUB TOM from MOEX CETS."""
    url = (
        f"{MOEX_ISS_BASE}/engines/currency/markets/selt/boards/CETS/"
        f"securities/USD000UTSTOM.json?marketdata.columns=LAST,MARKETPRICE,CLOSEPRICE,WAPRICE"
    )
    payload = await _fetch_json(url)
    if payload:
        return _parse_moex_last(payload)
    return None


async def fetch_eur_rub() -> float | None:
    """EUR/RUB TOM from MOEX CETS."""
    url = (
        f"{MOEX_ISS_BASE}/engines/currency/markets/selt/boards/CETS/"
        f"securities/EUR_RUB__TOM.json?marketdata.columns=LAST,MARKETPRICE,CLOSEPRICE,WAPRICE"
    )
    payload = await _fetch_json(url)
    if payload:
        return _parse_moex_last(payload)
    return None


async def fetch_brent() -> float | None:
    """Brent oil (USD/barrel) from Yahoo Finance."""
    payload = await _fetch_json(BRENT_URL)
    if not payload:
        return None
    try:
        result = payload["chart"]["result"][0]
        meta = result["meta"]
        # Prefer regular market price; fallback to latest close
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        if price:
            return round(float(price), 2)
        # Last resort: last element of the close array
        closes = result["indicators"]["quote"][0].get("close", [])
        for val in reversed(closes):
            if val is not None:
                return round(float(val), 2)
    except Exception as e:
        logger.warning(f"Brent parse failed: {e}")
    return None


async def fetch_cbr_rate() -> float | None:
    """Central Bank of Russia key rate (% per annum) via SOAP web service."""
    from_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S")
    to_date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <KeyRateXML xmlns="http://web.cbr.ru/">
      <fromDate>{from_date}</fromDate>
      <ToDate>{to_date}</ToDate>
    </KeyRateXML>
  </soap:Body>
</soap:Envelope>"""
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_HTTP_HEADERS) as client:
            r = await client.post(
                "https://www.cbr.ru/DailyInfoWebServ/DailyInfo.asmx",
                content=envelope,
                headers={
                    **_HTTP_HEADERS,
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": '"http://web.cbr.ru/KeyRateXML"',
                },
            )
            r.raise_for_status()
            root = ET.fromstring(r.text)
            latest = None
            for kr in root.findall(".//KR"):
                dt_elem = kr.find("DT")
                rate_elem = kr.find("Rate")
                if dt_elem is None or rate_elem is None:
                    continue
                dt = datetime.fromisoformat(dt_elem.text)
                value = float(rate_elem.text.replace(",", "."))
                if latest is None or dt > latest[0]:
                    latest = (dt, value)
            return round(latest[1], 2) if latest else None
    except Exception as e:
        logger.warning(f"CBR rate fetch failed: {e}")
        return None


async def get_macro_snapshot(refresh_age_minutes: int = 60) -> dict:
    """Return cached macro data or fetch fresh values from public sources.

    The result always contains the four keys; missing values are None.
    """
    cached = await db.get_latest_macro(max_age_minutes=refresh_age_minutes)
    if cached:
        logger.debug("Using cached macro snapshot")
        return cached

    logger.info("Fetching fresh macro snapshot")
    usd_rub, eur_rub, brent, cbr_rate = await asyncio.gather(
        fetch_usd_rub(),
        fetch_eur_rub(),
        fetch_brent(),
        fetch_cbr_rate(),
    )

    await db.save_macro_indicators(
        usd_rub=usd_rub,
        eur_rub=eur_rub,
        brent=brent,
        cbr_rate=cbr_rate,
    )

    return {
        "ts": datetime.now().isoformat(),
        "usd_rub": usd_rub,
        "eur_rub": eur_rub,
        "brent": brent,
        "cbr_rate": cbr_rate,
    }


def compute_macro_bullish(snapshot: dict) -> bool:
    """Convert raw macro values into a simple bullish/bearish flag for long filters.

    Conservative: returns False (caution on longs) when the ruble is very weak
    or the central bank is hiking aggressively. Brent strength can offset mild
    ruble weakness.
    """
    usd_rub = snapshot.get("usd_rub")
    eur_rub = snapshot.get("eur_rub")
    brent = snapshot.get("brent")
    cbr_rate = snapshot.get("cbr_rate")

    bearish_factors = 0
    if usd_rub is not None and usd_rub >= 95.0:
        bearish_factors += 1
    if eur_rub is not None and eur_rub >= 105.0:
        bearish_factors += 1
    if cbr_rate is not None and cbr_rate >= 18.0:
        bearish_factors += 1

    bullish_offset = brent is not None and brent >= 80.0

    # Strongly bearish if two or more factors fire; mild single factor is
    # offset by high Brent unless the ruble is extremely weak.
    if bearish_factors >= 2:
        return False
    if bearish_factors == 1:
        if usd_rub is not None and usd_rub >= 100.0:
            return False
        if not bullish_offset:
            return False
    return True

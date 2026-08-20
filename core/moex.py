"""Async client for MOEX ISS API — candles, securities, order book, indices."""

from __future__ import annotations

import asyncio
from datetime import datetime, date, timedelta
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from core.config import MOEX_ISS_BASE, MOEX_REQUEST_DELAY


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, (httpx.NetworkError, httpx.TimeoutException))

# MOEX candle intervals: minutes
INTERVALS = {
    "1m": 1,
    "10m": 10,
    "1h": 60,
    "1d": 24,
    "1w": 7,
    "1M": 31,
}

# Board IDs for different market segments
BOARDS = {
    "TQBR": "Т+1 акции (основной)",
    "TQBS": "Т+1 акции (второй эшелон)",
    "TQNL": "Т+1 акции (новый листинг)",
    "TQTF": "Т+1 ETF",
    "TQRD": "Т+1 депозитарные расписки",
}


class MoexClient:
    """Thin async wrapper over MOEX ISS JSON endpoints."""

    def __init__(self, base_url: str = MOEX_ISS_BASE, delay: float = MOEX_REQUEST_DELAY, max_concurrent: int = 5, price_ttl: float = 120.0):
        self._base = base_url.rstrip("/")
        self._delay = delay
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._client = httpx.AsyncClient(timeout=15.0)
        self._price_cache: dict[str, tuple[float, float]] = {}
        self._price_ttl = price_ttl

    async def close(self):
        await self._client.aclose()

    def _cached_price(self, ticker: str) -> float | None:
        entry = self._price_cache.get(ticker)
        if entry is None:
            return None
        ts, price = entry
        if asyncio.get_event_loop().time() - ts > self._price_ttl:
            self._price_cache.pop(ticker, None)
            return None
        return price

    def _set_cached_price(self, ticker: str, price: float) -> None:
        self._price_cache[ticker] = (asyncio.get_event_loop().time(), price)

    @retry(
        wait=wait_exponential(min=1, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    async def _get(self, path: str, params: dict | None = None) -> dict:
        """GET with rate-limiting and retry."""
        async with self._semaphore:
            await asyncio.sleep(self._delay)
            url = f"{self._base}/{path}.json"
            r = await self._client.get(url, params=params)
            r.raise_for_status()
            return r.json()

    @staticmethod
    def _parse_table(payload: dict, key: str) -> list[dict]:
        """Convert MOEX column/data format into list of dicts."""
        block = payload.get(key, {})
        columns = block.get("columns", [])
        data = block.get("data", [])
        return [dict(zip(columns, row)) for row in data]

    # ── Securities ──────────────────────────────────────────────

    async def list_securities(self, board: str = "TQBR") -> list[dict]:
        """Get all securities on a given board."""
        payload = await self._get(
            f"engines/stock/markets/shares/boards/{board}/securities",
            params={"securities.columns": "SECID,SHORTNAME,PREVPRICE,LOTSIZE,FACEVALUE"},
        )
        return self._parse_table(payload, "securities")

    async def last_price(self, ticker: str, board: str = "TQBR") -> float | None:
        """Get last traded price for a ticker (with TTL cache)."""
        cached = self._cached_price(ticker)
        if cached is not None:
            return cached
        try:
            payload = await self._get(
                f"engines/stock/markets/shares/boards/{board}/securities/{ticker}",
                params={"securities.columns": "LAST"},
            )
            data = self._parse_table(payload, "securities")
            if data and data[0].get("LAST"):
                price = float(data[0]["LAST"])
                self._set_cached_price(ticker, price)
                return price
            # Fallback: use previous close
            payload2 = await self._get(
                f"engines/stock/markets/shares/boards/{board}/securities/{ticker}",
                params={"securities.columns": "PREVPRICE"},
            )
            data2 = self._parse_table(payload2, "securities")
            if data2 and data2[0].get("PREVPRICE"):
                price = float(data2[0]["PREVPRICE"])
                self._set_cached_price(ticker, price)
                return price
            return None
        except Exception:
            return None

    # ── Candles ────────────────────────────────────────────────

    async def candles(
        self,
        ticker: str,
        interval: str = "1d",
        board: str = "TQBR",
        start: int = 0,
        limit: int = 500,
        reverse: bool = True,
        from_date: str | None = None,
        till_date: str | None = None,
    ) -> list[dict]:
        """Fetch candle data for a single ticker.

        interval: one of '1m','10m','1h','1d','1w','1M'
        start: offset for pagination (0 = beginning of history)
        limit: max candles per request (up to 500)
        reverse: if True, return candles newest-last (chronological order for analysis)
        from_date: YYYY-MM-DD — fetch candles from this date forward
        till_date: YYYY-MM-DD — fetch candles up to this date
        """
        int_val = INTERVALS.get(interval, 24)
        params: dict[str, Any] = {"interval": int_val, "limit": limit}
        if from_date:
            params["from"] = from_date
        if till_date:
            params["till"] = till_date
        if start is not None and not from_date:
            params["start"] = start
        payload = await self._get(
            f"engines/stock/markets/shares/boards/{board}/securities/{ticker}/candles",
            params=params,
        )
        data = self._parse_table(payload, "candles")
        if reverse:
            data.reverse()
        return data

    async def candles_full(
        self,
        ticker: str,
        interval: str = "1d",
        board: str = "TQBR",
        max_candles: int = 5000,
    ) -> list[dict]:
        """Paginate through all available candles."""
        all_candles: list[dict] = []
        start = 0
        batch = 500
        while len(all_candles) < max_candles:
            batch_data = await self.candles(ticker, interval, board, start=start, limit=batch, reverse=False)
            if not batch_data:
                break
            all_candles.extend(batch_data)
            if len(batch_data) < batch:
                break
            start += len(batch_data)
        return all_candles

    async def candles_recent(
        self,
        ticker: str,
        interval: str = "1d",
        board: str | None = None,
        count: int = 100,
    ) -> list[dict]:
        """Fetch the N most recent candles in chronological order (oldest first).

        Tries multiple boards automatically (TQBR → TQNL → TQBS → TQRD)
        so tickers from different market segments are covered.
        """
        boards = [board] if board else ["TQBR", "TQNL", "TQBS", "TQRD"]

        for b in boards:
            result = await self._candles_recent_for_board(ticker, interval, b, count)
            if result:
                return result

        return []

    async def _candles_recent_for_board(
        self,
        ticker: str,
        interval: str,
        board: str,
        count: int,
    ) -> list[dict]:
        """Internal helper: fetch the N most recent candles using date filter.

        MOEX ISS always returns up to 500 rows and ignores the `limit` parameter
        when a date range is used, so we request a tight window ending today and
        take the last `count` candles.
        """
        if interval == "1d":
            # ~250 trading days in a year; ask a bit more than count
            lookback_days = int(count * 1.5) + 10
        elif interval == "1h":
            # ~8 hourly candles per trading day
            lookback_days = max(count // 8 + 3, 5)
        elif interval == "10m":
            # ~48 ten-minute candles per trading day
            lookback_days = max(count // 24 + 2, 3)
        elif interval in ("1w", "1W"):
            lookback_days = count * 7 + 14
        elif interval == "1M":
            lookback_days = count * 30 + 30
        else:
            # 1m and other minute intervals: ~400 candles per trading day
            lookback_days = max(count // 400 + 2, 3)

        today = date.today()
        from_date = (today - timedelta(days=lookback_days)).isoformat()
        till_date = today.isoformat()

        # Request a wider batch than strictly needed; MOEX caps at 500 rows.
        all_data = await self.candles(
            ticker, interval, board,
            start=None, limit=500, reverse=False,
            from_date=from_date,
            till_date=till_date,
        )
        if not all_data:
            return []

        recent = all_data[-count:] if len(all_data) > count else all_data
        return recent

    # ── Order book ─────────────────────────────────────────────

    async def order_book(self, ticker: str, board: str = "TQBR", depth: int = 20) -> dict:
        """Fetch current order book (стакан)."""
        payload = await self._get(
            f"engines/stock/markets/shares/boards/{board}/securities/{ticker}/orderbook",
            params={"depth": depth},
        )
        bids = self._parse_table(payload, "bids")
        asks = self._parse_table(payload, "asks")
        return {"bids": bids, "asks": asks}

    async def _order_book_from_marketdata(self, ticker: str, board: str = "TQBR", error: str | None = None) -> dict:
        """Build a liquidity proxy from public marketdata when orderbook endpoint is unavailable."""
        try:
            payload = await self._get(
                f"engines/stock/markets/shares/boards/{board}/securities/{ticker}",
                params={"securities.columns": "BID,OFFER,SPREAD,LOTSIZE,VOLTODAY,NUMTRADES,LASTCHANGEPRC"},
            )
            rows = self._parse_table(payload, "marketdata")
            if not rows:
                return {"error": error or "нет данных по ликвидности"}
            r = rows[0]

            def _f(key: str) -> float | None:
                val = r.get(key)
                if val is None or val == "":
                    return None
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return None

            bid = _f("BID") or 0
            offer = _f("OFFER") or 0
            spread_val = _f("SPREAD")
            spread = spread_val if spread_val is not None else (float(offer) - float(bid) if bid and offer else None)
            mid = (float(bid) + float(offer)) / 2 if bid and offer else None
            spread_pct = round(float(spread) / mid * 100, 4) if spread and mid else None

            voltoday = _f("VOLTODAY")
            numtrades = _f("NUMTRADES")
            last_change = _f("LASTCHANGEPRC")

            # Try to add a short-term activity signal from recent trades
            trade_activity: str | None = None
            try:
                trades = await self.last_trades(ticker, board=board, limit=20)
                if trades:
                    buy_count = sum(1 for t in trades if str(t.get("BUYSELL", "")).upper() == "B")
                    sell_count = len(trades) - buy_count
                    if buy_count > sell_count * 1.3:
                        trade_activity = "pokupki"
                    elif sell_count > buy_count * 1.3:
                        trade_activity = "prodazhi"
                    else:
                        trade_activity = "neutralno"
            except Exception:
                trade_activity = None

            return {
                "best_bid": float(bid) if bid else None,
                "best_ask": float(offer) if offer else None,
                "spread": float(spread) if spread else None,
                "spread_pct": spread_pct,
                "mid": mid,
                "bid_qty_10": None,
                "ask_qty_10": None,
                "imbalance": None,
                "depth": 0,
                "voltoday": voltoday,
                "numtrades": int(numtrades) if numtrades is not None else None,
                "last_change_pct": last_change,
                "trade_activity": trade_activity,
                "note": "стакан недоступен без подписки; данные из public marketdata + последние сделки",
            }
        except Exception:
            return {"error": error or "нет данных по ликвидности"}

    async def order_book_summary(self, ticker: str, board: str = "TQBR", depth: int = 10) -> dict:
        """Fetch order book and compute short-term liquidity snapshot.

        Returns spread, best bid/ask, top-N volume totals and imbalance.
        Keys are intentionally in Russian for downstream LLM prompts.
        """
        try:
            ob = await self.order_book(ticker, board=board, depth=depth)
        except Exception as exc:
            # MOEX ISS orderbook often requires paid subscription; fall back to marketdata-derived liquidity proxy
            return await self._order_book_from_marketdata(ticker, board=board, error=str(exc))
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        if not bids or not asks:
            return await self._order_book_from_marketdata(ticker, board=board, error="стакан пуст")

        def _to_float(row, key):
            val = row.get(key)
            try:
                return float(val) if val is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

        best_bid = _to_float(bids[0], "PRICE")
        best_ask = _to_float(asks[0], "PRICE")
        spread = best_ask - best_bid
        mid = (best_ask + best_bid) / 2 if best_bid and best_ask else None

        def _sum_qty(levels, n):
            return sum(_to_float(row, "QUANTITY") for row in levels[:n])

        bid_qty = _sum_qty(bids, depth)
        ask_qty = _sum_qty(asks, depth)
        total_qty = bid_qty + ask_qty
        imbalance = (bid_qty - ask_qty) / total_qty if total_qty else 0.0

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": round(spread, 4) if spread else None,
            "spread_pct": round(spread / mid * 100, 4) if mid else None,
            "mid": mid,
            "bid_qty_10": int(bid_qty),
            "ask_qty_10": int(ask_qty),
            "imbalance": round(imbalance, 4),
            "depth": depth,
        }

    # ── Trade stream (last trades) ─────────────────────────────

    async def last_trades(self, ticker: str, board: str = "TQBR", limit: int = 50) -> list[dict]:
        """Get recent trades for a ticker."""
        payload = await self._get(
            f"engines/stock/markets/shares/boards/{board}/securities/{ticker}/trades",
            params={"limit": limit},
        )
        return self._parse_table(payload, "trades")

    # ── Indices ────────────────────────────────────────────────

    async def index_candles(
        self,
        index_id: str = "IMOEX",
        interval: str = "1d",
        count: int = 30,
    ) -> list[dict]:
        """Fetch historical candles for a stock market index (IMOEX, RTSI, etc.)."""
        if interval == "1d":
            lookback_days = count * 2 + 30
        elif interval in ("1w", "1W"):
            lookback_days = count * 14 + 30
        else:
            lookback_days = count * 2 + 30

        from_date = (date.today() - timedelta(days=lookback_days)).isoformat()
        limit = min(count * 2, 500)

        all_data = await self.candles(
            index_id, interval, "SNDX",
            start=None, limit=limit, reverse=False,
            from_date=from_date,
        )
        if not all_data:
            return []
        recent = all_data[-count:] if len(all_data) > count else all_data
        return recent

    async def index_value(self, index_id: str = "IMOEX") -> dict | None:
        """Get current value of a MOEX index."""
        payload = await self._get(f"engines/stock/markets/index/securities/{index_id}")
        rows = self._parse_table(payload, "marketdata")
        if not rows:
            return None
        r = rows[0]
        return {
            "value": r.get("CURRENTVALUE") or r.get("LASTVALUE"),
            "change_pct": r.get("LASTCHANGEPRC"),
            "open": r.get("OPENVALUE"),
            "high": r.get("HIGH"),
            "low": r.get("LOW"),
        }

    # ── Corporate events (dividends, splits) ───────────────────

    async def dividends(self, ticker: str) -> list[dict]:
        """Get dividend history for a ticker.

        Uses the ISS /securities/{ticker}/dividends endpoint which returns
        registryclosedate, value and currencyid. The registry close date is
        treated as the last safe day to hold a short position; we close shorts
        one trading day before it.
        """
        payload = await self._get(
            f"securities/{ticker}/dividends",
            params={"limit": 50},
        )
        rows = self._parse_table(payload, "dividends")
        return [
            {
                "ticker": row.get("secid", ticker),
                "registry_close_date": row.get("registryclosedate", ""),
                "dividend": row.get("value"),
                "currency": row.get("currencyid", "RUB"),
            }
            for row in rows
            if row.get("registryclosedate") and row.get("value")
        ]

    # ── Market summary ─────────────────────────────────────────

    async def market_summary(self, board: str = "TQBR") -> list[dict]:
        """Top-level market data: gainers, losers, volume leaders."""
        payload = await self._get(
            f"engines/stock/markets/shares/boards/{board}/securities",
            params={
                "securities.columns": "SECID,SHORTNAME,PREVPRICE,LAST,CHANGE,VOLUME,VALTODAY",
                "sort_order": "VALTODAY",
                "sort_order_desc": "desc",
                "limit": 50,
            },
        )
        return self._parse_table(payload, "securities")


def parse_moex_date(val: Any) -> date | None:
    """Parse MOEX date formats (YYYY-MM-DD or datetime string)."""
    if not val:
        return None
    s = str(val)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_moex_float(val: Any) -> float | None:
    """Parse MOEX numeric values (may be None or empty string)."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
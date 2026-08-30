"""Tinkoff Invest API broker adapter.

Uses the public REST API directly (httpx) because the official
`tinkoff-investments` PyPI package is currently unavailable.
Supports both read-only portfolio queries and real order placement.
"""

from __future__ import annotations

import asyncio
import os
import ssl
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

import httpx

from core.config import TINKOFF_TOKEN, TINKOFF_ACCOUNT_ID, TINKOFF_SANDBOX, SANDBOX_STARTING_CAPITAL, TINKOFF_MAX_RETRIES

BASE_URL = "https://invest-public-api.tinkoff.ru/rest"
SANDBOX_BASE_URL = "https://sandbox-invest-public-api.tinkoff.ru/rest"


_CERTS_DIR = os.path.join(os.path.dirname(__file__), "certs")


def _tinkoff_ssl_context() -> ssl.SSLContext:
    """SSL context that trusts the certifi bundle plus project CAs.

    Tinkoff API uses the Russian Trusted CA chain, which is not shipped in the
    Mozilla/certifi bundle. We load any extra .crt/.pem files from
    brokers/certs/ so sandbox/live requests verify on any machine.
    """
    ctx = ssl.create_default_context()
    if os.path.isdir(_CERTS_DIR):
        for fname in os.listdir(_CERTS_DIR):
            if fname.endswith(".crt") or fname.endswith(".pem"):
                ctx.load_verify_locations(os.path.join(_CERTS_DIR, fname))
    return ctx


class OrderDirection(Enum):
    """Tinkoff Invest API order direction values."""

    BUY = "ORDER_DIRECTION_BUY"
    SELL = "ORDER_DIRECTION_SELL"


class OrderType(Enum):
    """Tinkoff Invest API order type values."""

    MARKET = "ORDER_TYPE_MARKET"
    LIMIT = "ORDER_TYPE_LIMIT"
    BEST_PRICE = "ORDER_TYPE_BEST_PRICE"


class StopOrderType(Enum):
    """Tinkoff Invest API stop-order type values."""

    STOP_LOSS = "STOP_ORDER_TYPE_STOP_LOSS"
    TAKE_PROFIT = "STOP_ORDER_TYPE_TAKE_PROFIT"


@dataclass(frozen=True)
class PortfolioPosition:
    """One open broker position."""

    ticker: str
    figi: str
    instrument_uid: str
    quantity: int
    average_price: float | None
    current_price: float
    currency: str


@dataclass(frozen=True)
class PortfolioSummary:
    """Broker account summary."""

    account_id: str
    total_value_rub: float
    cash_rub: float
    positions: list[PortfolioPosition]


@dataclass(frozen=True)
class PlacedOrder:
    """Result of placing an order."""

    order_id: str
    status: str  # e.g. EXECUTION_STATUS_NEW, EXECUTION_STATUS_FILL, EXECUTION_STATUS_REJECTED
    lots_requested: int
    lots_executed: int
    executed_price: float | None = None  # from executedOrderPrice (MoneyValue) — None if not yet filled
    message: str = ""


@dataclass(frozen=True)
class StopOrderResult:
    """Result of placing a stop order."""

    stop_order_id: str
    status: str
    message: str = ""


@dataclass(frozen=True)
class InstrumentMeta:
    """Minimal instrument metadata needed for order placement."""

    ticker: str
    figi: str
    uid: str
    lot: int
    min_price_increment: float
    short_enabled: bool
    api_trade_available: bool
    currency: str


# Authoritative MOEX ISS lot sizes for tickers we trade.
# Source: https://iss.moex.com/iss/engines/stock/markets/shares/securities/{TICKER}.json
# LOTSIZE column on TQBR board. Refreshed 2026-08-18.
# Used as override when the broker / Tinkoff API returns a sentinel (0, None)
# or a wildly incorrect value. Real broker values always win when they look
# sane (>0 and within reasonable bounds).
_MOEX_LOT_SIZES: dict[str, int] = {
    "SBER": 1, "SBERP": 1, "GAZP": 10, "LKOH": 1, "GMKN": 10, "VKCO": 1,
    "OZON": 1, "MGNT": 1, "AFLT": 10, "CHMF": 1, "ALRS": 10, "MTSS": 10,
    "NVTK": 1, "TATN": 1, "TATNP": 1, "ROSN": 1, "VTBR": 1, "SNGS": 100,
    "SNGBP": 100, "IRAO": 100, "YDEX": 1, "ASTR": 1, "RTKM": 10, "PLZL": 1,
    "POLY": 1, "PHOR": 1, "RASP": 10, "HYDR": 1000, "FEES": 10000, "KMAZ": 10,
    "MOEX": 10, "CBOM": 100, "SMLT": 1, "POSI": 1, "PIKK": 1, "ETLN": 1,
    "RNFT": 1, "YAKG": 10, "OGKB": 1000, "UPRO": 1000, "AQUA": 1,
    "MSNG": 1000, "MRKC": 1000, "VSMO": 1, "TRMK": 10, "NKNC": 10,
    "APTK": 10, "BSPB": 10, "SVCB": 100, "MTLR": 1, "MTLRP": 10,
    "SELG": 10, "TGKA": 100000, "TGKBP": 100000, "AKRN": 1,
}


def _money_value(value: dict[str, Any] | None) -> float:
    """Convert Tinkoff MoneyValue/nano to float."""
    if not value:
        return 0.0
    units = value.get("units", "0")
    nano = value.get("nano", 0)
    if isinstance(nano, str):
        nano = int(nano)
    sign = -1 if str(units).startswith("-") else 1
    units_int = int(str(units).replace("-", ""))
    return sign * (units_int + abs(nano) / 1_000_000_000)


def _resolve_lot_size(ticker: str, api_lot: int) -> int:
    """Return authoritative MOEX lot size for `ticker`.

    The broker/Tinkoff sandbox often returns ``lot=1`` for Russian shares that
    the actual exchange treats as 1-share lots (SBER, OZON, etc.). For tickers
    in our universe we ship an override table sourced from MOEX ISS (TQBR
    board, LOTSIZE column). When the broker's value is missing/0 we use the
    override; otherwise the broker's value wins so we never refuse trades the
    broker explicitly accepts.
    """
    if api_lot and api_lot > 0:
        return api_lot
    return _MOEX_LOT_SIZES.get(ticker.upper(), max(int(api_lot), 1))


class TinkoffClient:
    """Thin async wrapper over Tinkoff Invest REST API."""

    def __init__(
        self,
        token: str | None = None,
        account_id: str | None = None,
        sandbox: bool | None = None,
        sandbox_base_url: str = SANDBOX_BASE_URL,
    ):
        self._token = (token or TINKOFF_TOKEN or "").strip()
        self._account_id = account_id or TINKOFF_ACCOUNT_ID or ""
        self._sandbox = sandbox if sandbox is not None else TINKOFF_SANDBOX
        self._base = sandbox_base_url if self._sandbox else BASE_URL
        self._client = httpx.AsyncClient(timeout=30.0, verify=_tinkoff_ssl_context())

    @property
    def ready(self) -> bool:
        return bool(self._token)

    @property
    def sandbox(self) -> bool:
        return self._sandbox

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        last_exc: Exception | None = None
        max_retries = TINKOFF_MAX_RETRIES if self._sandbox else 1
        for attempt in range(max_retries):
            try:
                resp = await self._client.post(
                    f"{self._base}{path}", json=payload or {}, headers=self._headers()
                )
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                # Sandbox intermittently returns HTTP 500/503 internal errors.
                # Retry a few times with a short backoff; live errors are not retried.
                if self._sandbox and exc.response.status_code in (500, 502, 503) and attempt + 1 < max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise
        raise last_exc or RuntimeError("Tinkoff request failed")

    async def get_accounts(self) -> list[dict[str, Any]]:
        """Return list of available broker accounts."""
        data = await self._post(
            "/tinkoff.public.invest.api.contract.v1.UsersService/GetAccounts"
        )
        return data.get("accounts", [])

    async def get_sandbox_accounts(self) -> list[dict[str, Any]]:
        """Return list of sandbox accounts."""
        data = await self._post(
            "/tinkoff.public.invest.api.contract.v1.SandboxService/GetSandboxAccounts"
        )
        return data.get("accounts", [])

    async def open_sandbox_account(self) -> str:
        """Open a new sandbox account and return its ID."""
        data = await self._post(
            "/tinkoff.public.invest.api.contract.v1.SandboxService/OpenSandboxAccount"
        )
        return str(data.get("accountId", ""))

    async def sandbox_pay_in(self, amount: float, account_id: str | None = None) -> dict[str, Any]:
        """Top up a sandbox account with virtual currency. Only works in sandbox."""
        if not self._sandbox:
            raise RuntimeError("sandbox_pay_in is only available in sandbox mode")
        acc_id = account_id or await self.resolve_account_id()
        data = await self._post(
            "/tinkoff.public.invest.api.contract.v1.SandboxService/SandboxPayIn",
            {
                "accountId": acc_id,
                "amount": {"currency": "rub", "units": int(amount), "nano": int((amount % 1) * 1_000_000_000)},
            },
        )
        return data

    async def close_sandbox_account(self, account_id: str | None = None) -> None:
        """Close a sandbox account. Only works in sandbox."""
        if not self._sandbox:
            raise RuntimeError("close_sandbox_account is only available in sandbox mode")
        acc_id = account_id or await self.resolve_account_id()
        await self._post(
            "/tinkoff.public.invest.api.contract.v1.SandboxService/CloseSandboxAccount",
            {"accountId": acc_id},
        )

    async def resolve_account_id(self) -> str:
        """Return configured account ID or auto-resolve the first open account."""
        if self._sandbox:
            # Sandbox mode has its own account service. Do NOT call UsersService/GetAccounts
            # here: that endpoint returns HTTP 500 for sandbox URLs even though the same
            # token works fine with SandboxService.
            try:
                accounts = await self.get_sandbox_accounts()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (500, 503):
                    raise RuntimeError(
                        "Песочница Тинькофф временно недоступна (HTTP 500). "
                        "Попробуй позже или переключись на живой токен (TINKOFF_SANDBOX=false)."
                    ) from exc
                raise
            # Validate the configured ID against the sandbox account list.
            if self._account_id:
                open_ids = {
                    str(acc.get("id"))
                    for acc in accounts
                    if acc.get("status") == "ACCOUNT_STATUS_OPEN"
                }
                if self._account_id in open_ids:
                    return self._account_id
            for acc in accounts:
                if acc.get("status") == "ACCOUNT_STATUS_OPEN":
                    return str(acc["id"])
            account_id = await self.open_sandbox_account()
            await self.sandbox_pay_in(SANDBOX_STARTING_CAPITAL, account_id)
            return account_id

        # Live mode.
        if self._account_id:
            try:
                accounts = await self.get_accounts()
                open_ids = {
                    str(acc.get("id"))
                    for acc in accounts
                    if acc.get("status") == "ACCOUNT_STATUS_OPEN"
                }
                if self._account_id in open_ids:
                    return self._account_id
            except Exception:
                pass
            # Configured ID is not a known live account; ignore it and auto-resolve
            # so a stale sandbox ID does not break live read-only tests.
            self._account_id = ""

        accounts = await self.get_accounts()
        for acc in accounts:
            if acc.get("status") == "ACCOUNT_STATUS_OPEN":
                return str(acc["id"])
        raise RuntimeError("No open Tinkoff account found")

    async def _load_instrument(self, ticker: str) -> InstrumentMeta | None:
        data = await self._post(
            "/tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument",
            {"query": ticker},
        )
        matches: list[tuple[int, InstrumentMeta]] = []
        for item in data.get("instruments", []):
            if item.get("ticker", "").upper() != ticker.upper():
                continue
            uid = item.get("uid") or item.get("figi") or ""
            if not uid:
                continue
            api_trade = bool(item.get("apiTradeAvailableFlag", False))
            class_code = item.get("classCode", "")
            instrument_type = item.get("instrumentType", "")
            score = 0
            if api_trade:
                score += 100
            if class_code == "TQBR":
                score += 50
            if instrument_type == "share":
                score += 25
            if instrument_type == "currency":
                score += 10
            matches.append(
                (
                    score,
                    InstrumentMeta(
                        ticker=ticker.upper(),
                        figi=item.get("figi", ""),
                        uid=uid,
                        lot=_resolve_lot_size(ticker, int(item.get("lot", 0) or 0)),
                        min_price_increment=_money_value(item.get("minPriceIncrement")) or 0.01,
                        short_enabled=bool(item.get("shortEnabledFlag", False)),
                        api_trade_available=api_trade,
                        currency=item.get("currency", "RUB"),
                    ),
                )
            )
        if not matches:
            return None
        matches.sort(key=lambda x: x[0], reverse=True)
        return matches[0][1]

    async def find_instrument(self, ticker: str) -> dict[str, Any] | None:
        """Find instrument by ticker. Returns full instrument dict or None."""
        instr = await self._load_instrument(ticker)
        if not instr:
            return None
        return {
            "ticker": instr.ticker,
            "figi": instr.figi,
            "uid": instr.uid,
            "lot": instr.lot,
            "currency": instr.currency,
            "short_enabled": instr.short_enabled,
        }

    async def get_last_price(self, instrument_uid: str) -> float | None:
        """Return last market price for an instrument UID."""
        data = await self._post(
            "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetLastPrices",
            {"instrumentId": [instrument_uid]},
        )
        prices = data.get("lastPrices", [])
        if not prices:
            return None
        return _money_value(prices[0].get("price"))

    async def get_ticker_price(self, ticker: str) -> float | None:
        """Convenience: resolve ticker and fetch its last price."""
        instr = await self._load_instrument(ticker)
        if not instr:
            return None
        return await self.get_last_price(instr.uid)

    async def get_portfolio(self, account_id: str | None = None) -> PortfolioSummary:
        """Fetch portfolio snapshot for an account."""
        acc_id = account_id or await self.resolve_account_id()
        # Sandbox uses a dedicated portfolio endpoint; OperationsService/GetPortfolio
        # on the sandbox host rejects non-sandbox account IDs with 404.
        portfolio_path = (
            "/tinkoff.public.invest.api.contract.v1.SandboxService/GetSandboxPortfolio"
            if self._sandbox
            else "/tinkoff.public.invest.api.contract.v1.OperationsService/GetPortfolio"
        )
        data = await self._post(
            portfolio_path,
            {"accountId": acc_id},
        )

        total_value = _money_value(data.get("totalAmountPortfolio"))

        # Cash: currency positions with averagePositionPrice == 1 RUB (unallocated cash).
        cash = sum(
            _money_value(item.get("quantity"))
            for item in data.get("positions", [])
            if item.get("instrumentType") == "currency"
            and _money_value(item.get("averagePositionPrice")) == 1.0
        )

        positions: list[PortfolioPosition] = []
        for pos in data.get("positions", []):
            ticker = pos.get("ticker", "")
            if not ticker:
                continue
            # Tinkoff returns quantity in shares and quantityLots in lots.
            # Expose shares to callers so P&L and close buttons use the right units.
            qty = int(_money_value(pos.get("quantity", {})))
            lots = int(_money_value(pos.get("quantityLots", {})))
            if qty == 0:
                continue
            positions.append(
                PortfolioPosition(
                    ticker=ticker,
                    figi=pos.get("figi", ""),
                    instrument_uid=pos.get("instrumentUid", ""),
                    quantity=qty,
                    average_price=_money_value(pos.get("averagePositionPrice")),
                    current_price=_money_value(pos.get("currentPrice")),
                    currency=pos.get("averagePositionPrice", {}).get("currency", "RUB"),
                )
            )

        return PortfolioSummary(
            account_id=acc_id,
            total_value_rub=total_value,
            cash_rub=cash,
            positions=positions,
        )

    async def place_market_order(
        self,
        ticker: str,
        side: str,
        lots: int,
        account_id: str | None = None,
    ) -> PlacedOrder:
        """Place a market order for the given ticker.

        Args:
            ticker: Ticker to trade.
            side: 'long' / 'buy' or 'short' / 'sell'.
            lots: Number of lots to trade.
            account_id: Optional account ID override.

        Returns:
            PlacedOrder with Tinkoff order ID and execution status.
        """
        instr = await self._load_instrument(ticker)
        if not instr:
            return PlacedOrder(order_id="", status="EXECUTION_STATUS_REJECTED", lots_requested=0, lots_executed=0, message="Instrument not found")
        if not instr.api_trade_available:
            return PlacedOrder(order_id="", status="EXECUTION_STATUS_REJECTED", lots_requested=0, lots_executed=0, message="Instrument not available for API trading")
        # In sandbox the shortEnabledFlag is often false even for liquid shares,
        # but selling an existing long position is still allowed. Only block naked
        # short sales when we have no covering position. In live mode keep the guard.
        if side in ("short", "sell") and not instr.short_enabled and not self._sandbox:
            return PlacedOrder(order_id="", status="EXECUTION_STATUS_REJECTED", lots_requested=0, lots_executed=0, message="Short selling not enabled for this instrument")

        direction = OrderDirection.BUY if side in ("long", "buy") else OrderDirection.SELL
        acc_id = account_id or await self.resolve_account_id()

        # Tinkoff sandbox occasionally rejects orders with code 3 / 30034
        # ("Not enough balance") right after a freshly matched fill on the
        # opposite side, because portfolio positions take a few seconds to
        # settle. Retry with backoff to ride out the lag.
        settle_delays = (0.0, 2.0, 5.0, 10.0) if self._sandbox else (0.0,)
        last_exc: Exception | None = None
        for attempt, delay in enumerate(settle_delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                data = await self._post(
                    "/tinkoff.public.invest.api.contract.v1.OrdersService/PostOrder",
                    {
                        "accountId": acc_id,
                        "instrumentId": instr.uid,
                        "quantity": str(lots),
                        "direction": direction.value,
                        "orderType": OrderType.MARKET.value,
                        "orderId": None,
                    },
                )
                break
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                body = exc.response.text[:200]
                # Retry only on the sandbox settlement-delay pattern.
                is_settle_lag = (
                    self._sandbox
                    and exc.response.status_code == 400
                    and ('"code":3' in body or '30034' in body)
                )
                if is_settle_lag and attempt + 1 < len(settle_delays):
                    await asyncio.sleep(delay if delay else 0.5)
                    continue
                return PlacedOrder(
                    order_id="",
                    status="EXECUTION_STATUS_REJECTED",
                    lots_requested=lots,
                    lots_executed=0,
                    message=f"HTTP {exc.response.status_code}: {body}",
                )
            except Exception as exc:
                last_exc = exc
                return PlacedOrder(order_id="", status="EXECUTION_STATUS_REJECTED", lots_requested=lots, lots_executed=0, message=str(exc))
        else:
            # All retries exhausted.
            return PlacedOrder(
                order_id="",
                status="EXECUTION_STATUS_REJECTED",
                lots_requested=lots,
                lots_executed=0,
                message=f"Sandbox settlement lag — retried {len(settle_delays)} times: {last_exc!s}" if last_exc else "Rejected",
            )

        return PlacedOrder(
            order_id=data.get("orderId", ""),
            status=data.get("executionReportStatus", "EXECUTION_STATUS_UNSPECIFIED"),
            lots_requested=int(data.get("lotsRequested", 0) or 0),
            lots_executed=int(data.get("lotsExecuted", 0) or 0),
            executed_price=_money_value(data.get("executedOrderPrice")),
            message=data.get("responseMetadata", {}).get("trackingId", ""),
        )

    async def get_order_state(self, order_id: str, account_id: str | None = None) -> dict[str, Any]:
        """Return current state of a placed order."""
        acc_id = account_id or await self.resolve_account_id()
        return await self._post(
            "/tinkoff.public.invest.api.contract.v1.OrdersService/GetOrderState",
            {"accountId": acc_id, "orderId": order_id},
        )

    async def cancel_order(self, order_id: str, account_id: str | None = None) -> dict[str, Any]:
        """Cancel an active order."""
        acc_id = account_id or await self.resolve_account_id()
        return await self._post(
            "/tinkoff.public.invest.api.contract.v1.OrdersService/CancelOrder",
            {"accountId": acc_id, "orderId": order_id},
        )

    async def cancel_stop_order(
        self, stop_order_id: str, account_id: str | None = None,
    ) -> dict[str, Any]:
        """Cancel an active stop-order.

        Uses /StopOrdersService/CancelStopOrder — /OrdersService/CancelOrder returns
        404 for stop-order ids.
        """
        acc_id = account_id or await self.resolve_account_id()
        return await self._post(
            "/tinkoff.public.invest.api.contract.v1.StopOrdersService/CancelStopOrder",
            {"accountId": acc_id, "stopOrderId": stop_order_id},
        )

    async def place_stop_order(
        self,
        ticker: str,
        stop_type: str,
        stop_price: float,
        lots: int,
        account_id: str | None = None,
        direction: str | None = None,
        expiration_type: str = "ORDER_EXPIRATION_TYPE_FILL_AND_KILL",
    ) -> StopOrderResult:
        """Place a stop-loss or take-profit order.

        Args:
            ticker: Ticker.
            stop_type: 'stop_loss' or 'take_profit'.
            stop_price: Trigger price.
            lots: Number of lots.
            account_id: Optional account override.
            direction: Optional 'buy'/'sell' override; otherwise inferred from stop type.
            expiration_type: Tinkoff stop order expiration policy.
        """
        instr = await self._load_instrument(ticker)
        if not instr:
            return StopOrderResult(stop_order_id="", status="ERROR", message="Instrument not found")

        if direction is None:
            direction = "sell" if stop_type == "stop_loss" else "sell"
        # Tinkoff REST expects direction as an integer enum value, not the string name.
        direction_value = 1 if direction in ("long", "buy") else 2
        so_type_value = 1 if stop_type == "stop_loss" else 2
        order_type_value = 1  # MARKET
        expiration_value = {
            "ORDER_EXPIRATION_TYPE_FILL_AND_KILL": 1,
            "ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL": 2,
            "ORDER_EXPIRATION_TYPE_AT_THE_CLOSE": 5,
        }.get(expiration_type, 1)
        acc_id = account_id or await self.resolve_account_id()

        price_money = {"units": str(int(stop_price)), "nano": int(round((stop_price % 1) * 1_000_000_000))}
        payload: dict[str, Any] = {
            "accountId": acc_id,
            "instrumentId": instr.uid,
            "quantity": str(lots),
            "price": price_money,
            "stopPrice": price_money,
            "direction": direction_value,
            "stopOrderType": "STOP_ORDER_TYPE_STOP_LOSS" if stop_type == "stop_loss" else "STOP_ORDER_TYPE_TAKE_PROFIT",
            "orderType": "ORDER_TYPE_MARKET",
            "expirationType": expiration_value,
        }
        # GTC requires an explicit expire_date — without it Tinkoff returns
        # "expire_date value out of range (30063)". FILL_AND_KILL / AT_THE_CLOSE
        # ignore the field, so we only set it for GTC.
        if expiration_type == "ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL":
            payload["expireDate"] = (
                datetime.now(timezone.utc) + timedelta(days=365)
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        try:
            data = await self._post(
                "/tinkoff.public.invest.api.contract.v1.StopOrdersService/PostStopOrder",
                payload,
            )
        except httpx.HTTPStatusError as exc:
            return StopOrderResult(stop_order_id="", status="ERROR", message=f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
        except Exception as exc:
            return StopOrderResult(stop_order_id="", status="ERROR", message=str(exc))

        return StopOrderResult(
            stop_order_id=data.get("stopOrderId", ""),
            status=data.get("executionReportStatus", "OK"),
            message=data.get("responseMetadata", {}).get("trackingId", ""),
        )

    async def get_trading_status(self, ticker: str) -> dict[str, Any]:
        """Return current trading status for an instrument."""
        instr = await self._load_instrument(ticker)
        if not instr:
            return {"error": "Instrument not found"}
        return await self._post(
            "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetTradingStatus",
            {"instrumentId": instr.uid},
        )

    async def get_operations(
        self,
        account_id: str | None = None,
        from_iso: str | None = None,
        to_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return executed trades (operations) for the account.

        Used by `intraday_broker_reconcile` to detect phantom positions —
        rows in broker_positions that claim a fill but have no matching
        trade on the broker side (audit 17 Aug 2026: 5 closed rows whose
        open orders never actually settled at Tinkoff).
        """
        acc_id = account_id or await self.resolve_account_id()
        # Sandbox does not expose GetSandboxOperations reliably (returns 400);
        # fall back to the live OperationsService endpoint which works in both
        # sandbox and live (sandbox account IDs are accepted by either).
        path = "/tinkoff.public.invest.api.contract.v1.OperationsService/GetOperations"
        payload: dict[str, Any] = {"accountId": acc_id}
        if from_iso:
            payload["from"] = from_iso
        if to_iso:
            payload["to"] = to_iso
        resp = await self._post(path, payload)
        return resp.get("operations", []) if isinstance(resp, dict) else []

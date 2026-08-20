"""Test connection to T-Bank Invest API using a read-only token.

Reads TINKOFF_TOKEN from .env.tinkoff and prints accounts, portfolio,
and latest prices for a few tickers. No orders are placed.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env.tinkoff")

TOKEN = os.getenv("TINKOFF_TOKEN", "")
BASE_URL = "https://invest-public-api.tinkoff.ru/rest"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _post(client: httpx.AsyncClient, path: str, payload: dict | None = None) -> dict:
    resp = await client.post(f"{BASE_URL}{path}", json=payload or {}, headers=_headers())
    resp.raise_for_status()
    return resp.json()


async def get_accounts(client: httpx.AsyncClient) -> list[dict]:
    data = await _post(client, "/tinkoff.public.invest.api.contract.v1.UsersService/GetAccounts")
    return data.get("accounts", [])


async def get_portfolio(client: httpx.AsyncClient, account_id: str) -> dict:
    return await _post(
        client,
        "/tinkoff.public.invest.api.contract.v1.OperationsService/GetPortfolio",
        {"accountId": account_id},
    )


async def get_last_price(client: httpx.AsyncClient, instrument_id: str) -> dict:
    return await _post(
        client,
        "/tinkoff.public.invest.api.contract.v1.MarketDataService/GetLastPrices",
        {"instrumentId": [instrument_id]},
    )


async def find_instrument(client: httpx.AsyncClient, ticker: str) -> dict | None:
    data = await _post(
        client,
        "/tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument",
        {"query": ticker},
    )
    for item in data.get("instruments", []):
        if item.get("ticker", "").upper() == ticker.upper() and item.get("apiTradeAvailableFlag"):
            return item
    return None


async def main():
    if not TOKEN:
        print("ERROR: TINKOFF_TOKEN is empty. Paste token into .env.tinkoff and save.")
        return

    print(f"Using token: {TOKEN[:10]}...{TOKEN[-10:]}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            accounts = await get_accounts(client)
        except httpx.HTTPStatusError as exc:
            print(f"ERROR fetching accounts: HTTP {exc.response.status_code}")
            print(exc.response.text)
            return
        except Exception as exc:
            print(f"ERROR fetching accounts: {exc}")
            return

        if not accounts:
            print("No accounts found. Check token permissions.")
            return

        print(f"\nFound {len(accounts)} account(s):\n")
        for acc in accounts:
            print(f"  ID: {acc.get('id')}")
            print(f"  Type: {acc.get('type')}")
            print(f"  Name: {acc.get('name')}")
            print(f"  Status: {acc.get('status')}")
            print()

        # Use first account for portfolio
        account_id = accounts[0]["id"]
        print(f"Fetching portfolio for account {account_id}...\n")
        try:
            portfolio = await get_portfolio(client, account_id)
        except Exception as exc:
            print(f"ERROR fetching portfolio: {exc}")
            return

        total_value = portfolio.get("totalAmountPortfolio", {})
        print(f"Total portfolio value: {total_value}")

        positions = portfolio.get("positions", [])
        print(f"\nOpen positions ({len(positions)}):")
        for pos in positions:
            ticker = pos.get("ticker", "?")
            figi = pos.get("figi", "?")
            qty = pos.get("quantity", {})
            current_price = pos.get("currentPrice", {})
            print(f"  {ticker} ({figi})  qty={qty}  price={current_price}")

        # Prices for a few tickers
        print("\nLatest prices:")
        for ticker in ["SBER", "GAZP", "LKOH"]:
            instr = await find_instrument(client, ticker)
            if not instr:
                print(f"  {ticker}: instrument not found")
                continue
            instrument_id = instr.get("uid") or instr.get("figi")
            print(f"  {ticker}: found {instr.get('name')} ({instrument_id})")
            try:
                price_data = await get_last_price(client, instrument_id)
                prices = price_data.get("lastPrices", [])
                if prices:
                    price_info = prices[0]
                    units = price_info.get("price", {}).get("units", "0")
                    nano = price_info.get("price", {}).get("nano", 0)
                    price = float(units) + int(nano) / 1_000_000_000
                    print(f"    price: {price:.2f}")
                else:
                    print(f"    price: no price data (response: {price_data})")
            except Exception as exc:
                print(f"    price error: {exc}")


if __name__ == "__main__":
    asyncio.run(main())

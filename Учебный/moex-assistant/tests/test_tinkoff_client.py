"""Tests for Tinkoff read-only broker adapter.

Requires a valid TINKOFF_TOKEN in .env.tinkoff. Tests are read-only and
never place orders.
"""

import os
import pytest

from core.config import TINKOFF_TOKEN, TINKOFF_ACCOUNT_ID
from brokers.tinkoff_client import TinkoffClient


@pytest.mark.skipif(not TINKOFF_TOKEN, reason="TINKOFF_TOKEN not set")
@pytest.mark.asyncio
async def test_tinkoff_client_reads_accounts():
    client = TinkoffClient(sandbox=False)
    accounts = await client.get_accounts()
    assert isinstance(accounts, list)
    if accounts:
        assert "id" in accounts[0]
    await client.close()


@pytest.mark.skipif(not TINKOFF_TOKEN, reason="TINKOFF_TOKEN not set")
@pytest.mark.asyncio
async def test_tinkoff_client_price_for_sber():
    client = TinkoffClient(sandbox=False)
    price = await client.get_ticker_price("SBER")
    assert price is not None
    assert price > 0
    await client.close()


@pytest.mark.skipif(not TINKOFF_TOKEN, reason="TINKOFF_TOKEN not set")
@pytest.mark.asyncio
async def test_tinkoff_client_portfolio():
    client = TinkoffClient(account_id=TINKOFF_ACCOUNT_ID, sandbox=False)
    portfolio = await client.get_portfolio()
    assert portfolio.account_id
    assert portfolio.total_value_rub >= 0
    assert isinstance(portfolio.positions, list)
    await client.close()

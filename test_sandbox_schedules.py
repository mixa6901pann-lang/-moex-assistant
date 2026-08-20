import asyncio
import sys
sys.path.insert(0, ".")
from brokers.tinkoff_client import TinkoffClient
from core.config import TINKOFF_TOKEN, TINKOFF_ACCOUNT_ID, TINKOFF_SANDBOX

async def main():
    client = TinkoffClient(token=TINKOFF_TOKEN, account_id=TINKOFF_ACCOUNT_ID, sandbox=TINKOFF_SANDBOX)
    acc = await client.resolve_account_id()
    try:
        # Try the TradingSchedules endpoint
        data = await client._post(
            "/tinkoff.public.invest.api.contract.v1.InstrumentsService/TradingSchedules",
            {"exchange": "MOEX", "from": "2026-07-30T00:00:00+03:00", "to": "2026-07-30T23:59:59+03:00"}
        )
        print("schedules:", data)
    except Exception as e:
        print("schedules error:", e)
    await client.close()

asyncio.run(main())

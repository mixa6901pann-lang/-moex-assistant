import asyncio
import sys
sys.path.insert(0, ".")
from brokers.tinkoff_client import TinkoffClient
from core.config import TINKOFF_TOKEN, TINKOFF_ACCOUNT_ID, TINKOFF_SANDBOX

async def main():
    client = TinkoffClient(token=TINKOFF_TOKEN, account_id=TINKOFF_ACCOUNT_ID, sandbox=TINKOFF_SANDBOX)
    acc = await client.resolve_account_id()
    print("account_id:", acc)
    print("portfolio before:", await client.get_portfolio())
    try:
        # Try a tiny buy order for AFLT
        result = await client.place_market_order("AFLT", "long", lots=1, account_id=acc)
        print("order result:", result)
    except Exception as e:
        print("order error:", e)
    print("portfolio after:", await client.get_portfolio())
    await client.close()

asyncio.run(main())

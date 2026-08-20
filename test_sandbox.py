import asyncio
import sys
sys.path.insert(0, ".")
from brokers.tinkoff_client import TinkoffClient
from core.config import TINKOFF_TOKEN, TINKOFF_ACCOUNT_ID, TINKOFF_SANDBOX

async def main():
    client = TinkoffClient(token=TINKOFF_TOKEN, account_id=TINKOFF_ACCOUNT_ID, sandbox=TINKOFF_SANDBOX)
    print("sandbox:", client.sandbox)
    print("ready:", client.ready)
    try:
        acc = await client.resolve_account_id()
        print("account_id:", acc)
    except Exception as e:
        print("resolve_account_id error:", e)
    try:
        portfolio = await client.get_portfolio()
        print("portfolio total:", portfolio.total_value_rub)
        print("cash:", portfolio.cash_rub)
        print("positions:", [(p.ticker, p.quantity, p.current_price) for p in portfolio.positions])
    except Exception as e:
        print("portfolio error:", e)
    await client.close()

asyncio.run(main())

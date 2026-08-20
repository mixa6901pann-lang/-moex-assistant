import asyncio
import sys
sys.path.insert(0, ".")
from brokers.tinkoff_client import TinkoffClient
from core.config import TINKOFF_TOKEN, TINKOFF_ACCOUNT_ID, TINKOFF_SANDBOX

async def main():
    client = TinkoffClient(token=TINKOFF_TOKEN, account_id=TINKOFF_ACCOUNT_ID, sandbox=TINKOFF_SANDBOX)
    for ticker in ["AFLT", "SBER", "GAZP", "MOEX"]:
        instr = await client._load_instrument(ticker)
        if instr:
            print(f"{ticker}: uid={instr.uid}, lot={instr.lot}, api_trade={instr.api_trade_available}, short={instr.short_enabled}")
            price = await client.get_last_price(instr.uid)
            print(f"  last_price={price}")
        else:
            print(f"{ticker}: not found")
    await client.close()

asyncio.run(main())

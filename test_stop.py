import asyncio
from brokers.tinkoff_client import TinkoffClient
async def main():
    c = TinkoffClient(sandbox=True)
    for price in [2.70, 2.80, 2.90]:
        so = await c.place_stop_order(ticker='IRAO', stop_type='stop_loss', stop_price=price, lots=10, direction='buy')
        print(f'sl {price}:', so)
        if so.stop_order_id:
            acc = await c.resolve_account_id()
            await c._post('/tinkoff.public.invest.api.contract.v1.StopOrdersService/CancelStopOrder', {'accountId': acc, 'stopOrderId': so.stop_order_id})
    await c.close()
asyncio.run(main())

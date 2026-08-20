"""List active stop orders in Tinkoff sandbox."""
import asyncio
import sys

sys.path.insert(0, "/root/moex")
from brokers.tinkoff_client import TinkoffClient


async def main() -> None:
    tc = TinkoffClient()
    if not tc.ready:
        print("not ready")
        return
    acc = await tc.resolve_account_id()
    data = await tc._post(
        "/tinkoff.public.invest.api.contract.v1.StopOrdersService/GetStopOrders",
        {"accountId": acc},
    )
    orders = data.get("stopOrders", [])
    print(f"active stop orders: {len(orders)}")
    for o in orders:
        print(
            f"  id={o.get('stopOrderId')} figi={o.get('figi')} "
            f"type={o.get('stopOrderType')} dir={o.get('direction')} "
            f"exp_type={o.get('expirationType')} expire={o.get('expireDate')} "
            f"status={o.get('status')}"
        )


if __name__ == "__main__":
    asyncio.run(main())

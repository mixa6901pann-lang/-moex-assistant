"""Cancel all active sandbox stop orders for a given ticker."""
import asyncio
import sys

sys.path.insert(0, "/root/moex")
from brokers.tinkoff_client import TinkoffClient


async def main() -> None:
    ticker = sys.argv[1] if len(sys.argv) > 1 else "GMKN"
    tc = TinkoffClient()
    if not tc.ready:
        print("not ready")
        return
    acc = await tc.resolve_account_id()
    data = await tc._post(
        "/tinkoff.public.invest.api.contract.v1.StopOrdersService/GetStopOrders",
        {"accountId": acc},
    )
    # Map figi -> ticker via instrument cache
    instr = await tc._load_instrument(ticker)
    if not instr:
        print(f"no instrument for {ticker}")
        return
    target_figi = instr.figi
    orders = data.get("stopOrders", [])
    cancelled = 0
    for o in orders:
        if o.get("figi") != target_figi:
            continue
        sid = o.get("stopOrderId")
        try:
            await tc.cancel_stop_order(sid, account_id=acc)
            print(f"cancelled {sid}")
            cancelled += 1
        except Exception as exc:
            print(f"FAILED cancel {sid}: {exc}")
    print(f"done: cancelled {cancelled} stop orders for {ticker}")


if __name__ == "__main__":
    asyncio.run(main())

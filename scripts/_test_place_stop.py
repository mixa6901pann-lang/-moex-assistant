"""Test helper: manually call place_stop_order for GMKN to see exact response."""
import asyncio
import sys

sys.path.insert(0, "/root/moex")
from brokers.tinkoff_client import TinkoffClient


async def main() -> None:
    tc = TinkoffClient()
    if not tc.ready:
        print("Tinkoff not ready")
        return

    # current GMKN last price
    last = await tc.get_last_price("GMKN")
    print(f"GMKN last price: {last}")

    # try GTC stop-loss at 114.79 for 1 lot
    print("\n=== try GTC stop-loss @ 114.79 ===")
    so = await tc.place_stop_order(
        ticker="GMKN",
        stop_type="stop_loss",
        stop_price=114.79,
        lots=1,
        direction="sell",
        expiration_type="ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL",
    )
    print(f"  stop_order_id={so.stop_order_id}")
    print(f"  status={so.status}")
    print(f"  message={so.message}")

    # try FILL_AND_KILL for comparison
    print("\n=== try FILL_AND_KILL stop-loss @ 114.79 ===")
    so = await tc.place_stop_order(
        ticker="GMKN",
        stop_type="stop_loss",
        stop_price=114.79,
        lots=1,
        direction="sell",
        expiration_type="ORDER_EXPIRATION_TYPE_FILL_AND_KILL",
    )
    print(f"  stop_order_id={so.stop_order_id}")
    print(f"  status={so.status}")
    print(f"  message={so.message}")


if __name__ == "__main__":
    asyncio.run(main())

"""Create a pending robot proposal for manual UI testing."""
import asyncio
from core import db


async def main():
    pid = await db.save_robot_proposal(
        ticker="SBER",
        side="long",
        source="intraday",
        signal="bounce_up",
        entry_px=255.0,
        qty=1,
        stop_px=250.0,
        take_px=260.0,
        confidence=72,
        reason="Bounce from daily low with volume spike",
        fee_rub=0.5,
        net_profit_pct=1.2,
    )
    print(f"created pending proposal id={pid}")
    await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())

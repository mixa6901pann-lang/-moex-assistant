"""Create test 3d proposal again with new code paths."""
import asyncio
import sys

sys.path.insert(0, "/root/moex")
from core import db


async def main() -> None:
    pid = await db.save_robot_proposal(
        ticker="LKOH",
        side="long",
        source="evening",
        signal="trend_up",
        entry_px=4266.0,
        qty=1,
        stop_px=4085.84,
        take_px=4400.0,
        confidence=70,
        reason="Test 3d position for trailing stop (post-GTC+cancel fix)",
        fee_rub=1.0,
        net_profit_pct=1.0,
        horizon="3d",
        proposal_mode="auto_trade",
        initial_atr=120.11,
        atr_mult=1.8,
    )
    print(f"created pending proposal id={pid}")
    await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())

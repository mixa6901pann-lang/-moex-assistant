"""Force a single evening_broker_check run."""
import asyncio
import sys

sys.path.insert(0, "/root/moex")
from main import MoexAssistant


async def main() -> None:
    a = MoexAssistant()
    try:
        await a.evening_broker_check()
    finally:
        try:
            from core import db
            if db.is_db_connected():
                await db.close_db()
        except Exception:
            pass
        try:
            await a.moex.close()
        except Exception:
            pass
        try:
            if a.tinkoff.ready:
                await a.tinkoff.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())

"""Run the evening trading decision task once and print semi-auto proposals.

Useful for testing the Tinkoff-linked trading robot without waiting for
the scheduled 18:00 MSK task.
"""

from __future__ import annotations

import asyncio

from main import MoexAssistant


async def main():
    assistant = MoexAssistant()
    try:
        await assistant.evening_trading_decision()
    finally:
        await assistant.moex.close()
        if assistant.tinkoff.ready:
            await assistant.tinkoff.close()
        try:
            from core import db
            if db.is_db_connected():
                await db.close_db()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())

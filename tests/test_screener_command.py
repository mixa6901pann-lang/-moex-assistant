"""Test /screener command output."""

import asyncio
from loguru import logger
from bot.bot_service import BotService


async def main():
    service = BotService()

    logger.info("=== Running /screener ===")
    results = await service.cmd_screener()
    text = service.format_screener(results)
    logger.info(f"Screener output:\n{text}")

    logger.info("=== Top 5 with direction analysis ===")
    for r in results[:5]:
        analysis = await service.cmd_ticker(r.ticker)
        if analysis and analysis.direction_advice:
            adv = analysis.direction_advice
            badge = "LONG" if adv.direction == "long" else "SHORT" if adv.direction == "short" else "WAIT"
            logger.info(f"{r.ticker}: {badge} ({adv.strength}) | Score: {r.score} | Signals: {r.signals}")
        else:
            logger.info(f"{r.ticker}: NO DATA | Score: {r.score}")

    await service.close()


if __name__ == "__main__":
    asyncio.run(main())

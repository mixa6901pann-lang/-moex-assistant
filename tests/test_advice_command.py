"""Test smart direction logic."""

import asyncio
from loguru import logger
from bot.bot_service import BotService


async def main():
    logger.info("=== Test smart recommendations ===")
    service = BotService()

    # 1. Test /ticker with new logic
    logger.info("1. SBER analysis")
    analysis = await service.cmd_ticker("SBER")
    if analysis and analysis.direction_advice:
        adv = analysis.direction_advice
        logger.info(f"Direction: {adv.direction}, strength: {adv.strength}")
        logger.info(f"Reason: {adv.reason}")
        logger.info(f"R/R: {adv.risk_reward}")
        logger.info(f"Warnings: {adv.warnings}")

    # 2. Test /advice
    logger.info("2. /advice for SBER")
    text = await service.cmd_advice("SBER")
    logger.info(f"Advice output: {text[:300]}...")

    # 3. Test /advice for NVTK
    logger.info("3. /advice for NVTK")
    text = await service.cmd_advice("NVTK")
    logger.info(f"Advice output: {text[:300]}...")

    # 4. Check /ticker formatting
    logger.info("4. Full /ticker for SBER")
    if analysis:
        formatted = service.format_ticker(analysis)
        logger.info(f"Formatted: {formatted[:400]}...")

    await service.close()
    logger.success("=== Test finished ===")


if __name__ == "__main__":
    asyncio.run(main())

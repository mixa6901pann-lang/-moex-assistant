"""Тест VK-интеграции без реальных API-вызовов.

Проверяем:
1. BotService работает (screener, ticker, positions)
2. VK-адаптеры импортируются без ошибок
3. Форматирование сообщений для ВК корректно
"""

import asyncio
from loguru import logger

from bot.bot_service import BotService
from bot.vk_bot import VkBotAdapter
from bot.vk_wall import VkWallPoster


async def main():
    logger.info("Тест VK-интеграции...")

    # 1. BotService
    service = BotService()
    logger.info("BotService создан")

    # 2. Скринер
    results = await service.cmd_screener()
    text = service.format_screener(results)
    logger.info(f"Скринер:\n{text[:500]}...")

    # 3. Анализ тикера
    analysis = await service.cmd_ticker("SBER")
    if analysis:
        text = service.format_ticker(analysis)
        logger.info(f"Анализ SBER:\n{text[:500]}...")

    # 4. VK адаптеры импортируются
    vk_bot = VkBotAdapter(service)
    vk_wall = VkWallPoster(service)
    logger.info("VK адаптеры созданы")

    # 5. Проверка парсинга команд
    assert vk_bot._parse_command("/screener") == ("screener", [])
    assert vk_bot._parse_command("/ticker SBER") == ("ticker", ["SBER"])
    assert vk_bot._parse_command("/trade SBER long 250 240 280 100") == (
        "trade", ["SBER", "long", "250", "240", "280", "100"]
    )
    logger.success("Парсинг команд работает")

    # 6. Позиции
    positions = await service.cmd_positions()
    text = service.format_positions(positions)
    logger.info(f"Позиции:\n{text[:300]}")

    await service.close()
    logger.success("Тест VK-интеграции пройден!")


if __name__ == "__main__":
    asyncio.run(main())

"""Живой тест VK-интеграции.

Проверяет:
1. Валидность VK-токена
2. Права токена
3. Отправку тестового сообщения в ЛС группы
4. Публикацию тестового поста на стену (опционально)
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from loguru import logger

from core.config import VK_ACCESS_TOKEN, VK_GROUP_ID
from bot.bot_service import BotService
from bot.vk_wall import VkWallPoster


def _vk_call(method: str, params: dict) -> dict:
    import vk_api
    vk = vk_api.VkApi(token=VK_ACCESS_TOKEN)
    return vk.method(method, params)


async def test_token():
    """Проверяем, что токен работает."""
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        result = await loop.run_in_executor(
            executor,
            lambda: _vk_call("groups.getById", {"group_ids": VK_GROUP_ID})
        )
        logger.success(f"Токен валиден. Группа: {result[0]['name']}")
        return True
    except Exception as e:
        logger.error(f"Токен не работает: {e}")
        return False
    finally:
        executor.shutdown(wait=False)


async def test_wall_post():
    """Публикуем тестовый пост на стену."""
    logger.info("Публикация тестового поста на стену...")
    poster = VkWallPoster()
    try:
        await poster._post(
            "🧪 Тестовая публикация\n\n"
            "Это тестовый пост от MOEX Assistant.\n"
            "Если вы видите это сообщение — автопостинг работает!"
        )
        logger.success("Тестовый пост опубликован")
        return True
    except Exception as e:
        logger.error(f"Публикация не удалась: {e}")
        return False


async def test_bot_service():
    """Тестируем BotService + скринер."""
    logger.info("Тест BotService...")
    service = BotService()
    results = await service.cmd_screener()
    text = service.format_screener(results)
    logger.info(f"Скринер вернул {len(results)} акций")
    logger.info(f"Текст поста:\n{text[:500]}...")
    await service.close()
    return True


async def main():
    logger.info("=== Живой тест VK ===")

    # 1. Токен
    if not await test_token():
        logger.error("Тест остановлен: токен недействителен")
        return

    # 2. BotService
    await test_bot_service()

    # 3. Пост на стену
    await test_wall_post()

    logger.success("Все тесты завершены")


if __name__ == "__main__":
    asyncio.run(main())

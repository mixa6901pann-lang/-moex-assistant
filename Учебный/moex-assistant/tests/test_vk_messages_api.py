"""Тест отправки сообщений через VK API.

Проверяем:
1. Отправка тестового сообщения в ЛС группы (messages.send)
2. Получение информации о группе
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from loguru import logger

from core.config import VK_ACCESS_TOKEN, VK_GROUP_ID


def _vk_call(method: str, params: dict) -> dict:
    import vk_api
    vk = vk_api.VkApi(token=VK_ACCESS_TOKEN)
    return vk.method(method, params)


async def test_group_info():
    """Получаем информацию о группе."""
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        result = await loop.run_in_executor(
            executor,
            lambda: _vk_call("groups.getById", {"group_ids": VK_GROUP_ID, "fields": "can_post"})
        )
        group = result[0]
        logger.info(f"Группа: {group.get('name')}")
        logger.info(f"ID: {group.get('id')}")
        logger.info(f"can_post: {group.get('can_post')}")
        logger.info(f"is_closed: {group.get('is_closed')}")
        return group
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return None
    finally:
        executor.shutdown(wait=False)


async def test_messages_info():
    """Проверяем возможность отправки сообщений."""
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        # Получаем список диалогов (проверяем права messages)
        result = await loop.run_in_executor(
            executor,
            lambda: _vk_call("messages.getConversations", {"count": 1})
        )
        logger.success(f"Messages API доступен. Диалогов: {result.get('count', 0)}")
        return True
    except Exception as e:
        logger.error(f"Messages API недоступен: {e}")
        return False
    finally:
        executor.shutdown(wait=False)


async def test_get_user_id():
    """Получаем ID текущего пользователя."""
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        result = await loop.run_in_executor(
            executor,
            lambda: _vk_call("users.get", {})
        )
        user_id = result[0]["id"]
        logger.info(f"Мой user_id: {user_id}")
        return user_id
    except Exception as e:
        logger.error(f"Не удалось получить user_id: {e}")
        return None
    finally:
        executor.shutdown(wait=False)


async def test_send_message(peer_id: int = None):
    """Отправляем тестовое сообщение.

    Для бота VK peer_id = ID пользователя (положительное число).
    """
    if peer_id is None:
        peer_id = await test_get_user_id()
        if peer_id is None:
            return False

    logger.info(f"Отправка тестового сообщения в peer_id={peer_id}...")
    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        result = await loop.run_in_executor(
            executor,
            lambda: _vk_call(
                "messages.send",
                {
                    "peer_id": peer_id,
                    "message": "🧪 Тестовое сообщение от MOEX Assistant!\n\nЕсли вы видите это — бот работает.",
                    "random_id": 0,
                }
            )
        )
        logger.success(f"Сообщение отправлено! ID: {result}")
        return True
    except Exception as e:
        logger.error(f"Отправка не удалась: {e}")
        return False
    finally:
        executor.shutdown(wait=False)


async def main():
    logger.info("=== Тест VK Messages API ===")

    # 1. Инфо о группе
    group = await test_group_info()
    if not group:
        return

    # 2. Messages API
    messages_ok = await test_messages_info()

    # 3. Отправка сообщения (если messages работает)
    if messages_ok:
        await test_send_message()

    logger.info("=== Тест завершён ===")


if __name__ == "__main__":
    asyncio.run(main())

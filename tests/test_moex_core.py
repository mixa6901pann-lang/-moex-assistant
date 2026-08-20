"""Тестовый запуск внутренних модулей без Telegram-бота.

Проверяет:
1. Инициализацию базы данных SQLite
2. Загрузку свечей с Московской биржи (один тикер)
3. Расчёт индикаторов
4. Работу скринера
"""

import asyncio
from datetime import datetime

from loguru import logger

from core import db
from core.moex import MoexClient
from strategies.indicators import df_from_candles, add_indicators, detect_signals, score_stock


async def main():
    logger.info("Запуск теста без Telegram...")

    # 1. База данных
    logger.info("Инициализация БД...")
    await db.get_db()
    logger.success("БД готова")

    # 2. Клиент MOEX
    moex = MoexClient()
    ticker = "SBER"

    logger.info(f"Загрузка свечей для {ticker}...")
    try:
        candles = await moex.candles_recent(ticker, count=100)
        logger.success(f"Получено {len(candles)} свечей")
    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")
        await moex.close()
        return

    # 3. Сохраняем в БД
    await db.save_candles(candles, ticker, "TQBR", "1d")
    logger.success("Свечи сохранены в БД")

    # 4. Индикаторы
    df = df_from_candles(candles)
    df = add_indicators(df)
    logger.info(f"Индикаторы рассчитаны. Последняя строка:\n{df.tail(1)}")

    # 5. Сигналы
    signals = detect_signals(df)
    logger.info(f"Сигналы: {signals}")

    # 6. Скоринг
    score = score_stock(df)
    logger.info(f"Score {ticker}: {score}")

    await moex.close()
    logger.success("Тест завершён успешно!")


if __name__ == "__main__":
    asyncio.run(main())

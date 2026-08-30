"""Telegram adapter — thin wrapper over BotService.

Uses python-telegram-bot for transport.
"""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

from core.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY
from bot.bot_service import BotService


class TelegramAdapter:
    """Thin adapter that delegates all logic to BotService."""

    def __init__(self, service: BotService | None = None):
        self.service = service or BotService()
        self._app: Application | None = None

    async def init(self):
        if not TELEGRAM_BOT_TOKEN:
            raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env")
        builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
        if TELEGRAM_PROXY:
            builder = builder.request(HTTPXRequest(proxy=TELEGRAM_PROXY))
        self._app = builder.build()
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("screener", self._cmd_screener))
        self._app.add_handler(CommandHandler("ticker", self._cmd_ticker))
        self._app.add_handler(CommandHandler("advice", self._cmd_advice))
        self._app.add_handler(CommandHandler("trade", self._cmd_trade))
        self._app.add_handler(CommandHandler("positions", self._cmd_positions))
        self._app.add_handler(CommandHandler("close", self._cmd_close))
        self._app.add_handler(CommandHandler("backtest", self._cmd_backtest))
        self._app.add_handler(CommandHandler("help", self._cmd_help))

    # ── Handlers ─────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Привет! Я помощник для торговли на Мосбирже.\n\n"
            "/screener — ТОП акций по сигналам\n"
            "/ticker SBER — анализ тикера\n"
            "/advice SBER — краткая рекомендация\n"
            "/trade SBER long 250 50000 — записать сделку\n"
            "/positions — открытые позиции\n"
            "/close <id> <цена> — закрыть сделку\n"
            "/help — справка"
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Команды:\n"
            "/screener — ТОП-15 акций по техсигналам\n"
            "/ticker <TICKER> — детальный анализ\n"
            "/advice <TICKER> — краткая рекомендация (лонг/шорт/ждать)\n"
            "/backtest <TICKER> [дней] — бэктест стратегии\n"
            "/trade <TICKER> <long|short> <вход> <стоп> <цель> <кол-во> — запись сделки\n"
            "/positions — открытые позиции\n"
            "/close <id> <цена_выхода> — закрыть сделку\n\n"
            "Бот также присылает утреннюю сводку и алерты днём."
        )

    async def _cmd_screener(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg = await update.message.reply_text("Запускаю скринер...")
        results = await self.service.cmd_screener()
        text = self.service.format_screener(results)
        await msg.edit_text(text, parse_mode="HTML")

    async def _cmd_ticker(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Укажи тикер: /ticker SBER")
            return
        ticker = ctx.args[0].upper()
        msg = await update.message.reply_text(f"Анализирую {ticker}...")
        result = await self.service.cmd_ticker(ticker)
        if result is None:
            await msg.edit_text(f"Нет данных по {ticker}")
            return
        text = self.service.format_ticker(result)
        await msg.edit_text(text, parse_mode="HTML")

    async def _cmd_advice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Укажи тикер: /advice SBER")
            return
        ticker = ctx.args[0].upper()
        msg = await update.message.reply_text(f"Думаю над {ticker}...")
        text = await self.service.cmd_advice(ticker)
        await msg.edit_text(text)

    async def _cmd_trade(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if len(ctx.args) < 4:
            await update.message.reply_text(
                "Формат: /trade <TICKER> <long|short> <вход> <стоп> [цель] [кол-во]"
            )
            return
        try:
            ticker = ctx.args[0].upper()
            side = ctx.args[1].lower()
            entry_px = float(ctx.args[2])
            stop_px = float(ctx.args[3]) if len(ctx.args) > 3 else None
            target_px = float(ctx.args[4]) if len(ctx.args) > 4 else None
            qty = int(ctx.args[5]) if len(ctx.args) > 5 else 1
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Ошибка в параметрах. Пример: /trade SBER long 250 240 280 100"
            )
            return

        record = await self.service.cmd_trade(ticker, side, entry_px, stop_px, target_px, qty)
        text = self.service.format_trade(record)
        await update.message.reply_text(text)

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        positions = await self.service.cmd_positions()
        text = self.service.format_positions(positions)
        await update.message.reply_text(text)

    async def _cmd_close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if len(ctx.args) < 2:
            await update.message.reply_text("Формат: /close <id> <цена_выхода>")
            return
        try:
            trade_id = int(ctx.args[0])
            exit_px = float(ctx.args[1])
        except (ValueError, IndexError):
            await update.message.reply_text("Ошибка. Пример: /close 3 260")
            return

        result = await self.service.cmd_close(trade_id, exit_px)
        text = self.service.format_close(result)
        await update.message.reply_text(text)

    async def _cmd_backtest(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not ctx.args:
            await update.message.reply_text("Укажи тикер: /backtest SBER")
            return
        ticker = ctx.args[0].upper()
        days = int(ctx.args[1]) if len(ctx.args) > 1 else 365
        msg = await update.message.reply_text(f"Бэктест {ticker} за {days} дней...")
        text = await self.service.cmd_backtest(ticker, days)
        await msg.edit_text(text)

    # ── Proactive notifications ─────────────────────────────────

    async def send_morning_report(self):
        if not TELEGRAM_CHAT_ID or not self._app:
            return
        text = await self.service.morning_report()
        await self._app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

    async def send_alert(self, ticker: str, message: str):
        if not TELEGRAM_CHAT_ID or not self._app:
            return
        text = await self.service.alert_text(ticker, message)
        await self._app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)

    # ── Run ─────────────────────────────────────────────────────

    async def run(self):
        await self.init()
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()


# Backward compatibility alias
MoexBot = TelegramAdapter


async def main():
    adapter = TelegramAdapter()
    await adapter.run()


if __name__ == "__main__":
    asyncio.run(main())

"""VK bot adapter using Long Poll API.

Delegates all logic to BotService.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from loguru import logger

from core.config import VK_ACCESS_TOKEN, VK_GROUP_ID
from bot.bot_service import BotService


class VkBotAdapter:
    """VK Long Poll adapter that delegates logic to BotService."""

    def __init__(self, service: BotService | None = None):
        self.service = service or BotService()
        self._vk = None
        self._longpoll = None
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._running = False
        self._last_plans: dict[int, dict] = {}  # peer_id -> trade plan dict

    def _init_vk(self):
        """Import vk_api here to avoid import errors when VK is disabled."""
        import vk_api
        from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

        self._vk = vk_api.VkApi(token=VK_ACCESS_TOKEN)
        self._longpoll = VkBotLongPoll(self._vk, VK_GROUP_ID)
        self._event_type = VkBotEventType
        logger.info("VK bot initialized")

    # ── Keyboard builders ───────────────────────────────────────

    @staticmethod
    def _build_main_keyboard() -> str:
        """Build reply keyboard JSON for VK."""
        from vk_api.keyboard import VkKeyboard, VkKeyboardColor

        kb = VkKeyboard(one_time=False)

        kb.add_button("📊 Скринер", VkKeyboardColor.PRIMARY)
        kb.add_button("📋 Позиции", VkKeyboardColor.SECONDARY)
        kb.add_button("📝 Торговля", VkKeyboardColor.PRIMARY)
        kb.add_button("❓ Помощь", VkKeyboardColor.SECONDARY)
        kb.add_line()

        kb.add_button("SBER", VkKeyboardColor.POSITIVE)
        kb.add_button("GAZP", VkKeyboardColor.POSITIVE)
        kb.add_button("LKOH", VkKeyboardColor.POSITIVE)
        kb.add_button("YNDX", VkKeyboardColor.POSITIVE)
        kb.add_line()

        kb.add_button("ROSN", VkKeyboardColor.POSITIVE)
        kb.add_button("TATN", VkKeyboardColor.POSITIVE)
        kb.add_button("MGNT", VkKeyboardColor.POSITIVE)
        kb.add_button("MTSS", VkKeyboardColor.POSITIVE)
        kb.add_line()

        kb.add_button("VTBR", VkKeyboardColor.POSITIVE)
        kb.add_button("SNGS", VkKeyboardColor.POSITIVE)
        kb.add_button("ALRS", VkKeyboardColor.POSITIVE)
        kb.add_button("AFLT", VkKeyboardColor.POSITIVE)
        kb.add_line()

        kb.add_button("NLMK", VkKeyboardColor.POSITIVE)
        kb.add_button("CHMF", VkKeyboardColor.POSITIVE)
        kb.add_button("PIKK", VkKeyboardColor.POSITIVE)
        kb.add_button("AFKS", VkKeyboardColor.POSITIVE)
        kb.add_line()

        kb.add_button("FIVE", VkKeyboardColor.POSITIVE)
        kb.add_button("POLY", VkKeyboardColor.POSITIVE)
        kb.add_button("MOEX", VkKeyboardColor.POSITIVE)
        kb.add_button("MAGN", VkKeyboardColor.POSITIVE)

        return kb.get_keyboard()

    @staticmethod
    def _build_trade_confirm_keyboard() -> str:
        """One-time keyboard shown after analysis with a trade plan."""
        from vk_api.keyboard import VkKeyboard, VkKeyboardColor

        kb = VkKeyboard(one_time=True)
        kb.add_button("✅ Записать сделку", VkKeyboardColor.POSITIVE)
        kb.add_button("❌ Отмена", VkKeyboardColor.NEGATIVE)
        return kb.get_keyboard()

    @staticmethod
    def _build_close_keyboard(positions: list) -> str:
        """One-time keyboard with close buttons for each position."""
        from vk_api.keyboard import VkKeyboard, VkKeyboardColor

        kb = VkKeyboard(one_time=True)
        for i, p in enumerate(positions):
            if i > 0 and i % 3 == 0:
                kb.add_line()
            kb.add_button(f"❌ Закрыть #{p.id}", VkKeyboardColor.NEGATIVE)
        return kb.get_keyboard()

    # ── Button mapping ──────────────────────────────────────────

    _BUTTON_MAP = {
        "📊 скринер": "/screener",
        "📋 позиции": "/positions",
        "📝 торговля": "/trade",
        "❓ помощь": "/help",
        "✅ записать сделку": "/record_trade",
        "❌ отмена": "/cancel",
        "sber": "/ticker SBER",
        "gazp": "/ticker GAZP",
        "lkoh": "/ticker LKOH",
        "yndx": "/ticker YNDX",
        "rosn": "/ticker ROSN",
        "tatn": "/ticker TATN",
        "mgnt": "/ticker MGNT",
        "mtss": "/ticker MTSS",
        "vtbr": "/ticker VTBR",
        "sngs": "/ticker SNGS",
        "alrs": "/ticker ALRS",
        "aflt": "/ticker AFLT",
        "nlmk": "/ticker NLMK",
        "chmf": "/ticker CHMF",
        "pikk": "/ticker PIKK",
        "afks": "/ticker AFKS",
        "five": "/ticker FIVE",
        "poly": "/ticker POLY",
        "moex": "/ticker MOEX",
        "magn": "/ticker MAGN",
    }

    # ── Command parsing ─────────────────────────────────────────

    @staticmethod
    def _parse_command(text: str) -> tuple[str, list[str]]:
        """Parse /command arg1 arg2 from message text."""
        if not text:
            return "", []
        parts = text.strip().split()
        if not parts:
            return "", []
        cmd = parts[0].lstrip("/").lower()
        args = parts[1:]
        return cmd, args

    # ── Handlers ───────────────────────────────────────────────

    async def _handle_message(self, event):
        """Process incoming VK message."""
        peer_id = event.object.message["peer_id"]
        text = event.object.message.get("text", "")

        # Handle close-position buttons first (dynamic IDs)
        lowered = text.lower().strip()
        if lowered.startswith("❌ закрыть #"):
            try:
                trade_id = int(lowered.replace("❌ закрыть #", "").strip())
                result = await self.service.cmd_close_market(trade_id)
                reply = self.service.format_close(result)
            except Exception as e:
                reply = f"Ошибка закрытия: {e}"
            await self._send_message(peer_id, reply, keyboard=self._build_main_keyboard())
            return

        mapped = self._BUTTON_MAP.get(lowered)
        if mapped:
            text = mapped

        cmd, args = self._parse_command(text)

        logger.info(f"VK command: {cmd}, args: {args}")

        try:
            if cmd == "start":
                reply = (
                    "Привет! Я помощник для торговли на Мосбирже.\n\n"
                    "Выбери команду кнопкой ниже или напиши текстом:\n"
                    "/screener — ТОП акций\n"
                    "/ticker SBER — анализ\n"
                    "/advice SBER — совет\n"
                    "/positions — открытые сделки\n"
                    "/help — справка"
                )
                await self._send_message(peer_id, reply, keyboard=self._build_main_keyboard())
                return
            elif cmd == "help":
                reply = (
                    "Команды:\n"
                    "📊 /screener — ТОП-15 акций по техсигналам\n"
                    "📈 /ticker <TICKER> — детальный анализ\n"
                    "💡 /advice <TICKER> — краткая рекомендация\n"
                    "📉 /backtest <TICKER> [дней] — бэктест стратегии\n"
                    "📋 /positions — открытые позиции\n"
                    "📝 /trade <TICKER> <long|short> <вход> <стоп> <цель> <кол-во> — запись сделки\n"
                    "🔒 /close <id> <цена> — закрыть сделку\n\n"
                    "Бот также публикует утреннюю сводку и алерты на стене группы."
                )
            elif cmd == "screener":
                await self._send_message(peer_id, "Запускаю скринер...")
                results = await self.service.cmd_screener()
                reply = self.service.format_screener(results)
            elif cmd == "ticker":
                if not args:
                    reply = "Укажи тикер: /ticker SBER"
                else:
                    ticker = args[0].upper()
                    await self._send_message(peer_id, f"Анализирую {ticker}...")
                    result = await self.service.cmd_ticker(ticker)
                    if result is None:
                        reply = f"Нет данных по {ticker}"
                    else:
                        reply = self.service.format_ticker(result)
                        if result.trade_plan:
                            plan = result.trade_plan
                            self._last_plans[peer_id] = {
                                "ticker": plan.ticker,
                                "side": plan.side,
                                "entry_px": plan.entry_px,
                                "stop_px": plan.stop_px,
                                "target_px": plan.target_px,
                                "qty": plan.qty,
                            }
                            await self._send_message(
                                peer_id,
                                reply,
                                keyboard=self._build_trade_confirm_keyboard(),
                            )
                            return
            elif cmd == "advice":
                if not args:
                    reply = "Укажи тикер: /advice SBER"
                else:
                    ticker = args[0].upper()
                    await self._send_message(peer_id, f"Думаю над {ticker}...")
                    reply = await self.service.cmd_advice(ticker)
            elif cmd == "trade":
                if len(args) < 4:
                    reply = "Формат: /trade <TICKER> <long|short> <вход> <стоп> [цель] [кол-во]"
                else:
                    try:
                        ticker = args[0].upper()
                        side = args[1].lower()
                        entry_px = float(args[2])
                        stop_px = float(args[3]) if len(args) > 3 else None
                        target_px = float(args[4]) if len(args) > 4 else None
                        qty = int(args[5]) if len(args) > 5 else 1
                    except (ValueError, IndexError):
                        reply = "Ошибка в параметрах. Пример: /trade SBER long 250 240 280 100"
                    else:
                        record = await self.service.cmd_trade(ticker, side, entry_px, stop_px, target_px, qty)
                        reply = self.service.format_trade(record)
            elif cmd == "record_trade":
                plan = self._last_plans.get(peer_id)
                if not plan:
                    reply = "Нет сохранённого плана. Сначала проанализируй тикер."
                else:
                    try:
                        record = await self.service.cmd_trade(
                            plan["ticker"],
                            plan["side"],
                            plan["entry_px"],
                            plan["stop_px"],
                            plan["target_px"],
                            plan["qty"],
                        )
                        reply = self.service.format_trade(record)
                    except Exception as e:
                        logger.error(f"Record trade error: {e}")
                        reply = f"Ошибка записи: {e}"
            elif cmd == "cancel":
                self._last_plans.pop(peer_id, None)
                reply = "Отменено."
            elif cmd == "positions":
                positions = await self.service.cmd_positions()
                reply = self.service.format_positions(positions)
                if positions:
                    await self._send_message(
                        peer_id,
                        reply,
                        keyboard=self._build_close_keyboard(positions),
                    )
                    return
            elif cmd == "close":
                if len(args) < 2:
                    reply = "Формат: /close <id> <цена_выхода>"
                else:
                    try:
                        trade_id = int(args[0])
                        exit_px = float(args[1])
                    except (ValueError, IndexError):
                        reply = "Ошибка. Пример: /close 3 260"
                    else:
                        result = await self.service.cmd_close(trade_id, exit_px)
                        reply = self.service.format_close(result)
            elif cmd == "backtest":
                if not args:
                    reply = "Укажи тикер: /backtest SBER"
                else:
                    ticker = args[0].upper()
                    days = int(args[1]) if len(args) > 1 else 365
                    await self._send_message(peer_id, f"Бэктест {ticker} за {days} дней...")
                    reply = await self.service.cmd_backtest(ticker, days)
            else:
                reply = "Неизвестная команда. Напиши /help для списка команд."

            await self._send_message(peer_id, reply, keyboard=self._build_main_keyboard())
        except Exception as e:
            logger.error(f"VK handle error: {e}")
            await self._send_message(peer_id, f"Ошибка: {e}", keyboard=self._build_main_keyboard())

    async def _send_message(self, peer_id: int, text: str, keyboard: str | None = None):
        """Send a message via VK API (run in thread pool)."""
        params = {"peer_id": peer_id, "message": text, "random_id": 0}
        if keyboard:
            params["keyboard"] = keyboard
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            self._executor,
            lambda: self._vk.method("messages.send", params)
        )

    # ── Run ─────────────────────────────────────────────────────

    async def run(self):
        if not VK_ACCESS_TOKEN or not VK_GROUP_ID:
            logger.warning("VK credentials not set, skipping VK bot")
            return

        self._init_vk()
        self._running = True
        logger.info("VK bot started, listening for messages...")

        loop = asyncio.get_event_loop()
        while self._running:
            try:
                events = await loop.run_in_executor(self._executor, self._longpoll.check)
                for event in events:
                    if event.type == self._event_type.MESSAGE_NEW:
                        asyncio.create_task(self._handle_message(event))
            except Exception as e:
                logger.error(f"VK Long Poll error: {e}")
                await asyncio.sleep(5)

    def stop(self):
        self._running = False
        self._executor.shutdown(wait=False)

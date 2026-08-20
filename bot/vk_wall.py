"""VK wall poster for scheduled reports.

Uses VK API wall.post to publish morning/evening reports and alerts.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from loguru import logger

from core.config import VK_ACCESS_TOKEN, VK_GROUP_ID
from bot.bot_service import BotService


class VkWallPoster:
    """Publishes reports and alerts to VK group wall."""

    def __init__(self, service: BotService | None = None):
        self.service = service or BotService()
        self._vk = None
        self._executor = ThreadPoolExecutor(max_workers=2)

    def _init_vk(self):
        import vk_api
        self._vk = vk_api.VkApi(token=VK_ACCESS_TOKEN)
        logger.info("VK wall poster initialized")

    async def _post(self, text: str):
        """Publish text to the group wall."""
        if not self._vk:
            self._init_vk()
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                self._executor,
                lambda: self._vk.method(
                    "wall.post",
                    {
                        "owner_id": f"-{VK_GROUP_ID}",
                        "from_group": 1,
                        "message": text,
                    },
                ),
            )
            logger.info("VK wall post published")
        except Exception as e:
            logger.error(f"VK wall post failed: {e}")

    # ── Scheduled reports ──────────────────────────────────────

    async def post_morning_report(self):
        """Publish morning screener to wall."""
        text = await self.service.morning_report()
        await self._post(text)

    async def post_evening_report(self):
        """Publish evening summary to wall."""
        text = await self.service.evening_report()
        await self._post(text)

    async def post_alert(self, ticker: str, message: str):
        """Publish intraday alert to wall."""
        text = await self.service.alert_text(ticker, message)
        await self._post(text)

    # ── Cleanup ─────────────────────────────────────────────────

    def close(self):
        self._executor.shutdown(wait=False)

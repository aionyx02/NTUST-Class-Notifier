# discord_bot.py
import asyncio
import logging

import discord

logger = logging.getLogger(__name__)


class DiscordBot(discord.Client):
    """
    機器人的連線與傳送訊息
    """

    def __init__(self, *, intents: discord.Intents, target_user_ids: list[int], startup_message: str):
        super().__init__(intents=intents)
        self.target_user_ids = tuple(target_user_ids)
        self.startup_message = startup_message  # 儲存啟動訊息
        self.ready_event = asyncio.Event()

    async def on_ready(self):
        """
        當 Bot 準備就緒時，發送啟動訊息
        """
        logger.info(f"已登入 Discord：{self.user}")
        self.ready_event.set()  # 必須先設定事件，send_dm 才能運作
        await self.send_dm(self.startup_message)  # 發送客製化的啟動訊息

    async def send_dm(self, message: str):
        """
        傳送私訊給預設的 target_user_ids
        """
        await self.ready_event.wait()  # 確保 Bot 已經 on_ready

        for target_user_id in self.target_user_ids:
            try:
                user = await self.fetch_user(target_user_id)
                await user.send(message)
                logger.debug(f"已向 <@{target_user_id}> 發送訊息：{message}")
            except discord.DiscordException as e:
                logger.error(f"傳送 Discord 私訊發生錯誤：{e}")
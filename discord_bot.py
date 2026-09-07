"""Discord Bot 的連線與訊息傳送。

收件對象可以是使用者（私訊）或文字頻道；同一個 key 的訊息會先刪舊的再送
新的，所以聊天室裡不會愈積愈多。
"""

import asyncio
import logging

import discord

logger = logging.getLogger(__name__)


class DiscordBot(discord.Client):
    """負責登入 Discord 並送出課程通知。

    Attributes:
        target_user_ids: 收件對象的 ID，可為使用者或頻道。
        startup_message: 登入完成後要送出的第一則訊息。
        message_key: 啟動訊息使用的 key，之後的更新用同一個 key 取代它。
        ready_event: on_ready 完成後才允許送訊息。
        sent_messages: key 對應到上一次送出的訊息，重送前會先刪掉。
        targets: 已解析過的收件對象快取。
    """

    def __init__(
        self,
        *,
        intents: discord.Intents,
        target_user_ids: list[int],
        startup_message: str,
        message_key: str = "狀態",
    ):
        """初始化 Bot。

        Args:
            intents: discord.py 的 intents 設定。
            target_user_ids: 收件對象的 ID 列表。
            startup_message: 登入完成後送出的訊息。
            message_key: 所有狀態更新共用的 key。
        """
        super().__init__(intents=intents)
        self.target_user_ids = tuple(target_user_ids)
        self.startup_message = startup_message
        self.message_key = message_key
        self.ready_event = asyncio.Event()
        self.sent_messages: dict[str, list[discord.Message]] = {}
        self.targets: dict[int, discord.abc.Messageable] = {}

    async def on_ready(self) -> None:
        """登入完成後送出啟動訊息。"""
        logger.info("已登入 Discord：%s", self.user)
        self.ready_event.set()  # 必須先設定事件，send_dm 才能運作。
        await self.send_dm(self.startup_message, key=self.message_key)

    async def send_dm(self, message: str, key: str | None = None) -> None:
        """送訊息給所有收件對象。

        給了 key 時會先刪掉上一次同一個 key 的訊息再送新的，所以聊天室裡
        永遠只留最新的那一則。

        Args:
            message: 要送出的內容。
            key: 用來取代舊訊息的識別字，None 代表這則不參與取代。
        """
        await self.ready_event.wait()

        if key:
            await self.delete_sent(key)

        sent: list[discord.Message] = []
        for target_id in self.target_user_ids:
            target = await self.resolve_target(target_id)
            if target is None:
                continue
            try:
                sent.append(await target.send(message))
                logger.debug("已向 %s 發送訊息", target_id)
            except discord.DiscordException as error:
                logger.error("傳送 Discord 訊息發生錯誤（%s）：%s",
                             target_id, error)

        if key and sent:
            self.sent_messages[key] = sent

    async def resolve_target(
        self, target_id: int
    ) -> discord.abc.Messageable | None:
        """把設定的 ID 解析成可以收訊息的對象。

        頻道 ID 會發到該頻道、使用者 ID 會發私訊。伺服器 ID 不能收訊息，
        這時直接把該伺服器的頻道 ID 列出來，省得使用者自己找。

        Args:
            target_id: .env 設定的 ID。

        Returns:
            可以呼叫 send() 的對象；無法解析時回傳 None。
        """
        if target_id in self.targets:
            return self.targets[target_id]

        target = self.get_channel(target_id)
        if target is None:
            target = await self._fetch_or_none(self.fetch_channel, target_id)
        if target is None:
            target = await self._fetch_or_none(self.fetch_user, target_id)

        if target is None:
            self._log_unresolved(target_id)
            return None

        self.targets[target_id] = target
        logger.info("通知對象 %s 解析為：%s", target_id, target)
        return target

    async def delete_sent(self, key: str) -> None:
        """刪掉某個 key 之前送出的訊息。

        Args:
            key: 要清掉的識別字；使用者自己刪過就當作已完成。
        """
        for message in self.sent_messages.pop(key, []):
            try:
                await message.delete()
                logger.debug("已刪除 %s 的舊訊息 %s", key, message.id)
            except discord.NotFound:
                logger.debug("%s 的舊訊息已不存在，略過刪除", key)
            except discord.DiscordException as error:
                logger.warning("刪除 %s 的舊訊息失敗：%s", key, error)

    @staticmethod
    async def _fetch_or_none(fetch, target_id: int):
        """呼叫 discord.py 的 fetch_* 並把錯誤轉成 None。

        Args:
            fetch: fetch_channel 或 fetch_user。
            target_id: 要查的 ID。

        Returns:
            查到的對象，或 None。
        """
        try:
            return await fetch(target_id)
        except discord.DiscordException:
            return None

    def _log_unresolved(self, target_id: int) -> None:
        """記錄無法解析的 ID，並在是伺服器 ID 時提示可用的頻道。

        Args:
            target_id: 無法解析的 ID。
        """
        guild = self.get_guild(target_id)
        if guild is None:
            logger.error("%s 不是看得到的頻道，也不是有效的使用者 ID，已略過。",
                         target_id)
            return

        channels = "、".join(
            f"#{channel.name}={channel.id}" for channel in guild.text_channels
        )
        logger.error(
            "%s 是伺服器「%s」的 ID，伺服器不能直接收訊息。"
            "請改填頻道 ID 或你自己的使用者 ID。可用的文字頻道：%s",
            target_id, guild.name, channels or "（沒有看得到的文字頻道）",
        )

"""Discord Bot 的連線與訊息傳送。

收件對象只要填 ID 就好，程式會自動判斷那是伺服器、文字頻道還是使用者：
填伺服器就自動挑一個機器人發得了言的文字頻道、填使用者就走私訊。同一個
key 的訊息會先刪舊的再送新的，所以聊天室裡不會愈積愈多。
"""

import asyncio
import logging

import discord

logger = logging.getLogger(__name__)


class DiscordBot(discord.Client):
    """負責登入 Discord 並送出課程通知。

    Attributes:
        target_ids: 收件對象的 ID，可為伺服器、文字頻道或使用者。
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
        target_ids: list[int],
        startup_message: str,
        message_key: str = "狀態",
    ):
        """初始化 Bot。

        Args:
            intents: discord.py 的 intents 設定。
            target_ids: 收件對象的 ID 列表，伺服器、頻道或使用者都可以。
            startup_message: 登入完成後送出的訊息。
            message_key: 所有狀態更新共用的 key。
        """
        super().__init__(intents=intents)
        self.target_ids = tuple(target_ids)
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
        for target_id in self.target_ids:
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
        """自動判斷 ID 的種類，並轉成可以收訊息的對象。

        依序判斷伺服器、文字頻道、使用者：伺服器本身不能收訊息，所以會自動
        挑一個機器人發得了言的文字頻道；頻道就直接發到該頻道；使用者則發
        私訊。結果會快取，之後不再重新判斷。

        Args:
            target_id: .env 設定的 ID。

        Returns:
            可以呼叫 send() 的對象；無法解析時回傳 None。
        """
        if target_id in self.targets:
            return self.targets[target_id]

        guild = await self._find_guild(target_id)
        if guild is not None:
            channel = await self._pick_channel(guild)
            if channel is None:
                logger.error(
                    "%s 是伺服器「%s」，但裡面沒有機器人能發言的文字頻道。"
                    "請確認邀請時給了「發送訊息」權限，或改填頻道 ID。",
                    target_id, guild.name,
                )
                return None
            return self._remember(
                target_id, channel, f"伺服器「{guild.name}」的 #{channel.name}"
            )

        channel = await self._find_channel(target_id)
        if isinstance(channel, discord.abc.Messageable):
            return self._remember(target_id, channel, "文字頻道")
        if channel is not None:
            logger.error("%s 是不能收訊息的頻道（%s），請改填文字頻道 ID。",
                         target_id, type(channel).__name__)
            return None

        user = await self._fetch_or_none(self.fetch_user, target_id)
        if user is not None:
            return self._remember(target_id, user, "使用者私訊")

        logger.error(
            "%s 不是機器人看得到的伺服器或頻道，"
            "也不是有效的使用者 ID，已略過。",
            target_id,
        )
        return None

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

    def _remember(
        self, target_id: int, target: discord.abc.Messageable, kind: str
    ) -> discord.abc.Messageable:
        """記住解析結果並回報判斷成什麼。

        Args:
            target_id: .env 設定的 ID。
            target: 解析出來的收件對象。
            kind: 判斷結果的中文說明，寫進 log 讓使用者確認。

        Returns:
            傳入的收件對象。
        """
        self.targets[target_id] = target
        logger.info("通知對象 %s 判定為%s", target_id, kind)
        return target

    async def _find_guild(self, target_id: int) -> discord.Guild | None:
        """查這個 ID 是不是機器人已加入的伺服器。

        Args:
            target_id: 要判斷的 ID。

        Returns:
            對應的伺服器，或 None。
        """
        guild = self.get_guild(target_id)
        if guild is not None:
            return guild
        return await self._fetch_or_none(self.fetch_guild, target_id)

    async def _find_channel(self, target_id: int):
        """查這個 ID 是不是機器人看得到的頻道。

        Args:
            target_id: 要判斷的 ID。

        Returns:
            對應的頻道；找不到時回傳 None。分類、語音等不能收訊息的頻道也
            會回傳，由呼叫端判斷並提示。
        """
        channel = self.get_channel(target_id)
        if channel is None:
            channel = await self._fetch_or_none(self.fetch_channel, target_id)
        return channel

    async def _pick_channel(
        self, guild: discord.Guild
    ) -> discord.TextChannel | None:
        """從伺服器裡挑一個機器人發得了言的文字頻道。

        優先用伺服器設定的系統頻道，其次照頻道順序找第一個有權限的。

        Args:
            guild: 要挑頻道的伺服器。

        Returns:
            可以發言的文字頻道；都沒有權限時回傳 None。
        """
        channels = list(guild.text_channels)
        if not channels:
            # fetch_guild 拿到的伺服器沒有頻道快取，要再抓一次。
            fetched = await self._fetch_or_none(guild.fetch_channels) or []
            channels = [
                channel for channel in fetched
                if isinstance(channel, discord.TextChannel)
            ]
        if not channels:
            return None

        me = guild.me
        if me is None and self.user is not None:
            me = await self._fetch_or_none(guild.fetch_member, self.user.id)

        ordered = sorted(channels, key=lambda channel: channel.position)
        if guild.system_channel in channels:
            ordered.insert(0, guild.system_channel)

        for channel in ordered:
            if me is None:
                # 查不到自己的身分就先試，權限不足時送出才會報錯。
                return channel
            allowed = channel.permissions_for(me)
            if allowed.view_channel and allowed.send_messages:
                return channel
        return None

    @staticmethod
    async def _fetch_or_none(fetch, *args):
        """呼叫 discord.py 的 fetch_* 並把錯誤轉成 None。

        Args:
            fetch: fetch_guild、fetch_channel、fetch_user 等方法。
            *args: 傳給該方法的參數。

        Returns:
            查到的對象，或 None。
        """
        try:
            return await fetch(*args)
        except discord.DiscordException:
            return None

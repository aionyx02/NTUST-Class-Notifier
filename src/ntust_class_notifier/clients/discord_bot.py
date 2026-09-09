"""Discord Bot 的連線與訊息傳送。

收件對象只要填 ID 就好，程式會自動判斷那是伺服器、文字頻道還是使用者：
填伺服器就自動挑一個機器人發得了言的文字頻道、填使用者就走私訊。

同一個 key 的狀態訊息永遠只有一則：更新時直接 edit 原本那一則，不是「先
刪再送」——先刪再送只要新的那則送失敗，頻道裡就完全沒有狀態了。訊息 ID
會寫進 config.data_dir()，程式重開之後還是編輯同一則，不會每次重跑就在頻
道裡多留一則孤兒訊息。
"""

import asyncio
import json
import logging
import pathlib

import discord

from ntust_class_notifier import config

logger = logging.getLogger(__name__)

# 訊息 ID 的存檔名稱，放在 config.data_dir() 底下。
STORE_FILENAME = "discord_messages.json"


class MessageStore:
    """記住每個 key 在各個對象送出的訊息，讓重啟後還編輯得回去。

    只存 ID，不存內容。存檔壞掉或讀不到時一律當成「沒有紀錄」，大不了重
    送一則新訊息，不該讓通知功能整個起不來。

    Attributes:
        path: 存檔位置。
    """

    def __init__(self, path: pathlib.Path | None = None):
        """開啟（或建立）訊息 ID 存檔。

        Args:
            path: 存檔位置，None 代表用 config.data_dir() 下的預設檔。
        """
        self.path = path or config.data_dir() / STORE_FILENAME
        self._data: dict[str, dict[str, list[int]]] = self._read()

    def _read(self) -> dict[str, dict[str, list[int]]]:
        """讀取存檔。

        Returns:
            key -> {收件對象 ID: [頻道 ID, 訊息 ID]}；讀不到時為空 dict。
        """
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            logger.warning("讀不到訊息紀錄 %s（%s），這次重新送出訊息。",
                           self.path, error)
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write(self) -> None:
        """把目前的紀錄寫回存檔。"""
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as error:
            logger.warning("寫不進訊息紀錄 %s（%s），下次重開會重送一則新"
                           "訊息。", self.path, error)

    def get(self, key: str, target_id: int) -> tuple[int, int] | None:
        """取出之前送出的訊息位置。

        Args:
            key: 訊息識別字。
            target_id: 收件對象 ID。

        Returns:
            (頻道 ID, 訊息 ID)；沒有紀錄時回傳 None。
        """
        entry = self._data.get(key, {}).get(str(target_id))
        if not isinstance(entry, list) or len(entry) != 2:
            return None
        try:
            return int(entry[0]), int(entry[1])
        except (TypeError, ValueError):
            return None

    def remember(
        self, key: str, target_id: int, channel_id: int, message_id: int
    ) -> None:
        """記住某個 key 在某個對象送出的訊息。

        Args:
            key: 訊息識別字。
            target_id: 收件對象 ID。
            channel_id: 訊息所在的頻道 ID。
            message_id: 訊息 ID。
        """
        self._data.setdefault(key, {})[str(target_id)] = [
            channel_id, message_id]
        self._write()

    def forget(self, key: str, target_id: int) -> None:
        """忘掉某個 key 在某個對象的紀錄。

        Args:
            key: 訊息識別字。
            target_id: 收件對象 ID。
        """
        if self._data.get(key, {}).pop(str(target_id), None) is not None:
            self._write()


class DiscordBot(discord.Client):
    """負責登入 Discord 並送出課程通知。

    Attributes:
        target_ids: 收件對象的 ID，可為伺服器、文字頻道或使用者。
        startup_message: 登入完成後要送出的第一則訊息。
        message_key: 啟動訊息使用的 key，之後的更新會編輯同一則訊息。
        ready_event: on_ready 完成後才允許送訊息。
        store: 訊息 ID 的存檔，重啟後靠它找回要編輯的訊息。
        sent_messages: (key, 收件對象) 對應到這次執行送出的訊息。
        targets: 已解析過的收件對象快取。
    """

    def __init__(
        self,
        *,
        intents: discord.Intents,
        target_ids: list[int],
        startup_message: str,
        message_key: str = "狀態",
        store: MessageStore | None = None,
    ):
        """初始化 Bot。

        Args:
            intents: discord.py 的 intents 設定。
            target_ids: 收件對象的 ID 列表，伺服器、頻道或使用者都可以。
            startup_message: 登入完成後送出的訊息。
            message_key: 所有狀態更新共用的 key。
            store: 訊息 ID 存檔，None 代表用預設位置。
        """
        super().__init__(intents=intents)
        self.target_ids = tuple(target_ids)
        self.startup_message = startup_message
        self.message_key = message_key
        self.ready_event = asyncio.Event()
        self.store = store if store is not None else MessageStore()
        self.sent_messages: dict[tuple[str, int], discord.abc.Snowflake] = {}
        self.targets: dict[int, discord.abc.Messageable] = {}

    async def on_ready(self) -> None:
        """登入完成後送出啟動訊息。"""
        logger.info("已登入 Discord：%s", self.user)
        self.ready_event.set()  # 必須先設定事件，send_dm 才能運作。
        await self.send_dm(self.startup_message, key=self.message_key)

    async def send_dm(self, message: str, key: str | None = None) -> None:
        """送訊息給所有收件對象。

        給了 key 時會直接編輯上一次同一個 key 的訊息（含上一次執行留下
        的），所以聊天室裡永遠只留那一則，而且更新失敗也不會讓頻道空著。

        Args:
            message: 要送出的內容。
            key: 用來更新舊訊息的識別字，None 代表這則單純送出。
        """
        await self.ready_event.wait()

        for target_id in self.target_ids:
            target = await self.resolve_target(target_id)
            if target is None:
                continue
            await self._deliver(target, target_id, message, key)

    async def _deliver(
        self,
        target: discord.abc.Messageable,
        target_id: int,
        message: str,
        key: str | None,
    ) -> None:
        """把訊息送到單一收件對象：能編輯就編輯，否則送一則新的。

        Args:
            target: 收件對象。
            target_id: 收件對象 ID。
            message: 要送出的內容。
            key: 訊息識別字，None 代表不參與更新。
        """
        if key and await self._edit_existing(target, target_id, message, key):
            return

        try:
            sent = await target.send(message)
        except discord.DiscordException as error:
            logger.error("傳送 Discord 訊息發生錯誤（%s）：%s",
                         target_id, error)
            return

        logger.debug("已向 %s 發送訊息", target_id)
        if key:
            self.sent_messages[(key, target_id)] = sent
            self.store.remember(key, target_id, sent.channel.id, sent.id)

    async def _edit_existing(
        self,
        target: discord.abc.Messageable,
        target_id: int,
        message: str,
        key: str,
    ) -> bool:
        """試著把舊訊息改成新內容。

        Args:
            target: 收件對象。
            target_id: 收件對象 ID。
            message: 新的內容。
            key: 訊息識別字。

        Returns:
            是否成功更新；False 代表呼叫端應該改送一則新訊息。
        """
        existing = self.sent_messages.get((key, target_id))
        if existing is None:
            existing = await self._restore(target, target_id, key)
            if existing is None:
                return False
            # 記下來，否則每一輪更新都要再還原一次（私訊還會每輪多開一次
            # DM 頻道），白白多打 Discord API。
            self.sent_messages[(key, target_id)] = existing

        try:
            await existing.edit(content=message)
            return True
        except discord.NotFound:
            logger.debug("%s 在 %s 的舊訊息已不存在，改送新的",
                         key, target_id)
        except discord.DiscordException as error:
            logger.warning("更新 %s 在 %s 的訊息失敗（%s），改送新的",
                           key, target_id, error)

        self.sent_messages.pop((key, target_id), None)
        self.store.forget(key, target_id)
        return False

    async def _restore(
        self, target: discord.abc.Messageable, target_id: int, key: str
    ) -> discord.PartialMessage | None:
        """把存檔裡的訊息 ID 還原成可以編輯的訊息。

        Args:
            target: 收件對象。
            target_id: 收件對象 ID。
            key: 訊息識別字。

        Returns:
            可以呼叫 edit() 的訊息；沒有紀錄或頻道找不到時回傳 None。
        """
        stored = self.store.get(key, target_id)
        if stored is None:
            return None

        channel_id, message_id = stored
        channel = await self._channel_for(target, channel_id)
        partial = getattr(channel, "get_partial_message", None)
        if partial is None:
            self.store.forget(key, target_id)
            return None

        logger.debug("沿用上一次執行留下的 %s 訊息 %s", key, message_id)
        return partial(message_id)

    async def _channel_for(
        self, target: discord.abc.Messageable, channel_id: int
    ) -> discord.abc.Messageable | discord.abc.GuildChannel | None:
        """找出當初送出訊息的頻道。

        Args:
            target: 收件對象。
            channel_id: 存檔裡記下的頻道 ID。

        Returns:
            對應的頻道；找不到時回傳 None。私訊的頻道 ID 抓不回來，所以
            改用收件對象自己的 DM 頻道。
        """
        create_dm = getattr(target, "create_dm", None)
        if create_dm is not None:
            return (getattr(target, "dm_channel", None)
                    or await self._fetch_or_none(create_dm))
        if getattr(target, "id", None) == channel_id:
            return target
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self._fetch_or_none(self.fetch_channel, channel_id)
        return channel

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

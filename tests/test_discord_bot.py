"""clients.discord_bot：收件對象的自動判斷與狀態訊息的更新。"""

import asyncio
import pathlib
import tempfile
import types

import discord
import pytest

from ntust_class_notifier.clients import discord_bot

GUILD_ID = 1503991654296715286
CHANNEL_ID = 1546487069977739304
LOCKED_CHANNEL_ID = 1503991655043305625
USER_ID = 1321403141173809193
CATEGORY_ID = 999
UNKNOWN_ID = 12345


class _Perms:
    def __init__(self, allowed: bool):
        self.view_channel = allowed
        self.send_messages = allowed


def _not_found() -> discord.NotFound:
    """組一個 discord.NotFound，代表訊息已經被人刪掉了。

    Returns:
        可以直接 raise 的例外。
    """
    return discord.NotFound(
        types.SimpleNamespace(status=404, reason="Not Found"), "unknown")


class _FakeMessage:
    """記錄自己被編輯成什麼的假訊息。

    Attributes:
        channel: 訊息所在的頻道。
        id: 訊息 ID。
        content: 目前的內容。
        gone: 是否已經被人刪掉（編輯時會丟 NotFound）。
    """

    def __init__(self, channel: "_FakeChannel", message_id: int,
                 content: str, gone: bool = False):
        self.channel = channel
        self.id = message_id
        self.content = content
        self.gone = gone

    async def edit(self, content: str | None = None) -> None:
        if self.gone:
            raise _not_found()
        self.content = content or ""


class _FakeChannel(discord.abc.Messageable):
    """能被 isinstance(..., Messageable) 認出的假頻道。

    Attributes:
        sent: 這個頻道裡目前留下的訊息。
    """

    def __init__(self, name: str, channel_id: int, position: int,
                 allowed: bool = True):
        self.name = name
        self.id = channel_id
        self.position = position
        self._allowed = allowed
        self.sent: list[_FakeMessage] = []
        self._next_id = 100

    async def _get_channel(self) -> "_FakeChannel":
        return self

    def permissions_for(self, member: object) -> _Perms:
        return _Perms(self._allowed)

    async def send(self, content: str) -> _FakeMessage:
        self._next_id += 1
        message = _FakeMessage(self, self._next_id, content)
        self.sent.append(message)
        return message

    def get_partial_message(self, message_id: int) -> _FakeMessage:
        for message in self.sent:
            if message.id == message_id:
                return message
        # 訊息不在這個頻道裡（例如被人刪了），編輯時才會知道。
        return _FakeMessage(self, message_id, "", gone=True)


class _FakeGuild:
    def __init__(self, name: str, channels: list[_FakeChannel]):
        self.name = name
        self.id = GUILD_ID
        self.text_channels = channels
        self.system_channel = None
        self.me = "member"


class _FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id
        self.name = "someone"


def _make_bot(guild: _FakeGuild | None,
              channel: _FakeChannel,
              target_ids: list[int] | None = None,
              store: discord_bot.MessageStore | None = None,
              ) -> discord_bot.DiscordBot:
    """建一個所有查詢都走假資料的 Bot。

    Args:
        guild: get_guild 要回傳的伺服器，None 代表查不到。
        channel: get_channel 要回傳的頻道。
        target_ids: 收件對象，預設沒有（只測 resolve_target）。
        store: 訊息 ID 存檔，None 代表用暫存的空存檔。

    Returns:
        可直接呼叫 resolve_target 或 send_dm 的 Bot。
    """

    class _Bot(discord_bot.DiscordBot):
        def get_guild(self, guild_id: int):
            return guild if guild and guild_id == guild.id else None

        async def fetch_guild(self, guild_id: int, **kwargs):
            raise discord.DiscordException("not found")

        def get_channel(self, channel_id: int):
            if channel_id == channel.id:
                return channel
            if channel_id == CATEGORY_ID:
                return object()  # 分類頻道不是 Messageable。
            return None

        async def fetch_channel(self, channel_id: int):
            raise discord.DiscordException("not found")

        async def fetch_user(self, user_id: int):
            if user_id == USER_ID:
                return _FakeUser(user_id)
            raise discord.DiscordException("unknown user")

    bot = _Bot(
        intents=discord.Intents.default(),
        target_ids=target_ids or [],
        startup_message="",
        # 沒指定就給一個誰也不會撞到的空目錄，免得測試之間互相干擾、或把
        # JSON 寫進專案目錄。
        store=store or discord_bot.MessageStore(
            pathlib.Path(tempfile.mkdtemp()) / "messages.json"),
    )
    bot.ready_event.set()  # 測試不連線，直接當成已登入。
    return bot


@pytest.fixture
def open_channel() -> _FakeChannel:
    return _FakeChannel("course-bot", CHANNEL_ID, position=5)


@pytest.fixture
def locked_channel() -> _FakeChannel:
    return _FakeChannel("一般", LOCKED_CHANNEL_ID, position=0, allowed=False)


def test_guild_id_resolves_to_a_writable_channel(
    open_channel: _FakeChannel, locked_channel: _FakeChannel
) -> None:
    guild = _FakeGuild("base", [locked_channel, open_channel])
    bot = _make_bot(guild, open_channel)

    target = asyncio.run(bot.resolve_target(GUILD_ID))

    # 位置在前但沒有發言權的頻道要跳過。
    assert target is open_channel


def test_guild_without_writable_channel_is_rejected(
    locked_channel: _FakeChannel,
) -> None:
    guild = _FakeGuild("locked", [locked_channel])
    bot = _make_bot(guild, locked_channel)

    assert asyncio.run(bot.resolve_target(GUILD_ID)) is None


def test_channel_id_resolves_to_that_channel(
    open_channel: _FakeChannel,
) -> None:
    bot = _make_bot(None, open_channel)

    assert asyncio.run(bot.resolve_target(CHANNEL_ID)) is open_channel


def test_user_id_resolves_to_a_direct_message(
    open_channel: _FakeChannel,
) -> None:
    bot = _make_bot(None, open_channel)

    target = asyncio.run(bot.resolve_target(USER_ID))

    assert isinstance(target, _FakeUser)


def test_non_messageable_channel_is_rejected(
    open_channel: _FakeChannel,
) -> None:
    bot = _make_bot(None, open_channel)

    assert asyncio.run(bot.resolve_target(CATEGORY_ID)) is None


def test_unknown_id_is_rejected(open_channel: _FakeChannel) -> None:
    bot = _make_bot(None, open_channel)

    assert asyncio.run(bot.resolve_target(UNKNOWN_ID)) is None


def test_resolved_target_is_cached(open_channel: _FakeChannel) -> None:
    bot = _make_bot(None, open_channel)

    asyncio.run(bot.resolve_target(CHANNEL_ID))

    assert bot.targets == {CHANNEL_ID: open_channel}


def _store(tmp_path: pathlib.Path) -> discord_bot.MessageStore:
    """建一個寫在暫存目錄的訊息 ID 存檔。

    Args:
        tmp_path: pytest 的暫存目錄。

    Returns:
        空的存檔。
    """
    return discord_bot.MessageStore(tmp_path / "discord_messages.json")


def test_status_updates_edit_the_same_message(
    open_channel: _FakeChannel, tmp_path: pathlib.Path
) -> None:
    bot = _make_bot(None, open_channel, [CHANNEL_ID], _store(tmp_path))

    asyncio.run(bot.send_dm("第一版", key="狀態"))
    asyncio.run(bot.send_dm("第二版", key="狀態"))

    # 先刪再送只要新的那則送失敗，頻道就完全沒有狀態了，所以改成直接編輯。
    assert len(open_channel.sent) == 1
    assert open_channel.sent[0].content == "第二版"


def test_a_message_without_a_key_is_always_a_new_one(
    open_channel: _FakeChannel, tmp_path: pathlib.Path
) -> None:
    bot = _make_bot(None, open_channel, [CHANNEL_ID], _store(tmp_path))

    asyncio.run(bot.send_dm("一次性通知"))
    asyncio.run(bot.send_dm("另一則"))

    assert [message.content for message in open_channel.sent] == [
        "一次性通知", "另一則"]


def test_a_deleted_message_falls_back_to_sending_a_new_one(
    open_channel: _FakeChannel, tmp_path: pathlib.Path
) -> None:
    bot = _make_bot(None, open_channel, [CHANNEL_ID], _store(tmp_path))
    asyncio.run(bot.send_dm("第一版", key="狀態"))
    open_channel.sent[0].gone = True  # 使用者自己把它刪了。

    asyncio.run(bot.send_dm("第二版", key="狀態"))

    assert [message.content for message in open_channel.sent] == [
        "第一版", "第二版"]


def test_the_message_id_survives_a_restart(
    open_channel: _FakeChannel, tmp_path: pathlib.Path
) -> None:
    path = tmp_path / "discord_messages.json"
    first = _make_bot(None, open_channel, [CHANNEL_ID],
                      discord_bot.MessageStore(path))
    asyncio.run(first.send_dm("開機了", key="狀態"))

    # 換一個 Bot，等於程式重開一次；ID 只留在記憶體的話這裡就會多一則。
    second = _make_bot(None, open_channel, [CHANNEL_ID],
                       discord_bot.MessageStore(path))
    asyncio.run(second.send_dm("又開機了", key="狀態"))

    assert len(open_channel.sent) == 1
    assert open_channel.sent[0].content == "又開機了"


def test_a_broken_store_file_just_means_a_new_message(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "discord_messages.json"
    path.write_text("{ 這不是 JSON", encoding="utf-8")

    store = discord_bot.MessageStore(path)

    # 存檔壞掉大不了重送一則，不該讓通知功能整個起不來。
    assert store.get("狀態", CHANNEL_ID) is None

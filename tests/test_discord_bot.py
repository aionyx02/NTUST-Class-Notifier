"""clients.discord_bot：收件對象的自動判斷。"""

import asyncio

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


class _FakeChannel(discord.abc.Messageable):
    """能被 isinstance(..., Messageable) 認出的假頻道。"""

    def __init__(self, name: str, channel_id: int, position: int,
                 allowed: bool = True):
        self.name = name
        self.id = channel_id
        self.position = position
        self._allowed = allowed

    async def _get_channel(self) -> "_FakeChannel":
        return self

    def permissions_for(self, member: object) -> _Perms:
        return _Perms(self._allowed)


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
              channel: _FakeChannel) -> discord_bot.DiscordBot:
    """建一個所有查詢都走假資料的 Bot。

    Args:
        guild: get_guild 要回傳的伺服器，None 代表查不到。
        channel: get_channel 要回傳的頻道。

    Returns:
        可直接呼叫 resolve_target 的 Bot。
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

    return _Bot(
        intents=discord.Intents.default(),
        target_ids=[],
        startup_message="",
    )


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

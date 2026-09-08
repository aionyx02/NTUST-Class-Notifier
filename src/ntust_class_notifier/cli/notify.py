"""ntust-notify：讀取 .env 的規則，監控課程空位並發 Discord 通知。

用法：
    uv run ntust-notify
"""

import asyncio
import logging
import sys

import discord

from ntust_class_notifier import config
from ntust_class_notifier.app import notify
from ntust_class_notifier.app import search
from ntust_class_notifier.clients import course_api
from ntust_class_notifier.clients import discord_bot
from ntust_class_notifier.clients import enrollment
from ntust_class_notifier.core import ruleset
from ntust_class_notifier.ui import board
from ntust_class_notifier.ui import console

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """設定 log 格式與輸出編碼。"""
    console.setup_terminal()  # 先切成 UTF-8，log 的中文才不會亂碼。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # 避免 httpx 的 debug log 過於頻繁地洗版。
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def login_selector(
    settings: config.Settings,
) -> enrollment.CourseSelector | None:
    """視設定登入選課系統。

    Args:
        settings: .env 設定。

    Returns:
        登入成功的客戶端；沒設定帳密或登入失敗時回傳 None。
    """
    if not settings.selector_enabled:
        logger.info("未設定 STUDENT_ID/PASSWORD，不啟用自動加選")
        return None

    selector = enrollment.CourseSelector(
        settings.student_id, settings.password)
    if await enrollment.run_sync(selector.login):
        logger.info("選課系統自動加選功能已啟用")
        return selector

    logger.warning("選課系統登入失敗，自動加選功能停用")
    return None


def create_bot(
    settings: config.Settings, startup_message: str
) -> discord_bot.DiscordBot | None:
    """視設定建立 Discord Bot。

    Args:
        settings: .env 設定。
        startup_message: 登入完成後要送出的訊息。

    Returns:
        建好但尚未連線的 Bot；沒設定 token 或收件對象時回傳 None。
    """
    if not settings.discord_enabled:
        logger.warning("未提供 DISCORD_BOT_TOKEN 或 DISCORD_TARGET_IDS，"
                       "將不會發送 Discord 通知。")
        return None

    return discord_bot.DiscordBot(
        intents=discord.Intents.default(),
        target_ids=list(settings.discord_target_ids),
        startup_message=startup_message,
        message_key=notify.STATUS_KEY,
    )


async def main(settings: config.Settings) -> None:
    """組裝設定並啟動所有監控任務。

    Args:
        settings: .env 設定。
    """
    if not settings.look_up_classes:
        logger.error("未設定 LOOK_UP_CLASSES，沒有任何可監控的課程。")
        return

    try:
        parsed = ruleset.parse_rules(settings.look_up_classes)
    except ruleset.RuleError as error:
        logger.error("LOOK_UP_CLASSES 規則錯誤：%s", error)
        sys.exit(1)

    client = course_api.CourseClient()
    explicit = (
        parsed[0].semester
        if parsed and all(rule.semester for rule in parsed)
        else ""
    )
    semester, note = await search.resolve_semester(client, explicit)
    if note:
        logger.info(note)

    selector = await login_selector(settings)
    initial = await search.search(client, parsed, semester)
    bot = create_bot(settings, board.startup_message(
        parsed, initial.counts, initial.courses, semester, bool(selector)))

    tasks = [asyncio.create_task(
        notify.monitor_courses(client, parsed, semester, bot, selector))]
    if selector:
        tasks.append(asyncio.create_task(notify.session_keepalive(selector)))
    if bot:
        # 一起 gather 才會保留 task 參考，Bot 斷線或 token 錯誤也才看得到。
        tasks.append(asyncio.create_task(bot.start(settings.discord_token)))

    await asyncio.gather(*tasks)


def run() -> None:
    """命令列進入點。"""
    setup_logging()
    try:
        settings = config.Settings.from_env()
    except config.ConfigError as error:
        logger.error("%s", error)
        sys.exit(1)

    try:
        asyncio.run(main(settings))
    except KeyboardInterrupt:
        logger.info("程式被使用者中斷，正在關閉...")


if __name__ == "__main__":
    run()

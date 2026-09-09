"""Discord 通知版監控迴圈。

任何一門課的人數有變動就重送看板；完全沒有變動時也會定時重送一次，讓訊息
上的時間戳不會停在啟動當下。
"""

import asyncio
import logging
import time

from ntust_class_notifier.app import enroll as enroll_app
from ntust_class_notifier.app import monitor
from ntust_class_notifier.app import search
from ntust_class_notifier.clients import course_api
from ntust_class_notifier.clients import discord_bot
from ntust_class_notifier.clients import enrollment
from ntust_class_notifier.clients import sound
from ntust_class_notifier.core import changes
from ntust_class_notifier.core import models
from ntust_class_notifier.core import ruleset
from ntust_class_notifier.ui import board

logger = logging.getLogger(__name__)

# 所有通知共用一個 key，聊天室裡永遠只留一則訊息。
STATUS_KEY = "狀態"

# 監控的最短週期；查詢比它久時會自動放寬成「查詢耗時 × 2」。
MIN_INTERVAL = 3.0

# 完全沒有變動時，最多隔這麼久重送一次看板，讓時間戳不會停在啟動當下。
HEARTBEAT_SECONDS = 300.0


async def monitor_courses(
    client: course_api.CourseClient,
    parsed: list[ruleset.Rule],
    semester: str,
    bot: discord_bot.DiscordBot | None,
    selector: enrollment.CourseSelector | None,
) -> None:
    """監控規則命中的課程人數。

    規則裡的課號是前綴，所以一條規則可能命中很多門課，全部都要監控。

    Args:
        client: 課程查詢客戶端。
        parsed: 篩選規則。
        semester: 規則沒指定學期時採用的學期。
        bot: Discord Bot，None 代表只輸出到終端機。
        selector: 已登入的選課系統客戶端，None 代表不自動加選。
    """
    previous: dict[str, models.Course] = {}
    previous_vacant: set[str] = set()
    enroller = enroll_app.AutoEnroller(selector) if selector else None
    # 從現在起算心跳，啟動訊息才不會馬上被看板取代。
    last_board = time.monotonic()
    rounds = 0

    while True:
        try:
            started = time.monotonic()
            result = await search.search(client, parsed, semester)
            if result.failed:
                # 查詢失敗時不比對，否則會把「查不到」誤判成人數歸零。
                logger.warning("查詢失敗：%s，本輪略過",
                               "、".join(result.failed))
                await asyncio.sleep(MIN_INTERVAL)
                continue

            rounds += 1
            current = {
                match.course.course_no: match.course
                for match in result.matches
            }
            vacant = await search.collect_vacant(
                client, result, semester)
            current_vacant = {course.course_no for course, _ in vacant}

            round_changes = (
                changes.diff_rounds(previous, current) if previous else []
            )
            lines = board.change_lines(round_changes)
            for line in lines:
                logger.info("%s", line.strip())

            new_ones = sorted(current_vacant - previous_vacant)
            if new_ones:
                sound.play_sound("vacancy")
                logger.info("偵測到空位：%s", "、".join(new_ones))

            # 交給 AutoEnroller，時段判斷與一輪的加選上限才會跟另外兩支
            # 指令完全一致。
            notes = await enroller.on_round(current_vacant) if enroller else []

            stale = time.monotonic() - last_board >= HEARTBEAT_SECONDS
            if bot and (round_changes or notes or stale
                        or current_vacant != previous_vacant):
                await bot.send_dm(
                    board.board_message(vacant, lines, len(result.matches),
                                        semester, notes),
                    key=STATUS_KEY,
                )
                last_board = time.monotonic()

            previous = current
            previous_vacant = current_vacant
            logger.debug("第 %d 輪：%d 門課程、%d 門有空位、%d 筆變動",
                         rounds, len(current), len(vacant), len(round_changes))

            # 查詢比週期還久時（規則範圍很大）自動放寬，免得請求疊在一起。
            await monitor.pace(MIN_INTERVAL, started)
        except Exception as error:  # noqa: BLE001 - 監控不該因單次錯誤中斷
            logger.error("監控課程時發生未知錯誤: %s", error, exc_info=True)
            await asyncio.sleep(60)

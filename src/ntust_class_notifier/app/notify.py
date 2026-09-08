"""Discord 通知版監控迴圈。

任何一門課的人數有變動就重送看板；完全沒有變動時也會定時重送一次，讓訊息
上的時間戳不會停在啟動當下。
"""

import asyncio
import logging
import time

from ntust_class_notifier.app import monitor
from ntust_class_notifier.app import search
from ntust_class_notifier.clients import course_api
from ntust_class_notifier.clients import discord_bot
from ntust_class_notifier.clients import enrollment
from ntust_class_notifier.clients import sound
from ntust_class_notifier.core import changes
from ntust_class_notifier.core import models
from ntust_class_notifier.core import periods
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
            vacant = await collect_vacant(client, result, semester)
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

            notes = []
            if selector and periods.get_current_period() == "dept":
                for course_no in new_ones:
                    notes.append(await enroll(selector, course_no))

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


async def collect_vacant(
    client: course_api.CourseClient,
    result: search.SearchResult,
    semester: str,
) -> list[tuple[models.Course, str]]:
    """挑出這一輪真的可以選的課程。

    Args:
        client: 課程查詢客戶端。
        result: 這一輪的查詢結果。
        semester: 這次查詢的學期。

    Returns:
        (課程, 系所名額說明) 的列表；系所已額滿的假空位不會列入。
    """
    # 系所名額一門課要一次請求，命中太多門就整輪跳過這項檢查。
    dept_allowed = len(result.matches) <= ruleset.DEPT_LOOKUP_LIMIT
    vacant: list[tuple[models.Course, str]] = []

    for match in result.matches:
        course = match.course
        if course.member_limit <= 0 or course.cur_member >= course.member_limit:
            continue

        dept_info = ""
        if match.dept and dept_allowed:
            limit = await client.get_department_limit(
                semester, course.course_no, match.dept)
            if limit is not None:
                persons, restrict = limit
                if persons >= restrict:
                    logger.debug("總數有空位但系所額滿: %s (%s %d/%d)",
                                 course.course_no, match.dept,
                                 persons, restrict)
                    continue
                dept_info = f"{persons} / {restrict}（{match.dept}）"

        vacant.append((course, dept_info))
    return vacant


async def enroll(
    selector: enrollment.CourseSelector, course_no: str
) -> str:
    """送出加選並驗證結果。

    Args:
        selector: 已登入的選課系統客戶端。
        course_no: 要加選的課程代碼。

    Returns:
        一行給訊息用的結果說明。
    """
    success, body = await enrollment.run_sync(
        selector.select_course, course_no)
    if not success:
        sound.play_sound("failure")
        return f"{course_no} 自動加選失敗：{body[:100]}"

    await asyncio.sleep(3)  # 等系統寫入後再確認課程是否真的在清單裡。
    verified = await enrollment.run_sync(selector.verify_enrolled, course_no)
    if verified:
        sound.play_sound("success")
        return f"{course_no} 加選成功，已確認出現在選課清單中"

    sound.play_sound("failure")
    return (
        f"{course_no} 已送出加選但未在清單中確認（名額可能已被搶走）："
        f"{body[:100]}"
    )


async def session_keepalive(selector: enrollment.CourseSelector) -> None:
    """每 3 分鐘訪問選課頁面以維持登入狀態。

    Args:
        selector: 已登入的選課系統客戶端。
    """
    while True:
        await asyncio.sleep(180)
        try:
            await enrollment.run_sync(selector.keepalive)
        except Exception as error:  # noqa: BLE001 - keepalive 失敗不該中斷
            logger.error("Session keepalive 錯誤: %s", error)

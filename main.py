"""主入口：讀取 .env 的篩選規則，監控課程空位並透過 Discord 通知。

Discord 通知採「單一訊息」模式：有空位的課程集合有變動時，先刪掉舊訊息再
送一則新的，所以聊天室裡永遠只有一則最新狀態。
"""

import asyncio
import datetime
import logging
import os
import sys
import time

import discord
import dotenv

import course_filter
import course_lookup
import course_selector
import course_watch
import discord_bot
import selection_period

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
# 避免 httpx 的 debug log 過於頻繁地洗版。
logging.getLogger("httpx").setLevel(logging.WARNING)

dotenv.load_dotenv()
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
STUDENT_ID = os.environ.get("STUDENT_ID", "")
PASSWORD = os.environ.get("PASSWORD", "")

# Discord 單則訊息上限 2000 字，留點餘裕給程式碼區塊符號。
DISCORD_LIMIT = 1900

# 所有通知共用一個 key，聊天室裡永遠只留一則訊息。
STATUS_KEY = "狀態"

# 啟動訊息最多列幾門課。
STARTUP_LIST_LIMIT = 15

# 監控的最短週期；查詢比它久時會自動放寬成「查詢耗時 × 2」。
MIN_INTERVAL = 3.0

try:
    _target_ids = os.environ.get("DISCORD_TARGET_USER_IDS", "")
    DISCORD_TARGET_USER_IDS = [
        int(value) for value in _target_ids.split(";") if value
    ]
except ValueError as _error:
    logger.error("DISCORD_TARGET_USER_IDS 格式錯誤，ID 必須是純數字: %s",
                 _error)
    sys.exit(1)

LOOK_UP_CLASSES = course_filter.rules_from_env()


def course_line(course: course_lookup.Course) -> str:
    """把課程排成與終端機監看相同的等寬一行。

    Args:
        course: 要顯示的課程。

    Returns:
        課號、課名、老師、節次、人數對齊後的字串。
    """
    return (
        f"{course_watch.format_course(course)} "
        f"{course.cur_member}/{course.member_limit}"
    )


def code_block(lines: list[str]) -> str:
    """把多行內容包成 Discord 的 diff 程式碼區塊。

    用 diff 語法上色：`+` 開頭的行是綠色（有空位）、`-` 開頭的行是紅色
    （被選走或額滿），其餘行前面補兩格讓欄位仍然對齊。等寬字體才對得齊，
    超過長度上限時在完整行的邊界截斷。

    Args:
        lines: 要包起來的每一行，已自行帶上 "+ "、"- " 或 "  " 前綴。

    Returns:
        可直接送出的訊息片段。
    """
    body = "\n".join(lines)
    if len(body) > DISCORD_LIMIT:
        body = body[:DISCORD_LIMIT].rsplit("\n", 1)[0] + "\n…（訊息過長已截斷）"
    return f"```diff\n{body}\n```"


def board_message(
    vacant: list[tuple[course_lookup.Course, str]],
    taken: list[course_lookup.Course],
    watched: int,
    semester: str,
    notes: list[str],
) -> str:
    """組出目前狀態的單一訊息。

    Args:
        vacant: 有空位的 (課程, 系所名額說明)，顯示為綠色。
        taken: 這一輪剛被別人選走的課程，顯示為紅色。
        watched: 這一輪監控的課程總數。
        semester: 這次查詢的學期。
        notes: 額外要附上的說明，例如自動加選結果。

    Returns:
        要送出的完整訊息。
    """
    now = f"{datetime.datetime.now():%H:%M:%S}"
    if vacant:
        lines = [f"+ 有空位 {len(vacant)} 門（{now} 更新）", ""]
        for course, dept_info in vacant:
            line = f"+ {course_line(course)}  剩 {course.vacancy}"
            if dept_info:
                line += f"  系所 {dept_info}"
            lines.append(line)
    else:
        lines = [f"- 目前沒有空位（{now} 更新）"]

    if taken:
        lines.append("")
        for course in taken:
            lines.append(f"- {course_line(course)}  名額被選走")

    lines += ["", f"  監看 {watched} 門課程・學期 {semester}"]

    header = "**有空位**" if vacant else "**目前沒有空位**"
    parts = [header, code_block(lines)]
    parts.extend(notes)
    return "\n".join(parts)


def startup_message_for(
    rules: list[course_filter.Rule],
    result: course_filter.SearchResult | None,
    semester: str,
    selector: course_selector.CourseSelector | None,
) -> str:
    """組出啟動訊息。

    Args:
        rules: 篩選規則。
        result: 啟動時的第一次查詢結果，None 代表沒有設定規則。
        semester: 這次查詢的學期。
        selector: 已登入的選課系統客戶端，None 代表不啟用自動加選。

    Returns:
        要送出的完整訊息。
    """
    if result is None or not result.matches:
        return "規則沒有命中任何課程，請檢查 .env 的 LOOK_UP_CLASSES。"

    lines = [f"  監看 {len(result.matches)} 門課程（學期 {semester}）"]
    for index, (rule, count) in enumerate(zip(rules, result.counts), start=1):
        lines.append(f"  規則 {index}：{rule.describe()} → {count} 門")
    lines.append("")
    for match in result.matches[:STARTUP_LIST_LIMIT]:
        course = match.course
        # 目前就有空位的標成綠色，額滿的標成紅色。
        prefix = "+" if course.member_limit > 0 and course.vacancy > 0 else "-"
        lines.append(f"{prefix} {course_line(course)}")
    if len(result.matches) > STARTUP_LIST_LIMIT:
        lines.append(f"  …等共 {len(result.matches)} 門")

    parts = [code_block(lines)]
    if selector:
        parts.append("自動加選已啟用（電選課加選期間偵測到空位將自動送出）")
    parts.append(
        "有空位的課程有變動時才更新，這則訊息會被直接取代，聊天室永遠只有一則。"
    )
    return "\n".join(parts)


async def enroll(
    selector: course_selector.CourseSelector, course_no: str
) -> str:
    """送出加選並驗證結果。

    Args:
        selector: 已登入的選課系統客戶端。
        course_no: 要加選的課程代碼。

    Returns:
        一行給訊息用的結果說明。
    """
    success, body = await course_selector.run_sync(
        selector.select_course, course_no)
    if not success:
        course_selector.play_sound("failure")
        return f"{course_no} 自動加選失敗：{body[:100]}"

    await asyncio.sleep(3)  # 等系統寫入後再確認課程是否真的在清單裡。
    verified = await course_selector.run_sync(
        selector.verify_enrolled, course_no)
    if verified:
        course_selector.play_sound("success")
        return f"{course_no} 加選成功，已確認出現在選課清單中"

    course_selector.play_sound("failure")
    return (
        f"{course_no} 已送出加選但未在清單中確認（名額可能已被搶走）："
        f"{body[:100]}"
    )


async def collect_vacant(
    client: course_lookup.CourseClient,
    result: course_filter.SearchResult,
    semester: str,
) -> list[tuple[course_lookup.Course, str]]:
    """挑出這一輪真的可以選的課程。

    Args:
        client: 課程查詢客戶端。
        result: 這一輪的查詢結果。
        semester: 這次查詢的學期。

    Returns:
        (課程, 系所名額說明) 的列表；系所已額滿的假空位不會列入。
    """
    # 系所名額一門課要一次請求，命中太多門就整輪跳過這項檢查。
    dept_allowed = len(result.matches) <= course_filter.DEPT_LOOKUP_LIMIT
    vacant: list[tuple[course_lookup.Course, str]] = []

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


async def monitor_specific_courses(
    client: course_lookup.CourseClient,
    rules: list[course_filter.Rule],
    semester: str,
    bot: discord_bot.DiscordBot | None,
    selector: course_selector.CourseSelector | None,
) -> None:
    """監控規則命中的課程是否有空位。

    規則裡的課號是前綴，所以一條規則可能命中很多門課，全部都要監控。有空位
    的課程集合有變動時才刪舊訊息、重送一則新的狀態。

    Args:
        client: 課程查詢客戶端。
        rules: 篩選規則。
        semester: 規則沒指定學期時採用的學期。
        bot: Discord Bot，None 代表只輸出到終端機。
        selector: 已登入的選課系統客戶端，None 代表不自動加選。
    """
    previous_vacant: set[str] | None = None

    while True:
        try:
            started = time.monotonic()
            result = await course_filter.search(client, rules, semester)
            if result.failed:
                logger.warning("查詢失敗：%s，本輪略過", "、".join(result.failed))
                await asyncio.sleep(MIN_INTERVAL)
                continue

            vacant = await collect_vacant(client, result, semester)
            current_vacant = {course.course_no for course, _ in vacant}

            if previous_vacant is None or current_vacant != previous_vacant:
                new_ones = sorted(current_vacant - (previous_vacant or set()))
                if new_ones:
                    course_selector.play_sound("vacancy")
                    logger.info("偵測到空位: %s", "、".join(new_ones))

                # 上一輪有空位、這一輪沒有的，就是剛被別人選走。
                by_no = {
                    match.course.course_no: match.course
                    for match in result.matches
                }
                taken = [
                    by_no[course_no]
                    for course_no in sorted(
                        (previous_vacant or set()) - current_vacant)
                    if course_no in by_no
                ]
                if taken:
                    logger.info("名額被選走: %s",
                                "、".join(course.course_no for course in taken))

                notes = []
                if (selector
                        and selection_period.get_current_period() == "dept"):
                    for course_no in new_ones:
                        notes.append(await enroll(selector, course_no))

                if bot:
                    await bot.send_dm(
                        board_message(vacant, taken, len(result.matches),
                                      semester, notes),
                        key=STATUS_KEY,
                    )

            previous_vacant = current_vacant

            # 查詢比週期還久時（規則範圍很大）自動放寬，免得請求疊在一起。
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(MIN_INTERVAL, elapsed * 2) - elapsed)
        except Exception as error:  # noqa: BLE001 - 監控不該因單次錯誤中斷
            logger.error("監控特定課程時發生未知錯誤: %s", error, exc_info=True)
            await asyncio.sleep(60)


async def session_keepalive(
    selector: course_selector.CourseSelector,
) -> None:
    """每 3 分鐘訪問選課頁面以維持登入狀態。

    Args:
        selector: 已登入的選課系統客戶端。
    """
    while True:
        await asyncio.sleep(180)
        try:
            await course_selector.run_sync(selector.keepalive)
        except Exception as error:  # noqa: BLE001 - keepalive 失敗不該中斷
            logger.error("Session keepalive 錯誤: %s", error)


async def login_selector() -> course_selector.CourseSelector | None:
    """視設定登入選課系統。

    Returns:
        登入成功的客戶端；沒設定帳密或登入失敗時回傳 None。
    """
    if not (STUDENT_ID and PASSWORD):
        logger.info("未設定 STUDENT_ID/PASSWORD，不啟用自動加選")
        return None

    selector = course_selector.CourseSelector(STUDENT_ID, PASSWORD)
    if await course_selector.run_sync(selector.login):
        logger.info("選課系統自動加選功能已啟用")
        return selector

    logger.warning("選課系統登入失敗，自動加選功能停用")
    return None


def start_bot(startup_message: str) -> discord_bot.DiscordBot | None:
    """視設定啟動 Discord Bot。

    Args:
        startup_message: 登入完成後要送出的訊息。

    Returns:
        已排程啟動的 Bot；沒設定 token 或收件對象時回傳 None。
    """
    if not (DISCORD_BOT_TOKEN and DISCORD_TARGET_USER_IDS):
        logger.warning("未提供 DISCORD_BOT_TOKEN 或 DISCORD_TARGET_USER_IDS，"
                       "將不會發送 Discord 通知。")
        return None

    bot = discord_bot.DiscordBot(
        intents=discord.Intents.default(),
        target_user_ids=DISCORD_TARGET_USER_IDS,
        startup_message=startup_message,
        message_key=STATUS_KEY,
    )
    asyncio.create_task(bot.start(DISCORD_BOT_TOKEN))
    return bot


async def main() -> None:
    """組裝設定並啟動所有監控任務。"""
    try:
        rules = course_filter.parse_rules(LOOK_UP_CLASSES)
    except course_filter.RuleError as error:
        logger.error("LOOK_UP_CLASSES 規則錯誤：%s", error)
        sys.exit(1)

    client = course_lookup.CourseClient()
    explicit = (
        rules[0].semester
        if rules and all(rule.semester for rule in rules)
        else ""
    )
    semester, note = await course_filter.resolve_semester(client, explicit)
    if note:
        logger.info(note)

    selector = await login_selector()

    initial = None
    if LOOK_UP_CLASSES:
        initial = await course_filter.search(client, rules, semester)
    bot = start_bot(startup_message_for(rules, initial, semester, selector))

    tasks = []
    if LOOK_UP_CLASSES:
        tasks.append(asyncio.create_task(monitor_specific_courses(
            client, rules, semester, bot, selector)))
    if selector:
        tasks.append(asyncio.create_task(session_keepalive(selector)))

    if not tasks:
        logger.error("未設定 LOOK_UP_CLASSES，沒有任何可監控的課程。")
        return

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程式被使用者中斷，正在關閉...")

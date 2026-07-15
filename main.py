import asyncio
import logging
import os
import sys
from datetime import date

import discord
import dotenv

from course_lookup import CourseClient, QueryPayload
from course_selector import CourseSelector, run_sync, play_sound
from discord_bot import DiscordBot

# ========== Logger 設定 ========== #
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)
# 避免 httpx 的 debug log 過於頻繁地洗版
logging.getLogger("httpx").setLevel(logging.WARNING)

# ========== 環境變數 & 設定 ========== #
dotenv.load_dotenv()
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
STUDENT_ID = os.environ.get("STUDENT_ID", "")
PASSWORD = os.environ.get("PASSWORD", "")
try:
    # 確保即使環境變數為空，也不會導致程式崩潰
    target_ids_str = os.environ.get("DISCORD_TARGET_USER_IDS", "")
    DISCORD_TARGET_USER_IDS = list(map(int, target_ids_str.split(';'))) if target_ids_str else []

    lookup_classes_str = os.environ.get("LOOK_UP_CLASSES", "")
    LOOK_UP_CLASSES = [cls for cls in lookup_classes_str.split(';') if cls]
except ValueError as e:
    logger.error(f"環境變數 DISCORD_TARGET_USER_IDS 格式錯誤，請檢查 .env 檔案中的用戶ID是否為純數字: {e}")
    sys.exit(1)

# 全域變數，用於儲存所有課程的先前人數狀態
previous_enrollment_states = {}

# 選課時段定義
PERIOD_DEPT_SELECT = (date(2026, 6, 22), date(2026, 6, 24))  # 電選課加選
PERIOD_OPEN_SELECT = (date(2026, 9, 7), date(2026, 9, 21))   # 全校加退選


def get_current_period() -> str:
    """根據日期判斷目前選課時段"""
    today = date.today()
    if PERIOD_DEPT_SELECT[0] <= today <= PERIOD_DEPT_SELECT[1]:
        return "dept"
    if PERIOD_OPEN_SELECT[0] <= today <= PERIOD_OPEN_SELECT[1]:
        return "open"
    return "unknown"


def get_selection_link() -> str:
    """根據時段回傳對應的選課連結"""
    period = get_current_period()
    if period == "dept":
        return "▸ **電選課加選:** https://courseselection.ntust.edu.tw/First/A06/A06"
    if period == "open":
        return "▸ **全校加退選:** https://courseselection.ntust.edu.tw/AddAndSub/B01/B01"
    return (
        "▸ **電選課加選:** https://courseselection.ntust.edu.tw/First/A06/A06\n"
        "▸ **全校加退選:** https://courseselection.ntust.edu.tw/AddAndSub/B01/B01"
    )


async def monitor_specific_courses(
    course_client: CourseClient,
    bot: DiscordBot | None,
    selector: CourseSelector | None,
):
    """監控特定課程是否有空位（含系所人數限制檢查 + 自動加選）"""
    while True:
        try:
            courses_to_check = await course_client.get_courses()
            for course_no, course_name, teacher, cur_member, limit, node in courses_to_check:
                course_info = f"{course_no} {course_name} | {teacher}"

                if limit <= 0 or cur_member >= limit:
                    logger.debug(f"課程依然額滿: {course_info} ({cur_member}/{limit})")
                    continue

                # 電選課加選期間，檢查該課程的系所人數限制
                dept_info_str = ""
                dept_name = course_client.get_department_for_course(course_no)
                if dept_name and get_current_period() == "dept":
                    semester = course_client.payloads[0].Semester
                    dept_result = await course_client.get_department_limit(semester, course_no, dept_name)
                    if dept_result is not None:
                        dept_persons, dept_restrict = dept_result
                        if dept_persons >= dept_restrict:
                            logger.debug(
                                f"總數有空位但系所額滿: {course_info} "
                                f"(總數 {cur_member}/{limit}, "
                                f"{dept_name} {dept_persons}/{dept_restrict})"
                            )
                            continue
                        dept_info_str = f"\n系所名額：{dept_persons} / {dept_restrict} ({dept_name})"

                # 有空位，播放音效提醒
                play_sound("vacancy")

                # 自動加選（電選課加選期間）
                enroll_result_str = ""
                if selector and get_current_period() == "dept":
                    success, body = await run_sync(selector.select_course, course_no)
                    if success:
                        # 驗證是否真的加選成功
                        await asyncio.sleep(3)
                        verified = await run_sync(selector.verify_enrolled, course_no)
                        if verified:
                            enroll_result_str = "\n\n**✅ 加選成功！已確認課程出現在選課清單中**"
                            play_sound("success")
                        else:
                            enroll_result_str = f"\n\n**⚠️ 已送出加選但未在清單中確認（可能名額已被搶走）**\n回應: `{body[:100]}`"
                            play_sound("failure")
                    else:
                        enroll_result_str = f"\n\n**❌ 自動加選失敗**\n回應: `{body[:100]}`"
                        play_sound("failure")

                link = get_selection_link()
                msg = (
                    f"**🎯 您鎖定的課程有空位囉！**\n\n"
                    f"```{course_info} ({','.join(node)})\n"
                    f"目前人數：{cur_member} / {limit}{dept_info_str}```"
                    f"{enroll_result_str}\n\n"
                    f"**🔗 相關連結**\n{link}"
                )
                logger.info(f"偵測到空位: {course_info} ({cur_member}/{limit}){dept_info_str}")
                if bot:
                    await bot.send_dm(msg)

            await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"監控特定課程時發生未知錯誤: {e}", exc_info=True)
            await asyncio.sleep(60)  # 發生錯誤時，等待較長時間再重試


async def session_keepalive(selector: CourseSelector):
    """每 3 分鐘訪問選課頁面以維持 session"""
    while True:
        await asyncio.sleep(180)
        try:
            await run_sync(selector.keepalive)
        except Exception as e:
            logger.error(f"Session keepalive 錯誤: {e}")


async def monitor_all_courses(course_client: CourseClient, bot: DiscordBot | None):
    """每 15 秒監控所有課程的人數變化"""
    global previous_enrollment_states
    is_first_run = True

    while True:
        try:
            all_courses = await course_client.get_all_courses()
            if not all_courses:
                logger.warning("無法取得所有課程列表，將於 60 秒後重試。")
                await asyncio.sleep(60)
                continue

            current_enrollment = {course[0]: course[3] for course in all_courses}

            # 如果是第一次執行，僅初始化狀態，不發送通知
            if is_first_run:
                previous_enrollment_states = current_enrollment
                is_first_run = False
                logger.info("已成功初始化所有課程人數狀態，開始監控變化。")
                await asyncio.sleep(15)
                continue

            # 遍歷所有課程，檢查人數變化
            for course_no, course_name, teacher, cur_member, limit in all_courses:
                previous_member = previous_enrollment_states.get(course_no)

                # 只有在先前狀態存在且人數有變動時才通知
                if (previous_member is not None and cur_member != previous_member) and "英文" in course_name:
                    change_emoji = "📈" if cur_member > previous_member else "📉"
                    course_info = f"{course_no} {course_name} | {teacher}"
                    msg = (
                        f"**{change_emoji} 課程人數變動通知**\n\n"
                        f"```{course_info}```\n"
                        f"**人數變化:** `{previous_member}` → `{cur_member}` / `{limit}`"
                    )
                    logger.info(f"人數變動: {course_info}, {previous_member} -> {cur_member}/{limit}")

                    if bot:
                        await bot.send_dm(msg)

            # 更新狀態以供下次比較
            previous_enrollment_states = current_enrollment

            logger.debug("完成一輪全課程掃描，等待 15 秒...")
            await asyncio.sleep(15)

        except Exception as e:
            logger.error(f"監控所有課程時發生未知錯誤: {e}", exc_info=True)
            # 發生錯誤時，等待較長時間再重試，避免短時間內大量錯誤請求
            await asyncio.sleep(60)


async def main():
    if not LOOK_UP_CLASSES and not "1141":  # 如果沒有指定課程，也沒有預設學期，則無法運行
        logger.error("錯誤：未設定 LOOK_UP_CLASSES，且無預設學期。請在 .env 中設定至少一門課。")
        sys.exit(1)

    # 建立 Payloads，格式: 學期&課程代碼&系所身份(可選)
    try:
        payloads = []
        course_departments: dict[str, str] = {}  # course_no -> department
        for class_ in LOOK_UP_CLASSES:
            parts = class_.split('&')
            semester, course_no = parts[0], parts[1]
            dept = parts[2] if len(parts) > 2 and parts[2] else ""
            payloads.append(QueryPayload(Semester=semester, CourseNo=course_no))
            if dept:
                course_departments[course_no] = dept

        if not payloads:
            payloads.append(QueryPayload(Semester="1141"))
            logger.info("未設定 LOOK_UP_CLASSES，將僅監控所有課程變動。")

    except IndexError:
        logger.error("LOOK_UP_CLASSES 格式錯誤，應為 '學期&課程代碼&系所(可選)'，例如 '1151&PE139A053&資訊工程系三'。")
        sys.exit(1)

    course_client = CourseClient(payloads=payloads, course_departments=course_departments)

    # ========== 選課系統登入 ========== #
    selector = None
    if STUDENT_ID and PASSWORD:
        selector = CourseSelector(STUDENT_ID, PASSWORD)
        logged_in = await run_sync(selector.login)
        if logged_in:
            logger.info("選課系統自動加選功能已啟用")
        else:
            logger.warning("選課系統登入失敗，自動加選功能停用")
            selector = None
    else:
        logger.info("未設定 STUDENT_ID/PASSWORD，不啟用自動加選")

    # 執行第一次查詢以獲取課程名稱，並建立啟動訊息
    initial_courses = []
    if LOOK_UP_CLASSES:
        initial_courses = await course_client.get_courses()

    startup_message_parts = ["==============="]
    if initial_courses:
        startup_message_parts.append("**🎯 正在監聽以下特定課程空位：**")
        for course_no, name, teacher, _, _, node in initial_courses:
            startup_message_parts.append(f"- `{course_no}` {name} | {teacher} ({','.join(node)})")
    if selector:
        startup_message_parts.append("\n**⚡ 自動加選已啟用（電選課加選期間偵測到空位將自動送出）**")

    startup_message_parts.append("\n**📊 同時，已啟動所有課程人數變動監控 (每15秒)。**")
    startup_message = "\n".join(startup_message_parts)

    # ========== Discord Bot 啟動 ========== #
    bot = None
    if DISCORD_BOT_TOKEN and DISCORD_TARGET_USER_IDS:
        intents = discord.Intents.default()
        bot = DiscordBot(
            intents=intents,
            target_user_ids=DISCORD_TARGET_USER_IDS,
            startup_message=startup_message
        )
        asyncio.create_task(bot.start(DISCORD_BOT_TOKEN))
    else:
        logger.warning("未提供 DISCORD_BOT_TOKEN 或 DISCORD_TARGET_USER_IDS，將不會發送 Discord 通知。")

    # ========== 啟動監控任務 ========== #
    tasks = []
    if LOOK_UP_CLASSES:
        tasks.append(asyncio.create_task(monitor_specific_courses(course_client, bot, selector)))

    # tasks.append(asyncio.create_task(monitor_all_courses(course_client, bot)))

    if selector:
        tasks.append(asyncio.create_task(session_keepalive(selector)))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程式被使用者中斷，正在關閉...")

"""自動加選：登入選課系統，並在課程出現空位時送出加選。

三支指令共用這一支，規則只有一套：有帳密才登入、只在選課時段送出、同一門
課只送一次（重送會被選課系統當成取消），而且一輪一次送太多門就整批不送。
"""

import asyncio
import contextlib
import dataclasses
import logging

from ntust_class_notifier.clients import enrollment
from ntust_class_notifier.clients import sound
from ntust_class_notifier.core import periods

logger = logging.getLogger(__name__)

# 送出加選後等這麼久再去看清單，選課系統寫入需要一點時間。
VERIFY_DELAY = 3.0

# 多久碰一次選課頁面維持 session。選課系統閒置一段時間就會登出，等空位常
# 常一等就是好幾個小時，不主動維持的話會在最需要的時候發現自己已被登出。
KEEPALIVE_SECONDS = 180.0

# 一輪最多自動加選幾門。規則寫得太廣（例如「課號:CS」）時，一次湧出幾十門
# 空位會變成連續送出幾十次加選，學校明文禁止這種行為。
MAX_PER_ROUND = 5


async def login_selector(
    student_id: str, password: str
) -> enrollment.CourseSelector | None:
    """視帳密登入選課系統。

    Args:
        student_id: 選課系統學號，空字串代表不啟用自動加選。
        password: 選課系統密碼。

    Returns:
        登入成功的客戶端；沒設定帳密或登入失敗時回傳 None。
    """
    if not (student_id and password):
        return None

    selector = enrollment.CourseSelector(student_id, password)
    if await enrollment.run_sync(selector.login):
        return selector

    selector.close()
    return None


async def enroll(
    selector: enrollment.CourseSelector, course_no: str, period: str
) -> str:
    """送出加選並驗證結果。

    Args:
        selector: 已登入的選課系統客戶端。
        course_no: 要加選的課程代碼。
        period: 目前時段，決定要走哪一組選課端點。

    Returns:
        一行給訊息用的結果說明。
    """
    stage = periods.get_period_name(period)
    success, body = await enrollment.run_sync(
        selector.select_course, course_no, period)
    if body == enrollment.ALREADY_ENROLLED:
        return f"{course_no} 已在選課清單中，略過加選"

    # 加退選的 ExtraJoin 回應是空的，沒有內容不代表失敗。
    detail = body.strip()[:100] or "系統沒有回傳訊息"
    if not success:
        sound.play_sound("failure")
        return f"{course_no} 自動加選失敗（{stage}）：{detail}"

    await asyncio.sleep(VERIFY_DELAY)  # 等系統寫入後再確認。
    verified = await enrollment.run_sync(
        selector.verify_enrolled, course_no, period)
    if verified:
        sound.play_sound("success")
        return f"{course_no} 加選成功（{stage}），已確認出現在選課清單中"

    sound.play_sound("failure")
    return (
        f"{course_no} 已送出加選但未在清單中確認（名額可能已被搶走）："
        f"{detail}"
    )


@dataclasses.dataclass
class AutoEnroller:
    """一輪一輪地把「剛出現空位」的課送出加選。

    Attributes:
        selector: 已登入的選課系統客戶端。
        previous: 上一輪有空位的課程代碼。
    """

    selector: enrollment.CourseSelector
    previous: set[str] = dataclasses.field(default_factory=set)

    async def on_round(self, vacant: set[str]) -> list[str]:
        """處理這一輪的空位。

        只對「上一輪還沒有空位」的課送出，所以空位持續存在不會一直重送。

        Args:
            vacant: 這一輪有空位的課程代碼。

        Returns:
            每一門的結果說明；沒有新空位或不在選課時段時為空列表。
        """
        # 時段沒到就連狀態都不記錄。先記下來的話，等到加退選開始的那一刻，
        # 這些空位全都會被當成「上一輪就有」而永遠不會送出——監控整晚等開
        # 放的情境正是這一支最該做對的事。
        period = periods.get_current_period()
        if period not in periods.SELECTION_PERIODS:
            logger.debug("目前是%s，不送出加選也不記錄狀態",
                         periods.get_period_name(period))
            return []

        new_ones = sorted(vacant - self.previous)
        self.previous = vacant
        if not new_ones:
            return []

        if len(new_ones) > MAX_PER_ROUND:
            return [
                f"這一輪有 {len(new_ones)} 門課同時出現空位，"
                f"超過 {MAX_PER_ROUND} 門就不自動加選（請把規則縮小到你真正"
                f"要搶的課）：{'、'.join(new_ones[:5])}…"
            ]

        return [
            await enroll(self.selector, course_no, period)
            for course_no in new_ones
        ]


async def keep_session_alive(
    selector: enrollment.CourseSelector,
) -> None:
    """定時碰一次選課頁面，讓登入狀態不會在等待空位時悄悄過期。

    keepalive 本身就會在偵測到失效時重新登入，所以這裡只負責固定呼叫與
    記錄失敗，不中斷監控。

    Args:
        selector: 已登入的選課系統客戶端。
    """
    while True:
        await asyncio.sleep(KEEPALIVE_SECONDS)
        try:
            if not await enrollment.run_sync(selector.keepalive):
                logger.warning("Session 維持失敗，下一輪會再試一次")
        except Exception as error:  # noqa: BLE001 - keepalive 失敗不該中斷
            logger.error("Session keepalive 錯誤: %s", error)


@contextlib.asynccontextmanager
async def session_kept_alive(enroller: "AutoEnroller | None"):
    """在區塊執行期間於背景維持登入狀態。

    Args:
        enroller: 自動加選器，None 代表沒有要登入，什麼都不做。

    Yields:
        None，離開區塊時會停掉背景工作。
    """
    if enroller is None:
        yield
        return

    task = asyncio.create_task(keep_session_alive(enroller.selector))
    try:
        yield
    finally:
        task.cancel()

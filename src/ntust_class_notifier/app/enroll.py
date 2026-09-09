"""自動加選：登入選課系統，並在課程出現空位時送出加選。

三支指令共用這一支，規則只有一套：明確打開 AUTO_ENROLL 且有帳密才登入、
只在選課時段送出、搶到的課不會再送（重送會被選課系統當成取消），而且一輪
一次送太多門就整批不送。

送出失敗的課不會就此放棄：只要那門課的空位還在，下一輪會再試，最多試
MAX_TRIES_PER_COURSE 次。每一次送出前都先重新讀一次選課清單（電選課含志願
序），確認不在清單裡才送——重送已經送出去的課會被選課系統當成取消；讀不到
清單時一律不送，而且那不算用掉一次重試。選課系統收攤後狀態整個丟掉，下次
開放是全新的機會。
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

# 同一門課在同一次「空位出現」期間最多送幾次。網路斷一下就永遠不再試是
# 錯的，但無限重試就等於連續送出，所以要有上限。
MAX_TRIES_PER_COURSE = 3

# 內建時程整份過期時要對使用者說的話。時程是人工抄進 core/periods.py 的，
# 過期就代表沒人更新過；這時候不知道下一次選課是什麼時候，寧可只監控也不要
# 亂送。三支指令共用同一句，改一次就好。
SCHEDULE_EXPIRED_NOTE = (
    "程式內建的選課時程已全部結束，無法確認現在是不是選課期間，"
    "只監控不加選（請更新 core/periods.py 的日期）。"
)

# 一門課這一輪的處置結果。
DONE = "done"          # 選上了，或本來就在清單裡：不必再送。
RETRY = "retry"        # 這次沒成功，空位還在就再試一次。
GAVE_UP = "gave_up"    # 試到上限（或整批被擋下）：這次空位期間不再送。


@dataclasses.dataclass
class Attempt:
    """一門課在這一次「空位出現」期間的加選狀態。

    Attributes:
        state: DONE、RETRY 或 GAVE_UP。
        tries: 已經真的送出過幾次 ExtraJoin；讀不到清單而沒送的那幾輪不算。
        skipped: 上一輪是不是因為讀不到清單而沒送；用來讓那則提醒不要每輪
            重複。
    """

    state: str = RETRY
    tries: int = 0
    skipped: bool = False


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
) -> tuple[str, str]:
    """送出加選並驗證結果。

    Args:
        selector: 已登入的選課系統客戶端。
        course_no: 要加選的課程代碼。
        period: 目前時段，決定要走哪一組選課端點。

    Returns:
        (處置結果, 一行給訊息用的說明)；處置結果是 DONE 或 RETRY。只有
        「確認出現在選課清單中」與「本來就在清單裡」算 DONE，其餘（送不
        出去、送出了但沒進清單）都是 RETRY，交給呼叫端決定要不要再試。
    """
    stage = periods.get_period_name(period)
    success, body = await enrollment.run_sync(
        selector.select_course, course_no, period)
    if body == enrollment.ALREADY_ENROLLED:
        return DONE, f"{course_no} 已在選課清單中，略過加選"

    # 加退選的 ExtraJoin 回應是空的，沒有內容不代表失敗。
    detail = body.strip()[:100] or "系統沒有回傳訊息"
    if not success:
        sound.play_sound("failure")
        return RETRY, f"{course_no} 自動加選失敗（{stage}）：{detail}"

    await asyncio.sleep(VERIFY_DELAY)  # 等系統寫入後再確認。
    verified = await enrollment.run_sync(
        selector.verify_enrolled, course_no, period)
    if verified:
        sound.play_sound("success")
        return DONE, f"{course_no} 加選成功（{stage}），已確認出現在選課清單中"

    sound.play_sound("failure")
    return RETRY, (
        f"{course_no} 已送出加選但未在清單中確認（名額可能已被搶走）："
        f"{detail}"
    )


@dataclasses.dataclass
class AutoEnroller:
    """一輪一輪地把「有空位而且還沒搞定」的課送出加選。

    Attributes:
        selector: 已登入的選課系統客戶端。
        attempts: 每門課這一次空位期間的加選狀態；課程的空位消失就會清
            掉，名額再次釋出時等於一次全新的機會。
        flooded: 上一輪是不是因為同時湧出太多空位而整批不送；用來讓那則
            提醒一次空位潮只講一次。
    """

    selector: enrollment.CourseSelector
    attempts: dict[str, Attempt] = dataclasses.field(default_factory=dict)
    flooded: bool = False

    async def on_round(self, vacant: set[str]) -> list[str]:
        """處理這一輪的空位。

        Args:
            vacant: 這一輪有空位的課程代碼。

        Returns:
            每一門的結果說明；沒有要送出或不在選課時段時為空列表。
        """
        # 時段沒到就連狀態都不記錄。先記下來的話，等到加退選開始的那一刻，
        # 這些空位全都會被當成「上一輪就處理過」而永遠不會送出——監控整晚
        # 等開放的情境正是這一支最該做對的事。
        period = periods.get_current_period()
        if period not in periods.SELECTION_PERIODS:
            # 收攤（或還沒開放）就把狀態整個丟掉：下次開放是全新的機會，
            # 16:50 才試到放棄的課，隔天九點必須從頭再試一次。
            self.attempts.clear()
            self.flooded = False
            logger.debug("目前是%s，不送出加選也不記錄狀態",
                         periods.describe())
            return []

        self._forget_refilled(vacant)
        pending = sorted(
            course_no for course_no in vacant
            if self.attempts.get(course_no, Attempt()).state == RETRY
        )

        if len(pending) > MAX_PER_ROUND:
            # 這一輪整批不送，但不留下任何狀態：空位潮退到 5 門以內時就該
            # 自動恢復加選，把它們標成放棄的話會一直不送到補滿為止。提醒
            # 只在空位潮開始時講一次，免得每一輪都洗一遍。
            was_flooded, self.flooded = self.flooded, True
            if was_flooded:
                return []
            return [
                f"這一輪有 {len(pending)} 門課同時出現空位，"
                f"超過 {MAX_PER_ROUND} 門就不自動加選（請把規則縮小到你真正"
                f"要搶的課）：{'、'.join(pending[:5])}…"
            ]

        self.flooded = False
        if not pending:
            return []

        notes = [await self._try_once(course_no, period)
                 for course_no in pending]
        return [note for note in notes if note]

    async def _try_once(self, course_no: str, period: str) -> str:
        """送出一門課的加選並更新它的狀態。

        Args:
            course_no: 課程代碼。
            period: 目前時段。

        Returns:
            一行結果說明；試到上限時會附上不再重試的提醒。
        """
        attempt = self.attempts.setdefault(course_no, Attempt())

        # 電選課階段 select_course() 擋重送用的清單不含志願序，已經排進志願
        # 序的課再送一次 ExtraJoin 會把它取消，所以這裡自己先讀一次含志願序
        # 的清單。而且每一次送出前都要讀，不能只在重試時讀：課程的空位消失
        # 一輪就會被當成全新的機會，tries 歸零，那道關卡就跳過去了。
        # 加退選階段兩份清單本來就一樣，select_course() 自己擋得住，不必為
        # 它多打一次同一頁。
        if not enrollment.guard_covers_submitted(period):
            submitted = await self._already_submitted(course_no, period)
            if submitted:
                attempt.state = DONE
                attempt.skipped = False
                return f"{course_no} 已在選課清單中，不送出加選"
            if submitted is None:
                # 讀不到清單就不知道它在不在志願序裡，寧可這一輪不送。這不
                # 算一次送出：session 抖個幾輪就把重試額度用光的話，等於又
                # 回到「失敗一次就永遠不再試」。
                first_time, attempt.skipped = not attempt.skipped, True
                return (f"{course_no} 讀不到選課清單，暫時不送出加選"
                        if first_time else "")
            attempt.skipped = False

        attempt.tries += 1
        state, note = await enroll(self.selector, course_no, period)
        if state == DONE:
            attempt.state = DONE
            return note
        return self._settle(attempt, note)

    def _settle(self, attempt: Attempt, note: str) -> str:
        """記下一次沒成功的嘗試，試到上限就不再重試。

        Args:
            attempt: 這門課的加選狀態。
            note: 這次的結果說明。

        Returns:
            補上重試說明後的結果字串。
        """
        if attempt.tries >= MAX_TRIES_PER_COURSE:
            attempt.state = GAVE_UP
            return (
                f"{note}（已試 {attempt.tries} 次，這次空位期間不再重試；"
                "名額補滿後再度釋出時會重新開始）"
            )
        attempt.state = RETRY
        return f"{note}（下一輪空位還在就再試一次）"

    async def _already_submitted(
        self, course_no: str, period: str
    ) -> bool | None:
        """確認這門課是不是已經送出去了（電選課含志願序）。

        Args:
            course_no: 課程代碼。
            period: 目前時段。

        Returns:
            在清單中為 True、不在為 False；讀不到清單時回傳 None，呼叫端
            要當成「不確定」而不是「不在」。
        """
        submitted = await enrollment.run_sync(
            self.selector.submitted_courses, period)
        if submitted is None:
            return None
        return course_no.upper() in submitted

    def _forget_refilled(self, vacant: set[str]) -> None:
        """清掉已經沒有空位的課程狀態。

        名額補滿又再度釋出是一次全新的機會，之前試過幾次、放棄過與否都不
        該影響新的一輪。

        Args:
            vacant: 這一輪有空位的課程代碼。
        """
        for course_no in list(self.attempts):
            if course_no not in vacant:
                del self.attempts[course_no]


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

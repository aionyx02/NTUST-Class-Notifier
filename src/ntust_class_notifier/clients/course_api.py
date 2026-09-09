"""台科大課程查詢 API 的存取層。

提供 querycourse.ntust.edu.tw 的查詢封裝：單一關鍵字、指定條件、全校查詢，
以及系所保留名額查詢。

整支程式共用同一個 httpx.AsyncClient：監控是每幾秒查一次的長時間輪詢，每
輪都重開連線等於每輪重新握手，也讓 keep-alive 完全失效。選課高峰期 API 會
回 429／5xx，所以請求一律走 _request()，照 Retry-After 或指數退避重試，並
把冷卻時間記下來——下一輪的第一個請求會先等冷卻結束，等於自動放慢輪詢，
不必每個監控迴圈各寫一套。
"""

import asyncio
import dataclasses
import logging
import time

import httpx

from ntust_class_notifier.core import models

logger = logging.getLogger(__name__)


_API_URL = "https://querycourse.ntust.edu.tw/querycourse/api/courses"


_LIMIT_API_URL = (
    "https://querycourse.ntust.edu.tw/QueryCourse/api/LimitOnTheNumber"
)


# 全校查詢的資料約 2 MB，伺服器要跑 70~80 秒，timeout 必須放得比一般查詢寬。
_BROAD_TIMEOUT = 180.0


_NORMAL_TIMEOUT = 30.0


# 同一個請求最多送幾次（含第一次）。
MAX_ATTEMPTS = 3


# 第一次退避等幾秒，之後每次加倍。
BACKOFF_SECONDS = 2.0


# 退避與冷卻的上限，免得 Retry-After 給了一個離譜的值就整個停擺。
MAX_BACKOFF_SECONDS = 120.0


# 這些狀態碼是「等一下再來」，不是請求本身寫錯。
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def _retry_after(response: httpx.Response) -> float | None:
    """讀取回應裡的 Retry-After。

    Args:
        response: 伺服器的回應。

    Returns:
        伺服器要求等待的秒數；沒有或不是秒數格式時回傳 None（HTTP-date
        寫法很少見，遇到就當作沒給，改用指數退避）。
    """
    raw = response.headers.get("Retry-After", "").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class CourseClient:
    """課程查詢用的 API 客戶端。

    Attributes:
        api_url: 課程查詢 API 位址。
        limit_api_url: 系所保留名額 API 位址。
    """

    def __init__(self):
        """初始化客戶端。"""
        self.api_url = _API_URL
        self.limit_api_url = _LIMIT_API_URL
        self._http: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cooldown_until = 0.0

    async def __aenter__(self) -> "CourseClient":
        """支援 async with。

        Returns:
            自己。
        """
        return self

    async def __aexit__(self, *exc_info) -> None:
        """離開 async with 時關掉連線。

        Args:
            *exc_info: 例外資訊，這裡不處理。
        """
        await self.aclose()

    async def aclose(self) -> None:
        """關掉共用的連線。"""
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None
        self._loop = None

    def connection(self) -> httpx.AsyncClient:
        """取得共用的 httpx 客戶端。

        連線池綁在建立它的事件迴圈上，而 CLI 可能分段呼叫 asyncio.run，所
        以換了迴圈就得重建一個。

        Returns:
            可以直接用的客戶端。
        """
        loop = asyncio.get_running_loop()
        if self._http is None or self._http.is_closed or self._loop is not loop:
            if self._http is not None and not self._http.is_closed:
                # 只會發生在呼叫端分段 asyncio.run 的時候：舊連線池綁在已經
                # 關掉的迴圈上，這裡 await 不了也關不掉，只能留給 GC 並出個
                # 聲，免得連線悄悄洩漏。
                logger.warning("事件迴圈換過了，重建連線；舊連線沒能關閉。")
            self._http = httpx.AsyncClient(
                http2=True,
                # 連線數壓在個位數：這支程式只有輪詢與少量名額查詢，開太多
                # 併發連線只會更快撞上限流。
                limits=httpx.Limits(
                    max_connections=8, max_keepalive_connections=4),
                timeout=_NORMAL_TIMEOUT,
            )
            self._loop = loop
        return self._http

    def cooldown_remaining(self) -> float:
        """還要冷卻幾秒才會送出下一個請求。

        Returns:
            剩餘秒數，沒有冷卻時為 0。
        """
        return max(0.0, self._cooldown_until - time.monotonic())

    def _start_cooldown(self, seconds: float) -> None:
        """記下被限流後要冷卻多久。

        Args:
            seconds: 要冷卻的秒數。
        """
        seconds = min(seconds, MAX_BACKOFF_SECONDS)
        self._cooldown_until = max(
            self._cooldown_until, time.monotonic() + seconds)

    async def _wait_out_cooldown(self) -> None:
        """上一次被限流時，先把冷卻等完再送出請求。"""
        remaining = self.cooldown_remaining()
        if remaining > 0:
            logger.info("上一次被限流，先等 %.0f 秒再查詢。", remaining)
            await asyncio.sleep(remaining)

    async def _request(
        self, description: str, best_effort: bool = False, **kwargs
    ) -> httpx.Response:
        """送出一次請求，遇到限流或連線問題就退避重試。

        Args:
            description: 寫進 log 的請求說明。
            best_effort: 這個查詢可有可無時填 True——只送一次、失敗就算
                了，也不會留下冷卻去拖累其他查詢。
            **kwargs: 傳給 httpx.AsyncClient.request() 的參數。

        Returns:
            狀態碼正常的回應。

        Raises:
            httpx.HTTPError: 重試完仍然失敗，由呼叫端決定如何處理。
        """
        attempts = 1 if best_effort else MAX_ATTEMPTS
        if not best_effort:
            await self._wait_out_cooldown()
        backoff = BACKOFF_SECONDS

        for attempt in range(1, attempts + 1):
            last = attempt == attempts
            try:
                response = await self.connection().request(**kwargs)
                if response.status_code not in RETRY_STATUS:
                    response.raise_for_status()
                    return response
                # Retry-After: 0 是「馬上可以再來」，不是「沒給」，所以
                # 用 is None 判斷而不是 or。
                asked = _retry_after(response)
                wait = backoff if asked is None else asked
                if last:
                    # 重試額度用完了還在限流，冷卻留給下一輪，別讓監控迴圈
                    # 立刻又打過來。
                    if not best_effort:
                        self._start_cooldown(wait)
                    response.raise_for_status()
                logger.warning(
                    "%s 收到 %d，第 %d/%d 次，等 %.0f 秒後重試。",
                    description, response.status_code, attempt,
                    attempts, wait)
            except httpx.TransportError as error:
                if last:
                    # 連線一直失敗時也要放慢，不然監控迴圈會照原週期猛打一
                    # 個連不上的伺服器。
                    if not best_effort:
                        self._start_cooldown(backoff)
                    raise
                wait = backoff
                logger.warning(
                    "%s 連線失敗（%s），第 %d/%d 次，等 %.0f 秒後重試。",
                    description, error, attempt, attempts, wait)

            self._start_cooldown(wait)
            await asyncio.sleep(min(wait, MAX_BACKOFF_SECONDS))
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        # 迴圈一定會 return 或 raise，這行只是讓型別檢查安心。
        raise RuntimeError(f"{description} 重試流程異常結束")

    async def search_courses(
        self, keyword: str, semester: str | None = None
    ) -> list[models.Course]:
        """以課程代碼搜尋課程。

        Args:
            keyword: 完整代碼（CS1003301）或前綴（CS、CS10）。API 的比對
                規則是「前綴、不分大小寫」，所以前綴會抓到所有符合的課。
            semester: 學期代碼，預設為自動判斷的最新學期。

        Returns:
            符合的課程列表。
        """
        payload = models.QueryPayload(
            Semester=semester or models.current_semester(), CourseNo=keyword
        )
        return await self._post(payload, timeout=15.0)

    async def search_all(
        self, semester: str | None = None
    ) -> list[models.Course]:
        """查詢該學期的所有課程。

        API 沒有「列出全部」的參數，用 CourseTeacher=" " 觸發全查。

        Args:
            semester: 學期代碼，預設為自動判斷的最新學期。

        Returns:
            以課程代碼去重並排序後的課程列表。
        """
        semester = semester or models.current_semester()
        payload = models.QueryPayload(Semester=semester, CourseTeacher=" ")
        courses = await self._post(payload, timeout=_BROAD_TIMEOUT)

        # 同一課程代碼可能出現多筆，保留第一筆即可。
        unique: dict[str, models.Course] = {}
        for course in courses:
            unique.setdefault(course.course_no, course)
        logger.debug(
            "學期 %s 全校共 %d 筆、%d 門不重複課程。",
            semester, len(courses), len(unique),
        )
        return sorted(unique.values(), key=lambda course: course.course_no)

    async def search_payload(
        self, payload: models.QueryPayload
    ) -> list[models.Course]:
        """以組好的 payload 直接查詢，供 app.search 的規則查詢使用。

        Args:
            payload: 已填好條件的查詢參數。

        Returns:
            符合的課程列表。
        """
        broad = not (
            payload.CourseNo
            or payload.CourseName
            or payload.CourseTeacher.strip()
        )
        timeout = _BROAD_TIMEOUT if broad else _NORMAL_TIMEOUT
        return await self._post(payload, timeout=timeout)

    async def get_department_limit(
        self, semester: str, course_no: str, dept_name: str
    ) -> tuple[int, int] | None:
        """查詢特定課程對某系所的保留名額。

        Args:
            semester: 學期代碼。
            course_no: 課程代碼。
            dept_name: DepartmentAliase 的子字串，例如 "資訊工程系三"。

        Returns:
            (目前人數, 人數上限)；該課沒有系所名額或查不到時回傳 None。
        """
        if self.cooldown_remaining() > 0:
            # 名額查詢是錦上添花，查不到就當作沒有系所限制。它一門課要打一
            # 次請求、一輪可能有幾十門，被限流時每一門都去等冷卻的話，整輪
            # 會停擺好幾分鐘——那比少擋幾個假空位糟得多。
            logger.warning(
                "限流冷卻中，跳過 %s 的系所限制查詢；這一輪可能把系所已額滿"
                "的假空位當成真的。", course_no)
            return None

        try:
            response = await self._request(
                f"查詢 {course_no} 的系所限制",
                # 失敗就放棄，不要為了它一路退避重試把整輪拖住，也不要讓
                # 它的失敗變成冷卻去擋住真正要緊的課程查詢。
                best_effort=True,
                method="GET",
                url=self.limit_api_url,
                params={
                    "semester": semester,
                    "courseNo": course_no,
                    "mylanguage": "zh",
                },
                timeout=10.0,
            )
            data = response.json()

            if data.get("Display") != "true" or not data.get("Result"):
                return None

            for entry in data["Result"]:
                if dept_name in entry.get("DepartmentAliase", ""):
                    return int(entry["Persons"]), int(entry["Restrict"])

            logger.debug("課程 %s 的系所限制中未找到 '%s'",
                         course_no, dept_name)
            return None
        except Exception as error:  # noqa: BLE001 - 查不到就當作沒有限制
            logger.error("查詢課程 %s 系所限制時發生錯誤: %s",
                         course_no, error)
            return None

    async def _post(
        self,
        payload: models.QueryPayload,
        timeout: float = _NORMAL_TIMEOUT,
    ) -> list[models.Course]:
        """送出一次查詢並轉成 Course 列表。

        Args:
            payload: 查詢參數。
            timeout: 逾時秒數。

        Returns:
            符合的課程列表。

        Raises:
            httpx.HTTPError: 重試完仍然失敗，由呼叫端決定如何處理。
        """
        payload_dict = dataclasses.asdict(payload)
        logger.debug("POST %s | payload=%s", self.api_url, payload_dict)
        response = await self._request(
            f"查詢 {payload_dict}",
            method="POST",
            url=self.api_url,
            json=payload_dict,
            timeout=timeout,
        )
        data = response.json() or []
        logger.debug("查詢 %s 找到 %d 門課程。", payload_dict, len(data))
        return [models.Course.from_api(item) for item in data]

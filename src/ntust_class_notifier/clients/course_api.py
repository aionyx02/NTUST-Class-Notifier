"""台科大課程查詢 API 的存取層。

提供 querycourse.ntust.edu.tw 的查詢封裝：單一關鍵字、指定條件、全校查詢，
以及系所保留名額查詢。
"""

import dataclasses
import logging

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
        async with httpx.AsyncClient() as client:
            return await self._post(client, payload, timeout=15.0)

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
        async with httpx.AsyncClient() as client:
            courses = await self._post(client, payload, timeout=_BROAD_TIMEOUT)

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
        async with httpx.AsyncClient() as client:
            return await self._post(client, payload, timeout=timeout)

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
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    self.limit_api_url,
                    params={
                        "semester": semester,
                        "courseNo": course_no,
                        "mylanguage": "zh",
                    },
                    timeout=10.0,
                )
                response.raise_for_status()
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
        client: httpx.AsyncClient,
        payload: models.QueryPayload,
        timeout: float = _NORMAL_TIMEOUT,
    ) -> list[models.Course]:
        """送出一次查詢並轉成 Course 列表。

        Args:
            client: 共用的 httpx 客戶端。
            payload: 查詢參數。
            timeout: 逾時秒數。

        Returns:
            符合的課程列表。

        Raises:
            httpx.HTTPError: 網路或 HTTP 狀態碼錯誤，由呼叫端決定如何處理。
        """
        payload_dict = dataclasses.asdict(payload)
        logger.debug("POST %s | payload=%s", self.api_url, payload_dict)
        response = await client.post(
            self.api_url, json=payload_dict, timeout=timeout
        )
        response.raise_for_status()
        data = response.json() or []
        logger.debug("查詢 %s 找到 %d 門課程。", payload_dict, len(data))
        return [models.Course.from_api(item) for item in data]

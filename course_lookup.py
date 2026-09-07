"""台科大課程查詢 API 的存取層。

提供 querycourse.ntust.edu.tw 的查詢封裝：單一關鍵字、多關鍵字並行、
全校查詢，以及系所保留名額查詢。
"""

import asyncio
import dataclasses
import datetime
import logging

import httpx

logger = logging.getLogger(__name__)

_API_URL = "https://querycourse.ntust.edu.tw/querycourse/api/courses"
_LIMIT_API_URL = (
    "https://querycourse.ntust.edu.tw/QueryCourse/api/LimitOnTheNumber"
)

# 全校查詢的資料約 2 MB，伺服器要跑 70~80 秒，timeout 必須放得比一般查詢寬。
_BROAD_TIMEOUT = 180.0
_NORMAL_TIMEOUT = 30.0


def current_semester(today: datetime.date | None = None) -> str:
    """依日期推算目前的學期代碼。

    界線放在 6 月而不是開學的 9 月：電選課加選在 6 月下旬，那時搶的已經是
    下一個上學期的課，用 8 月當界線會在最需要的時候推算錯學期。

    Args:
        today: 要換算的日期，預設為今天。

    Returns:
        四碼學期代碼，例如 2026-09 回傳 "1151"、2027-02 回傳 "1152"。
    """
    today = today or datetime.date.today()
    if today.month >= 6:
        return f"{today.year - 1911}1"
    return f"{today.year - 1912}2"


@dataclasses.dataclass
class QueryPayload:
    """查詢課程 API 的 payload。

    欄位名稱必須與 API 的 JSON key 完全一致（大小寫敏感），因此這裡刻意
    不使用 snake_case。OnleyNTUST 的拼字錯誤來自 API 本身。
    """

    Semester: str = "1151"
    CourseNo: str = ""
    CourseName: str = ""
    CourseTeacher: str = ""
    Dimension: str = ""
    CourseNotes: str = ""
    CampusNotes: str = ""
    ForeignLanguage: int = 0
    OnlyGeneral: int = 0
    OnleyNTUST: int = 0
    OnlyMaster: int = 0
    OnlyUnderGraduate: int = 0
    OnlyNode: int = 0
    Language: str = "zh"


@dataclasses.dataclass(frozen=True)
class Course:
    """單門課程的查詢結果。

    Attributes:
        course_no: 課程代碼，例如 "CS1003301"。
        course_name: 課程名稱。
        teacher: 授課老師。
        cur_member: 目前修課人數。
        member_limit: 人數上限。
        node: 上課節次，例如 ("W6", "W7", "W8")。
    """

    course_no: str
    course_name: str
    teacher: str
    cur_member: int
    member_limit: int
    node: tuple[str, ...] = dataclasses.field(default_factory=tuple)

    @property
    def vacancy(self) -> int:
        """剩餘名額，額滿或超收時為 0。"""
        return max(self.member_limit - self.cur_member, 0)

    @classmethod
    def from_api(cls, data: dict) -> "Course":
        """由 querycourse API 的單筆 JSON 建立 Course。

        Args:
            data: API 回傳陣列中的一筆課程資料。

        Returns:
            對應的 Course。
        """
        # Restrict2 是網頁上顯示的名額，通常比總名額 Restrict1 準確；
        # 為 0 時才退回 Restrict1。
        restrict2 = int(data.get("Restrict2") or 0)
        restrict1 = int(data.get("Restrict1") or 0)
        return cls(
            course_no=data.get("CourseNo", "N/A"),
            course_name=data.get("CourseName", "未知課程名稱"),
            teacher=data.get("CourseTeacher", "未知老師"),
            cur_member=int(data.get("ChooseStudent") or 0),
            member_limit=restrict2 or restrict1,
            node=tuple(n for n in (data.get("Node") or "").split(",") if n),
        )


class CourseClient:
    """課程查詢用的 API 客戶端。

    Attributes:
        payloads: 舊介面 get_courses() 使用的查詢參數。
        course_departments: 舊介面使用的 {課號: 系所身分}。
    """

    def __init__(
        self,
        payloads: list[QueryPayload] | None = None,
        course_departments: dict[str, str] | None = None,
    ):
        """初始化客戶端。

        Args:
            payloads: 舊介面 get_courses() 要查詢的 payload 列表，新介面
                （search_* 系列）不需要。
            course_departments: 舊介面使用的每門課程系所身分，key 為課號、
                value 為 DepartmentAliase 的子字串。
        """
        self.payloads = tuple(payloads or ())
        self.course_departments = course_departments or {}
        self.api_url = _API_URL
        self.limit_api_url = _LIMIT_API_URL
        for course_no, dept in self.course_departments.items():
            logger.info("課程 %s 設定系所身分: %s", course_no, dept)

    @property
    def default_semester(self) -> str:
        """未指定學期時採用的學期，優先取第一個 payload。"""
        if self.payloads:
            return self.payloads[0].Semester
        return current_semester()

    def get_department_for_course(self, course_no: str) -> str:
        """取得特定課程設定的系所身分。

        Args:
            course_no: 課程代碼。

        Returns:
            系所身分字串，沒設定時為空字串。
        """
        return self.course_departments.get(course_no, "")

    async def search_courses(
        self, keyword: str, semester: str | None = None
    ) -> list[Course]:
        """以課程代碼搜尋課程。

        Args:
            keyword: 完整代碼（CS1003301）或前綴（CS、CS10）。API 的比對
                規則是「前綴、不分大小寫」，所以前綴會抓到所有符合的課。
            semester: 學期代碼，預設用 default_semester。

        Returns:
            符合的課程列表。
        """
        payload = QueryPayload(
            Semester=semester or self.default_semester, CourseNo=keyword
        )
        async with httpx.AsyncClient() as client:
            return await self._post(client, payload, timeout=15.0)

    async def search_all(self, semester: str | None = None) -> list[Course]:
        """查詢該學期的所有課程。

        API 沒有「列出全部」的參數，用 CourseTeacher=" " 觸發全查。

        Args:
            semester: 學期代碼，預設用 default_semester。

        Returns:
            以課程代碼去重並排序後的課程列表。
        """
        semester = semester or self.default_semester
        payload = QueryPayload(Semester=semester, CourseTeacher=" ")
        async with httpx.AsyncClient() as client:
            courses = await self._post(client, payload, timeout=_BROAD_TIMEOUT)

        # 同一課程代碼可能出現多筆，保留第一筆即可。
        unique: dict[str, Course] = {}
        for course in courses:
            unique.setdefault(course.course_no, course)
        logger.debug(
            "學期 %s 全校共 %d 筆、%d 門不重複課程。",
            semester, len(courses), len(unique),
        )
        return sorted(unique.values(), key=lambda course: course.course_no)

    async def search_many(
        self, keywords: list[str], semester: str | None = None
    ) -> tuple[list[Course], list[str]]:
        """並行搜尋多組關鍵字並合併結果。

        Args:
            keywords: 課程代碼或前綴的列表；為空代表不限定代碼，改抓該學期
                的全部課程。
            semester: 學期代碼，預設用 default_semester。

        Returns:
            (依代碼排序且去重的課程列表, 查詢失敗的關鍵字列表)。
        """
        keywords = list(dict.fromkeys(keywords))  # 去重且保留順序。
        semester = semester or self.default_semester

        if not keywords:
            try:
                return await self.search_all(semester), []
            except Exception as error:  # noqa: BLE001 - 查詢失敗不該中斷監控
                logger.error("查詢全部課程失敗: %s - %s",
                             type(error).__name__, error)
                return [], ["全部課程"]

        async with httpx.AsyncClient() as client:
            payloads = [
                QueryPayload(Semester=semester, CourseNo=keyword)
                for keyword in keywords
            ]
            results = await asyncio.gather(
                *[self._post(client, payload, timeout=15.0)
                  for payload in payloads],
                return_exceptions=True,
            )

        merged: dict[str, Course] = {}
        failed: list[str] = []
        for keyword, result in zip(keywords, results):
            if isinstance(result, Exception):
                logger.error("搜尋 '%s' 失敗: %s - %s",
                             keyword, type(result).__name__, result)
                failed.append(keyword)
                continue
            for course in result:
                merged.setdefault(course.course_no, course)

        sorted_courses = sorted(
            merged.values(), key=lambda course: course.course_no
        )
        return sorted_courses, failed

    async def search_payload(self, payload: QueryPayload) -> list[Course]:
        """以組好的 payload 直接查詢，供 course_filter 的規則查詢使用。

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
        payload: QueryPayload,
        timeout: float = _NORMAL_TIMEOUT,
    ) -> list[Course]:
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
        return [Course.from_api(item) for item in data]

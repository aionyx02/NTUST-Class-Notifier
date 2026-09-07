import asyncio
import logging
from dataclasses import dataclass, asdict, field
from datetime import date

import httpx

# 建立 logger
logger = logging.getLogger(__name__)


def current_semester(today: date | None = None) -> str:
    """
    依日期推算目前學期代碼，例如 2026-09 -> "1151"、2027-02 -> "1152"。

    界線放在 6 月而不是開學的 9 月：電選課加選在 6 月下旬，那時搶的已經是
    下一個上學期的課，用 8 月當界線會在最需要的時候推算錯學期。
    """
    today = today or date.today()
    if today.month >= 6:
        return f"{today.year - 1911}1"
    return f"{today.year - 1912}2"


@dataclass
class QueryPayload:
    """
    用於封裝查詢課程 API 時的 payload 參數。
    欄位名稱需與 API 的 JSON key 完全一致 (大小寫敏感)。
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


@dataclass(frozen=True)
class Course:
    """單門課程的查詢結果。"""
    course_no: str
    course_name: str
    teacher: str
    cur_member: int
    member_limit: int
    node: tuple[str, ...] = field(default_factory=tuple)

    @property
    def vacancy(self) -> int:
        """剩餘名額（額滿或超收時為 0）。"""
        return max(self.member_limit - self.cur_member, 0)

    @classmethod
    def from_api(cls, data: dict) -> "Course":
        """由 querycourse API 的單筆 JSON 建立 Course。"""
        # Restrict2 是網路上顯示的名額，通常比總名額 Restrict1 準確；為 0 時才退回 Restrict1
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
    """
    用於封裝課程相關的資料請求與日誌紀錄
    """

    def __init__(self, payloads: list[QueryPayload] | None = None,
                 course_departments: dict[str, str] | None = None):
        """
        初始化時，必須提供一個 QueryPayload 實例的列表。
        course_departments: 每門課程的系所身份 {course_no: department_alias_substring}
        """
        self.payloads = tuple(payloads or ())
        self.course_departments = course_departments or {}
        self.api_url = "https://querycourse.ntust.edu.tw/querycourse/api/courses"
        self.limit_api_url = "https://querycourse.ntust.edu.tw/QueryCourse/api/LimitOnTheNumber"
        for payload in self.payloads:
            logger.debug(f"CourseClient initialized with payload={payload}")
        if self.course_departments:
            for cno, dept in self.course_departments.items():
                logger.info(f"課程 {cno} 設定系所身份: {dept}")

    def get_department_for_course(self, course_no: str) -> str:
        """取得特定課程設定的系所身份"""
        return self.course_departments.get(course_no, "")

    @property
    def default_semester(self) -> str:
        """未指定學期時採用的學期：優先取第一個 payload，否則依日期推算。"""
        return self.payloads[0].Semester if self.payloads else current_semester()

    async def search_courses(self, keyword: str, semester: str | None = None) -> list[Course]:
        """
        以課程代碼搜尋課程。keyword 可以是完整代碼 (CS1003301)，
        也可以是前綴 (CS、CS10)，此時所有符合的課程都會被抓下來。
        API 的比對規則為「前綴、不分大小寫」。
        """
        async with httpx.AsyncClient() as client:
            return await self._search_single(client, keyword, semester or self.default_semester)

    async def search_all(self, semester: str | None = None) -> list[Course]:
        """
        查詢該學期「所有」課程。
        API 沒有列全部的參數，用 CourseTeacher=" " 觸發全查。
        """
        semester = semester or self.default_semester
        payload = asdict(QueryPayload(Semester=semester, CourseTeacher=" "))
        logger.debug(f"POST all courses URL={self.api_url} | Payload={payload}")
        # 全校資料約 2 MB、伺服器要跑 70~80 秒，timeout 必須放很寬
        async with httpx.AsyncClient() as client:
            response = await client.post(self.api_url, json=payload, timeout=180.0)
            response.raise_for_status()
            data = response.json() or []
        # 同一課程代碼可能出現多筆，這裡以代碼去重（保留第一筆）
        unique: dict[str, Course] = {}
        for item in data:
            course = Course.from_api(item)
            unique.setdefault(course.course_no, course)
        logger.debug(f"學期 {semester} 全校共 {len(data)} 筆、{len(unique)} 門不重複課程。")
        return sorted(unique.values(), key=lambda c: c.course_no)

    async def search_many(
            self, keywords: list[str], semester: str | None = None
    ) -> tuple[list[Course], list[str]]:
        """
        並行搜尋多組關鍵字，合併去除重複的課程。
        keywords 為空時代表不限定代碼，直接抓該學期全部課程。
        :return (課程列表 (依代碼排序), 查詢失敗的關鍵字列表)
        """
        keywords = list(dict.fromkeys(keywords))  # 去重且保留順序
        semester = semester or self.default_semester

        if not keywords:
            try:
                return await self.search_all(semester), []
            except Exception as e:
                logger.error(f"查詢全部課程失敗: {type(e).__name__} - {e}")
                return [], ["全部課程"]

        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(
                *[self._search_single(client, kw, semester) for kw in keywords],
                return_exceptions=True,
            )

        merged: dict[str, Course] = {}
        failed: list[str] = []
        for keyword, res in zip(keywords, results):
            if isinstance(res, Exception):
                logger.error(f"搜尋 '{keyword}' 失敗: {type(res).__name__} - {res}")
                failed.append(keyword)
                continue
            for course in res:
                merged.setdefault(course.course_no, course)

        return sorted(merged.values(), key=lambda c: c.course_no), failed

    async def search_payload(self, payload: QueryPayload) -> list[Course]:
        """
        直接以組好的 QueryPayload 查詢，供 course_filter 的規則查詢使用。
        沒有任何縮小範圍的條件時資料量很大，timeout 要放寬。
        """
        broad = not (payload.CourseNo or payload.CourseName or payload.CourseTeacher.strip())
        async with httpx.AsyncClient() as client:
            return await self._search_payload(client, payload, timeout=180.0 if broad else 30.0)

    async def _search_payload(
            self, client: httpx.AsyncClient, payload: QueryPayload, timeout: float = 30.0
    ) -> list[Course]:
        """送出一次查詢並轉成 Course 列表。"""
        payload_dict = asdict(payload)
        logger.debug(f"POST search URL={self.api_url} | Payload={payload_dict}")
        response = await client.post(self.api_url, json=payload_dict, timeout=timeout)
        response.raise_for_status()
        data = response.json() or []
        logger.debug(f"查詢 {payload_dict} 找到 {len(data)} 門課程。")
        return [Course.from_api(item) for item in data]

    async def _search_single(
            self, client: httpx.AsyncClient, keyword: str, semester: str
    ) -> list[Course]:
        """對單一關鍵字發出查詢，回傳所有符合的課程。"""
        return await self._search_payload(
            client, QueryPayload(Semester=semester, CourseNo=keyword), timeout=15.0)

    async def get_courses(self) -> list[tuple[str, str, str, int, int, list[str]]]:
        """
        批次查詢多門課程，並行化發送請求。
        """
        async with httpx.AsyncClient() as client:
            tasks = [
                self._get_single_course(client, self.api_url, payload)
                for payload in self.payloads
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        valid_results = []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                # 當 _get_single_course 內部發生錯誤並拋出時，在此處記錄
                logger.error(f"查詢課程 {self.payloads[i].CourseNo} 的任務最終失敗: {res}")
            else:
                valid_results.append(res)
        return valid_results

    async def get_all_courses(self) -> list[tuple[str, str, str, int, int]]:
        """
        查詢該學期所有課程的資料。
        使用 CourseTeacher: " " 來觸發查詢所有課程的 API。
        """
        if not self.payloads:
            logger.warning("沒有可用的 payload 來決定學期，無法查詢所有課程。")
            return []

        # 使用第一個 payload 的學期資訊來查詢
        semester = self.payloads[0].Semester
        # 建立一個專門用來查詢所有課程的 payload
        all_courses_payload = QueryPayload(Semester=semester, CourseTeacher=" ")

        async with httpx.AsyncClient() as client:
            try:
                payload_dict = asdict(all_courses_payload)
                logger.debug(f"POST all courses URL={self.api_url} | Payload={payload_dict}")
                response = await client.post(self.api_url, json=payload_dict, timeout=30.0)
                response.raise_for_status()
                data = response.json()

                if not data:
                    logger.info(f"查詢所有課程 API 回應為空，可能該學期尚未開放。")
                    return []

                courses = []
                for course_data in data:
                    course_no = course_data.get("CourseNo", "N/A")
                    course_name = course_data.get("CourseName", "未知課程名稱")
                    teacher = course_data.get('CourseTeacher', '未知老師')
                    cur_member = int(course_data.get("ChooseStudent", 0))
                    # Restrict2 是網路上顯示的名額，通常比總名額 Restrict1 準確
                    member_limit = int(course_data.get("Restrict2", course_data.get("Restrict1", 0)))
                    courses.append((course_no, course_name, teacher, cur_member, member_limit))

                logger.debug(f"成功取得 {len(courses)} 門課程的資料。")
                return courses

            except httpx.RequestError as e:
                # 網路層級的錯誤 (e.g., DNS解析失敗, 連線被拒)
                logger.error(f"請求所有課程時發生網路錯誤: {e}")
                return []
            except httpx.HTTPStatusError as e:
                # HTTP 狀態碼錯誤 (e.g., 404 Not Found, 500 Server Error)
                logger.error(f"請求所有課程時伺服器回應錯誤狀態: {e}")
                return []
            except Exception as e:
                # 其他所有未預期的錯誤 (e.g., JSON 解碼失敗)
                logger.error(f"處理所有課程資料時發生未知錯誤: {e}")
                return []

    async def get_department_limit(self, semester: str, course_no: str, dept_name: str) -> tuple[int, int] | None:
        """
        查詢特定課程的系所人數限制。
        :param dept_name: DepartmentAliase 的子字串
        :return (目前人數, 人數上限), or None if not found
        """
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    self.limit_api_url,
                    params={"semester": semester, "courseNo": course_no, "mylanguage": "zh"},
                    timeout=10.0,
                )
                response.raise_for_status()
                data = response.json()

                if data.get("Display") != "true" or not data.get("Result"):
                    return None

                for entry in data["Result"]:
                    if dept_name in entry.get("DepartmentAliase", ""):
                        return int(entry["Persons"]), int(entry["Restrict"])

                logger.debug(f"課程 {course_no} 的系所限制中未找到 '{dept_name}'")
                return None

            except Exception as e:
                logger.error(f"查詢課程 {course_no} 系所限制時發生錯誤: {e}")
                return None

    @staticmethod
    async def _get_single_course(
            client: httpx.AsyncClient, url: str, payload: QueryPayload
    ) -> tuple[str, str, str, int, int, list[str]]:
        """
        單筆課程查詢函式：使用現有 client 對指定 url 發出 POST 請求
        :return (課程代碼, 課程名稱, 授課老師, 目前人數, 人數上限, 課堂時間)
        """
        payload_dict = asdict(payload)
        course_no_for_log = payload.CourseNo
        try:
            logger.debug(f"POST URL={url} | Payload={payload_dict}")
            response = await client.post(url, json=payload_dict, timeout=5.0)
            response.raise_for_status()  # 若狀態碼不是 2xx，會在此拋出 HTTPStatusError
            data = response.json()

            if not data:
                logger.warning(f"課程 {course_no_for_log} 查無資料或 API 回應為空。")
                return course_no_for_log, "查無此課程", "N/A", 0, 0, []

            first_course = data[0]
            course_name = first_course.get("CourseName", "未知課程名稱")
            teacher = first_course.get('CourseTeacher', '未知老師')
            cur_member = int(first_course.get("ChooseStudent", 0))
            member_limit = int(first_course.get("Restrict2", first_course.get("Restrict1", 0)))
            node = first_course.get("Node", "").split(",")
            logger.debug(
                f"成功取得課程 {course_no_for_log} 數據：{course_name}, "
                f"人數={cur_member}, 上限={member_limit}"
            )
            return course_no_for_log, course_name, teacher, cur_member, member_limit, node

        except httpx.RequestError as e:
            # 將具體的網路錯誤拋出，讓上層的 get_courses 統一處理和記錄
            logger.error(f"請求課程 {course_no_for_log} 時發生網路層錯誤: {type(e).__name__} - {e.request.url}")
            raise  # 重新拋出異常
        except Exception as e:
            # 其他所有錯誤也統一拋出
            logger.error(f"處理課程 {course_no_for_log} 時發生非預期錯誤: {type(e).__name__} - {e}")
            raise  # 重新拋出異常

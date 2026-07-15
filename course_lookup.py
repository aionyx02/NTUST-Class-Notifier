import asyncio
import logging
from dataclasses import dataclass, asdict

import httpx

# 建立 logger
logger = logging.getLogger(__name__)


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


class CourseClient:
    """
    用於封裝課程相關的資料請求與日誌紀錄
    """

    def __init__(self, payloads: list[QueryPayload], course_departments: dict[str, str] | None = None):
        """
        初始化時，必須提供一個 QueryPayload 實例的列表。
        course_departments: 每門課程的系所身份 {course_no: department_alias_substring}
        """
        self.payloads = tuple(payloads)
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

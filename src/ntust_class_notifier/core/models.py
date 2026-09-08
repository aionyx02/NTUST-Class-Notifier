"""課程資料模型與學期換算。

這一層不碰網路，只描述「一門課長什麼樣子」與「學期怎麼算」。
"""

import dataclasses
import datetime


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


def previous_semester(semester: str) -> str:
    """回傳上一個學期代碼。

    Args:
        semester: 四碼學期代碼，例如 "1151"。

    Returns:
        上一學期，例如 "1151" 回傳 "1142"、"1152" 回傳 "1151"。
    """
    year, term = semester[:-1], semester[-1]
    if term == "2":
        return f"{year}1"
    return f"{int(year) - 1}2"


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

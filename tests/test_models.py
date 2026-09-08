"""core.models：學期推算與課程資料轉換。"""

import datetime

import pytest

from ntust_class_notifier.core import models


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (datetime.date(2026, 9, 8), "1151"),   # 開學後：上學期
        (datetime.date(2026, 6, 23), "1151"),  # 電選課加選：已算下個上學期
        (datetime.date(2026, 5, 31), "1142"),  # 6 月前：仍是下學期
        (datetime.date(2027, 2, 1), "1152"),   # 隔年 2 月：下學期
    ],
)
def test_current_semester(today: datetime.date, expected: str) -> None:
    assert models.current_semester(today) == expected


def test_course_from_api_prefers_restrict2() -> None:
    course = models.Course.from_api({
        "CourseNo": "CS1003301",
        "CourseName": "計算機程式設計",
        "CourseTeacher": "姚智原",
        "ChooseStudent": 45,
        "Restrict1": "9999",
        "Restrict2": "55",
        "Node": "W6,W7,W8",
    })

    assert course.member_limit == 55  # Restrict1 是 9999 這種假上限。
    assert course.vacancy == 10
    assert course.node == ("W6", "W7", "W8")


def test_course_from_api_falls_back_to_restrict1() -> None:
    course = models.Course.from_api({"Restrict1": "30", "Restrict2": "0"})

    assert course.member_limit == 30
    assert course.course_no == "N/A"
    assert course.node == ()


def test_vacancy_never_negative() -> None:
    course = models.Course("X", "課", "師", cur_member=60, member_limit=55)

    assert course.vacancy == 0

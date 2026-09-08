"""兩輪查詢之間的比對結果。

「哪門課被選走、哪門課有人退選」只在這裡算一次，終端機與 Discord 各自把
Change render 成自己的樣子（ui.report、ui.board）。
"""

import dataclasses

from ntust_class_notifier.core import models

# Change.kind 的可能值。
ADDED = "added"        # 這一輪才出現在查詢結果裡
REMOVED = "removed"    # 這一輪從查詢結果消失
TAKEN = "taken"        # 人數增加：名額被別人選走
RELEASED = "released"  # 人數減少：有人退選
LIMIT = "limit"        # 人數沒變，只改了名額上限


@dataclasses.dataclass(frozen=True)
class Change:
    """一門課在兩輪之間的變化。

    Attributes:
        course: 這一輪的狀態；課程消失時是上一輪的狀態。
        before: 上一輪的狀態，None 代表這一輪才出現或剛消失。
        kind: 變化類型，見本模組的 ADDED / REMOVED / TAKEN / RELEASED /
            LIMIT。
    """

    course: models.Course
    before: models.Course | None
    kind: str

    @property
    def delta(self) -> int:
        """人數增減；沒有上一輪資料時為 0。"""
        if self.before is None:
            return 0
        return self.course.cur_member - self.before.cur_member

    @property
    def limit_changed(self) -> bool:
        """名額上限是否也跟著變了。"""
        return (self.before is not None
                and self.course.member_limit != self.before.member_limit)

    @property
    def touches_vacancy(self) -> bool:
        """這筆變化跟空位有沒有關係。

        變動後還有空位就算；剛好把最後一個名額填滿的那一筆也要算，否則它會
        靜靜消失。
        """
        if self.before is None:
            return self.course.vacancy > 0
        return self.course.vacancy > 0 or self.before.vacancy > 0


def diff_rounds(
    previous: dict[str, models.Course],
    current: dict[str, models.Course],
) -> list[Change]:
    """比對前後兩輪的查詢結果。

    Args:
        previous: 上一輪的 {課號: 課程}。
        current: 這一輪的 {課號: 課程}。

    Returns:
        這一輪所有的變化；沒有變化時為空列表。
    """
    changes: list[Change] = []
    for course_no, course in current.items():
        before = previous.get(course_no)
        if before is None:
            changes.append(Change(course, None, ADDED))
        elif course.cur_member != before.cur_member:
            kind = TAKEN if course.cur_member > before.cur_member else RELEASED
            changes.append(Change(course, before, kind))
        elif course.member_limit != before.member_limit:
            changes.append(Change(course, before, LIMIT))

    for course_no in sorted(previous.keys() - current.keys()):
        changes.append(Change(previous[course_no], None, REMOVED))
    return changes

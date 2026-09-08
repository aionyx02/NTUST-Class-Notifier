"""core.changes：兩輪之間的比對（終端機與 Discord 共用同一份結果）。"""

from ntust_class_notifier.core import changes
from ntust_class_notifier.core import models


def _course(course_no: str, cur: int, limit: int = 55) -> models.Course:
    return models.Course(course_no, "計算機程式設計", "姚智原", cur, limit,
                         ("W6", "W7"))


def test_seat_taken() -> None:
    (change,) = changes.diff_rounds({"A": _course("A", 44)},
                                    {"A": _course("A", 45)})

    assert change.kind == changes.TAKEN
    assert change.delta == 1
    assert change.limit_changed is False


def test_seat_released() -> None:
    (change,) = changes.diff_rounds({"A": _course("A", 45)},
                                    {"A": _course("A", 44)})

    assert change.kind == changes.RELEASED
    assert change.delta == -1


def test_limit_only_change() -> None:
    (change,) = changes.diff_rounds({"A": _course("A", 45, 55)},
                                    {"A": _course("A", 45, 60)})

    assert change.kind == changes.LIMIT
    assert change.delta == 0
    assert change.limit_changed is True


def test_added_and_removed() -> None:
    found = changes.diff_rounds({"A": _course("A", 10)},
                                {"B": _course("B", 5)})

    assert [change.kind for change in found] == [changes.ADDED,
                                                 changes.REMOVED]
    assert all(change.before is None for change in found)
    assert all(change.delta == 0 for change in found)


def test_no_change_is_empty() -> None:
    same = {"A": _course("A", 10)}

    assert changes.diff_rounds(same, dict(same)) == []


def test_touches_vacancy_keeps_the_last_seat_being_filled() -> None:
    # 44/55 -> 45/45：填滿最後一個名額的那一筆也算跟空位有關。
    filled = changes.Change(_course("A", 45, 45), _course("A", 44, 45),
                            changes.TAKEN)
    still_full = changes.Change(_course("A", 50, 45), _course("A", 49, 45),
                                changes.TAKEN)

    assert filled.touches_vacancy is True
    assert still_full.touches_vacancy is False

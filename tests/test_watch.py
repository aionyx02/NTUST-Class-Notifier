"""app.watch：終端機監看要輸出哪些變動。"""

import pytest

from ntust_class_notifier.app import search
from ntust_class_notifier.app import watch
from ntust_class_notifier.core import models
from ntust_class_notifier.ui import console


def _course(course_no: str, cur: int, limit: int = 55) -> models.Course:
    return models.Course(course_no, "計算機程式設計", "姚智原", cur, limit,
                         ("W6", "W7"))


def _printer() -> console.Printer:
    return console.Printer(interactive=False, use_color=False)


def _filters(course_no: str, only_vacant: bool) -> dict[str, search.Match]:
    return {
        course_no: search.Match(_course(course_no, 0), only_vacant=only_vacant)
    }


def test_only_vacant_hides_changes_that_stay_full(
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = {"A": _course("A", 50, 45)}
    current = {"A": _course("A", 51, 45)}

    shown = watch._report_changes(
        _printer(), previous, current, _filters("A", only_vacant=True))

    assert shown == 0
    assert capsys.readouterr().out == ""


def test_only_vacant_still_shows_the_last_seat_being_filled(
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = {"A": _course("A", 44, 45)}
    current = {"A": _course("A", 45, 45)}

    shown = watch._report_changes(
        _printer(), previous, current, _filters("A", only_vacant=True))

    assert shown == 1
    assert "額滿" in capsys.readouterr().out


def test_only_vacant_never_hides_added_or_removed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 課程進出查詢結果跟「有沒有空位」無關，一律要讓使用者看到。
    previous = {"GONE": _course("GONE", 50, 45)}
    current = {"NEW": _course("NEW", 50, 45)}
    filters = {
        "GONE": search.Match(_course("GONE", 0), only_vacant=True),
        "NEW": search.Match(_course("NEW", 0), only_vacant=True),
    }

    shown = watch._report_changes(_printer(), previous, current, filters)

    output = capsys.readouterr().out
    assert shown == 2
    assert "新增課程" in output
    assert "課程已從查詢結果消失" in output


def test_changes_are_reported_without_a_filter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = {"A": _course("A", 44)}
    current = {"A": _course("A", 45)}

    shown = watch._report_changes(
        _printer(), previous, current, _filters("A", only_vacant=False))

    assert shown == 1
    assert "44 → 45/55 (+1)" in capsys.readouterr().out

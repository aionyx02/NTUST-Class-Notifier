"""ui.board：Discord 看板的文字組裝。"""

from ntust_class_notifier.core import changes
from ntust_class_notifier.core import models
from ntust_class_notifier.core import ruleset
from ntust_class_notifier.ui import board


def _course(course_no: str, cur: int, limit: int = 55) -> models.Course:
    return models.Course(course_no, "計算機程式設計", "姚智原", cur, limit,
                         ("W6", "W7"))


def _change(before: models.Course, after: models.Course) -> changes.Change:
    (change,) = changes.diff_rounds({before.course_no: before},
                                    {after.course_no: after})
    return change


def test_taken_seat_is_a_red_diff_line() -> None:
    line = board.change_line(_change(_course("CS1", 44), _course("CS1", 45)))

    assert line.startswith("-")  # diff 區塊裡的紅色。
    assert "44 → 45/55 (+1)" in line
    assert "被選走" in line


def test_released_seat_is_a_green_diff_line() -> None:
    line = board.change_line(_change(_course("CS1", 45), _course("CS1", 44)))

    assert line.startswith("+")
    assert "有人退選" in line
    assert "剩 11" in line


def test_limit_change_is_a_plain_line() -> None:
    line = board.change_line(
        _change(_course("CS1", 45, 55), _course("CS1", 45, 60)))

    assert line.startswith("  ")
    assert "名額由 55 調整為 60" in line


def test_added_and_removed_lines() -> None:
    found = changes.diff_rounds({"A": _course("A", 10)},
                                {"B": _course("B", 5)})
    lines = board.change_lines(found)

    assert any("新增課程" in line for line in lines)
    assert any("已從查詢結果消失" in line for line in lines)


def test_board_message_lists_vacancies_and_changes() -> None:
    message = board.board_message(
        vacant=[(_course("CS1", 44), "12 / 20（資訊工程系三）")],
        change_text=["- CS2  被選走"],
        watched=2,
        semester="1151",
        notes=["CS1 加選成功"],
    )

    assert message.startswith("**有空位**")
    assert "```diff" in message
    assert "系所 12 / 20（資訊工程系三）" in message
    assert "這一輪的人數變動 1 筆" in message
    assert message.endswith("CS1 加選成功")


def test_board_message_without_vacancy() -> None:
    message = board.board_message([], [], 3, "1151", [])

    assert message.startswith("**目前沒有空位**")
    assert "- 目前沒有空位" in message


def test_code_block_truncates_on_line_boundary() -> None:
    lines = [f"  第 {index} 行" * 20 for index in range(60)]

    block = board.code_block(lines)

    assert len(block) < len("\n".join(lines))
    assert "（訊息過長已截斷）" in block


def test_startup_message_marks_vacancy_per_course() -> None:
    parsed = ruleset.parse_rules("課號:CS")

    message = board.startup_message(
        parsed, [2], [_course("CS1", 44), _course("CS2", 55)], "1151",
        auto_enroll=False,
    )

    assert "+ CS1" in message   # 有空位
    assert "- CS2" in message   # 額滿
    assert "規則 1：課號 CS 開頭 → 2 門" in message
    assert "自動加選" not in message


def test_startup_message_without_matches() -> None:
    message = board.startup_message([], [], [], "1151", auto_enroll=True)

    assert "LOOK_UP_CLASSES" in message

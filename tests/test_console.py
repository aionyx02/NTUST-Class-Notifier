"""ui.console：等寬對齊與上色。"""

from ntust_class_notifier.core import models
from ntust_class_notifier.ui import console


def test_display_width_counts_wide_chars_as_two() -> None:
    assert console.display_width("CS10") == 4
    assert console.display_width("計算機") == 6
    assert console.display_width("CS 計算機") == 9


def test_fit_pads_to_exact_width() -> None:
    assert console.display_width(console.fit("CS", 10)) == 10
    assert console.display_width(console.fit("計算機程式設計", 10)) == 10


def test_fit_truncates_with_ellipsis() -> None:
    result = console.fit("計算機程式設計", 6)

    # 尾巴接省略號，長度不足的部分才補空白。
    assert result.rstrip().endswith("…")
    assert console.display_width(result) == 6


def test_format_course_columns_are_stable() -> None:
    short = models.Course("CS1", "程式", "姚", 1, 2, ("W6",))
    long = models.Course(
        "CS1003301", "計算機程式設計與實習演練", "姚智原教授", 45, 55,
        ("W6", "W7", "W8"),
    )

    assert (console.display_width(console.format_course(short))
            == console.display_width(console.format_course(long)))


def test_printer_color_can_be_disabled() -> None:
    colored = console.Printer(interactive=False, use_color=True)
    plain = console.Printer(interactive=False, use_color=False)

    assert colored.color("有空位", console.GREEN).endswith(console.RESET)
    assert plain.color("有空位", console.GREEN) == "有空位"

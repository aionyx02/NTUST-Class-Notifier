"""app.alert：搶課監控的表頭。"""

import pytest

from ntust_class_notifier.app import alert as alert_app
from ntust_class_notifier.app import search
from ntust_class_notifier.core import models
from ntust_class_notifier.core import ruleset
from ntust_class_notifier.ui import console


def _watcher(enroller: object | None) -> alert_app.Watcher:
    """建一個只夠印表頭的 Watcher。

    Args:
        enroller: 自動加選器，None 代表沒啟用。

    Returns:
        可以直接呼叫 print_header() 的 Watcher。
    """
    return alert_app.Watcher(
        client=None,
        rules=[ruleset.Rule()],
        semester="1151",
        interval=3.0,
        printer=console.Printer(interactive=False, use_color=False),
        enroller=enroller,
    )


def _checked() -> list[tuple[search.Match, bool, str]]:
    """一門有空位的課，湊成 print_header() 要的形狀。

    Returns:
        (命中資料, 是否有空位, 系所名額說明) 的列表。
    """
    course = models.Course("CS1003301", "計算機程式設計", "姚智原", 44, 55)
    return [(search.Match(course), True, "")]


def test_header_says_nothing_about_enrolling_when_it_is_off(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _watcher(None).print_header(_checked(), [1])

    # 這支也拿來展示，畫面上不該出現一個沒有在運作的功能——連「不會自動
    # 加選」這種否定句都算。表頭仍會印「目前時段：電選課加選」，但那是學校
    # 的階段名稱，跟這支程式有沒有加選功能無關，所以只挑真正會洩漏的字串。
    out = capsys.readouterr().out
    assert "不會自動加選" not in out
    assert "自動送出加選" not in out


def test_header_says_so_when_enrolling_is_on(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _watcher(object()).print_header(_checked(), [1])

    assert "並自動送出加選" in capsys.readouterr().out

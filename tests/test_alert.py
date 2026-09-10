"""app.alert：搶課監控的表頭。"""

import pytest

from ntust_class_notifier.app import alert as alert_app
from ntust_class_notifier.app import search
from ntust_class_notifier.core import models
from ntust_class_notifier.core import periods
from ntust_class_notifier.core import ruleset
from ntust_class_notifier.ui import console


@pytest.fixture
def steady_period(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """固定時段說明。

    表頭會印出目前時段，而「電選課加選」這個名字本身就含「加選」兩個字，
    不固定住的話這個測試會隨今天是哪一天而時好時壞。

    Args:
        monkeypatch: pytest 的替換工具。

    Returns:
        同一個 monkeypatch。
    """
    monkeypatch.setattr(periods, "describe", lambda *args: "非選課時段")
    return monkeypatch


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
    steady_period: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _watcher(None).print_header(_checked(), [1])

    # 這支也拿來展示，畫面上不該出現一個沒有在運作的功能——連「不會自動
    # 加選」這種否定句都算。
    assert "加選" not in capsys.readouterr().out


def test_header_says_so_when_enrolling_is_on(
    steady_period: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _watcher(object()).print_header(_checked(), [1])

    assert "並自動送出加選" in capsys.readouterr().out

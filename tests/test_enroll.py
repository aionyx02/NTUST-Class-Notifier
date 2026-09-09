"""app.enroll：什麼時候會送出加選、送出後怎麼回報。"""

import asyncio

import pytest

from ntust_class_notifier.app import enroll as enroll_app
from ntust_class_notifier.clients import enrollment
from ntust_class_notifier.clients import sound
from ntust_class_notifier.core import models
from ntust_class_notifier.core import periods


class _StubSelector:
    """記錄加選呼叫的假選課客戶端。

    Attributes:
        calls: 每一次呼叫的 (方法, 課碼, 時段)。
        enrolled: verify_enrolled 要回答的結果。
        already: select_course 是否回報「已在清單中」。
    """

    def __init__(self, enrolled: bool = True, already: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.enrolled = enrolled
        self.already = already

    def select_course(self, course_no: str, period: str) -> tuple[bool, str]:
        self.calls.append(("select", course_no, period))
        if self.already:
            return True, enrollment.ALREADY_ENROLLED
        return True, ""  # 加退選的 ExtraJoin 回應就是空字串。

    def verify_enrolled(self, course_no: str, period: str) -> bool:
        self.calls.append(("verify", course_no, period))
        return self.enrolled

    @property
    def added(self) -> list[str]:
        return [no for kind, no, _ in self.calls if kind == "select"]


@pytest.fixture
def quiet(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """關掉音效與驗證等待，測試才不會真的停三秒。

    Args:
        monkeypatch: pytest 的替換工具。

    Returns:
        同一個 monkeypatch。
    """
    monkeypatch.setattr(sound, "play_sound", lambda *args, **kwargs: None)
    monkeypatch.setattr(enroll_app, "VERIFY_DELAY", 0)
    return monkeypatch


def _course(course_no: str, cur: int, limit: int = 55) -> models.Course:
    return models.Course(course_no, "計算機程式設計", "姚智原", cur, limit)


def test_enroll_passes_the_period_through(quiet: pytest.MonkeyPatch) -> None:
    selector = _StubSelector()

    note = asyncio.run(enroll_app.enroll(selector, "TCG175302", "open"))

    # 時段要一路傳到 client，加退選才不會打到電選課的端點。
    assert selector.calls == [
        ("select", "TCG175302", "open"),
        ("verify", "TCG175302", "open"),
    ]
    assert "加選成功（全校加退選）" in note


def test_enroll_reports_when_the_course_never_shows_up(
    quiet: pytest.MonkeyPatch,
) -> None:
    note = asyncio.run(
        enroll_app.enroll(_StubSelector(enrolled=False), "TCG175302", "open"))

    # 回應是空的，訊息不能因此變成空白的一行。
    assert "未在清單中確認" in note
    assert "系統沒有回傳訊息" in note


def test_enroll_skips_a_course_already_in_the_list(
    quiet: pytest.MonkeyPatch,
) -> None:
    selector = _StubSelector(already=True)

    note = asyncio.run(enroll_app.enroll(selector, "TCG175302", "open"))

    # 沒有真的送出，就不必再等三秒去驗證一次。
    assert note == "TCG175302 已在選課清單中，略過加選"
    assert selector.calls == [("select", "TCG175302", "open")]


def _rounds(
    monkeypatch: pytest.MonkeyPatch,
    vacancies: list[set[str]],
    period: str = "open",
) -> _StubSelector:
    """跑幾輪 AutoEnroller 並回傳假 client。

    Args:
        monkeypatch: 用來固定目前時段。
        vacancies: 每一輪有空位的課程代碼。
        period: 要假裝的時段。

    Returns:
        記錄了所有呼叫的假 client。
    """
    monkeypatch.setattr(
        periods, "get_current_period", lambda *args: period)
    selector = _StubSelector()
    enroller = enroll_app.AutoEnroller(selector=selector)

    async def drive() -> None:
        for vacant in vacancies:
            await enroller.on_round(vacant)

    asyncio.run(drive())
    return selector


def test_auto_enroller_only_sends_on_the_first_appearance(
    quiet: pytest.MonkeyPatch,
) -> None:
    selector = _rounds(quiet, [
        {"TCG175302"},              # 出現空位 → 送出
        {"TCG175302"},              # 空位還在 → 不重送
        {"TCG175302", "CS1003301"},  # 多了一門 → 只送新的那門
    ])

    assert selector.added == ["TCG175302", "CS1003301"]


def test_auto_enroller_sends_again_after_the_seat_was_taken(
    quiet: pytest.MonkeyPatch,
) -> None:
    selector = _rounds(quiet, [{"TCG175302"}, set(), {"TCG175302"}])

    # 名額被補滿又再度釋出，是一次新的機會，要再搶一次。
    assert selector.added == ["TCG175302", "TCG175302"]


def test_auto_enroller_still_fires_when_the_period_finally_opens(
    quiet: pytest.MonkeyPatch,
) -> None:
    periods_seen = iter(["unknown", "unknown", "open"])
    quiet.setattr(
        periods, "get_current_period", lambda *args: next(periods_seen))
    selector = _StubSelector()
    enroller = enroll_app.AutoEnroller(selector=selector)

    async def drive() -> None:
        # 整晚在等加退選開放：前兩輪還沒到時段，第三輪才開放。
        for _ in range(3):
            await enroller.on_round({"TCG175302"})

    asyncio.run(drive())

    # 時段沒到就記下狀態的話，開放的那一刻會被當成「上一輪就有空位」而
    # 永遠不送出——這正是等開放的情境最不能出的錯。
    assert selector.added == ["TCG175302"]


def test_auto_enroller_stays_quiet_outside_selection_periods(
    quiet: pytest.MonkeyPatch,
) -> None:
    selector = _rounds(quiet, [{"TCG175302"}], period="unknown")

    assert selector.calls == []


def test_auto_enroller_refuses_to_spray_requests(
    quiet: pytest.MonkeyPatch,
) -> None:
    flood = {f"CS100330{i}" for i in range(enroll_app.MAX_PER_ROUND + 1)}
    quiet.setattr(periods, "get_current_period", lambda *args: "open")
    selector = _StubSelector()
    enroller = enroll_app.AutoEnroller(selector=selector)

    notes = asyncio.run(enroller.on_round(flood))

    # 規則寫太廣時一次幾十門空位，連續送出會被學校視為機器人搶課。
    assert selector.calls == []
    assert len(notes) == 1
    assert "不自動加選" in notes[0]


def test_login_selector_needs_both_halves_of_the_credentials() -> None:
    assert asyncio.run(enroll_app.login_selector("", "")) is None
    assert asyncio.run(enroll_app.login_selector("B11215000", "")) is None
    assert asyncio.run(enroll_app.login_selector("", "pw")) is None

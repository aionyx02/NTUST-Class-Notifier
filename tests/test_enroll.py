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
        enrolled: 送出成功後課程會不會真的出現在清單裡。
        already: 是不是一開始每門課就都在清單中。
        failures: 前幾次 select_course 要當成送不出去（模擬網路問題）。
        listed: 目前在選課清單裡的課碼。
        unreadable: 是不是連選課清單都讀不到。
    """

    def __init__(self, enrolled: bool = True, already: bool = False,
                 failures: int = 0, unreadable: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.enrolled = enrolled
        self.already = already
        self.failures = failures
        self.unreadable = unreadable
        self.listed: set[str] = set()

    def submitted_courses(self, period: str) -> set[str] | None:
        self.calls.append(("list", "", period))
        if self.unreadable:
            return None
        return set(self.listed)

    def select_course(self, course_no: str, period: str) -> tuple[bool, str]:
        self.calls.append(("select", course_no, period))
        if self.failures > 0:
            self.failures -= 1
            return False, "連線失敗"
        if self.already or course_no in self.listed:
            return True, enrollment.ALREADY_ENROLLED
        if self.enrolled:
            self.listed.add(course_no)
        return True, ""  # 加退選的 ExtraJoin 回應就是空字串。

    def verify_enrolled(self, course_no: str, period: str) -> bool:
        self.calls.append(("verify", course_no, period))
        return self.already or course_no in self.listed

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

    state, note = asyncio.run(enroll_app.enroll(selector, "TCG175302", "open"))

    # 時段要一路傳到 client，加退選才不會打到電選課的端點。
    assert selector.calls == [
        ("select", "TCG175302", "open"),
        ("verify", "TCG175302", "open"),
    ]
    assert state == enroll_app.DONE
    assert "加選成功（全校加退選）" in note


def test_enroll_reports_when_the_course_never_shows_up(
    quiet: pytest.MonkeyPatch,
) -> None:
    state, note = asyncio.run(
        enroll_app.enroll(_StubSelector(enrolled=False), "TCG175302", "open"))

    # 回應是空的，訊息不能因此變成空白的一行。
    assert "未在清單中確認" in note
    assert "系統沒有回傳訊息" in note
    # 名額可能只是被別人先搶走，還沒到該放棄的時候。
    assert state == enroll_app.RETRY


def test_enroll_skips_a_course_already_in_the_list(
    quiet: pytest.MonkeyPatch,
) -> None:
    selector = _StubSelector(already=True)

    state, note = asyncio.run(enroll_app.enroll(selector, "TCG175302", "open"))

    # 沒有真的送出，就不必再等三秒去驗證一次。
    assert state == enroll_app.DONE
    assert note == "TCG175302 已在選課清單中，略過加選"
    assert selector.calls == [("select", "TCG175302", "open")]


def _rounds(
    monkeypatch: pytest.MonkeyPatch,
    vacancies: list[set[str]],
    period: str = "open",
    selector: _StubSelector | None = None,
) -> _StubSelector:
    """跑幾輪 AutoEnroller 並回傳假 client。

    Args:
        monkeypatch: 用來固定目前時段。
        vacancies: 每一輪有空位的課程代碼。
        period: 要假裝的時段。
        selector: 指定的假 client，None 代表用一個全新的。

    Returns:
        記錄了所有呼叫的假 client。
    """
    monkeypatch.setattr(
        periods, "get_current_period", lambda *args: period)
    selector = selector or _StubSelector()
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
    # enrolled=False：送出去了但沒搶到，所以不會進選課清單。
    selector = _rounds(quiet, [{"TCG175302"}, set(), {"TCG175302"}],
                       selector=_StubSelector(enrolled=False))

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


def test_auto_enroller_retries_a_course_that_failed_to_send(
    quiet: pytest.MonkeyPatch,
) -> None:
    selector = _rounds(
        quiet,
        [{"TCG175302"}, {"TCG175302"}],
        selector=_StubSelector(failures=1),
    )

    # 第一輪因為網路問題沒送出去；空位還在就該再試，舊版只送這一次就
    # 永遠不再碰它了。
    assert selector.added == ["TCG175302", "TCG175302"]


def test_auto_enroller_stops_after_too_many_failures(
    quiet: pytest.MonkeyPatch,
) -> None:
    tries = enroll_app.MAX_TRIES_PER_COURSE
    selector = _rounds(
        quiet,
        [{"TCG175302"}] * (tries + 2),
        selector=_StubSelector(failures=tries + 2),
    )

    # 一直失敗就不能一直送，否則等於連續送出。
    assert selector.added == ["TCG175302"] * tries


def test_auto_enroller_forgets_failures_after_the_seat_is_refilled(
    quiet: pytest.MonkeyPatch,
) -> None:
    tries = enroll_app.MAX_TRIES_PER_COURSE
    selector = _rounds(
        quiet,
        [{"TCG175302"}] * tries + [set(), {"TCG175302"}],
        selector=_StubSelector(failures=tries),
    )

    # 補滿後再度釋出是一次全新的機會，之前試到放棄不該留到下一次。
    assert selector.added == ["TCG175302"] * (tries + 1)


def test_auto_enroller_never_resends_a_course_it_already_got(
    quiet: pytest.MonkeyPatch,
) -> None:
    selector = _rounds(quiet, [{"TCG175302"}] * 4)

    # 重送 ExtraJoin 會把已經選上的課從清單裡拿掉，只能送一次。
    assert selector.added == ["TCG175302"]


def test_a_course_already_in_the_wish_list_is_never_sent(
    quiet: pytest.MonkeyPatch,
) -> None:
    selector = _StubSelector()
    selector.listed.add("TCG175302")  # 早就排進志願序了。

    _rounds(quiet, [{"TCG175302"}], period="dept", selector=selector)

    # 電選課擋重送的清單不含志願序，所以這一關得由這裡自己把守：對已經送
    # 出去的課再送一次 ExtraJoin，那門課會從清單消失。
    assert selector.added == []


def test_every_dept_send_rechecks_the_list_first(
    quiet: pytest.MonkeyPatch,
) -> None:
    quiet.setattr(periods, "get_current_period", lambda *args: "dept")
    selector = _StubSelector(failures=1)
    enroller = enroll_app.AutoEnroller(selector=selector)

    async def drive() -> None:
        await enroller.on_round({"TCG175302"})
        # 第一次其實送成功了（電選課就是進志願序），只是回應沒收到。
        selector.listed.add("TCG175302")
        await enroller.on_round({"TCG175302"})

    asyncio.run(drive())

    assert selector.added == ["TCG175302"]


def test_the_open_stage_does_not_reread_the_same_page(
    quiet: pytest.MonkeyPatch,
) -> None:
    selector = _rounds(quiet, [{"TCG175302"}])

    # 加退選的擋重送清單就是完整清單，select_course() 自己擋得住；多讀一
    # 次同一頁只是白白多打學校系統一次。
    assert not [kind for kind, _, _ in selector.calls if kind == "list"]
    assert selector.added == ["TCG175302"]


def test_an_unreadable_list_stops_the_send(
    quiet: pytest.MonkeyPatch,
) -> None:
    selector = _rounds(quiet, [{"TCG175302"}], period="dept",
                       selector=_StubSelector(unreadable=True))

    # 讀不到清單就不知道它在不在志願序裡，寧可這一輪不送。
    assert selector.added == []


def test_an_unreadable_list_does_not_burn_the_retry_budget(
    quiet: pytest.MonkeyPatch,
) -> None:
    rounds = enroll_app.MAX_TRIES_PER_COURSE + 2
    selector = _StubSelector(unreadable=True)

    quiet.setattr(periods, "get_current_period", lambda *args: "dept")
    enroller = enroll_app.AutoEnroller(selector=selector)

    async def drive() -> list[str]:
        notes: list[str] = []
        for _ in range(rounds):
            notes.extend(await enroller.on_round({"TCG175302"}))
        selector.unreadable = False  # session 恢復了。
        notes.extend(await enroller.on_round({"TCG175302"}))
        return notes

    notes = asyncio.run(drive())

    # session 抖幾輪就把重試額度用光的話，等於又回到「失敗一次就永遠不再
    # 試」；而且那句提醒也不必每輪講一次。
    assert selector.added == ["TCG175302"]
    assert len(notes) == 2


def test_giving_up_is_forgotten_after_the_system_closes(
    quiet: pytest.MonkeyPatch,
) -> None:
    tries = enroll_app.MAX_TRIES_PER_COURSE
    # 試到放棄 → 17:00 收攤 → 隔天九點又開放。
    seen = iter(["open"] * tries + ["unknown", "open"])
    quiet.setattr(periods, "get_current_period", lambda *args: next(seen))
    selector = _StubSelector(failures=tries + 1)
    enroller = enroll_app.AutoEnroller(selector=selector)

    async def drive() -> None:
        for _ in range(tries + 2):
            await enroller.on_round({"TCG175302"})

    asyncio.run(drive())

    # 下一次開放是全新的機會，16:50 才放棄的課不該就這樣被放生一整天。
    assert selector.added == ["TCG175302"] * (tries + 1)


def test_a_flood_that_dies_down_starts_enrolling_again(
    quiet: pytest.MonkeyPatch,
) -> None:
    flood = {f"CS100330{i}" for i in range(enroll_app.MAX_PER_ROUND + 1)}
    calm = {"CS1003300", "CS1003301"}

    selector = _rounds(quiet, [flood, calm])

    # 整批不送的那一輪不該留下狀態，否則剩兩門有空位時也永遠不會送。
    assert sorted(selector.added) == sorted(calm)


def test_the_flood_warning_is_only_said_once(
    quiet: pytest.MonkeyPatch,
) -> None:
    flood = {f"CS100330{i}" for i in range(enroll_app.MAX_PER_ROUND + 1)}
    quiet.setattr(periods, "get_current_period", lambda *args: "open")
    enroller = enroll_app.AutoEnroller(selector=_StubSelector())

    async def drive() -> list[str]:
        notes: list[str] = []
        for _ in range(3):
            notes.extend(await enroller.on_round(flood))
        return notes

    notes = asyncio.run(drive())

    # 空位潮可能持續好幾分鐘，同一句話不必每輪講一次。
    assert len(notes) == 1


def test_login_selector_needs_both_halves_of_the_credentials() -> None:
    assert asyncio.run(enroll_app.login_selector("", "")) is None
    assert asyncio.run(enroll_app.login_selector("B11215000", "")) is None
    assert asyncio.run(enroll_app.login_selector("", "pw")) is None

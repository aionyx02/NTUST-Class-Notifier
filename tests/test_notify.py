"""app.notify：Discord 監控迴圈什麼時候會重送看板。"""

import asyncio

import pytest

from ntust_class_notifier.app import notify
from ntust_class_notifier.app import search
from ntust_class_notifier.clients import sound
from ntust_class_notifier.core import models
from ntust_class_notifier.core import ruleset


def _course(course_no: str, cur: int, limit: int = 55) -> models.Course:
    return models.Course(course_no, "計算機程式設計", "姚智原", cur, limit,
                         ("W6", "W7"))


class _StubBot:
    """記錄送出內容的假 Bot。"""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_dm(self, message: str, key: str | None = None) -> None:
        self.sent.append(message)


def _result(courses: list[models.Course]) -> search.SearchResult:
    return search.SearchResult(
        matches=[search.Match(course=course, only_vacant=False, dept="")
                 for course in courses],
        counts=[len(courses)],
    )


def _run_monitor(
    monkeypatch: pytest.MonkeyPatch,
    rounds: list[list[models.Course]],
    seconds: float,
    heartbeat: float,
) -> _StubBot:
    """跑幾輪監控迴圈並回傳假 Bot。

    Args:
        monkeypatch: 用來替換查詢與音效。
        rounds: 每一輪要回傳的課程，用完就一直沿用最後一輪。
        seconds: 要讓迴圈跑多久。
        heartbeat: 心跳秒數。

    Returns:
        收集到訊息的假 Bot。
    """
    calls = {"n": 0}

    async def fake_search(client, parsed, semester):
        courses = rounds[min(calls["n"], len(rounds) - 1)]
        calls["n"] += 1
        return _result(courses)

    monkeypatch.setattr(search, "search", fake_search)
    monkeypatch.setattr(sound, "play_sound", lambda *args, **kwargs: None)
    monkeypatch.setattr(notify, "MIN_INTERVAL", 0.01)
    monkeypatch.setattr(notify, "HEARTBEAT_SECONDS", heartbeat)

    bot = _StubBot()

    async def drive() -> None:
        task = asyncio.create_task(notify.monitor_courses(
            None, [ruleset.Rule()], "1151", bot, None))
        await asyncio.sleep(seconds)
        task.cancel()

    asyncio.run(drive())
    return bot


def test_monitor_sends_a_board_for_every_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rounds = [
        [_course("CS1", 45, 45), _course("EE1", 30, 40)],  # 只有 EE1 有空位
        [_course("CS1", 45, 45), _course("EE1", 31, 40)],  # 人數變動，空位沒變
        [_course("CS1", 44, 45), _course("EE1", 31, 40)],  # CS1 出現空位
        [_course("CS1", 45, 45), _course("EE1", 31, 40)],  # CS1 空位被補滿
    ]

    bot = _run_monitor(monkeypatch, rounds, seconds=0.2, heartbeat=10 ** 6)

    # 第 2 輪的人數變動沒有改變「有沒有空位」，舊版就是這裡漏掉的。
    assert len(bot.sent) == 4
    assert "30 → 31/40" in bot.sent[1]
    assert "45 → 44/45" in bot.sent[2]


def test_monitor_resends_on_heartbeat_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steady = [[_course("CS1", 45, 45)]]

    bot = _run_monitor(monkeypatch, steady, seconds=0.55, heartbeat=0.1)

    # 沒有任何變動時只靠心跳更新，不會每輪洗版。
    assert 3 <= len(bot.sent) <= 8
    assert all("目前沒有空位" in message for message in bot.sent)


def test_collect_vacant_skips_full_and_department_full() -> None:
    class _Client:
        async def get_department_limit(self, semester, course_no, dept):
            return (12, 12) if course_no == "DEPT" else None

    result = search.SearchResult(
        matches=[
            search.Match(_course("FULL", 55, 55)),
            search.Match(_course("DEPT", 40, 55), dept="資訊工程系三"),
            search.Match(_course("OPEN", 40, 55)),
        ],
        counts=[3],
    )

    vacant = asyncio.run(notify.collect_vacant(_Client(), result, "1151"))

    assert [course.course_no for course, _ in vacant] == ["OPEN"]

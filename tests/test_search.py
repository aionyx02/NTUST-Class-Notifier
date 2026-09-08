"""app.search：規則怎麼變成實際查詢（用假客戶端，不連網路）。"""

import asyncio

import pytest

from ntust_class_notifier.app import search
from ntust_class_notifier.core import models
from ntust_class_notifier.core import ruleset


class _FakeClient:
    """記錄查詢參數的假客戶端。"""

    def __init__(self, courses: list[models.Course]):
        self.courses = courses
        self.payloads: list[models.QueryPayload] = []
        self.broad_calls = 0
        self.probes: list[str] = []

    async def search_payload(
        self, payload: models.QueryPayload
    ) -> list[models.Course]:
        self.payloads.append(payload)
        return self.courses

    async def search_all(self, semester: str) -> list[models.Course]:
        self.broad_calls += 1
        return self.courses

    async def search_courses(
        self, keyword: str, semester: str
    ) -> list[models.Course]:
        self.probes.append(semester)
        return self.courses


def _course(course_no: str) -> models.Course:
    return models.Course(course_no, "課程", "老師", 10, 20)


def test_search_expands_each_value_into_one_query() -> None:
    client = _FakeClient([_course("CS1003301")])
    parsed = ruleset.parse_rules("課號:CS,EE 學制:大學部")

    result = asyncio.run(search.search(client, parsed, "1151"))

    assert [payload.CourseNo for payload in client.payloads] == ["CS", "EE"]
    assert all(p.OnlyUnderGraduate == 1 for p in client.payloads)
    assert all(p.Semester == "1151" for p in client.payloads)
    assert result.counts == [1]  # 兩次查詢的結果會去重成一門。


def test_search_merges_rules_and_keeps_dept() -> None:
    client = _FakeClient([_course("PE139A053")])
    parsed = ruleset.parse_rules(
        "課號:PE 系所:資訊工程系三; 課號:PE 只看空位:是")

    result = asyncio.run(search.search(client, parsed, "1151"))

    (match,) = result.matches
    assert match.dept == "資訊工程系三"
    # 只有每一條規則都要求時才算「只看空位」。
    assert match.only_vacant is False


def test_empty_rule_list_queries_the_whole_school() -> None:
    client = _FakeClient([_course("CS1003301")])

    asyncio.run(search.search(client, [], "1151"))

    assert client.broad_calls == 1
    assert client.payloads == []


def test_failed_rule_is_reported_not_raised() -> None:
    class _BrokenClient(_FakeClient):
        async def search_payload(self, payload):
            raise RuntimeError("boom")

    client = _BrokenClient([])
    parsed = ruleset.parse_rules("課號:CS")

    result = asyncio.run(search.search(client, parsed, "1151"))

    assert result.failed == ["課號 CS 開頭"]
    assert result.matches == []


def test_explicit_semester_skips_the_probe() -> None:
    client = _FakeClient([_course("CS1003301")])

    semester, note = asyncio.run(search.resolve_semester(client, "1142"))

    assert (semester, note) == ("1142", "")
    assert client.probes == []


def test_semester_falls_back_when_probe_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(models, "current_semester", lambda: "1151")
    client = _FakeClient([])  # 探測查不到任何課程。

    semester, note = asyncio.run(search.resolve_semester(client))

    assert semester == "1142"
    assert "1142" in note

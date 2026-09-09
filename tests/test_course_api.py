"""clients.course_api：共用連線，以及被限流時怎麼退讓。"""

import asyncio

import httpx
import pytest

from ntust_class_notifier.clients import course_api

COURSE_JSON = {
    "CourseNo": "CS1003301",
    "CourseName": "計算機程式設計",
    "CourseTeacher": "姚智原",
    "ChooseStudent": "44",
    "Restrict2": "55",
    "Node": "W6,W7",
}


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """把退避時間縮到幾乎不用等，測試才不會真的睡好幾秒。

    Args:
        monkeypatch: pytest 的替換工具。

    Returns:
        同一個 monkeypatch。
    """
    monkeypatch.setattr(course_api, "BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(course_api, "MAX_BACKOFF_SECONDS", 0.05)
    return monkeypatch


def _client(
    monkeypatch: pytest.MonkeyPatch, replies: list[httpx.Response]
) -> tuple[course_api.CourseClient, list[httpx.Request]]:
    """建一個所有請求都走假 transport 的 CourseClient。

    Args:
        monkeypatch: 用來換掉共用連線。
        replies: 依序要回傳的回應，用完就一直沿用最後一個。

    Returns:
        (客戶端, 收到的請求列表)。
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return replies[min(len(seen) - 1, len(replies) - 1)]

    client = course_api.CourseClient()
    fake = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(client, "connection", lambda: fake)
    return client, seen


def test_a_throttled_query_is_retried_until_it_works(
    fast: pytest.MonkeyPatch,
) -> None:
    client, seen = _client(fast, [
        httpx.Response(429),
        httpx.Response(429),
        httpx.Response(200, json=[COURSE_JSON]),
    ])

    courses = asyncio.run(client.search_courses("CS1003301"))

    assert len(seen) == 3
    assert [course.course_no for course in courses] == ["CS1003301"]


def test_server_errors_are_retried_too(fast: pytest.MonkeyPatch) -> None:
    client, seen = _client(fast, [
        httpx.Response(503),
        httpx.Response(200, json=[COURSE_JSON]),
    ])

    asyncio.run(client.search_courses("CS1003301"))

    assert len(seen) == 2


def test_connection_errors_are_retried(fast: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=[COURSE_JSON])

    client = course_api.CourseClient()
    fake = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fast.setattr(client, "connection", lambda: fake)

    asyncio.run(client.search_courses("CS1003301"))

    assert attempts["n"] == 3


def test_giving_up_still_leaves_a_cooldown_for_the_next_round(
    fast: pytest.MonkeyPatch,
) -> None:
    client, seen = _client(fast, [httpx.Response(429)])

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.search_courses("CS1003301"))

    # 重試額度用完不代表可以馬上再打一次；下一輪要先把冷卻等完。
    assert len(seen) == course_api.MAX_ATTEMPTS
    assert client.cooldown_remaining() > 0


def test_retry_after_wins_over_the_exponential_backoff(
    fast: pytest.MonkeyPatch,
) -> None:
    fast.setattr(course_api, "MAX_BACKOFF_SECONDS", 60.0)
    # 只送一次就放棄，測試才不用真的等 Retry-After 說的那 30 秒。
    fast.setattr(course_api, "MAX_ATTEMPTS", 1)
    client, seen = _client(fast, [httpx.Response(
        429, headers={"Retry-After": "30"})])

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.search_courses("CS1003301"))

    # 伺服器說等 30 秒就等 30 秒，不是我們自己算的 0.01 秒。
    assert len(seen) == 1
    assert client.cooldown_remaining() > 20


def test_department_limit_fails_fast_instead_of_holding_up_the_round(
    fast: pytest.MonkeyPatch,
) -> None:
    client, seen = _client(fast, [httpx.Response(500)])

    result = asyncio.run(
        client.get_department_limit("1151", "PE139A053", "資訊工程系三"))

    # 查不到系所限制只是「當作沒有限制」。一輪可能有幾十門課要查，為它退避
    # 重試會把整輪拖上好幾分鐘，比少擋幾個假空位糟得多。
    assert result is None
    assert len(seen) == 1


def test_department_limit_is_skipped_while_a_cooldown_is_pending(
    fast: pytest.MonkeyPatch,
) -> None:
    fast.setattr(course_api, "MAX_BACKOFF_SECONDS", 60.0)
    fast.setattr(course_api, "MAX_ATTEMPTS", 1)
    client, seen = _client(fast, [
        httpx.Response(429, headers={"Retry-After": "30"}),
        httpx.Response(200, json={}),
    ])

    async def throttled_then_limit() -> tuple[int, int] | None:
        with pytest.raises(httpx.HTTPStatusError):
            await client.search_courses("CS1003301")  # 這一次被限流。
        return await client.get_department_limit(
            "1151", "PE139A053", "資訊工程系三")

    result = asyncio.run(throttled_then_limit())

    # 被限流時每一門課都去等冷卻，整輪就停擺了。
    assert result is None
    assert len(seen) == 1  # 只有那次被限流的查詢，名額查詢根本沒送出。


def test_the_connection_is_shared_between_queries() -> None:
    client = course_api.CourseClient()

    async def two_connections() -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
        async with client:
            return client.connection(), client.connection()

    first, second = asyncio.run(two_connections())

    # 每輪都開一個新的 AsyncClient 等於每輪重新握手，keep-alive 全白費。
    assert first is second
    assert first.is_closed  # 離開 async with 就要關掉。

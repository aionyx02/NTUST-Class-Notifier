"""把篩選規則變成實際的課程查詢。

一條規則可能展開成多次查詢（逗號多值），多條規則之間取聯集；這裡也負責
決定要查哪一個學期。
"""

import asyncio
import dataclasses
import logging

from ntust_class_notifier.clients import course_api
from ntust_class_notifier.core import models
from ntust_class_notifier.core import ruleset

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class Match:
    """一門命中的課程，連同適用於它的輸出規則。

    Attributes:
        course: 課程資料。
        only_vacant: 是否只輸出跟空位有關的變動。
        dept: 要檢查的系所名額身分，空字串代表不檢查。
    """

    course: models.Course
    only_vacant: bool = False
    dept: str = ""


@dataclasses.dataclass
class SearchResult:
    """一輪規則查詢的結果。

    Attributes:
        matches: 命中的課程，依課程代碼排序。
        counts: 每條規則各自命中幾門，順序與規則相同。
        failed: 查詢失敗的規則說明。
    """

    matches: list[Match] = dataclasses.field(default_factory=list)
    counts: list[int] = dataclasses.field(default_factory=list)
    failed: list[str] = dataclasses.field(default_factory=list)

    @property
    def courses(self) -> list[models.Course]:
        """命中的課程列表。"""
        return [match.course for match in self.matches]


async def resolve_semester(
    client: course_api.CourseClient, explicit: str = ""
) -> tuple[str, str]:
    """決定要查詢的學期。

    explicit 有值就直接採用；否則以日期推算後再送一次輕量查詢確認，查不到
    資料就退回上一學期。

    Args:
        client: 用來探測的課程查詢客戶端。
        explicit: 使用者指定的學期，空字串代表自動判斷。

    Returns:
        (學期代碼, 給使用者看的說明)；沒有特別狀況時說明為空字串。
    """
    if explicit:
        return explicit, ""

    candidate = models.current_semester()
    try:
        probe = await client.search_courses("CS", candidate)
    except Exception as error:  # noqa: BLE001 - 探測失敗就沿用推算值
        logger.warning("探測學期 %s 失敗，直接採用推算值: %s", candidate, error)
        return candidate, ""

    if probe:
        return candidate, ""

    fallback = models.previous_semester(candidate)
    return fallback, (
        f"學期 {candidate} 查不到課程資料，改用上一學期 {fallback}。"
    )


async def search(
    client: course_api.CourseClient, rules: list[ruleset.Rule], semester: str
) -> SearchResult:
    """依規則查詢課程。

    規則之間取聯集。同一門課被多條規則命中時，只看空位要「每一條都要求」
    才成立，系所則取第一個有指定的。

    Args:
        client: 課程查詢客戶端。
        rules: 規則列表，空列表視為一條「全校」規則。
        semester: 規則沒指定學期時採用的學期。

    Returns:
        這一輪的查詢結果。
    """
    rules = rules or [ruleset.Rule()]
    result = SearchResult(counts=[0] * len(rules))
    matched: dict[str, tuple[models.Course, list[ruleset.Rule]]] = {}

    outcomes = await asyncio.gather(
        *[_run_rule(client, rule, semester) for rule in rules],
        return_exceptions=True,
    )

    pairs = zip(rules, outcomes, strict=True)
    for index, (rule, outcome) in enumerate(pairs):
        if isinstance(outcome, Exception):
            logger.error("規則「%s」查詢失敗: %s - %s",
                         rule.describe(), type(outcome).__name__, outcome)
            result.failed.append(rule.describe())
            continue
        result.counts[index] = len(outcome)
        for course in outcome:
            existing = matched.get(course.course_no)
            if existing is None:
                matched[course.course_no] = (course, [rule])
            else:
                existing[1].append(rule)

    for course_no in sorted(matched):
        course, hit_rules = matched[course_no]
        result.matches.append(
            Match(
                course=course,
                only_vacant=all(rule.only_vacant for rule in hit_rules),
                dept=next(
                    (rule.dept for rule in hit_rules if rule.dept), ""
                ),
            )
        )
    return result


async def _run_rule(
    client: course_api.CourseClient, rule: ruleset.Rule, semester: str
) -> list[models.Course]:
    """執行單一規則的查詢並合併它自己展開的多次查詢。

    Args:
        client: 課程查詢客戶端。
        rule: 要執行的規則。
        semester: 規則沒指定學期時採用的學期。

    Returns:
        這條規則命中的課程列表。
    """
    if rule.is_broad:
        return await client.search_all(rule.semester or semester)

    payloads = _rule_payloads(rule, semester)
    found = await asyncio.gather(
        *[client.search_payload(payload) for payload in payloads]
    )
    merged: dict[str, models.Course] = {}
    for courses in found:
        for course in courses:
            merged.setdefault(course.course_no, course)
    return list(merged.values())


def _rule_payloads(
    rule: ruleset.Rule, semester: str
) -> list[models.QueryPayload]:
    """把一條規則展開成實際要送出的查詢。

    逗號多值代表「任一符合」，API 沒有 OR 語法，只能一個值送一次查詢。

    Args:
        rule: 要展開的規則。
        semester: 規則沒指定學期時採用的學期。

    Returns:
        要送出的 payload 列表。
    """
    payloads = []
    for code in rule.codes or ("",):
        for name in rule.names or ("",):
            for teacher in rule.teachers or ("",):
                for level in rule.levels or ("",):
                    payload = models.QueryPayload(
                        Semester=rule.semester or semester,
                        CourseNo=code,
                        CourseName=name,
                        CourseTeacher=teacher,
                    )
                    if level:
                        setattr(payload, level, 1)
                    payloads.append(payload)
    return payloads

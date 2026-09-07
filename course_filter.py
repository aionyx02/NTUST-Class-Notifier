"""課程篩選規則的解析、說明與查詢執行。

規則語法是 `欄位:值`，同一條規則用空白隔開、多條規則用 `;` 隔開。
沒寫的欄位就是不限制，例如：

    課號:CS 學制:大學部; 課號:PE139A053 系所:資訊工程系三

代表「（CS 開頭且大學部）或（PE139A053）」。同一欄位可用逗號列多個值
（任一符合即可），舊格式 `學期&課號&系所` 也會自動辨識。
"""

import asyncio
import dataclasses
import difflib
import logging
import os
import re

import course_lookup

logger = logging.getLogger(__name__)

# 一條規則命中超過這個門數就不逐門查系所名額，因為那支 API 一次只能問一門課。
DEPT_LOOKUP_LIMIT = 50

# 一條規則展開後的查詢數上限，避免逗號多值相乘後爆炸。
MAX_QUERIES_PER_RULE = 30

# .env 裡放規則的變數名，另接受 LOOK_UP_CLASSES_1、_2… 等編號變數。
ENV_RULE_VAR = "LOOK_UP_CLASSES"

_SPLIT_COLON = re.compile(r"[:：]", re.UNICODE)
_BARE_CODE = re.compile(r"^[A-Za-z0-9]+$")

_TRUE_VALUES = frozenset({"是", "y", "yes", "true", "1", "on"})
_FALSE_VALUES = frozenset({"否", "n", "no", "false", "0", "off"})

# 學制的值對應到 API 的參數名。
_LEVEL_FLAGS = {
    "大學部": "OnlyUnderGraduate",
    "研究所": "OnlyMaster",
    "通識": "OnlyGeneral",
    "undergrad": "OnlyUnderGraduate",
    "master": "OnlyMaster",
    "general": "OnlyGeneral",
}
_LEVEL_NAMES = {
    "OnlyUnderGraduate": "大學部",
    "OnlyMaster": "研究所",
    "OnlyGeneral": "通識",
}


class RuleError(ValueError):
    """規則寫錯時拋出，訊息已經是可以直接顯示給使用者的中文。"""


@dataclasses.dataclass(frozen=True)
class FieldSpec:
    """一個可用欄位的定義，供解析、--help 與 README 共用。

    Attributes:
        name: 正式的中文欄位名。
        aliases: 可以互換使用的英文別名。
        desc: 一行說明。
        example: 範例寫法。
    """

    name: str
    aliases: tuple[str, ...]
    desc: str
    example: str


FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("學期", ("semester", "sem"),
              "學期代碼；不寫就自動用最新學期", "學期:1151"),
    FieldSpec("課號", ("code", "courseno", "no"),
              "課程代碼前綴，不分大小寫", "課號:CS"),
    FieldSpec("課名", ("name", "coursename"), "課名子字串", "課名:程式設計"),
    FieldSpec("老師", ("teacher",), "老師姓名子字串", "老師:姚"),
    FieldSpec("學制", ("level",), "大學部 / 研究所 / 通識", "學制:大學部"),
    FieldSpec("系所", ("dept", "department"),
              f"系所名額身分；命中 {DEPT_LOOKUP_LIMIT} 門以內才查",
              "系所:資訊工程系三"),
    FieldSpec("只看空位", ("vacant", "onlyvacant"),
              "是 / 否；只輸出跟空位有關的變動", "只看空位:是"),
)

# 別名（含正式名稱）對應到正式欄位名。
_FIELD_LOOKUP: dict[str, str] = {}
for _spec in FIELDS:
    _FIELD_LOOKUP[_spec.name] = _spec.name
    for _alias in _spec.aliases:
        _FIELD_LOOKUP[_alias] = _spec.name


def field_help() -> str:
    """組出欄位說明表，--help 與 README 共用同一份。

    Returns:
        多行的欄位說明字串。
    """
    lines = ["可用欄位（沒寫的欄位就是不限制）："]
    width = max(len(spec.name) for spec in FIELDS) + 2
    for spec in FIELDS:
        alias = "/".join(spec.aliases)
        lines.append(
            f"  {spec.name:<{width}}{spec.desc}"
            f"（別名 {alias}；例 {spec.example}）"
        )
    lines.append("")
    lines.append(
        "多條規則用 ; 分隔取聯集，同一欄位用逗號列多值取任一：課號:CS,EE21"
    )
    return "\n".join(lines)


@dataclasses.dataclass(frozen=True)
class Rule:
    """一條篩選規則，空的規則代表「全校所有課程」。

    Attributes:
        semester: 學期代碼，空字串代表用預設學期。
        codes: 課程代碼前綴，任一符合即可。
        names: 課名子字串，任一符合即可。
        teachers: 老師姓名子字串，任一符合即可。
        levels: 學制對應的 API 參數名，例如 "OnlyUnderGraduate"。
        dept: 系所名額身分。
        only_vacant: 是否只輸出跟空位有關的變動。
        source: 原始規則文字，錯誤訊息用。
    """

    semester: str = ""
    codes: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    teachers: tuple[str, ...] = ()
    levels: tuple[str, ...] = ()
    dept: str = ""
    only_vacant: bool = False
    source: str = ""

    @property
    def is_broad(self) -> bool:
        """是否沒有任何能交給伺服器縮小範圍的條件（等於要抓全校）。"""
        return not (self.codes or self.names or self.teachers or self.levels)

    def describe(self) -> str:
        """把規則翻成中文句子，啟動時回顯用。

        Returns:
            例如「課號 CS 開頭、學制 大學部」。
        """
        parts = []
        if self.codes:
            parts.append("課號 " + " 或 ".join(self.codes) + " 開頭")
        if self.names:
            parts.append(
                "課名含 " + " 或 ".join(f"「{name}」" for name in self.names)
            )
        if self.teachers:
            parts.append(
                "老師含 " + " 或 ".join(f"「{who}」" for who in self.teachers)
            )
        if self.levels:
            parts.append(
                "學制 " + "、".join(_LEVEL_NAMES[flag] for flag in self.levels)
            )
        if self.dept:
            parts.append(f"系所 {self.dept}")
        if not parts:
            parts.append("全校所有課程")
        if self.only_vacant:
            parts.append("只看空位")
        if self.semester:
            parts.append(f"學期 {self.semester}")
        return "、".join(parts)


def parse_rules(source: str | list[str]) -> list[Rule]:
    """解析規則字串或命令列參數。

    Args:
        source: .env 的規則字串，或命令列參數列表（會先用空白接起來）。

    Returns:
        規則列表；完全沒有內容時回傳空列表，由呼叫端決定是否視為全校。

    Raises:
        RuleError: 欄位名不認得、值不合法，或展開後的查詢數超過上限。
    """
    text = " ".join(source) if isinstance(source, list) else source
    chunks = [chunk for chunk in text.split(";") if chunk.strip()]
    return [_parse_one(chunk) for chunk in chunks]


def rules_from_env() -> str:
    """收集 .env 裡的規則字串。

    除了 LOOK_UP_CLASSES，也接受 LOOK_UP_CLASSES_1、LOOK_UP_CLASSES_2… 並
    合併起來，因為 .env 同名變數寫成多行時只有最後一行有效，想「一行一條
    規則」就得用不同的變數名。

    Returns:
        以 `;` 接起來的規則字串，沒有設定時為空字串。
    """
    numbered = sorted(
        (
            (name, value)
            for name, value in os.environ.items()
            if name.startswith(f"{ENV_RULE_VAR}_") and value.strip()
        ),
        key=lambda item: (len(item[0]), item[0]),  # _1 _2 … _10 的自然順序
    )
    values = [os.environ.get(ENV_RULE_VAR, "")]
    values.extend(value for _, value in numbered)
    return ";".join(value.strip() for value in values if value.strip())


def previous_semester(semester: str) -> str:
    """回傳上一個學期代碼。

    Args:
        semester: 四碼學期代碼，例如 "1151"。

    Returns:
        上一學期，例如 "1151" 回傳 "1142"、"1152" 回傳 "1151"。
    """
    year, term = semester[:-1], semester[-1]
    if term == "2":
        return f"{year}1"
    return f"{int(year) - 1}2"


async def resolve_semester(
    client: course_lookup.CourseClient, explicit: str = ""
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

    candidate = course_lookup.current_semester()
    try:
        probe = await client.search_courses("CS", candidate)
    except Exception as error:  # noqa: BLE001 - 探測失敗就沿用推算值
        logger.warning("探測學期 %s 失敗，直接採用推算值: %s", candidate, error)
        return candidate, ""

    if probe:
        return candidate, ""

    fallback = previous_semester(candidate)
    return fallback, f"學期 {candidate} 查不到課程資料，改用上一學期 {fallback}。"


@dataclasses.dataclass
class Match:
    """一門命中的課程，連同適用於它的輸出規則。

    Attributes:
        course: 課程資料。
        only_vacant: 是否只輸出跟空位有關的變動。
        dept: 要檢查的系所名額身分，空字串代表不檢查。
    """

    course: course_lookup.Course
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
    def courses(self) -> list[course_lookup.Course]:
        """命中的課程列表。"""
        return [match.course for match in self.matches]


async def search(
    client: course_lookup.CourseClient, rules: list[Rule], semester: str
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
    rules = rules or [Rule()]
    result = SearchResult(counts=[0] * len(rules))
    matched: dict[str, tuple[course_lookup.Course, list[Rule]]] = {}

    outcomes = await asyncio.gather(
        *[_run_rule(client, rule, semester) for rule in rules],
        return_exceptions=True,
    )

    for index, (rule, outcome) in enumerate(zip(rules, outcomes)):
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
    client: course_lookup.CourseClient, rule: Rule, semester: str
) -> list[course_lookup.Course]:
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
    merged: dict[str, course_lookup.Course] = {}
    for courses in found:
        for course in courses:
            merged.setdefault(course.course_no, course)
    return list(merged.values())


def _rule_payloads(
    rule: Rule, semester: str
) -> list[course_lookup.QueryPayload]:
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
                    payload = course_lookup.QueryPayload(
                        Semester=rule.semester or semester,
                        CourseNo=code,
                        CourseName=name,
                        CourseTeacher=teacher,
                    )
                    if level:
                        setattr(payload, level, 1)
                    payloads.append(payload)
    return payloads


def _parse_one(chunk: str) -> Rule:
    """解析單一條規則。

    Args:
        chunk: 一條規則的原始文字。

    Returns:
        對應的 Rule。

    Raises:
        RuleError: 欄位名不認得、值不合法，或展開後的查詢數超過上限。
    """
    chunk = chunk.strip()
    if not chunk:
        return Rule()
    if not _SPLIT_COLON.search(chunk) and "&" in chunk:
        return _parse_legacy(chunk)

    values: dict[str, object] = {"source": chunk}
    codes: list[str] = []
    names: list[str] = []
    teachers: list[str] = []

    for token in chunk.split():
        if not _SPLIT_COLON.search(token):
            # 裸字：純英數視為課號，其他要求明寫欄位，免得把課名當成課號。
            if _BARE_CODE.match(token):
                codes.extend(_split_values(token))
                continue
            raise RuleError(
                f"「{token}」看不出是什麼條件。"
                f"中文條件請寫成 課名:{token} 或 老師:{token}。\n"
                f"可用欄位：{' '.join(spec.name for spec in FIELDS)}"
            )

        raw_name, raw_value = _SPLIT_COLON.split(token, 1)
        name = raw_name.strip()
        canonical = (
            _FIELD_LOOKUP.get(name) or _FIELD_LOOKUP.get(name.lower())
        )
        if canonical is None:
            raise _unknown_field_error(name, chunk)
        if not raw_value.strip():
            continue  # 欄位寫了但沒給值，視為沒有這個條件。

        if canonical == "學期":
            values["semester"] = raw_value.strip()
        elif canonical == "課號":
            codes.extend(_split_values(raw_value))
        elif canonical == "課名":
            names.extend(_split_values(raw_value))
        elif canonical == "老師":
            teachers.extend(_split_values(raw_value))
        elif canonical == "學制":
            values["levels"] = _parse_levels(raw_value, chunk)
        elif canonical == "系所":
            values["dept"] = raw_value.strip()
        elif canonical == "只看空位":
            values["only_vacant"] = _parse_bool(raw_value, chunk)

    rule = Rule(
        codes=tuple(codes),
        names=tuple(names),
        teachers=tuple(teachers),
        **values,
    )
    _check_query_count(rule, chunk)
    return rule


def _parse_legacy(chunk: str) -> Rule:
    """解析舊格式 `學期&課號&系所(可選)`。

    Args:
        chunk: 一條舊格式規則。

    Returns:
        對應的 Rule。

    Raises:
        RuleError: 缺少課程代碼。
    """
    parts = [part.strip() for part in chunk.split("&")]
    semester = parts[0] if parts[0].isdigit() else ""
    code = parts[1] if len(parts) > 1 else ""
    dept = parts[2] if len(parts) > 2 else ""
    if not code:
        raise RuleError(f"舊格式規則缺少課程代碼：{chunk}（應為 學期&課號&系所）")
    return Rule(semester=semester, codes=(code,), dept=dept, source=chunk)


def _check_query_count(rule: Rule, chunk: str) -> None:
    """檢查規則展開後的查詢數是否超過上限。

    Args:
        rule: 已解析的規則。
        chunk: 原始規則文字，錯誤訊息用。

    Raises:
        RuleError: 展開後的查詢數超過 MAX_QUERIES_PER_RULE。
    """
    queries = (
        max(len(rule.codes), 1)
        * max(len(rule.names), 1)
        * max(len(rule.teachers), 1)
        * max(len(rule.levels), 1)
    )
    if queries > MAX_QUERIES_PER_RULE:
        raise RuleError(
            f"這條規則會展開成 {queries} 次查詢"
            f"（上限 {MAX_QUERIES_PER_RULE}），逗號多值請減少。\n"
            f"（規則：{chunk}）"
        )


def _split_values(raw: str) -> tuple[str, ...]:
    """把逗號分隔的多值拆開並去掉空白。

    Args:
        raw: 例如 "CS, EE21"。

    Returns:
        例如 ("CS", "EE21")。
    """
    return tuple(value.strip() for value in raw.split(",") if value.strip())


def _parse_bool(raw: str, source: str) -> bool:
    """解析是／否類型的值。

    Args:
        raw: 使用者寫的值。
        source: 原始規則文字，錯誤訊息用。

    Returns:
        對應的布林值。

    Raises:
        RuleError: 值不是可接受的是／否寫法。
    """
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise RuleError(
        f"「只看空位」的值只能是 是／否，但寫了「{raw}」。（規則：{source}）"
    )


def _parse_levels(raw: str, source: str) -> tuple[str, ...]:
    """把學制的值轉成 API 參數名。

    Args:
        raw: 例如 "大學部" 或 "大學部,研究所"。
        source: 原始規則文字，錯誤訊息用。

    Returns:
        API 參數名的 tuple，例如 ("OnlyUnderGraduate",)。

    Raises:
        RuleError: 出現不認得的學制。
    """
    flags: list[str] = []
    for value in _split_values(raw):
        key = value.lower() if value.isascii() else value
        flag = _LEVEL_FLAGS.get(key)
        if flag is None:
            raise RuleError(
                f"不認得的學制「{value}」，只能是 大學部／研究所／通識。"
                f"（規則：{source}）"
            )
        if flag not in flags:
            flags.append(flag)
    return tuple(flags)


def _unknown_field_error(name: str, source: str) -> RuleError:
    """組出「不認得欄位」的錯誤，並猜最接近的欄位名。

    Args:
        name: 使用者寫的欄位名。
        source: 原始規則文字。

    Returns:
        可以直接 raise 的 RuleError。
    """
    close = difflib.get_close_matches(
        name, list(_FIELD_LOOKUP), n=1, cutoff=0.4
    )
    hint = f"你是不是要打「{_FIELD_LOOKUP[close[0]]}」？" if close else ""
    return RuleError(
        f"不認得欄位「{name}」。{hint}\n"
        f"可用欄位：{' '.join(spec.name for spec in FIELDS)}\n"
        f"（規則：{source}）"
    )

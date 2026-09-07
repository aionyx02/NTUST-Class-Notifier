"""
課程篩選規則：解析、說明與查詢執行。

規則語法：`欄位:值`，同一條用空白隔開，多條用 `;` 分隔。
沒寫的欄位就是不限制，例如 `課號:CS 學制:大學部; 課號:PE139A053 系所:資訊工程系三`
代表「（CS 開頭且大學部）或（PE139A053）」。

同一欄位可用逗號列多個值（任一符合即可）：`課號:CS,EE21`。
舊格式 `學期&課號&系所` 仍可辨識，會自動轉成新規則。
"""

import asyncio
import difflib
import logging
import re
from dataclasses import dataclass, field

from course_lookup import Course, CourseClient, QueryPayload, current_semester

logger = logging.getLogger(__name__)

# 一條規則命中超過這個門數就不逐門查系所名額（那支 API 一次只能問一門課）
DEPT_LOOKUP_LIMIT = 50
# 一條規則展開後的查詢數上限，避免逗號多值相乘爆炸
MAX_QUERIES_PER_RULE = 30

_SPLIT_COLON = re.compile(r"[:：]", re.UNICODE)
_BARE_CODE = re.compile(r"^[A-Za-z0-9]+$")


class RuleError(ValueError):
    """規則寫錯時拋出，訊息已經是可以直接顯示給使用者的中文。"""


@dataclass(frozen=True)
class FieldSpec:
    """一個可用欄位的定義，同時供解析、--help 與 README 使用。"""
    name: str
    aliases: tuple[str, ...]
    desc: str
    example: str


FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("學期", ("semester", "sem"), "學期代碼；不寫就自動用最新學期", "學期:1151"),
    FieldSpec("課號", ("code", "courseno", "no"), "課程代碼前綴，不分大小寫", "課號:CS"),
    FieldSpec("課名", ("name", "coursename"), "課名子字串", "課名:程式設計"),
    FieldSpec("老師", ("teacher",), "老師姓名子字串", "老師:姚"),
    FieldSpec("學制", ("level",), "大學部 / 研究所 / 通識", "學制:大學部"),
    FieldSpec("系所", ("dept", "department"), f"系所名額身分；命中 {DEPT_LOOKUP_LIMIT} 門以內才查", "系所:資訊工程系三"),
    FieldSpec("只看空位", ("vacant", "onlyvacant"), "是 / 否；只輸出跟空位有關的變動", "只看空位:是"),
)

# 別名 -> 正式欄位名
_FIELD_LOOKUP: dict[str, str] = {}
for _spec in FIELDS:
    _FIELD_LOOKUP[_spec.name] = _spec.name
    for _alias in _spec.aliases:
        _FIELD_LOOKUP[_alias] = _spec.name

# 學制值 -> API 參數
_LEVEL_FLAGS = {
    "大學部": "OnlyUnderGraduate",
    "研究所": "OnlyMaster",
    "通識": "OnlyGeneral",
    "undergrad": "OnlyUnderGraduate",
    "master": "OnlyMaster",
    "general": "OnlyGeneral",
}
_LEVEL_NAMES = {"OnlyUnderGraduate": "大學部", "OnlyMaster": "研究所", "OnlyGeneral": "通識"}

_TRUE_VALUES = {"是", "y", "yes", "true", "1", "on"}
_FALSE_VALUES = {"否", "n", "no", "false", "0", "off"}


def field_help() -> str:
    """欄位說明表，--help 與 README 共用同一份。"""
    lines = ["可用欄位（沒寫的欄位就是不限制）："]
    width = max(len(f.name) for f in FIELDS) + 2
    for spec in FIELDS:
        alias = "/".join(spec.aliases)
        lines.append(f"  {spec.name:<{width}}{spec.desc}（別名 {alias}；例 {spec.example}）")
    lines.append("")
    lines.append("多條規則用 ; 分隔取聯集，同一欄位用逗號列多值取任一：課號:CS,EE21")
    return "\n".join(lines)


@dataclass(frozen=True)
class Rule:
    """一條篩選規則。空的規則代表「全校所有課程」。"""
    semester: str = ""
    codes: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    teachers: tuple[str, ...] = ()
    levels: tuple[str, ...] = ()  # 存 API 參數名，如 OnlyUnderGraduate
    dept: str = ""
    only_vacant: bool = False
    source: str = ""  # 原始文字，錯誤訊息用

    @property
    def is_broad(self) -> bool:
        """沒有任何能交給伺服器縮小範圍的條件 = 要抓全校。"""
        return not (self.codes or self.names or self.teachers or self.levels)

    def describe(self) -> str:
        """把規則翻成中文句子，啟動時回顯用。"""
        parts = []
        if self.codes:
            parts.append("課號 " + " 或 ".join(self.codes) + " 開頭")
        if self.names:
            parts.append("課名含 " + " 或 ".join(f"「{n}」" for n in self.names))
        if self.teachers:
            parts.append("老師含 " + " 或 ".join(f"「{t}」" for t in self.teachers))
        if self.levels:
            parts.append("學制 " + "、".join(_LEVEL_NAMES[lv] for lv in self.levels))
        if self.dept:
            parts.append(f"系所 {self.dept}")
        if not parts:
            parts.append("全校所有課程")
        if self.only_vacant:
            parts.append("只看空位")
        if self.semester:
            parts.append(f"學期 {self.semester}")
        return "、".join(parts)


# ========== 解析 ========== #
def _split_values(raw: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in raw.split(",") if v.strip())


def _parse_bool(raw: str, source: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise RuleError(f"「只看空位」的值只能是 是／否，但寫了「{raw}」。（規則：{source}）")


def _parse_levels(raw: str, source: str) -> tuple[str, ...]:
    flags = []
    for value in _split_values(raw):
        flag = _LEVEL_FLAGS.get(value.lower() if value.isascii() else value)
        if flag is None:
            raise RuleError(
                f"不認得的學制「{value}」，只能是 大學部／研究所／通識。（規則：{source}）")
        if flag not in flags:
            flags.append(flag)
    return tuple(flags)


def _unknown_field_error(name: str, source: str) -> RuleError:
    close = difflib.get_close_matches(name, list(_FIELD_LOOKUP), n=1, cutoff=0.4)
    hint = f"你是不是要打「{_FIELD_LOOKUP[close[0]]}」？" if close else ""
    return RuleError(
        f"不認得欄位「{name}」。{hint}\n"
        f"可用欄位：{' '.join(f.name for f in FIELDS)}\n"
        f"（規則：{source}）"
    )


def _parse_legacy(chunk: str) -> Rule:
    """舊格式 `學期&課號&系所(可選)`。"""
    parts = [p.strip() for p in chunk.split("&")]
    semester = parts[0] if parts[0].isdigit() else ""
    code = parts[1] if len(parts) > 1 else ""
    dept = parts[2] if len(parts) > 2 else ""
    if not code:
        raise RuleError(f"舊格式規則缺少課程代碼：{chunk}（應為 學期&課號&系所）")
    return Rule(semester=semester, codes=(code,), dept=dept, source=chunk)


def _parse_one(chunk: str) -> Rule:
    chunk = chunk.strip()
    if not chunk:
        return Rule()
    if ":" not in chunk and "：" not in chunk and "&" in chunk:
        return _parse_legacy(chunk)

    values: dict[str, object] = {"source": chunk}
    codes: list[str] = []
    names: list[str] = []
    teachers: list[str] = []

    for token in chunk.split():
        if not _SPLIT_COLON.search(token):
            # 裸字：純英數視為課號，其他要求明寫欄位，避免把課名當成課號
            if _BARE_CODE.match(token):
                codes.extend(_split_values(token))
                continue
            raise RuleError(
                f"「{token}」看不出是什麼條件。中文條件請寫成 課名:{token} 或 老師:{token}。\n"
                f"可用欄位：{' '.join(f.name for f in FIELDS)}"
            )

        raw_name, raw_value = _SPLIT_COLON.split(token, 1)
        name = raw_name.strip()
        canonical = _FIELD_LOOKUP.get(name) or _FIELD_LOOKUP.get(name.lower())
        if canonical is None:
            raise _unknown_field_error(name, chunk)
        if not raw_value.strip():
            continue  # 欄位寫了但沒給值 = 沒有這個條件

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

    rule = Rule(codes=tuple(codes), names=tuple(names), teachers=tuple(teachers), **values)  # type: ignore[arg-type]
    queries = max(len(rule.codes), 1) * max(len(rule.names), 1) * \
              max(len(rule.teachers), 1) * max(len(rule.levels), 1)
    if queries > MAX_QUERIES_PER_RULE:
        raise RuleError(
            f"這條規則會展開成 {queries} 次查詢（上限 {MAX_QUERIES_PER_RULE}），逗號多值請減少。\n"
            f"（規則：{chunk}）")
    return rule


def parse_rules(source: str | list[str]) -> list[Rule]:
    """
    解析規則字串（.env）或命令列參數列表。
    完全沒有內容時回傳空列表，呼叫端自行決定是否代表「全校」。
    """
    text = " ".join(source) if isinstance(source, list) else source
    chunks = [c for c in text.split(";") if c.strip()]
    return [_parse_one(chunk) for chunk in chunks]


# ========== 學期 ========== #
def previous_semester(semester: str) -> str:
    """1151 -> 1142；1152 -> 1151。"""
    year, term = semester[:-1], semester[-1]
    return f"{year}1" if term == "2" else f"{int(year) - 1}2"


async def resolve_semester(client: CourseClient, explicit: str = "") -> tuple[str, str]:
    """
    決定要用哪個學期。explicit 有值就直接採用，否則以日期推算後再探測確認。
    :return (學期, 給使用者看的說明；沒有特別事情發生就是空字串)
    """
    if explicit:
        return explicit, ""

    candidate = current_semester()
    try:
        probe = await client.search_courses("CS", candidate)
    except Exception as e:
        logger.warning(f"探測學期 {candidate} 失敗，直接採用推算值: {e}")
        return candidate, ""

    if probe:
        return candidate, ""

    fallback = previous_semester(candidate)
    return fallback, f"學期 {candidate} 查不到課程資料，改用上一學期 {fallback}。"


# ========== 查詢執行 ========== #
@dataclass
class Match:
    """一門命中的課程，連同它適用的輸出規則。"""
    course: Course
    only_vacant: bool = False
    dept: str = ""


@dataclass
class SearchResult:
    matches: list[Match] = field(default_factory=list)
    counts: list[int] = field(default_factory=list)  # 每條規則各自命中幾門
    failed: list[str] = field(default_factory=list)  # 查詢失敗的規則描述

    @property
    def courses(self) -> list[Course]:
        return [m.course for m in self.matches]


def _rule_payloads(rule: Rule, semester: str) -> list[QueryPayload]:
    """把一條規則展開成實際要送出的查詢（逗號多值 = 多次查詢）。"""
    payloads = []
    for code in rule.codes or ("",):
        for name in rule.names or ("",):
            for teacher in rule.teachers or ("",):
                for level in rule.levels or ("",):
                    payload = QueryPayload(
                        Semester=rule.semester or semester,
                        CourseNo=code, CourseName=name, CourseTeacher=teacher,
                    )
                    if level:
                        setattr(payload, level, 1)
                    payloads.append(payload)
    return payloads


async def search(client: CourseClient, rules: list[Rule], semester: str) -> SearchResult:
    """
    依規則查詢課程。規則之間取聯集，同一門課被多條規則命中時：
    只看空位取「每一條都要求」才成立，系所取第一個有指定的。
    """
    rules = rules or [Rule()]
    result = SearchResult(counts=[0] * len(rules))
    matched: dict[str, tuple[Course, list[Rule]]] = {}

    async def run_rule(rule: Rule) -> list[Course]:
        if rule.is_broad:
            return await client.search_all(rule.semester or semester)
        payloads = _rule_payloads(rule, semester)
        found = await asyncio.gather(*[client.search_payload(p) for p in payloads])
        merged: dict[str, Course] = {}
        for courses in found:
            for course in courses:
                merged.setdefault(course.course_no, course)
        return list(merged.values())

    outcomes = await asyncio.gather(*[run_rule(rule) for rule in rules], return_exceptions=True)

    for index, (rule, outcome) in enumerate(zip(rules, outcomes)):
        if isinstance(outcome, Exception):
            logger.error(f"規則「{rule.describe()}」查詢失敗: {type(outcome).__name__} - {outcome}")
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
        result.matches.append(Match(
            course=course,
            only_vacant=all(r.only_vacant for r in hit_rules),
            dept=next((r.dept for r in hit_rules if r.dept), ""),
        ))
    return result

"""Discord 看板的文字組裝。

用 diff 程式碼區塊上色：`+` 開頭是綠色（有空位、有人退選）、`-` 開頭是紅色
（沒空位、被選走），其餘行補兩格讓等寬欄位對齊。
"""

import datetime

from ntust_class_notifier.core import changes
from ntust_class_notifier.core import models
from ntust_class_notifier.core import ruleset
from ntust_class_notifier.ui import console

# Discord 單則訊息上限 2000 字，留點餘裕給程式碼區塊符號。
DISCORD_LIMIT = 1900

# 啟動訊息最多列幾門課。
STARTUP_LIST_LIMIT = 15

# 看板最多列幾筆人數變動。
CHANGE_LIST_LIMIT = 12


def course_line(course: models.Course) -> str:
    """把課程排成與終端機監看相同的等寬一行。

    Args:
        course: 要顯示的課程。

    Returns:
        課號、課名、老師、節次、人數對齊後的字串。
    """
    return (
        f"{console.format_course(course)} "
        f"{course.cur_member}/{course.member_limit}"
    )


def code_block(lines: list[str]) -> str:
    """把多行內容包成 Discord 的 diff 程式碼區塊。

    等寬字體才對得齊，超過長度上限時在完整行的邊界截斷。

    Args:
        lines: 要包起來的每一行，已自行帶上 "+ "、"- " 或 "  " 前綴。

    Returns:
        可直接送出的訊息片段。
    """
    body = "\n".join(lines)
    if len(body) > DISCORD_LIMIT:
        body = body[:DISCORD_LIMIT].rsplit("\n", 1)[0] + "\n…（訊息過長已截斷）"
    return f"```diff\n{body}\n```"


def change_line(change: changes.Change) -> str:
    """把一筆變化排成看板用的一行。

    Args:
        change: 要顯示的變化。

    Returns:
        以 "-"（被選走）、"+"（有人退選）或 "  "（其他）開頭的一行。
    """
    course = change.course
    if change.kind == changes.ADDED:
        return f"  {course_line(course)}  新增課程"
    if change.kind == changes.REMOVED:
        return f"  {course_line(course)}  已從查詢結果消失"
    if change.kind == changes.LIMIT:
        return (
            f"  {course_line(course)}  "
            f"名額由 {change.before.member_limit} 調整為 {course.member_limit}"
        )

    taken = change.kind == changes.TAKEN
    tail = f"剩 {course.vacancy}" if course.vacancy > 0 else "額滿"
    if change.limit_changed:
        tail += (f"，上限 {change.before.member_limit} → "
                 f"{course.member_limit}")
    return (
        f"{'-' if taken else '+'} {console.format_course(course)} "
        f"{change.before.cur_member} → "
        f"{course.cur_member}/{course.member_limit} "
        f"({change.delta:+d})  {'被選走' if taken else '有人退選'}，{tail}"
    )


def change_lines(items: list[changes.Change]) -> list[str]:
    """把整輪的變化排成看板用的多行。

    Args:
        items: 這一輪的所有變化。

    Returns:
        每筆變化一行。
    """
    return [change_line(change) for change in items]


def board_message(
    vacant: list[tuple[models.Course, str]],
    change_text: list[str],
    watched: int,
    semester: str,
    notes: list[str],
) -> str:
    """組出目前狀態的單一訊息。

    Args:
        vacant: 有空位的 (課程, 系所名額說明)，顯示為綠色。
        change_text: 這一輪的人數變動，已由 change_lines() 上好前綴。
        watched: 這一輪監控的課程總數。
        semester: 這次查詢的學期。
        notes: 額外要附上的說明，例如自動加選結果。

    Returns:
        要送出的完整訊息。
    """
    now = f"{datetime.datetime.now():%H:%M:%S}"
    if vacant:
        lines = [f"+ 有空位 {len(vacant)} 門（{now} 更新）", ""]
        for course, dept_info in vacant:
            line = f"+ {course_line(course)}  剩 {course.vacancy}"
            if dept_info:
                line += f"  系所 {dept_info}"
            lines.append(line)
    else:
        lines = [f"- 目前沒有空位（{now} 更新）"]

    if change_text:
        lines += ["", f"  這一輪的人數變動 {len(change_text)} 筆"]
        lines += change_text[:CHANGE_LIST_LIMIT]
        if len(change_text) > CHANGE_LIST_LIMIT:
            lines.append(f"  …等共 {len(change_text)} 筆")

    lines += ["", f"  監看 {watched} 門課程・學期 {semester}"]

    header = "**有空位**" if vacant else "**目前沒有空位**"
    parts = [header, code_block(lines)]
    parts.extend(notes)
    return "\n".join(parts)


def startup_message(
    parsed: list[ruleset.Rule],
    counts: list[int],
    courses: list[models.Course],
    semester: str,
    auto_enroll: bool,
) -> str:
    """組出啟動訊息。

    Args:
        parsed: 篩選規則。
        counts: 每條規則命中的門數。
        courses: 啟動時命中的課程。
        semester: 這次查詢的學期。
        auto_enroll: 是否啟用了自動加選。

    Returns:
        要送出的完整訊息。
    """
    if not courses:
        return "規則沒有命中任何課程，請檢查 .env 的 LOOK_UP_CLASSES。"

    lines = [f"  監看 {len(courses)} 門課程（學期 {semester}）"]
    pairs = zip(parsed, counts, strict=False)
    for index, (rule, count) in enumerate(pairs, start=1):
        lines.append(f"  規則 {index}：{rule.describe()} → {count} 門")
    lines.append("")
    for course in courses[:STARTUP_LIST_LIMIT]:
        # 目前就有空位的標成綠色，額滿的標成紅色。
        prefix = "+" if course.member_limit > 0 and course.vacancy > 0 else "-"
        lines.append(f"{prefix} {course_line(course)}")
    if len(courses) > STARTUP_LIST_LIMIT:
        lines.append(f"  …等共 {len(courses)} 門")

    parts = [code_block(lines)]
    if auto_enroll:
        parts.append(
            "自動加選已啟用（電選課加選與全校加退選期間偵測到空位將自動送出）"
        )
    parts.append(
        "人數一有變動就更新，這則訊息會被直接取代，聊天室永遠只有一則。"
    )
    return "\n".join(parts)

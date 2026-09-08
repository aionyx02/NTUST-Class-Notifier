"""終端機的內容輸出：規則回顯、課程清單與人數變動。

只負責把資料變成畫面，不做查詢；要查什麼由 app 層決定。
"""

import datetime

from ntust_class_notifier.core import changes
from ntust_class_notifier.core import models
from ntust_class_notifier.core import ruleset
from ntust_class_notifier.ui import console

# 課程超過這個數量就不逐一列出啟動清單，可用 --list 強制列出。
LIST_LIMIT = 50

_CHANGE_LABELS = {
    changes.TAKEN: ("被選走", console.RED),
    changes.RELEASED: ("有人退選", console.GREEN),
    changes.ADDED: ("新增課程", console.CYAN),
    changes.REMOVED: ("課程已從查詢結果消失", console.YELLOW),
    changes.LIMIT: ("名額調整", console.CYAN),
}


def print_rules(
    printer: console.Printer,
    parsed: list[ruleset.Rule],
    counts: list[int],
    semester: str,
) -> None:
    """逐條回顯規則的中文解讀與命中門數。

    Args:
        printer: 輸出器。
        parsed: 規則列表。
        counts: 每條規則命中的門數。
        semester: 這次查詢的學期。
    """
    pairs = zip(parsed, counts, strict=False)
    for index, (rule, count) in enumerate(pairs, start=1):
        printer.line(
            printer.color(f"   規則 {index}：{rule.describe()}", console.CYAN)
            + f"  →  {count} 門"
        )
    printer.line(printer.color(f"   學期 {semester}", console.DIM))


def zero_result_hint(
    printer: console.Printer,
    parsed: list[ruleset.Rule],
    semester: str,
) -> None:
    """所有規則都命中 0 門時，印出可能原因與放寬建議。

    Args:
        printer: 輸出器。
        parsed: 使用者給的規則。
        semester: 這次查詢的學期。
    """
    printer.line(printer.color(
        "錯誤：沒有任何課程符合這些規則。", console.RED))
    hints = [
        f"學期 {semester} 是否正確？可用 -s 指定，或確認該學期課表已公告。"
    ]
    if any(rule.names or rule.teachers for rule in parsed):
        hints.append("課名與老師是「子字串」比對，試試更短的關鍵字。")
    if any(rule.codes for rule in parsed):
        hints.append(
            "課號是「前綴」比對，只吃開頭（1003 這種中段數字查不到）。")
    if any(rule.levels for rule in parsed):
        hints.append(
            "學制與其他條件是「且」的關係，試著把 學制 拿掉再查。")
    for hint in hints:
        printer.line(printer.color(f"建議：{hint}", console.YELLOW))


def print_courses(
    printer: console.Printer, courses: list[models.Course]
) -> None:
    """列出課程與目前人數。

    Args:
        printer: 輸出器。
        courses: 要列出的課程。
    """
    for course in courses:
        printer.line(
            f"   {console.format_course(course)} "
            f"{course.cur_member}/{course.member_limit}"
        )


def format_change(printer: console.Printer, change: changes.Change) -> str:
    """把一筆變化排成終端機用的一行。

    Args:
        printer: 用來上色的輸出器。
        change: 要顯示的變化。

    Returns:
        整行上色的訊息，被別人選走是紅色、有人退選是綠色。
    """
    label, code = _CHANGE_LABELS[change.kind]
    course = change.course
    now = f"{datetime.datetime.now():%H:%M:%S}"
    head = f"[{now}] {console.format_course(course)} "

    if change.before is None:
        body = f"{course.cur_member}/{course.member_limit}  {label}"
        return printer.color(head + body, code)

    if change.kind == changes.LIMIT:
        body = (
            f"{course.cur_member}/{course.member_limit}  {label}，"
            f"上限 {change.before.member_limit} → {course.member_limit}"
        )
        return printer.color(head + body, code)

    tail = f"剩 {course.vacancy}" if course.vacancy > 0 else "額滿"
    if change.limit_changed:
        tail += (f"，上限 {change.before.member_limit} → "
                 f"{course.member_limit}")
    body = (
        f"{change.before.cur_member} → "
        f"{course.cur_member}/{course.member_limit} "
        f"({change.delta:+d})  {label}，{tail}"
    )
    return printer.color(head + body, code)


def print_alert(
    printer: console.Printer,
    course: models.Course,
    dept_info: str,
    link: str,
) -> None:
    """印出有空位的大字提醒。

    Args:
        printer: 輸出器。
        course: 出現空位的課程。
        dept_info: 系所名額說明，空字串代表不顯示。
        link: 選課系統連結。
    """
    bar = "═" * 64
    printer.line("")
    printer.line(printer.color(bar, console.GREEN))
    printer.line(printer.color(
        f"  有空位！ {course.course_no}  {course.course_name}",
        console.BOLD + console.GREEN))
    printer.line(printer.color(
        f"     授課老師：{course.teacher}    "
        f"上課時間：{','.join(course.node) or '未定'}",
        console.GREEN))
    printer.line(printer.color(
        f"     目前人數：{course.cur_member} / {course.member_limit}"
        f"    剩餘 {course.vacancy} 個名額", console.GREEN))
    if dept_info:
        printer.line(printer.color(
            f"     系所名額：{dept_info}", console.GREEN))
    printer.line(printer.color(f"     選課連結：{link}", console.GREEN))
    printer.line(printer.color(
        f"     {datetime.datetime.now():%Y-%m-%d %H:%M:%S}", console.GREEN))
    printer.line(printer.color(bar, console.GREEN))

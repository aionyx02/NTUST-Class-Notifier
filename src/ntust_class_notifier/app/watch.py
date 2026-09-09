"""人數監看迴圈：只在人數有變動時輸出一行。"""

import datetime
import time

from ntust_class_notifier.app import enroll as enroll_app
from ntust_class_notifier.app import monitor
from ntust_class_notifier.app import search
from ntust_class_notifier.clients import course_api
from ntust_class_notifier.core import changes
from ntust_class_notifier.core import models
from ntust_class_notifier.core import ruleset
from ntust_class_notifier.ui import console
from ntust_class_notifier.ui import report


async def watch(
    client: course_api.CourseClient,
    parsed: list[ruleset.Rule],
    semester: str,
    interval: float,
    printer: console.Printer,
    list_all: bool = False,
    enroller: enroll_app.AutoEnroller | None = None,
) -> None:
    """持續監看課程人數，只輸出有變動的部分。

    Args:
        client: 課程查詢客戶端。
        parsed: 篩選規則。
        semester: 規則沒指定學期時採用的學期。
        interval: 每輪週期秒數。
        printer: 輸出器。
        list_all: 啟動時是否列出全部課程。
        enroller: 自動加選器，None 代表只監看不加選。
    """
    previous: dict[str, models.Course] = {}
    filters: dict[str, search.Match] = {}
    change_count = 0
    rounds = 0
    announced = interval

    while True:
        rounds += 1
        started = time.monotonic()
        result = await search.search(client, parsed, semester)
        now = f"{datetime.datetime.now():%H:%M:%S}"

        if result.failed:
            # 查詢失敗時不做比對，否則會把「查不到」誤判成人數歸零。
            printer.line(printer.color(
                f"[{now}] 查詢失敗：{'、'.join(result.failed)}，本輪略過",
                console.YELLOW))
            await monitor.pace(interval, started)
            continue

        current = {
            match.course.course_no: match.course for match in result.matches
        }
        filters = {
            match.course.course_no: match for match in result.matches
        }

        first_round = not previous
        if first_round:
            if not current:
                report.print_rules(printer, parsed, result.counts, semester)
                report.zero_result_hint(printer, parsed, semester)
                return
            _print_header(printer, current, parsed, result.counts, semester,
                          interval, list_all)

        # 第一輪就有空位也要搶，所以放在印完表頭之後、比對變動之前。
        # 空位一定要用扣掉系所名額的版本：拿總人數的假空位去加選不只白送
        # 請求，還會讓那門課留在 previous 裡，名額真的釋出時反而不搶了。
        if enroller:
            vacant = await search.collect_vacant(client, result, semester)
            notes = await enroller.on_round(
                {course.course_no for course, _ in vacant})
            for note in notes:
                printer.line(printer.color(f"[{now}] {note}", console.GREEN))

        if first_round:
            previous = current
            announced = await monitor.pace(interval, started)
            if announced > interval:
                printer.line(printer.color(
                    f"   查詢耗時較久，每輪週期自動放寬為 {announced:.0f} 秒。",
                    console.YELLOW))
            continue

        change_count += _report_changes(
            printer, previous, current, filters)
        previous = current
        printer.status(
            f"[{now}] 監看 {len(current)} 門課程・第 {rounds} 輪・"
            f"已記錄 {change_count} 筆變動"
        )

        effective = await monitor.pace(interval, started)
        if effective > announced * 1.5 or effective < announced / 1.5:
            printer.line(printer.color(
                f"[{now}] 每輪週期自動調整為 {effective:.0f} 秒"
                "（查詢耗時變化）。", console.YELLOW))
            announced = effective


def _print_header(
    printer: console.Printer,
    current: dict[str, models.Course],
    parsed: list[ruleset.Rule],
    counts: list[int],
    semester: str,
    interval: float,
    list_all: bool,
) -> None:
    """印出啟動時的規則回顯與課程清單。

    Args:
        printer: 輸出器。
        current: 這一輪命中的課程。
        parsed: 規則列表。
        counts: 每條規則命中的門數。
        semester: 這次查詢的學期。
        interval: 每輪週期秒數。
        list_all: 是否強制列出全部課程。
    """
    printer.line(printer.color(
        f"── 監看 {len(current)} 門課程（每 {interval:g} 秒；Ctrl+C 結束）──",
        console.CYAN))
    report.print_rules(printer, parsed, counts, semester)
    if list_all or len(current) <= report.LIST_LIMIT:
        report.print_courses(printer, list(current.values()))
    else:
        printer.line(printer.color(
            f"   （課程超過 {report.LIST_LIMIT} 門，清單省略；"
            "加 --list 可列出全部）", console.DIM))
    printer.line(printer.color("── 以下只顯示人數變動 ──", console.CYAN))


def _report_changes(
    printer: console.Printer,
    previous: dict[str, models.Course],
    current: dict[str, models.Course],
    filters: dict[str, search.Match],
) -> int:
    """比對兩輪結果並輸出變動。

    Args:
        printer: 輸出器。
        previous: 上一輪的課程。
        current: 這一輪的課程。
        filters: 每門課適用的輸出規則。

    Returns:
        這一輪輸出了幾筆變動。
    """
    shown = 0
    for change in changes.diff_rounds(previous, current):
        match = filters.get(change.course.course_no)
        # 「只看空位」只擋人數變動；課程新增或消失一律要讓使用者知道。
        countable = change.kind not in (changes.ADDED, changes.REMOVED)
        if (countable and match and match.only_vacant
                and not change.touches_vacancy):
            continue
        printer.line(report.format_change(printer, change))
        shown += 1
    return shown

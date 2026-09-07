"""終端機版搶課監控，不需要 Discord。

每隔數秒檢查鎖定的課程，一旦出現空位就顯示大字提醒並發出音效。本腳本只
查詢公開的課程查詢 API，不會登入選課系統、也不會替你送出加選。

篩選規則寫成 `欄位:值`，多條用 `;` 隔開；命令列有給就取代 .env 的
LOOK_UP_CLASSES，兩邊都沒有就監控全校所有課程。

用法：
    uv run course_alert.py 課號:CS1003301
    uv run course_alert.py CS1003301 EE21
    uv run course_alert.py 課號:PE139A053 系所:資訊工程系三
    uv run course_alert.py 課名:程式設計 學制:大學部
"""

import asyncio
import dataclasses
import datetime
import logging
import time

import course_filter
import course_lookup
import course_selector
import course_watch
import selection_period

logger = logging.getLogger(__name__)

# 第一輪就有空位的課程超過這個數量時，只在清單標記不逐一跳提醒。
FIRST_ROUND_ALERT_LIMIT = 3


@dataclasses.dataclass
class Watcher:
    """搶課監控主體，只負責查詢、判斷與提醒。

    Attributes:
        client: 課程查詢客戶端。
        rules: 篩選規則。
        semester: 規則沒指定學期時採用的學期。
        interval: 每輪週期秒數。
        printer: 終端機輸出器。
        sound: 是否播放提示音。
        list_all: 啟動時是否列出全部課程。
        had_vacancy: 每門課上一輪是否有空位。
        alerts: 已經提醒過幾次。
        rounds: 已經跑過幾輪。
        dept_skipped: 這一輪是否因課程過多而跳過系所名額檢查。
    """

    client: course_lookup.CourseClient
    rules: list[course_filter.Rule]
    semester: str
    interval: float
    printer: course_watch.Printer
    sound: bool = True
    list_all: bool = False
    had_vacancy: dict[str, bool] = dataclasses.field(default_factory=dict)
    alerts: int = 0
    rounds: int = 0
    dept_skipped: bool = False

    def beep(self, sound_type: str) -> None:
        """播放提示音。

        Args:
            sound_type: course_selector.play_sound() 接受的音效類型。
        """
        if self.sound:
            course_selector.play_sound(sound_type)

    def selection_link(self) -> str:
        """依目前時段回傳選課系統連結。

        Returns:
            對應時段的網址；非選課時段時兩個都給。
        """
        period = selection_period.get_current_period()
        if period == "dept":
            return selection_period.DEPT_SELECT_LINK
        if period == "open":
            return selection_period.OPEN_SELECT_LINK
        return (
            f"{selection_period.DEPT_SELECT_LINK} / "
            f"{selection_period.OPEN_SELECT_LINK}"
        )

    def alert(self, course: course_lookup.Course, dept_info: str) -> None:
        """印出有空位的大字提醒。

        Args:
            course: 出現空位的課程。
            dept_info: 系所名額說明，空字串代表不顯示。
        """
        printer = self.printer
        bar = "═" * 64
        printer.line("")
        printer.line(printer.color(bar, course_watch.GREEN))
        printer.line(printer.color(
            f"  有空位！ {course.course_no}  {course.course_name}",
            course_watch.BOLD + course_watch.GREEN))
        printer.line(printer.color(
            f"     授課老師：{course.teacher}    "
            f"上課時間：{','.join(course.node) or '未定'}",
            course_watch.GREEN))
        printer.line(printer.color(
            f"     目前人數：{course.cur_member} / {course.member_limit}"
            f"    剩餘 {course.vacancy} 個名額", course_watch.GREEN))
        if dept_info:
            printer.line(printer.color(
                f"     系所名額：{dept_info}", course_watch.GREEN))
        printer.line(printer.color(
            f"     選課連結：{self.selection_link()}", course_watch.GREEN))
        printer.line(printer.color(
            f"     {datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
            course_watch.GREEN))
        printer.line(printer.color(bar, course_watch.GREEN))

    def status(self, courses: list[course_lookup.Course]) -> None:
        """更新底部狀態列。

        Args:
            courses: 這一輪監控的課程。
        """
        now = f"{datetime.datetime.now():%H:%M:%S}"
        vacant = [
            course for course in courses
            if course.member_limit > 0 and course.vacancy > 0
        ]
        if len(courses) <= 6:
            detail = " · ".join(
                f"{course.course_no} {course.cur_member}/{course.member_limit}"
                for course in courses
            )
        else:
            detail = f"{len(courses)} 門課程・有空位 {len(vacant)} 門"
        self.printer.status(
            f"[{now}] {detail}・第 {self.rounds} 輪・已提醒 {self.alerts} 次"
        )

    def print_header(
        self,
        checked: list[tuple[course_filter.Match, bool, str]],
        counts: list[int],
    ) -> None:
        """印出啟動時的規則回顯與課程清單。

        Args:
            checked: 這一輪每門課的 (命中資料, 是否有空位, 系所名額說明)。
            counts: 每條規則命中的門數。
        """
        printer = self.printer
        vacant = sum(1 for _, has_vacancy, _ in checked if has_vacancy)
        printer.line(printer.color(
            f"── 搶課監控 {len(checked)} 門課程"
            f"（每 {self.interval:g} 秒；Ctrl+C 結束）──", course_watch.CYAN))
        course_watch.print_rules(
            printer, self.rules, counts, self.semester)

        if self.list_all or len(checked) <= course_watch.LIST_LIMIT:
            for match, has_vacancy, _ in checked:
                course = match.course
                mark = (
                    printer.color("有空位", course_watch.GREEN)
                    if has_vacancy
                    else printer.color("額滿", course_watch.RED)
                )
                counts = f"{course.cur_member}/{course.member_limit}"
                printer.line(
                    f"   {course_watch.fit(course.course_no, 10)} "
                    f"{course_watch.fit(course.course_name, 26)} "
                    f"{course_watch.fit(course.teacher, 12)} "
                    f"{course_watch.fit(','.join(course.node), 14)} "
                    f"{course_watch.fit(counts, 9)}"
                    f"{f'[{match.dept}] ' if match.dept else ''}{mark}"
                )
        else:
            printer.line(printer.color(
                f"   （課程超過 {course_watch.LIST_LIMIT} 門，清單省略；"
                f"加 --list 可列出全部）", course_watch.CYAN))

        period = selection_period.get_current_period()
        printer.line(
            f"   目前有空位：{printer.color(str(vacant), course_watch.GREEN)} 門　"
            f"目前時段：{selection_period.get_period_name(period)}"
        )
        if self.dept_skipped:
            printer.line(printer.color(
                f"   課程超過 {course_filter.DEPT_LOOKUP_LIMIT} 門，"
                f"已跳過系所名額檢查", course_watch.YELLOW))
        printer.line(printer.color(
            "── 之後只在課程「由額滿變成有空位」時提醒（不會自動加選）──",
            course_watch.CYAN))

    async def check_vacancy(
        self, match: course_filter.Match, dept_allowed: bool
    ) -> tuple[bool, str]:
        """判斷課程目前是否真的可以選。

        Args:
            match: 命中的課程與它適用的規則。
            dept_allowed: 這一輪是否允許查系所名額。

        Returns:
            (是否有空位, 系所名額說明字串)。
        """
        course = match.course
        if course.member_limit <= 0 or course.cur_member >= course.member_limit:
            return False, ""
        if not match.dept or not dept_allowed:
            return True, ""

        # 總人數有空位還要看系所名額，否則是搶不到的假空位。
        result = await self.client.get_department_limit(
            self.semester, course.course_no, match.dept)
        if result is None:
            return True, ""

        persons, restrict = result
        dept_info = f"{persons} / {restrict}（{match.dept}）"
        if persons >= restrict:
            logger.debug("%s 總數有空位但系所額滿：%s",
                         course.course_no, dept_info)
            return False, dept_info
        return True, dept_info

    async def run(self) -> None:
        """持續監控，直到使用者中斷。"""
        announced = self.interval

        while True:
            self.rounds += 1
            started = time.monotonic()
            result = await course_filter.search(
                self.client, self.rules, self.semester)

            if result.failed:
                # 查詢失敗不做判斷，否則會把「查不到」誤判成額滿或空位。
                self.printer.line(self.printer.color(
                    f"[{datetime.datetime.now():%H:%M:%S}] 查詢失敗："
                    f"{'、'.join(result.failed)}，本輪略過", course_watch.YELLOW))
                await course_watch.pace(self.interval, started)
                continue

            if not result.matches:
                if self.rounds == 1:
                    course_watch.print_rules(
                        self.printer, self.rules, result.counts, self.semester)
                    course_watch.zero_result_hint(
                        self.printer, self.rules, self.semester)
                return

            # 系所名額一門課要一次請求，課程太多就整輪跳過。
            dept_allowed = (
                len(result.matches) <= course_filter.DEPT_LOOKUP_LIMIT
            )
            self.dept_skipped = (
                not dept_allowed and any(match.dept for match in result.matches)
            )
            checked = [
                (match, *await self.check_vacancy(match, dept_allowed))
                for match in result.matches
            ]

            if self.rounds == 1 and self._handle_first_round(
                    checked, result.counts):
                self.status(result.courses)
                announced = await course_watch.pace(self.interval, started)
                continue

            self._report(checked)
            self.status(result.courses)

            effective = await course_watch.pace(self.interval, started)
            if effective > announced * 1.5 or effective < announced / 1.5:
                self.printer.line(self.printer.color(
                    f"   每輪週期自動調整為 {effective:.0f} 秒（查詢耗時變化）。",
                    course_watch.YELLOW))
                announced = effective

    def _handle_first_round(
        self,
        checked: list[tuple[course_filter.Match, bool, str]],
        counts: list[int],
    ) -> bool:
        """處理第一輪：印出表頭，必要時抑制大量提醒。

        Args:
            checked: 這一輪每門課的檢查結果。
            counts: 每條規則命中的門數。

        Returns:
            是否略過這一輪的逐門提醒（一開始就有一堆空位時）。
        """
        self.print_header(checked, counts)
        vacant = [row for row in checked if row[1]]
        if len(vacant) <= FIRST_ROUND_ALERT_LIMIT:
            return False

        # 多半是範圍很大的規則，只記錄狀態不洗版。
        for match, has_vacancy, _ in checked:
            self.had_vacancy[match.course.course_no] = has_vacancy
        self.beep("vacancy")
        return True

    def _report(
        self, checked: list[tuple[course_filter.Match, bool, str]]
    ) -> None:
        """比對上一輪狀態並輸出提醒。

        Args:
            checked: 這一輪每門課的檢查結果。
        """
        for match, has_vacancy, dept_info in checked:
            course = match.course
            was_open = self.had_vacancy.get(course.course_no, False)

            # 只在「額滿 → 有空位」的瞬間提醒，避免每輪洗版。
            if has_vacancy and not was_open:
                self.alerts += 1
                self.alert(course, dept_info)
                self.beep("vacancy")
            elif not has_vacancy and was_open:
                # 名額被別人選走：紅色。
                self.printer.line(self.printer.color(
                    f"[{datetime.datetime.now():%H:%M:%S}] "
                    f"{course_watch.fit(course.course_no, 10)} "
                    f"{course_watch.fit(course.course_name, 26)} "
                    f"名額被選走，已額滿 "
                    f"{course.cur_member}/{course.member_limit}",
                    course_watch.RED))

            self.had_vacancy[course.course_no] = has_vacancy


def main() -> None:
    """命令列進入點。"""
    parser = course_watch.build_parser(
        "終端機版搶課監控：課程一出現空位就大字提醒並發出音效"
        "（只提醒，不會自動加選）。",
        default_interval=3.0,
    )
    parser.add_argument("--no-sound", action="store_true", help="停用提示音")
    args = parser.parse_args()
    printer = course_watch.setup_output(args)

    if args.interval < 1:
        parser.error("每輪週期請勿小於 1 秒，以免被選課系統限流 (429)。")

    client = course_lookup.CourseClient()
    try:
        rules, semester, is_old = asyncio.run(
            course_watch.prepare(client, args, printer))
    except course_filter.RuleError as error:
        parser.exit(2, printer.color(f"規則錯誤：{error}\n", course_watch.RED))

    watcher = Watcher(
        client=client,
        rules=rules,
        semester=semester,
        interval=args.interval,
        printer=printer,
        sound=not args.no_sound,
        list_all=args.list,
    )

    try:
        if is_old:
            asyncio.run(
                course_watch.list_once(client, rules, semester, printer))
            return
        asyncio.run(watcher.run())
    except KeyboardInterrupt:
        printer.clear_status()
        printer.line(printer.color(
            f"已停止監控（共提醒 {watcher.alerts} 次）。", course_watch.CYAN))


if __name__ == "__main__":
    main()

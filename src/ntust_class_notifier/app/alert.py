"""搶課監控：課程一出現空位就提醒。

預設只查詢公開的課程查詢 API；.env 明確設定 AUTO_ENROLL=true 又填了帳密
時，會連同自動加選一起做（跟 ntust-watch、ntust-notify 同一套規則）。
"""

import dataclasses
import datetime
import logging
import time

from ntust_class_notifier.app import enroll as enroll_app
from ntust_class_notifier.app import monitor
from ntust_class_notifier.app import search
from ntust_class_notifier.clients import course_api
from ntust_class_notifier.clients import sound
from ntust_class_notifier.core import models
from ntust_class_notifier.core import periods
from ntust_class_notifier.core import ruleset
from ntust_class_notifier.ui import console
from ntust_class_notifier.ui import report

logger = logging.getLogger(__name__)


# 第一輪就有空位的課程超過這個數量時，只在清單標記不逐一跳提醒。
FIRST_ROUND_ALERT_LIMIT = 3


@dataclasses.dataclass
class Watcher:
    """搶課監控主體：負責查詢與判斷，提醒橫幅與規則回顯交給 ui.report。

    Attributes:
        client: 課程查詢客戶端。
        rules: 篩選規則。
        semester: 規則沒指定學期時採用的學期。
        interval: 每輪週期秒數。
        printer: 終端機輸出器。
        sound: 是否播放提示音。
        list_all: 啟動時是否列出全部課程。
        enroller: 自動加選器，None 代表只提醒不加選。
        had_vacancy: 每門課上一輪是否有空位。
        alerts: 已經提醒過幾次。
        rounds: 已經跑過幾輪。
        dept_skipped: 這一輪是否因課程過多而跳過系所名額檢查。
    """

    client: course_api.CourseClient
    rules: list[ruleset.Rule]
    semester: str
    interval: float
    printer: console.Printer
    sound: bool = True
    list_all: bool = False
    enroller: enroll_app.AutoEnroller | None = None
    had_vacancy: dict[str, bool] = dataclasses.field(default_factory=dict)
    alerts: int = 0
    rounds: int = 0
    dept_skipped: bool = False

    def beep(self, sound_type: str) -> None:
        """播放提示音。

        Args:
            sound_type: sound.play_sound() 接受的音效類型。
        """
        if self.sound:
            sound.play_sound(sound_type)

    def selection_link(self) -> str:
        """依今天落在哪一段選課時程回傳選課系統連結。

        看的是日期而不是「現在收不收加選」：半夜看到提醒的人還是需要那個
        網址，明天九點才點得下去。

        Returns:
            對應時段的網址；日期不在任何選課時程內時兩個都給。
        """
        # 連結看的是「今天屬於哪一段」，不是「現在收不收加選」：晚上看到
        # 提醒的人還是需要那個網址，明天九點才點得下去。
        return periods.get_period_link(periods.get_scheduled_period())

    def status(self, courses: list[models.Course]) -> None:
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
        checked: list[tuple[search.Match, bool, str]],
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
            f"（每 {self.interval:g} 秒；Ctrl+C 結束）──", console.CYAN))
        report.print_rules(printer, self.rules, counts, self.semester)

        if self.list_all or len(checked) <= report.LIST_LIMIT:
            for match, has_vacancy, _ in checked:
                course = match.course
                mark = (
                    printer.color("有空位", console.GREEN)
                    if has_vacancy
                    else printer.color("額滿", console.RED)
                )
                members = f"{course.cur_member}/{course.member_limit}"
                printer.line(
                    f"   {console.fit(course.course_no, 10)} "
                    f"{console.fit(course.course_name, 26)} "
                    f"{console.fit(course.teacher, 12)} "
                    f"{console.fit(','.join(course.node), 14)} "
                    f"{console.fit(members, 9)}"
                    f"{f'[{match.dept}] ' if match.dept else ''}{mark}"
                )
        else:
            printer.line(printer.color(
                f"   （課程超過 {report.LIST_LIMIT} 門，清單省略；"
                f"加 --list 可列出全部）", console.CYAN))

        printer.line(
            f"   目前有空位：{printer.color(str(vacant), console.GREEN)} 門　"
            f"目前時段：{periods.describe()}"
        )
        if self.dept_skipped:
            printer.line(printer.color(
                f"   課程超過 {ruleset.DEPT_LOOKUP_LIMIT} 門，"
                f"已跳過系所名額檢查", console.YELLOW))
        # 沒啟用自動加選時連提都不提：這支也拿來展示，畫面上不需要出現一個
        # 沒有在運作的功能。
        tail = "並自動送出加選" if self.enroller else ""
        printer.line(printer.color(
            f"── 之後只在課程「由額滿變成有空位」時提醒{tail} ──",
            console.CYAN))

    async def check_vacancy(
        self, match: search.Match, dept_allowed: bool
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
            result = await search.search(
                self.client, self.rules, self.semester)

            if result.failed:
                # 查詢失敗不做判斷，否則會把「查不到」誤判成額滿或空位。
                self.printer.line(self.printer.color(
                    f"[{datetime.datetime.now():%H:%M:%S}] 查詢失敗："
                    f"{'、'.join(result.failed)}，本輪略過", console.YELLOW))
                await monitor.pace(self.interval, started)
                continue

            if not result.matches:
                if self.rounds == 1:
                    report.print_rules(
                        self.printer, self.rules, result.counts, self.semester)
                    report.zero_result_hint(
                        self.printer, self.rules, self.semester)
                return

            # 系所名額一門課要一次請求，課程太多就整輪跳過。
            dept_allowed = (
                len(result.matches) <= ruleset.DEPT_LOOKUP_LIMIT
            )
            self.dept_skipped = (
                not dept_allowed and any(match.dept for match in result.matches)
            )
            checked = [
                (match, *await self.check_vacancy(match, dept_allowed))
                for match in result.matches
            ]

            # 空位判斷已經扣掉系所額滿的假空位，直接拿來加選最準。
            if self.enroller:
                notes = await self.enroller.on_round({
                    match.course.course_no
                    for match, has_vacancy, _ in checked if has_vacancy
                })
                for note in notes:
                    self.printer.line(self.printer.color(
                        f"[{datetime.datetime.now():%H:%M:%S}] {note}",
                        console.GREEN))

            if self.rounds == 1 and self._handle_first_round(
                    checked, result.counts):
                self.status(result.courses)
                announced = await monitor.pace(self.interval, started)
                continue

            self._report(checked)
            self.status(result.courses)

            effective = await monitor.pace(self.interval, started)
            if effective > announced * 1.5 or effective < announced / 1.5:
                self.printer.line(self.printer.color(
                    f"   每輪週期自動調整為 {effective:.0f} 秒"
                    "（查詢耗時變化）。", console.YELLOW))
                announced = effective

    def _handle_first_round(
        self,
        checked: list[tuple[search.Match, bool, str]],
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
        self, checked: list[tuple[search.Match, bool, str]]
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
                report.print_alert(self.printer, course, dept_info,
                                   self.selection_link())
                self.beep("vacancy")
            elif not has_vacancy and was_open:
                # 名額被別人選走：紅色。
                self.printer.line(self.printer.color(
                    f"[{datetime.datetime.now():%H:%M:%S}] "
                    f"{console.fit(course.course_no, 10)} "
                    f"{console.fit(course.course_name, 26)} "
                    f"名額被選走，已額滿 "
                    f"{course.cur_member}/{course.member_limit}",
                    console.RED))

            self.had_vacancy[course.course_no] = has_vacancy

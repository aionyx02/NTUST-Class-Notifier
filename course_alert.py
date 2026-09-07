"""
終端機版搶課監控（不需要 Discord）。

每隔數秒檢查鎖定的課程，一旦出現空位就在終端機顯示大字提醒並發出音效，
剩下的加選動作由你自己在選課系統上完成——本腳本只查詢與提醒，不會自動送出加選。

篩選規則寫成 `欄位:值`，多條用 ; 分隔；命令列有給就取代 .env 的 LOOK_UP_CLASSES，
兩邊都沒有就監控全校所有課程。

用法:
    uv run course_alert.py 課號:CS1003301                    # 監控單一課程
    uv run course_alert.py CS1003301 EE21                    # 純英數裸字視為課號
    uv run course_alert.py 課號:PE139A053 系所:資訊工程系三   # 一併檢查系所名額
    uv run course_alert.py 課名:程式設計 學制:大學部          # 課名子字串 + 學制
    uv run course_alert.py                                   # 讀 .env；.env 也空就是全校
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from course_filter import DEPT_LOOKUP_LIMIT, Match, Rule, RuleError, search
from course_lookup import Course, CourseClient
from course_selector import play_sound
from course_watch import (BOLD, CYAN, GREEN, LIST_LIMIT, RED, YELLOW,
                          Printer, build_parser, fit, list_once, pace, prepare,
                          print_rules, setup_output, zero_result_hint)
from selection_period import (DEPT_SELECT_LINK, OPEN_SELECT_LINK,
                              get_current_period, get_period_name)

logger = logging.getLogger(__name__)

FIRST_ROUND_ALERT_LIMIT = 3  # 第一輪就有空位的課程超過這個數量時，只在清單標記不逐一跳提醒


@dataclass
class Watcher:
    """搶課監控主體：只負責查詢、判斷與提醒。"""
    client: CourseClient
    rules: list[Rule]
    semester: str
    interval: float
    printer: Printer
    sound: bool = True
    list_all: bool = False
    had_vacancy: dict[str, bool] = field(default_factory=dict)
    alerts: int = 0
    rounds: int = 0
    dept_skipped: bool = False

    # ---------- 小工具 ---------- #
    def beep(self, sound_type: str) -> None:
        if self.sound:
            play_sound(sound_type)

    def selection_link(self) -> str:
        period = get_current_period()
        if period == "dept":
            return DEPT_SELECT_LINK
        if period == "open":
            return OPEN_SELECT_LINK
        return f"{DEPT_SELECT_LINK} / {OPEN_SELECT_LINK}"

    # ---------- 輸出 ---------- #
    def alert(self, course: Course, dept_info: str) -> None:
        """有空位（有人釋出名額）時的大字提醒，整段綠色。"""
        p = self.printer
        bar = "═" * 64
        p.line("")
        p.line(p.color(bar, GREEN))
        p.line(p.color(f"  有空位！ {course.course_no}  {course.course_name}", BOLD + GREEN))
        p.line(p.color(f"     授課老師：{course.teacher}    "
                       f"上課時間：{','.join(course.node) or '未定'}", GREEN))
        p.line(p.color(f"     目前人數：{course.cur_member} / {course.member_limit}"
                       f"    剩餘 {course.vacancy} 個名額", GREEN))
        if dept_info:
            p.line(p.color(f"     系所名額：{dept_info}", GREEN))
        p.line(p.color(f"     選課連結：{self.selection_link()}", GREEN))
        p.line(p.color(f"     {datetime.now():%Y-%m-%d %H:%M:%S}", GREEN))
        p.line(p.color(bar, GREEN))

    def status(self, courses: list[Course]) -> None:
        """底部狀態列：課程少時逐門顯示人數，課程多時只顯示統計。"""
        now = f"{datetime.now():%H:%M:%S}"
        vacant = [c for c in courses if c.member_limit > 0 and c.vacancy > 0]
        if len(courses) <= 6:
            detail = " · ".join(f"{c.course_no} {c.cur_member}/{c.member_limit}" for c in courses)
        else:
            detail = f"{len(courses)} 門課程・有空位 {len(vacant)} 門"
        self.printer.status(f"[{now}] {detail}・第 {self.rounds} 輪・已提醒 {self.alerts} 次")

    def print_header(self, results: list[tuple[Match, bool, str]], counts: list[int]) -> None:
        p = self.printer
        vacant = sum(1 for _, has_vacancy, _ in results if has_vacancy)
        p.line(p.color(f"── 搶課監控 {len(results)} 門課程"
                       f"（每 {self.interval:g} 秒；Ctrl+C 結束）──", CYAN))
        print_rules(p, self.rules, counts, self.semester)
        if self.list_all or len(results) <= LIST_LIMIT:
            for match, has_vacancy, _ in results:
                course = match.course
                mark = p.color("有空位", GREEN) if has_vacancy else p.color("額滿", RED)
                p.line(f"   {fit(course.course_no, 10)} {fit(course.course_name, 26)} "
                       f"{fit(course.teacher, 12)} {fit(','.join(course.node), 14)} "
                       f"{fit(f'{course.cur_member}/{course.member_limit}', 9)}"
                       f"{f'[{match.dept}] ' if match.dept else ''}{mark}")
        else:
            p.line(p.color(f"   （課程超過 {LIST_LIMIT} 門，清單省略；加 --list 可列出全部）", CYAN))
        p.line(f"   目前有空位：{p.color(str(vacant), GREEN)} 門　"
               f"目前時段：{get_period_name(get_current_period())}")
        if self.dept_skipped:
            p.line(p.color(f"   課程超過 {DEPT_LOOKUP_LIMIT} 門，已跳過系所名額檢查", YELLOW))
        p.line(p.color("── 之後只在課程「由額滿變成有空位」時提醒（不會自動加選）──", CYAN))

    # ---------- 判斷 ---------- #
    async def check_vacancy(self, match: Match, dept_allowed: bool) -> tuple[bool, str]:
        """
        判斷課程目前是否真的可以選。
        :return (是否有空位, 系所名額說明字串)
        """
        course = match.course
        if course.member_limit <= 0 or course.cur_member >= course.member_limit:
            return False, ""
        if not match.dept or not dept_allowed:
            return True, ""

        # 總人數有空位還要看系所名額有沒有滿，否則是搶不到的假空位
        result = await self.client.get_department_limit(self.semester, course.course_no, match.dept)
        if result is None:
            return True, ""
        persons, restrict = result
        dept_info = f"{persons} / {restrict}（{match.dept}）"
        if persons >= restrict:
            logger.debug(f"{course.course_no} 總數有空位但系所額滿：{dept_info}")
            return False, dept_info
        return True, dept_info

    # ---------- 主迴圈 ---------- #
    async def run(self) -> None:
        announced = self.interval

        while True:
            self.rounds += 1
            started = time.monotonic()
            result = await search(self.client, self.rules, self.semester)

            if result.failed:
                # 查詢失敗不做判斷，否則會把「查不到」誤判成額滿或空位
                self.printer.line(self.printer.color(
                    f"[{datetime.now():%H:%M:%S}] 查詢失敗：{'、'.join(result.failed)}，本輪略過", YELLOW))
                await pace(self.interval, started)
                continue

            if not result.matches:
                if self.rounds == 1:
                    print_rules(self.printer, self.rules, result.counts, self.semester)
                    zero_result_hint(self.printer, self.rules, self.semester)
                return

            # 系所名額一門課一次請求，課程太多就整輪跳過
            dept_allowed = len(result.matches) <= DEPT_LOOKUP_LIMIT
            self.dept_skipped = not dept_allowed and any(m.dept for m in result.matches)

            checked = [(m, *await self.check_vacancy(m, dept_allowed)) for m in result.matches]

            if self.rounds == 1:
                self.print_header(checked, result.counts)
                vacant = [row for row in checked if row[1]]
                if len(vacant) > FIRST_ROUND_ALERT_LIMIT:
                    # 一開始就有一堆空位（多半是範圍很大的規則），只記錄狀態不洗版
                    for match, has_vacancy, _ in checked:
                        self.had_vacancy[match.course.course_no] = has_vacancy
                    self.beep("vacancy")
                    self.status(result.courses)
                    announced = await pace(self.interval, started)
                    continue

            for match, has_vacancy, dept_info in checked:
                course = match.course
                was_open = self.had_vacancy.get(course.course_no, False)

                # 只在「額滿 → 有空位」的瞬間提醒，避免每輪洗版
                if has_vacancy and not was_open:
                    self.alerts += 1
                    self.alert(course, dept_info)
                    self.beep("vacancy")
                elif not has_vacancy and was_open:
                    # 名額被別人選走：紅色
                    self.printer.line(self.printer.color(
                        f"[{datetime.now():%H:%M:%S}] {fit(course.course_no, 10)} "
                        f"{fit(course.course_name, 26)} 名額被選走，已額滿 "
                        f"{course.cur_member}/{course.member_limit}", RED))

                self.had_vacancy[course.course_no] = has_vacancy

            self.status(result.courses)
            effective = await pace(self.interval, started)
            if effective > announced * 1.5 or effective < announced / 1.5:
                self.printer.line(self.printer.color(
                    f"   每輪週期自動調整為 {effective:.0f} 秒（查詢耗時變化）。", YELLOW))
                announced = effective


def main() -> None:
    parser = build_parser(
        "終端機版搶課監控：課程一出現空位就大字提醒並發出音效（只提醒，不會自動加選）。",
        default_interval=3.0)
    parser.add_argument("--no-sound", action="store_true", help="停用提示音")
    args = parser.parse_args()
    printer = setup_output(args)

    if args.interval < 1:
        parser.error("每輪週期請勿小於 1 秒，以免被選課系統限流 (429)。")

    client = CourseClient()
    try:
        rules, semester, is_old = asyncio.run(prepare(client, args, printer))
    except RuleError as e:
        parser.exit(2, printer.color(f"規則錯誤：{e}\n", RED))

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
            asyncio.run(list_once(client, rules, semester, printer))
            return
        asyncio.run(watcher.run())
    except KeyboardInterrupt:
        printer.clear_status()
        printer.line(printer.color(f"已停止監控（共提醒 {watcher.alerts} 次）。", CYAN))


if __name__ == "__main__":
    main()

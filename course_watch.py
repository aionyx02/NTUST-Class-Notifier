"""
課程人數即時監看器。

每隔數秒查詢一次課程人數，只在人數有變動時輸出一行（整行上色）：
  - 人數變多（被別人選走）：紅色
  - 人數變少（有人退選）：綠色
  - 沒有變動：不輸出任何內容

篩選規則寫成 `欄位:值`，多條用 ; 分隔；命令列有給就取代 .env 的 LOOK_UP_CLASSES，
兩邊都沒有就監看全校所有課程。

用法:
    uv run course_watch.py 課號:CS 學制:大學部      # CS 開頭的大學部課程
    uv run course_watch.py CS                        # 純英數裸字視為課號
    uv run course_watch.py 課名:程式設計 老師:姚     # 課名與老師都是子字串比對
    uv run course_watch.py                           # 讀 .env；.env 也空就是全校
"""

import argparse
import asyncio
import logging
import os
import sys
import time
import unicodedata
from datetime import datetime

import dotenv

from course_filter import (Match, Rule, RuleError, field_help, parse_rules,
                           resolve_semester, search)
from course_lookup import Course, CourseClient, current_semester

LIST_LIMIT = 50  # 課程超過這個數量就不逐一列出啟動清單（--list 可強制列出）

# ========== 終端機色彩 ========== #
RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[91m"
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
DIM = "\033[2m"

logger = logging.getLogger(__name__)


def setup_terminal() -> bool:
    """
    設定終端機：UTF-8 輸出 + Windows ANSI 色彩支援。
    :return 是否為互動式終端機 (決定要不要顯示底部即時狀態列；色彩不受此影響)
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if os.name == "nt":
        # 開啟 ENABLE_VIRTUAL_TERMINAL_PROCESSING，讓舊版 conhost 也能顯示 ANSI 色碼
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:  # pragma: no cover - 取不到 console 時直接忽略
            pass
    return sys.stdout.isatty()


# ========== 版面排版 ========== #
def display_width(text: str) -> int:
    """計算字串在等寬終端機上的顯示寬度 (全形字算 2 格)。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def fit(text: str, width: int) -> str:
    """把字串裁切或補齊到指定的顯示寬度。"""
    if display_width(text) > width:
        out = ""
        for ch in text:
            if display_width(out) + display_width(ch) > width - 1:
                break
            out += ch
        text = out + "…"
    return text + " " * max(width - display_width(text), 0)


class Printer:
    """負責輸出：變動明細逐行往上堆疊，底部保留一行即時狀態列。"""

    def __init__(self, interactive: bool, use_color: bool):
        self.interactive = interactive
        self.use_color = use_color
        self._status_shown = False

    def color(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.use_color else text

    def line(self, text: str) -> None:
        """輸出一行內容（會先清掉狀態列）。"""
        prefix = "\r\033[K" if self.interactive and self._status_shown else ""
        sys.stdout.write(f"{prefix}{text}\n")
        sys.stdout.flush()
        self._status_shown = False

    def status(self, text: str) -> None:
        """更新底部狀態列（非互動式終端機則忽略，避免污染 log）。"""
        if not self.interactive:
            return
        sys.stdout.write(f"\r\033[K{self.color(text, DIM)}")
        sys.stdout.flush()
        self._status_shown = True

    def clear_status(self) -> None:
        if self.interactive and self._status_shown:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            self._status_shown = False


async def pace(interval: float, started: float) -> float:
    """
    把 interval 當成「每輪週期」並自動保護：查詢比間隔還久時，
    下一輪改用「查詢耗時 × 2」，免得請求疊在一起。
    :return 實際採用的週期秒數
    """
    elapsed = time.monotonic() - started
    effective = max(interval, elapsed * 2)
    await asyncio.sleep(max(0.0, effective - elapsed))
    return effective


def format_course(course: Course) -> str:
    """課程的固定寬度描述：代碼 + 名稱 + 老師 + 上課時間。"""
    return (
        f"{fit(course.course_no, 10)} "
        f"{fit(course.course_name, 26)} "
        f"{fit(course.teacher, 12)} "
        f"{fit(','.join(course.node), 14)}"
    )


def format_change(printer: Printer, old: Course, new: Course) -> str:
    """組出一行人數變動訊息：被別人選走整行紅色、有人退選整行綠色。"""
    increased = new.cur_member > old.cur_member
    code = RED if increased else GREEN
    label = "被選走" if increased else "有人退選"
    delta = new.cur_member - old.cur_member

    tail = f"剩 {new.vacancy}" if new.vacancy > 0 else "額滿"
    if new.member_limit != old.member_limit:
        tail += f"，上限 {old.member_limit} → {new.member_limit}"

    return printer.color(
        f"[{datetime.now():%H:%M:%S}] {format_course(new)} "
        f"{old.cur_member} → {new.cur_member}/{new.member_limit} ({delta:+d})  {label}，{tail}",
        code,
    )


def show_vacancy_change(old: Course, new: Course) -> bool:
    """
    「只看空位」的判定：變動後還有空位就顯示，
    以及這次剛好把最後一個名額填滿的那一筆也要顯示（否則會靜靜消失）。
    """
    return new.vacancy > 0 or old.vacancy > 0


def zero_result_hint(printer: Printer, rules: list[Rule], semester: str) -> None:
    """所有規則都命中 0 門時，印出可能的原因與放寬建議。"""
    printer.line(printer.color("錯誤：沒有任何課程符合這些規則。", RED))
    hints = [f"學期 {semester} 是否正確？可用 -s 指定，或確認該學期課表已公告。"]
    if any(rule.names or rule.teachers for rule in rules):
        hints.append("課名與老師是「子字串」比對，試試更短的關鍵字（例如只打「程式」）。")
    if any(rule.codes for rule in rules):
        hints.append("課號是「前綴」比對，只吃開頭（1003 這種中段數字查不到）。")
    if any(rule.levels for rule in rules):
        hints.append("學制與其他條件是「且」的關係，試著把 學制 拿掉再查一次。")
    for hint in hints:
        printer.line(printer.color(f"建議：{hint}", YELLOW))


def print_rules(printer: Printer, rules: list[Rule], counts: list[int], semester: str) -> None:
    """逐條回顯規則的中文解讀與命中門數。"""
    for index, (rule, count) in enumerate(zip(rules, counts), start=1):
        printer.line(printer.color(
            f"   規則 {index}：{rule.describe()}", CYAN) + f"  →  {count} 門")
    printer.line(printer.color(f"   學期 {semester}", DIM))


# ========== 監看主迴圈 ========== #
async def watch(
        client: CourseClient,
        rules: list[Rule],
        semester: str,
        interval: float,
        printer: Printer,
        list_all: bool = False,
) -> None:
    previous: dict[str, Course] = {}
    filters: dict[str, Match] = {}
    changes = 0
    rounds = 0
    announced_interval = interval

    while True:
        rounds += 1
        started = time.monotonic()
        result = await search(client, rules, semester)
        now = f"{datetime.now():%H:%M:%S}"

        if result.failed:
            # 查詢失敗時不做比對，否則會把「查不到」誤判成人數歸零或課程消失
            printer.line(printer.color(
                f"[{now}] 查詢失敗：{'、'.join(result.failed)}，本輪略過", YELLOW))
            await pace(interval, started)
            continue

        current = {m.course.course_no: m.course for m in result.matches}
        filters = {m.course.course_no: m for m in result.matches}

        if not previous:
            if not current:
                print_rules(printer, rules, result.counts, semester)
                zero_result_hint(printer, rules, semester)
                return
            printer.line(printer.color(
                f"── 監看 {len(current)} 門課程（每 {interval:g} 秒；Ctrl+C 結束）──", CYAN))
            print_rules(printer, rules, result.counts, semester)
            if list_all or len(current) <= LIST_LIMIT:
                for course in current.values():
                    printer.line(f"   {format_course(course)} {course.cur_member}/{course.member_limit}")
            else:
                printer.line(printer.color(
                    f"   （課程超過 {LIST_LIMIT} 門，清單省略；加 --list 可列出全部）", DIM))
            printer.line(printer.color("── 以下只顯示人數變動 ──", CYAN))
            previous = current
            announced_interval = await pace(interval, started)
            if announced_interval > interval:
                printer.line(printer.color(
                    f"   查詢耗時較久，每輪週期自動放寬為 {announced_interval:.0f} 秒。", YELLOW))
            continue

        for course_no, course in current.items():
            old = previous.get(course_no)
            if old is None:
                printer.line(printer.color(
                    f"[{now}] {format_course(course)} "
                    f"{course.cur_member}/{course.member_limit}  新增課程", CYAN))
                changes += 1
            elif course.cur_member != old.cur_member:
                only_vacant = filters[course_no].only_vacant
                if only_vacant and not show_vacancy_change(old, course):
                    continue
                printer.line(format_change(printer, old, course))
                changes += 1

        for course_no in previous.keys() - current.keys():
            printer.line(printer.color(
                f"[{now}] {format_course(previous[course_no])} 課程已從查詢結果消失", YELLOW))
            changes += 1

        previous = current
        printer.status(f"[{now}] 監看 {len(current)} 門課程・第 {rounds} 輪・已記錄 {changes} 筆變動")
        effective = await pace(interval, started)
        if effective > announced_interval * 1.5 or effective < announced_interval / 1.5:
            printer.line(printer.color(
                f"[{now}] 每輪週期自動調整為 {effective:.0f} 秒（查詢耗時變化）。", YELLOW))
            announced_interval = effective


async def list_once(client: CourseClient, rules: list[Rule], semester: str, printer: Printer) -> None:
    """舊學期只列一次就結束，不進監看迴圈。"""
    result = await search(client, rules, semester)
    if result.failed:
        printer.line(printer.color(f"查詢失敗：{'、'.join(result.failed)}", YELLOW))
        return
    printer.line(printer.color(f"── 舊學期 {semester}，只列出不監看 ──", CYAN))
    print_rules(printer, rules, result.counts, semester)
    if not result.courses:
        zero_result_hint(printer, rules, semester)
        return
    for course in result.courses:
        printer.line(f"   {format_course(course)} {course.cur_member}/{course.member_limit}")
    printer.line(printer.color(f"── 共 {len(result.courses)} 門 ──", CYAN))


# ========== 規則來源 ========== #
def load_rules(queries: list[str], printer: Printer) -> list[Rule]:
    """命令列有給就用命令列，否則讀 .env 的 LOOK_UP_CLASSES；都沒有代表全校。"""
    if queries:
        return parse_rules(queries)

    dotenv.load_dotenv()
    env_rules = os.environ.get("LOOK_UP_CLASSES", "")
    if env_rules.strip():
        printer.line(printer.color("（使用 .env 的 LOOK_UP_CLASSES 規則）", DIM))
        return parse_rules(env_rules)
    return [Rule()]


def effective_semesters(rules: list[Rule], default: str) -> set[str]:
    return {rule.semester or default for rule in rules}


def build_parser(description: str, default_interval: float) -> argparse.ArgumentParser:
    """兩支腳本共用的參數定義，--help 直接附上完整欄位表。"""
    setup_terminal()  # argparse 可能立刻印出 --help，先確保輸出是 UTF-8
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=field_help(),
    )
    parser.add_argument("queries", nargs="*",
                        help="篩選規則（欄位:值，; 分隔多條）；省略時讀 .env 的 LOOK_UP_CLASSES")
    parser.add_argument("-i", "--interval", type=float, default=default_interval,
                        help=f"每輪週期秒數 (預設 {default_interval:g}；查詢較久時會自動放寬)")
    parser.add_argument("-s", "--semester", default=None, help="學期代碼，例如 1151 (預設自動判斷)")
    parser.add_argument("--list", action="store_true",
                        help=f"啟動時列出全部課程（預設超過 {LIST_LIMIT} 門就省略）")
    parser.add_argument("--no-color", action="store_true", help="停用色彩輸出")
    parser.add_argument("-d", "--debug", action="store_true", help="顯示除錯訊息")
    return parser


def setup_output(args) -> Printer:
    interactive = setup_terminal()
    # 色彩不綁 isatty：PyCharm 等執行視窗不是 tty 但看得懂 ANSI 色碼
    use_color = not args.no_color and not os.environ.get("NO_COLOR")
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return Printer(interactive, use_color)


async def prepare(client: CourseClient, args, printer: Printer) -> tuple[list[Rule], str, bool]:
    """
    解析規則並決定學期。
    :return (規則列表, 預設學期, 是否為舊學期唯讀模式)
    """
    rules = load_rules(args.queries, printer)
    explicit = args.semester or ""
    if not explicit and rules and all(rule.semester for rule in rules):
        explicit = rules[0].semester

    semester, note = await resolve_semester(client, explicit)
    if note:
        printer.line(printer.color(note, YELLOW))

    latest = current_semester()
    is_old = all(sem < latest for sem in effective_semesters(rules, semester))
    return rules, semester, is_old


def main() -> None:
    parser = build_parser(
        "每隔數秒監看課程人數，只在有人選走 (紅) 或退選 (綠) 時輸出。", default_interval=5.0)
    args = parser.parse_args()
    printer = setup_output(args)

    if args.interval < 1:
        parser.error("每輪週期請勿小於 1 秒，以免被選課系統限流 (429)。")

    client = CourseClient()
    try:
        rules, semester, is_old = asyncio.run(prepare(client, args, printer))
    except RuleError as e:
        parser.exit(2, printer.color(f"規則錯誤：{e}\n", RED))

    try:
        if is_old:
            asyncio.run(list_once(client, rules, semester, printer))
            return
        asyncio.run(watch(client, rules, semester, args.interval, printer, args.list))
    except KeyboardInterrupt:
        printer.clear_status()
        printer.line(printer.color("已停止監看。", CYAN))


if __name__ == "__main__":
    main()

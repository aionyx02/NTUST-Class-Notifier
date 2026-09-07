"""終端機課程人數監看器。

每隔數秒查詢一次課程人數，只在人數有變動時輸出一行（整行上色）：被別人
選走是紅色、有人退選是綠色，沒有變動就不輸出。

篩選規則寫成 `欄位:值`，多條用 `;` 隔開；命令列有給就取代 .env 的
LOOK_UP_CLASSES，兩邊都沒有就監看全校所有課程。

用法：
    uv run course_watch.py 課號:CS 學制:大學部
    uv run course_watch.py CS
    uv run course_watch.py 課名:程式設計 老師:姚
    uv run course_watch.py
"""

import argparse
import asyncio
import datetime
import logging
import os
import sys
import time
import unicodedata

import dotenv

import course_filter
import course_lookup

# 課程超過這個數量就不逐一列出啟動清單，可用 --list 強制列出。
LIST_LIMIT = 50

# 終端機色碼。
RESET = "\033[0m"
BOLD = "\033[1m"
RED = "\033[91m"
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
DIM = "\033[2m"

logger = logging.getLogger(__name__)


def setup_terminal() -> bool:
    """設定終端機的輸出編碼與 ANSI 色彩支援。

    Returns:
        是否為互動式終端機。這只決定要不要顯示底部的即時狀態列，色彩不受
        它影響（PyCharm 等執行視窗不是 tty 但看得懂 ANSI 色碼）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if os.name == "nt":
        # 開啟 ENABLE_VIRTUAL_TERMINAL_PROCESSING，讓舊版 conhost 也能顯示
        # ANSI 色碼。
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:  # noqa: BLE001 - 取不到 console 時直接忽略
            pass
    return sys.stdout.isatty()


def display_width(text: str) -> int:
    """計算字串在等寬終端機上佔幾格。

    Args:
        text: 要量測的字串。

    Returns:
        顯示寬度，全形字算兩格。
    """
    return sum(
        2 if unicodedata.east_asian_width(char) in "WF" else 1
        for char in text
    )


def fit(text: str, width: int) -> str:
    """把字串裁切或補齊到指定的顯示寬度。

    Args:
        text: 原始字串。
        width: 目標顯示寬度。

    Returns:
        寬度剛好的字串，過長時以省略號結尾。
    """
    if display_width(text) > width:
        out = ""
        for char in text:
            if display_width(out) + display_width(char) > width - 1:
                break
            out += char
        text = out + "…"
    return text + " " * max(width - display_width(text), 0)


class Printer:
    """終端機輸出：明細往上堆疊，底部保留一行即時狀態列。

    Attributes:
        interactive: 是否為互動式終端機，決定要不要顯示狀態列。
        use_color: 是否輸出 ANSI 色碼。
    """

    def __init__(self, interactive: bool, use_color: bool):
        """初始化輸出器。

        Args:
            interactive: 是否為互動式終端機。
            use_color: 是否輸出色彩。
        """
        self.interactive = interactive
        self.use_color = use_color
        self._status_shown = False

    def color(self, text: str, code: str) -> str:
        """替字串加上色碼。

        Args:
            text: 要上色的字串。
            code: ANSI 色碼常數。

        Returns:
            上色後的字串；停用色彩時原樣回傳。
        """
        return f"{code}{text}{RESET}" if self.use_color else text

    def line(self, text: str) -> None:
        """輸出一行內容，必要時先清掉狀態列。

        Args:
            text: 要輸出的內容。
        """
        prefix = "\r\033[K" if self.interactive and self._status_shown else ""
        sys.stdout.write(f"{prefix}{text}\n")
        sys.stdout.flush()
        self._status_shown = False

    def status(self, text: str) -> None:
        """更新底部狀態列。

        非互動式終端機會忽略，避免污染重新導向的輸出。

        Args:
            text: 狀態列內容。
        """
        if not self.interactive:
            return
        sys.stdout.write(f"\r\033[K{self.color(text, DIM)}")
        sys.stdout.flush()
        self._status_shown = True

    def clear_status(self) -> None:
        """清掉底部狀態列。"""
        if self.interactive and self._status_shown:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
            self._status_shown = False


async def pace(interval: float, started: float) -> float:
    """睡到這一輪的週期結束，並在查詢過久時自動放寬。

    Args:
        interval: 使用者設定的每輪週期秒數。
        started: 這一輪開始時的 time.monotonic()。

    Returns:
        實際採用的週期秒數；查詢比週期還久時會是「查詢耗時 × 2」。
    """
    elapsed = time.monotonic() - started
    effective = max(interval, elapsed * 2)
    await asyncio.sleep(max(0.0, effective - elapsed))
    return effective


def format_course(course: course_lookup.Course) -> str:
    """把課程排成固定寬度的一行。

    Args:
        course: 要顯示的課程。

    Returns:
        課號、課名、老師、節次四欄對齊後的字串。
    """
    return (
        f"{fit(course.course_no, 10)} "
        f"{fit(course.course_name, 26)} "
        f"{fit(course.teacher, 12)} "
        f"{fit(','.join(course.node), 14)}"
    )


def format_change(
    printer: Printer,
    old: course_lookup.Course,
    new: course_lookup.Course,
) -> str:
    """組出一行人數變動訊息。

    Args:
        printer: 用來上色的輸出器。
        old: 上一輪的課程狀態。
        new: 這一輪的課程狀態。

    Returns:
        整行上色的訊息，被別人選走是紅色、有人退選是綠色。
    """
    increased = new.cur_member > old.cur_member
    code = RED if increased else GREEN
    label = "被選走" if increased else "有人退選"
    delta = new.cur_member - old.cur_member

    tail = f"剩 {new.vacancy}" if new.vacancy > 0 else "額滿"
    if new.member_limit != old.member_limit:
        tail += f"，上限 {old.member_limit} → {new.member_limit}"

    return printer.color(
        f"[{datetime.datetime.now():%H:%M:%S}] {format_course(new)} "
        f"{old.cur_member} → {new.cur_member}/{new.member_limit} "
        f"({delta:+d})  {label}，{tail}",
        code,
    )


def show_vacancy_change(
    old: course_lookup.Course, new: course_lookup.Course
) -> bool:
    """判斷「只看空位」時這筆變動要不要顯示。

    Args:
        old: 上一輪的課程狀態。
        new: 這一輪的課程狀態。

    Returns:
        變動後還有空位就顯示；這次剛好把最後一個名額填滿的那一筆也要顯示，
        否則它會靜靜消失。
    """
    return new.vacancy > 0 or old.vacancy > 0


def zero_result_hint(
    printer: Printer, rules: list[course_filter.Rule], semester: str
) -> None:
    """所有規則都命中 0 門時，印出可能原因與放寬建議。

    Args:
        printer: 輸出器。
        rules: 使用者給的規則。
        semester: 這次查詢的學期。
    """
    printer.line(printer.color("錯誤：沒有任何課程符合這些規則。", RED))
    hints = [
        f"學期 {semester} 是否正確？可用 -s 指定，或確認該學期課表已公告。"
    ]
    if any(rule.names or rule.teachers for rule in rules):
        hints.append("課名與老師是「子字串」比對，試試更短的關鍵字。")
    if any(rule.codes for rule in rules):
        hints.append("課號是「前綴」比對，只吃開頭（1003 這種中段數字查不到）。")
    if any(rule.levels for rule in rules):
        hints.append("學制與其他條件是「且」的關係，試著把 學制 拿掉再查。")
    for hint in hints:
        printer.line(printer.color(f"建議：{hint}", YELLOW))


def print_rules(
    printer: Printer,
    rules: list[course_filter.Rule],
    counts: list[int],
    semester: str,
) -> None:
    """逐條回顯規則的中文解讀與命中門數。

    Args:
        printer: 輸出器。
        rules: 規則列表。
        counts: 每條規則命中的門數。
        semester: 這次查詢的學期。
    """
    for index, (rule, count) in enumerate(zip(rules, counts), start=1):
        printer.line(
            printer.color(f"   規則 {index}：{rule.describe()}", CYAN)
            + f"  →  {count} 門"
        )
    printer.line(printer.color(f"   學期 {semester}", DIM))


async def watch(
    client: course_lookup.CourseClient,
    rules: list[course_filter.Rule],
    semester: str,
    interval: float,
    printer: Printer,
    list_all: bool = False,
) -> None:
    """持續監看課程人數，只輸出有變動的部分。

    Args:
        client: 課程查詢客戶端。
        rules: 篩選規則。
        semester: 規則沒指定學期時採用的學期。
        interval: 每輪週期秒數。
        printer: 輸出器。
        list_all: 啟動時是否列出全部課程。
    """
    previous: dict[str, course_lookup.Course] = {}
    filters: dict[str, course_filter.Match] = {}
    changes = 0
    rounds = 0
    announced = interval

    while True:
        rounds += 1
        started = time.monotonic()
        result = await course_filter.search(client, rules, semester)
        now = f"{datetime.datetime.now():%H:%M:%S}"

        if result.failed:
            # 查詢失敗時不做比對，否則會把「查不到」誤判成人數歸零。
            printer.line(printer.color(
                f"[{now}] 查詢失敗：{'、'.join(result.failed)}，本輪略過",
                YELLOW))
            await pace(interval, started)
            continue

        current = {
            match.course.course_no: match.course for match in result.matches
        }
        filters = {
            match.course.course_no: match for match in result.matches
        }

        if not previous:
            if not current:
                print_rules(printer, rules, result.counts, semester)
                zero_result_hint(printer, rules, semester)
                return
            _print_header(printer, current, rules, result.counts, semester,
                          interval, list_all)
            previous = current
            announced = await pace(interval, started)
            if announced > interval:
                printer.line(printer.color(
                    f"   查詢耗時較久，每輪週期自動放寬為 {announced:.0f} 秒。",
                    YELLOW))
            continue

        changes += _report_changes(printer, previous, current, filters, now)
        previous = current
        printer.status(
            f"[{now}] 監看 {len(current)} 門課程・第 {rounds} 輪・"
            f"已記錄 {changes} 筆變動"
        )

        effective = await pace(interval, started)
        if effective > announced * 1.5 or effective < announced / 1.5:
            printer.line(printer.color(
                f"[{now}] 每輪週期自動調整為 {effective:.0f} 秒（查詢耗時變化）。",
                YELLOW))
            announced = effective


async def list_once(
    client: course_lookup.CourseClient,
    rules: list[course_filter.Rule],
    semester: str,
    printer: Printer,
) -> None:
    """列出一次結果就結束，供舊學期使用。

    Args:
        client: 課程查詢客戶端。
        rules: 篩選規則。
        semester: 要查詢的學期。
        printer: 輸出器。
    """
    result = await course_filter.search(client, rules, semester)
    if result.failed:
        printer.line(printer.color(
            f"查詢失敗：{'、'.join(result.failed)}", YELLOW))
        return

    printer.line(printer.color(f"── 舊學期 {semester}，只列出不監看 ──", CYAN))
    print_rules(printer, rules, result.counts, semester)
    if not result.courses:
        zero_result_hint(printer, rules, semester)
        return
    for course in result.courses:
        printer.line(
            f"   {format_course(course)} "
            f"{course.cur_member}/{course.member_limit}"
        )
    printer.line(printer.color(f"── 共 {len(result.courses)} 門 ──", CYAN))


def load_rules(
    queries: list[str], printer: Printer
) -> list[course_filter.Rule]:
    """決定要使用的規則。

    Args:
        queries: 命令列給的規則片段。
        printer: 輸出器，用來提示規則來自 .env。

    Returns:
        規則列表；命令列與 .env 都沒有時回傳一條空規則（代表全校）。

    Raises:
        course_filter.RuleError: 規則寫錯。
    """
    if queries:
        return course_filter.parse_rules(queries)

    dotenv.load_dotenv()
    env_rules = course_filter.rules_from_env()
    if env_rules:
        printer.line(printer.color("（使用 .env 的 LOOK_UP_CLASSES 規則）", DIM))
        return course_filter.parse_rules(env_rules)
    return [course_filter.Rule()]


def build_parser(
    description: str, default_interval: float
) -> argparse.ArgumentParser:
    """建立兩支腳本共用的參數解析器。

    Args:
        description: 腳本說明。
        default_interval: 預設的每輪週期秒數。

    Returns:
        已設定好共用參數的解析器，--help 會附上完整欄位表。
    """
    setup_terminal()  # argparse 可能立刻印出 --help，先確保輸出是 UTF-8。
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=course_filter.field_help(),
    )
    parser.add_argument(
        "queries", nargs="*",
        help="篩選規則（欄位:值，; 分隔多條）；省略時讀 .env 的 LOOK_UP_CLASSES")
    parser.add_argument(
        "-i", "--interval", type=float, default=default_interval,
        help=f"每輪週期秒數 (預設 {default_interval:g}；查詢較久時會自動放寬)")
    parser.add_argument(
        "-s", "--semester", default=None,
        help="學期代碼，例如 1151 (預設自動判斷)")
    parser.add_argument(
        "--list", action="store_true",
        help=f"啟動時列出全部課程（預設超過 {LIST_LIMIT} 門就省略）")
    parser.add_argument("--no-color", action="store_true", help="停用色彩輸出")
    parser.add_argument("-d", "--debug", action="store_true", help="顯示除錯訊息")
    return parser


def setup_output(args: argparse.Namespace) -> Printer:
    """依參數設定輸出與 logging。

    Args:
        args: 已解析的命令列參數。

    Returns:
        設定好的輸出器。
    """
    interactive = setup_terminal()
    # 色彩不綁 isatty：PyCharm 等執行視窗不是 tty 但看得懂 ANSI 色碼。
    use_color = not args.no_color and not os.environ.get("NO_COLOR")
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return Printer(interactive, use_color)


async def prepare(
    client: course_lookup.CourseClient,
    args: argparse.Namespace,
    printer: Printer,
) -> tuple[list[course_filter.Rule], str, bool]:
    """解析規則並決定學期。

    Args:
        client: 課程查詢客戶端。
        args: 已解析的命令列參數。
        printer: 輸出器。

    Returns:
        (規則列表, 預設學期, 是否為舊學期唯讀模式)。

    Raises:
        course_filter.RuleError: 規則寫錯。
    """
    rules = load_rules(args.queries, printer)
    explicit = args.semester or ""
    if not explicit and rules and all(rule.semester for rule in rules):
        explicit = rules[0].semester

    semester, note = await course_filter.resolve_semester(client, explicit)
    if note:
        printer.line(printer.color(note, YELLOW))

    latest = course_lookup.current_semester()
    effective = {rule.semester or semester for rule in rules}
    return rules, semester, all(item < latest for item in effective)


def _print_header(
    printer: Printer,
    current: dict[str, course_lookup.Course],
    rules: list[course_filter.Rule],
    counts: list[int],
    semester: str,
    interval: float,
    list_all: bool,
) -> None:
    """印出啟動時的規則回顯與課程清單。

    Args:
        printer: 輸出器。
        current: 這一輪命中的課程。
        rules: 規則列表。
        counts: 每條規則命中的門數。
        semester: 這次查詢的學期。
        interval: 每輪週期秒數。
        list_all: 是否強制列出全部課程。
    """
    printer.line(printer.color(
        f"── 監看 {len(current)} 門課程（每 {interval:g} 秒；Ctrl+C 結束）──",
        CYAN))
    print_rules(printer, rules, counts, semester)
    if list_all or len(current) <= LIST_LIMIT:
        for course in current.values():
            printer.line(
                f"   {format_course(course)} "
                f"{course.cur_member}/{course.member_limit}"
            )
    else:
        printer.line(printer.color(
            f"   （課程超過 {LIST_LIMIT} 門，清單省略；加 --list 可列出全部）",
            DIM))
    printer.line(printer.color("── 以下只顯示人數變動 ──", CYAN))


def _report_changes(
    printer: Printer,
    previous: dict[str, course_lookup.Course],
    current: dict[str, course_lookup.Course],
    filters: dict[str, course_filter.Match],
    now: str,
) -> int:
    """比對兩輪結果並輸出變動。

    Args:
        printer: 輸出器。
        previous: 上一輪的課程。
        current: 這一輪的課程。
        filters: 每門課適用的輸出規則。
        now: 這一輪的時間字串。

    Returns:
        這一輪輸出了幾筆變動。
    """
    changes = 0
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
            f"[{now}] {format_course(previous[course_no])} "
            "課程已從查詢結果消失", YELLOW))
        changes += 1
    return changes


def main() -> None:
    """命令列進入點。"""
    parser = build_parser(
        "每隔數秒監看課程人數，只在有人選走 (紅) 或退選 (綠) 時輸出。",
        default_interval=5.0,
    )
    args = parser.parse_args()
    printer = setup_output(args)

    if args.interval < 1:
        parser.error("每輪週期請勿小於 1 秒，以免被選課系統限流 (429)。")

    client = course_lookup.CourseClient()
    try:
        rules, semester, is_old = asyncio.run(prepare(client, args, printer))
    except course_filter.RuleError as error:
        parser.exit(2, printer.color(f"規則錯誤：{error}\n", RED))

    try:
        if is_old:
            asyncio.run(list_once(client, rules, semester, printer))
            return
        asyncio.run(
            watch(client, rules, semester, args.interval, printer, args.list)
        )
    except KeyboardInterrupt:
        printer.clear_status()
        printer.line(printer.color("已停止監看。", CYAN))


if __name__ == "__main__":
    main()

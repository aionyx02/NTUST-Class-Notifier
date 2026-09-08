"""三支指令共用的命令列處理。

負責參數解析、規則來源（命令列或 .env）、logging 與學期判斷。
"""

import argparse
import logging
import os
import sys

from ntust_class_notifier import config
from ntust_class_notifier.app import search
from ntust_class_notifier.clients import course_api
from ntust_class_notifier.core import models
from ntust_class_notifier.core import ruleset
from ntust_class_notifier.ui import console
from ntust_class_notifier.ui import report


def build_parser(
    description: str, default_interval: float
) -> argparse.ArgumentParser:
    """建立兩支程式共用的參數解析器。

    Args:
        description: 程式說明。
        default_interval: 預設的每輪週期秒數。

    Returns:
        已設定好共用參數的解析器，--help 會附上完整欄位表。
    """
    console.setup_terminal()  # argparse 可能立刻印 --help，先確保是 UTF-8。
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=ruleset.field_help(),
    )
    parser.add_argument(
        "queries", nargs="*",
        help="篩選規則（欄位:值，; 分隔多條）；"
             "省略時讀 .env 的 LOOK_UP_CLASSES")
    parser.add_argument(
        "-i", "--interval", type=float, default=default_interval,
        help=f"每輪週期秒數 (預設 {default_interval:g}；查詢較久時會自動放寬)")
    parser.add_argument(
        "-s", "--semester", default=None,
        help="學期代碼，例如 1151 (預設自動判斷)")
    parser.add_argument(
        "--list", action="store_true",
        help=f"啟動時列出全部課程（預設超過 {report.LIST_LIMIT} 門就省略）")
    parser.add_argument("--no-color", action="store_true", help="停用色彩輸出")
    parser.add_argument(
        "-d", "--debug", action="store_true", help="顯示除錯訊息")
    return parser


def setup_output(args: argparse.Namespace) -> console.Printer:
    """依參數設定輸出與 logging。

    Args:
        args: 已解析的命令列參數。

    Returns:
        設定好的輸出器。
    """
    interactive = console.setup_terminal()
    config.load_env()  # NO_COLOR 寫在 .env 裡也要算數。
    # 色彩不綁 isatty：PyCharm 等執行視窗不是 tty 但看得懂 ANSI 色碼。
    use_color = not args.no_color and not os.environ.get("NO_COLOR")
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return console.Printer(interactive, use_color)


def load_rules(
    queries: list[str], printer: console.Printer
) -> list[ruleset.Rule]:
    """決定要使用的規則。

    Args:
        queries: 命令列給的規則片段。
        printer: 輸出器，用來提示規則來自 .env。

    Returns:
        規則列表；命令列與 .env 都沒有時回傳一條空規則（代表全校）。

    Raises:
        ruleset.RuleError: 規則寫錯。
    """
    if queries:
        return ruleset.parse_rules(queries)

    config.load_env()
    env_rules = config.look_up_classes()
    if env_rules:
        printer.line(printer.color(
            "（使用 .env 的 LOOK_UP_CLASSES 規則）", console.DIM))
        return ruleset.parse_rules(env_rules)
    return [ruleset.Rule()]


async def prepare(
    client: course_api.CourseClient,
    args: argparse.Namespace,
    printer: console.Printer,
) -> tuple[list[ruleset.Rule], str, bool]:
    """解析規則並決定學期。

    Args:
        client: 課程查詢客戶端。
        args: 已解析的命令列參數。
        printer: 輸出器。

    Returns:
        (規則列表, 預設學期, 是否為舊學期唯讀模式)。

    Raises:
        ruleset.RuleError: 規則寫錯。
    """
    parsed = load_rules(args.queries, printer)
    explicit = args.semester or ""
    if not explicit and parsed and all(rule.semester for rule in parsed):
        explicit = parsed[0].semester

    semester, note = await search.resolve_semester(client, explicit)
    if note:
        printer.line(printer.color(note, console.YELLOW))

    latest = models.current_semester()
    effective = {rule.semester or semester for rule in parsed}
    return parsed, semester, all(item < latest for item in effective)


async def list_once(
    client: course_api.CourseClient,
    parsed: list[ruleset.Rule],
    semester: str,
    printer: console.Printer,
) -> None:
    """查一次就把結果列出來，供舊學期使用。

    Args:
        client: 課程查詢客戶端。
        parsed: 篩選規則。
        semester: 要查詢的學期。
        printer: 輸出器。
    """
    result = await search.search(client, parsed, semester)
    if result.failed:
        printer.line(printer.color(
            f"查詢失敗：{'、'.join(result.failed)}", console.YELLOW))
        return

    printer.line(printer.color(
        f"── 舊學期 {semester}，只列出不監看 ──", console.CYAN))
    report.print_rules(printer, parsed, result.counts, semester)
    if not result.courses:
        report.zero_result_hint(printer, parsed, semester)
        return
    report.print_courses(printer, result.courses)
    printer.line(printer.color(
        f"── 共 {len(result.courses)} 門 ──", console.CYAN))

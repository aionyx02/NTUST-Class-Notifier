"""ntust-watch：終端機課程人數監看。

每隔數秒查詢一次課程人數，只在人數有變動時輸出一行（整行上色）：被別人
選走是紅色、有人退選是綠色，沒有變動就不輸出。

用法：
    uv run ntust-watch 課號:CS 學制:大學部
    uv run ntust-watch CS
    uv run ntust-watch 課名:程式設計 老師:姚
    uv run ntust-watch
"""

import asyncio

from ntust_class_notifier.app import watch as watch_app
from ntust_class_notifier.cli import options
from ntust_class_notifier.clients import course_api
from ntust_class_notifier.core import ruleset
from ntust_class_notifier.ui import console


def main() -> None:
    """命令列進入點。"""
    parser = options.build_parser(
        "每隔數秒監看課程人數，只在有人選走 (紅) 或退選 (綠) 時輸出。",
        default_interval=5.0,
    )
    args = parser.parse_args()
    printer = options.setup_output(args)

    if args.interval < 1:
        parser.error("每輪週期請勿小於 1 秒，以免被選課系統限流 (429)。")

    client = course_api.CourseClient()
    try:
        parsed, semester, is_old = asyncio.run(
            options.prepare(client, args, printer))
    except ruleset.RuleError as error:
        parser.exit(2, printer.color(f"規則錯誤：{error}\n", console.RED))

    try:
        if is_old:
            asyncio.run(
                options.list_once(client, parsed, semester, printer))
            return
        asyncio.run(watch_app.watch(
            client, parsed, semester, args.interval, printer, args.list))
    except KeyboardInterrupt:
        printer.clear_status()
        printer.line(printer.color("已停止監看。", console.CYAN))


if __name__ == "__main__":
    main()

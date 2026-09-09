"""ntust-watch：終端機課程人數監看。

每隔數秒查詢一次課程人數，只在人數有變動時輸出一行（整行上色）：被別人
選走是紅色、有人退選是綠色，沒有變動就不輸出。.env 有填 STUDENT_ID/PASSWORD
時會登入選課系統，課程一出現空位就自動送出加選（--no-enroll 可停用）。

用法：
    uv run ntust-watch 課號:CS 學制:大學部
    uv run ntust-watch CS
    uv run ntust-watch 課名:程式設計 老師:姚
    uv run ntust-watch
"""

import asyncio

from ntust_class_notifier import config
from ntust_class_notifier.app import enroll as enroll_app
from ntust_class_notifier.app import watch as watch_app
from ntust_class_notifier.cli import options
from ntust_class_notifier.clients import course_api
from ntust_class_notifier.core import ruleset
from ntust_class_notifier.ui import console


async def _monitor(client, parsed, semester, args, printer) -> None:
    """登入（如有需要）後開始監看，期間在背景維持 session。

    監看與 keepalive 必須跑在同一個事件迴圈裡，等空位等上幾小時登入狀態才
    不會過期。

    Args:
        client: 課程查詢客戶端。
        parsed: 篩選規則。
        semester: 預設學期。
        args: 已解析的命令列參數。
        printer: 輸出器。
    """
    enroller = await options.setup_enroller(args, printer)
    async with enroll_app.session_kept_alive(enroller):
        await watch_app.watch(client, parsed, semester, args.interval,
                              printer, args.list, enroller)


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
        asyncio.run(_monitor(client, parsed, semester, args, printer))
    except config.ConfigError as error:
        parser.exit(2, printer.color(f"{error}\n", console.RED))
    except KeyboardInterrupt:
        printer.clear_status()
        printer.line(printer.color("已停止監看。", console.CYAN))


if __name__ == "__main__":
    main()

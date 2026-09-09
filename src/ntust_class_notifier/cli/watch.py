"""ntust-watch：終端機課程人數監看。

每隔數秒查詢一次課程人數，只在人數有變動時輸出一行（整行上色）：被別人
選走是紅色、有人退選是綠色，沒有變動就不輸出。.env 寫了 AUTO_ENROLL=true
又填了 STUDENT_ID/PASSWORD 時會登入選課系統，課程一出現空位就自動送出加選
（--no-enroll 可單次停用）。

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


async def _run(args, printer) -> None:
    """整支程式的非同步流程。

    查詢客戶端共用一條連線，所以規則解析、監看與 keepalive 都得跑在同一個
    事件迴圈裡；等空位等上幾小時，登入狀態才不會過期。

    Args:
        args: 已解析的命令列參數。
        printer: 輸出器。

    Raises:
        ruleset.RuleError: 規則寫錯。
        config.ConfigError: .env 設定寫錯。
    """
    async with course_api.CourseClient() as client:
        parsed, semester, is_old = await options.prepare(client, args, printer)
        if is_old:
            await options.list_once(client, parsed, semester, printer)
            return

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

    try:
        asyncio.run(_run(args, printer))
    except ruleset.RuleError as error:
        parser.exit(2, printer.color(f"規則錯誤：{error}\n", console.RED))
    except config.ConfigError as error:
        parser.exit(2, printer.color(f"{error}\n", console.RED))
    except KeyboardInterrupt:
        printer.clear_status()
        printer.line(printer.color("已停止監看。", console.CYAN))


if __name__ == "__main__":
    main()

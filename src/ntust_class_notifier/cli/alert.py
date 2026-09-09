"""ntust-alert：終端機搶課監控。

每隔數秒檢查鎖定的課程，一旦出現空位就顯示大字提醒並發出音效。預設只查詢
公開的課程查詢 API；.env 有填 STUDENT_ID/PASSWORD 時會登入選課系統並自動送
出加選（--no-enroll 可停用）。

用法：
    uv run ntust-alert 課號:CS1003301
    uv run ntust-alert CS1003301 EE21
    uv run ntust-alert 課號:PE139A053 系所:資訊工程系三
    uv run ntust-alert 課名:程式設計 學制:大學部
"""

import asyncio

from ntust_class_notifier import config
from ntust_class_notifier.app import alert as alert_app
from ntust_class_notifier.app import enroll as enroll_app
from ntust_class_notifier.cli import options
from ntust_class_notifier.clients import course_api
from ntust_class_notifier.core import ruleset
from ntust_class_notifier.ui import console


async def _monitor(watcher, args, printer) -> None:
    """登入（如有需要）後開始搶課監控，期間在背景維持 session。

    監控與 keepalive 必須跑在同一個事件迴圈裡，等空位等上幾小時登入狀態才
    不會過期。

    Args:
        watcher: 搶課監控主體。
        args: 已解析的命令列參數。
        printer: 輸出器。
    """
    watcher.enroller = await options.setup_enroller(args, printer)
    async with enroll_app.session_kept_alive(watcher.enroller):
        await watcher.run()


def main() -> None:
    """命令列進入點。"""
    parser = options.build_parser(
        "終端機版搶課監控：課程一出現空位就大字提醒並發出音效"
        "（.env 有帳密時一併自動加選）。",
        default_interval=3.0,
    )
    parser.add_argument("--no-sound", action="store_true", help="停用提示音")
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

    watcher = alert_app.Watcher(
        client=client,
        rules=parsed,
        semester=semester,
        interval=args.interval,
        printer=printer,
        sound=not args.no_sound,
        list_all=args.list,
    )

    try:
        if is_old:
            asyncio.run(
                options.list_once(client, parsed, semester, printer))
            return
        asyncio.run(_monitor(watcher, args, printer))
    except config.ConfigError as error:
        parser.exit(2, printer.color(f"{error}\n", console.RED))
    except KeyboardInterrupt:
        printer.clear_status()
        printer.line(printer.color(
            f"已停止監控（共提醒 {watcher.alerts} 次）。", console.CYAN))


if __name__ == "__main__":
    main()

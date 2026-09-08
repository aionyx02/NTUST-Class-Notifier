"""ntust-alert：終端機搶課監控。

每隔數秒檢查鎖定的課程，一旦出現空位就顯示大字提醒並發出音效。只查詢公開
的課程查詢 API，不會登入選課系統、也不會替你送出加選。

用法：
    uv run ntust-alert 課號:CS1003301
    uv run ntust-alert CS1003301 EE21
    uv run ntust-alert 課號:PE139A053 系所:資訊工程系三
    uv run ntust-alert 課名:程式設計 學制:大學部
"""

import asyncio

from ntust_class_notifier.app import alert as alert_app
from ntust_class_notifier.cli import options
from ntust_class_notifier.clients import course_api
from ntust_class_notifier.core import ruleset
from ntust_class_notifier.ui import console


def main() -> None:
    """命令列進入點。"""
    parser = options.build_parser(
        "終端機版搶課監控：課程一出現空位就大字提醒並發出音效"
        "（只提醒，不會自動加選）。",
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
        asyncio.run(watcher.run())
    except KeyboardInterrupt:
        printer.clear_status()
        printer.line(printer.color(
            f"已停止監控（共提醒 {watcher.alerts} 次）。", console.CYAN))


if __name__ == "__main__":
    main()

"""ntust-alert：終端機搶課監控。

每隔數秒檢查鎖定的課程，一旦出現空位就顯示大字提醒並發出音效。預設只查詢
公開的課程查詢 API；.env 寫了 AUTO_ENROLL=true 又填了 STUDENT_ID/PASSWORD
時會登入選課系統並自動送出加選（--no-enroll 可單次停用）。

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


async def _run(args, printer, alerts: list[int]) -> None:
    """整支程式的非同步流程。

    查詢客戶端共用一條連線，所以規則解析、監控與 keepalive 都得跑在同一個
    事件迴圈裡；等空位等上幾小時，登入狀態才不會過期。

    Args:
        args: 已解析的命令列參數。
        printer: 輸出器。
        alerts: 單元素的容器，用來把提醒次數帶回給結束訊息（Ctrl+C 時
            watcher 已經在這個函式裡了，外面拿不到）。

    Raises:
        ruleset.RuleError: 規則寫錯。
        config.ConfigError: .env 設定寫錯。
    """
    async with course_api.CourseClient() as client:
        parsed, semester, is_old = await options.prepare(client, args, printer)
        if is_old:
            await options.list_once(client, parsed, semester, printer)
            return

        watcher = alert_app.Watcher(
            client=client,
            rules=parsed,
            semester=semester,
            interval=args.interval,
            printer=printer,
            sound=not args.no_sound,
            list_all=args.list,
        )
        watcher.enroller = await options.setup_enroller(args, printer)
        try:
            async with enroll_app.session_kept_alive(watcher.enroller):
                await watcher.run()
        finally:
            alerts[0] = watcher.alerts


def main() -> None:
    """命令列進入點。"""
    parser = options.build_parser(
        "終端機版搶課監控：課程一出現空位就大字提醒並發出音效"
        "（.env 打開 AUTO_ENROLL 時一併自動加選）。",
        default_interval=3.0,
    )
    parser.add_argument("--no-sound", action="store_true", help="停用提示音")
    args = parser.parse_args()
    printer = options.setup_output(args)

    if args.interval < 1:
        parser.error("每輪週期請勿小於 1 秒，以免被選課系統限流 (429)。")

    alerts = [0]
    try:
        asyncio.run(_run(args, printer, alerts))
    except ruleset.RuleError as error:
        parser.exit(2, printer.color(f"規則錯誤：{error}\n", console.RED))
    except config.ConfigError as error:
        parser.exit(2, printer.color(f"{error}\n", console.RED))
    except KeyboardInterrupt:
        printer.clear_status()
        printer.line(printer.color(
            f"已停止監控（共提醒 {alerts[0]} 次）。", console.CYAN))


if __name__ == "__main__":
    main()

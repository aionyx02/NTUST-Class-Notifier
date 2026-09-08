"""終端機輸出：色彩、等寬對齊與底部狀態列。

色彩不綁 isatty，因為 PyCharm 等執行視窗不是 tty 但看得懂 ANSI 色碼。
"""

import os
import sys
import unicodedata

from ntust_class_notifier.core import models

# 終端機色碼。
RESET = "\033[0m"


BOLD = "\033[1m"


RED = "\033[91m"


GREEN = "\033[92m"


CYAN = "\033[96m"


YELLOW = "\033[93m"


DIM = "\033[2m"


def setup_terminal() -> bool:
    """設定終端機的輸出編碼與 ANSI 色彩支援。

    Returns:
        是否為互動式終端機。這只決定要不要顯示底部的即時狀態列，色彩不受
        它影響。
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


def format_course(course: models.Course) -> str:
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

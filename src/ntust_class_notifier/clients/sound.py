"""跨平台提示音。

macOS 用系統音效、Windows 用嗶聲，其餘平台以終端機響鈴代替。
"""

import logging
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)


# Windows 嗶聲音型 (頻率 Hz, 長度 ms)
_WIN_BEEPS = {
    "vacancy": [(880, 150), (1175, 150), (1568, 300)],   # 有空位：上行
    "success": [(1047, 120), (1319, 120), (1568, 400)],  # 加選成功：歡呼
    "failure": [(440, 250), (330, 400)],                 # 加選失敗：下行
}


def play_sound(sound_type: str = "vacancy"):
    """播放提示音。

    macOS 用系統音效、Windows 用嗶聲，其餘平台以終端機響鈴代替。

    Args:
        sound_type: "vacancy"、"success" 或 "failure"。
    """
    if sys.platform == "darwin":
        sounds = {
            "vacancy": "Glass",       # 有空位
            "success": "Hero",        # 加選成功
            "failure": "Sosumi",      # 加選失敗
        }
        sound_name = sounds.get(sound_type, "Glass")
        subprocess.Popen(
            ["afplay", f"/System/Library/Sounds/{sound_name}.aiff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return

    if sys.platform == "win32":
        import winsound

        def _beep():
            try:
                beeps = _WIN_BEEPS.get(sound_type, _WIN_BEEPS["vacancy"])
                for freq, duration in beeps:
                    winsound.Beep(freq, duration)
            except Exception as e:  # 沒有喇叭或音效裝置時不要影響監控
                logger.debug("播放提示音失敗: %s", e)

        # winsound.Beep 是阻塞的，丟到背景執行緒避免卡住事件迴圈
        threading.Thread(target=_beep, daemon=True).start()
        return

    print("\a", end="", flush=True)

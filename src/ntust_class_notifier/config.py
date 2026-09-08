"""集中處理 .env 設定。

環境變數只在這個模組讀取，其他模組一律透過 Settings 或這裡的函式取得，
所以 .env.example 列出的變數就是全部的設定項目。
"""

import dataclasses
import os
import pathlib
import re

import dotenv

# 篩選規則的變數名；另接受 LOOK_UP_CLASSES_1、_2… 等編號變數。
RULE_VAR = "LOOK_UP_CLASSES"

# 執行期資料（cookie 等）的存放目錄，可用環境變數覆寫。
DATA_DIR_VAR = "NTUST_DATA_DIR"
DEFAULT_DATA_DIR = pathlib.Path.home() / ".ntust-class-notifier"

# 收件對象允許用分號、逗號或空白分隔。
_SPLIT_IDS = re.compile(r"[;,\s]+")


class ConfigError(ValueError):
    """設定寫錯時拋出，訊息已經是可以直接顯示給使用者的中文。"""


def data_dir() -> pathlib.Path:
    """取得執行期資料目錄，不存在就建立。

    cookie 這類執行期檔案不該寫進套件安裝目錄，預設放使用者家目錄下的
    ~/.ntust-class-notifier，可用 NTUST_DATA_DIR 覆寫。

    Returns:
        已存在的目錄路徑。
    """
    raw = os.environ.get(DATA_DIR_VAR, "").strip()
    path = pathlib.Path(raw) if raw else DEFAULT_DATA_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_env() -> None:
    """讀取專案根目錄的 .env，已存在的環境變數優先。"""
    dotenv.load_dotenv()


def look_up_classes() -> str:
    """收集 .env 裡的規則字串。

    除了 LOOK_UP_CLASSES，也接受 LOOK_UP_CLASSES_1、LOOK_UP_CLASSES_2… 並
    合併起來，因為 .env 同名變數寫成多行時只有最後一行有效，想「一行一條
    規則」就得用不同的變數名。

    Returns:
        以 `;` 接起來的規則字串，沒有設定時為空字串。
    """
    numbered = sorted(
        (
            (name, value)
            for name, value in os.environ.items()
            if name.startswith(f"{RULE_VAR}_") and value.strip()
        ),
        key=lambda item: (len(item[0]), item[0]),  # _1 _2 … _10 的自然順序
    )
    values = [os.environ.get(RULE_VAR, "")]
    values.extend(value for _, value in numbered)
    return ";".join(value.strip() for value in values if value.strip())


def parse_target_ids(raw: str) -> tuple[int, ...]:
    """把收件對象字串拆成 ID。

    Args:
        raw: 以 ;、, 或空白分隔的 ID 字串。

    Returns:
        ID 序列，順序與設定相同。

    Raises:
        ConfigError: 其中有非數字的內容。
    """
    tokens = [token for token in _SPLIT_IDS.split(raw.strip()) if token]
    try:
        return tuple(int(token) for token in tokens)
    except ValueError as error:
        raise ConfigError(
            f"DISCORD_TARGET_IDS 只能填純數字的 ID：{error}"
        ) from error


@dataclasses.dataclass(frozen=True)
class Settings:
    """一次讀齊的 .env 設定。

    Attributes:
        look_up_classes: 篩選規則字串，空字串代表沒有設定。
        discord_token: Discord Bot Token，空字串代表不啟用通知。
        discord_target_ids: 收件對象 ID，可為伺服器、頻道或使用者。
        student_id: 選課系統學號，空字串代表不啟用自動加選。
        password: 選課系統密碼。
    """

    look_up_classes: str = ""
    discord_token: str = ""
    discord_target_ids: tuple[int, ...] = ()
    student_id: str = ""
    password: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        """讀取 .env 與環境變數。

        Returns:
            填好的設定。

        Raises:
            ConfigError: 設定值格式錯誤。
        """
        load_env()
        raw_ids = (
            os.environ.get("DISCORD_TARGET_IDS")
            # 舊名稱，保留相容。
            or os.environ.get("DISCORD_TARGET_USER_IDS", "")
        )
        return cls(
            look_up_classes=look_up_classes(),
            discord_token=os.environ.get("DISCORD_BOT_TOKEN", ""),
            discord_target_ids=parse_target_ids(raw_ids),
            student_id=os.environ.get("STUDENT_ID", ""),
            password=os.environ.get("PASSWORD", ""),
        )

    @property
    def discord_enabled(self) -> bool:
        """有 token 也有收件對象時才會發 Discord 通知。"""
        return bool(self.discord_token and self.discord_target_ids)

    @property
    def selector_enabled(self) -> bool:
        """有學號也有密碼時才會登入選課系統。"""
        return bool(self.student_id and self.password)

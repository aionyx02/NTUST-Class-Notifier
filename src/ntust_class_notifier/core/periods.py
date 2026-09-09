"""選課時段的定義與對應連結。

notifier 與 alert 共用同一份日期，避免兩邊各存一份而失準。
"""

import datetime

# 電選課加選：由系所分配名額的階段。
PERIOD_DEPT_SELECT = (datetime.date(2026, 6, 22), datetime.date(2026, 6, 24))

# 全校加退選。
PERIOD_OPEN_SELECT = (datetime.date(2026, 9, 7), datetime.date(2026, 9, 21))

DEPT_SELECT_LINK = "https://courseselection.ntust.edu.tw/First/A06/A06"
OPEN_SELECT_LINK = "https://courseselection.ntust.edu.tw/AddAndSub/B01/B01"

# 會送出加選的時段；其餘時段（"unknown"）選課系統根本不收加選。
SELECTION_PERIODS = ("dept", "open")

_PERIOD_NAMES = {"dept": "電選課加選", "open": "全校加退選"}


def get_current_period(today: datetime.date | None = None) -> str:
    """判斷指定日期落在哪個選課時段。

    Args:
        today: 要判斷的日期，預設為今天。

    Returns:
        "dept"（電選課加選）、"open"（全校加退選）或 "unknown"。
    """
    today = today or datetime.date.today()
    if PERIOD_DEPT_SELECT[0] <= today <= PERIOD_DEPT_SELECT[1]:
        return "dept"
    if PERIOD_OPEN_SELECT[0] <= today <= PERIOD_OPEN_SELECT[1]:
        return "open"
    return "unknown"


def get_period_name(period: str) -> str:
    """把時段代碼轉成中文名稱。

    Args:
        period: get_current_period() 的回傳值。

    Returns:
        中文時段名稱，未知時回傳「非選課時段」。
    """
    return _PERIOD_NAMES.get(period, "非選課時段")

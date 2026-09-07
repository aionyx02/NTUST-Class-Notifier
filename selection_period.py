"""選課時段定義與對應連結（main.py 與 course_alert.py 共用）"""

from datetime import date

# 選課時段定義
PERIOD_DEPT_SELECT = (date(2026, 6, 22), date(2026, 6, 24))  # 電選課加選
PERIOD_OPEN_SELECT = (date(2026, 9, 7), date(2026, 9, 21))  # 全校加退選

DEPT_SELECT_LINK = "https://courseselection.ntust.edu.tw/First/A06/A06"
OPEN_SELECT_LINK = "https://courseselection.ntust.edu.tw/AddAndSub/B01/B01"


def get_current_period(today: date | None = None) -> str:
    """根據日期判斷目前選課時段，回傳 "dept" / "open" / "unknown"。"""
    today = today or date.today()
    if PERIOD_DEPT_SELECT[0] <= today <= PERIOD_DEPT_SELECT[1]:
        return "dept"
    if PERIOD_OPEN_SELECT[0] <= today <= PERIOD_OPEN_SELECT[1]:
        return "open"
    return "unknown"


def get_period_name(period: str) -> str:
    """時段代碼轉成中文名稱。"""
    return {"dept": "電選課加選", "open": "全校加退選"}.get(period, "非選課時段")


def get_selection_link() -> str:
    """根據時段回傳對應的選課連結"""
    period = get_current_period()
    if period == "dept":
        return f"▸ **電選課加選:** {DEPT_SELECT_LINK}"
    if period == "open":
        return f"▸ **全校加退選:** {OPEN_SELECT_LINK}"
    return (
        f"▸ **電選課加選:** {DEPT_SELECT_LINK}\n"
        f"▸ **全校加退選:** {OPEN_SELECT_LINK}"
    )

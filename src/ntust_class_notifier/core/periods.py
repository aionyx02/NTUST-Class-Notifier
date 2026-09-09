"""選課時段的定義與對應連結。

三支指令共用同一份時程，避免各存一份而失準。時程精確到分鐘：只比日期的
話，開放首日的凌晨就會開始送出加選、最後一天收攤之後還會一路送到午夜，
兩邊都是白送請求。

寫在這裡的日期是人工抄自學校公告的，過期了程式不會自己知道，所以判斷一
律 fail-closed——時段沒到、已經收攤、或整份時程都過去了，都只監控不加選。
"""

import datetime
import zoneinfo

# 選課系統的時區。台灣自 1979 年起沒有日光節約時間，所以拿不到 tzdata
# 時退回固定 +08:00，結果與 Asia/Taipei 完全一致。
try:
    TAIPEI: datetime.tzinfo = zoneinfo.ZoneInfo("Asia/Taipei")
except zoneinfo.ZoneInfoNotFoundError:  # pragma: no cover - 看系統有沒有
    TAIPEI = datetime.timezone(datetime.timedelta(hours=8), "Asia/Taipei")

# 每日開放時間。學校公告的加選時間是 09:00～17:00，這個區間以外送出的加
# 選不會成立，收攤前一分鐘送出也一樣（結束時間不含）。
DAILY_OPEN = datetime.time(9, 0)
DAILY_CLOSE = datetime.time(17, 0)

# 電選課加選：由系所分配名額的階段（起訖日皆含）。
PERIOD_DEPT_SELECT = (datetime.date(2026, 6, 22), datetime.date(2026, 6, 24))

# 全校加退選。
PERIOD_OPEN_SELECT = (datetime.date(2026, 9, 7), datetime.date(2026, 9, 21))

DEPT_SELECT_LINK = "https://courseselection.ntust.edu.tw/First/A06/A06"
OPEN_SELECT_LINK = "https://courseselection.ntust.edu.tw/AddAndSub/B01/B01"

# 會送出加選的時段；其餘時段（"unknown"）選課系統根本不收加選。
SELECTION_PERIODS = ("dept", "open")

_PERIOD_DATES = {
    "dept": PERIOD_DEPT_SELECT,
    "open": PERIOD_OPEN_SELECT,
}

_PERIOD_NAMES = {"dept": "電選課加選", "open": "全校加退選"}

_PERIOD_LINKS = {"dept": DEPT_SELECT_LINK, "open": OPEN_SELECT_LINK}


def now() -> datetime.datetime:
    """取得選課系統所在時區的現在時刻。

    Returns:
        帶時區的台北時間。
    """
    return datetime.datetime.now(TAIPEI)


def _as_taipei(moment: datetime.datetime | None) -> datetime.datetime:
    """把傳進來的時刻統一成台北時間。

    Args:
        moment: 要判斷的時刻，None 代表現在；沒有時區的一律當成台北時間，
            因為所有時程都是學校公告的當地時間。

    Returns:
        帶時區的台北時間。
    """
    if moment is None:
        return now()
    if moment.tzinfo is None:
        return moment.replace(tzinfo=TAIPEI)
    return moment.astimezone(TAIPEI)


def get_scheduled_period(moment: datetime.datetime | None = None) -> str:
    """判斷指定時刻落在哪一段選課時程，不管當天開放了沒。

    給畫面與連結用；要不要送出加選一律看 get_current_period()。

    Args:
        moment: 要判斷的時刻，預設為現在。

    Returns:
        "dept"、"open" 或 "unknown"。
    """
    today = _as_taipei(moment).date()
    for period, (first, last) in _PERIOD_DATES.items():
        if first <= today <= last:
            return period
    return "unknown"


def is_open(moment: datetime.datetime | None = None) -> bool:
    """判斷指定時刻是否落在每日開放時間內。

    Args:
        moment: 要判斷的時刻，預設為現在。

    Returns:
        是否在 DAILY_OPEN（含）到 DAILY_CLOSE（不含）之間。
    """
    clock = _as_taipei(moment).timetz().replace(tzinfo=None)
    return DAILY_OPEN <= clock < DAILY_CLOSE


def get_current_period(moment: datetime.datetime | None = None) -> str:
    """判斷指定時刻選課系統收不收加選。

    日期與每日開放時間都要對上才算數；任何一邊不確定就回 "unknown"，
    上層看到 "unknown" 就只監控、不送出。

    Args:
        moment: 要判斷的時刻，預設為現在。

    Returns:
        "dept"（電選課加選）、"open"（全校加退選）或 "unknown"。
    """
    period = get_scheduled_period(moment)
    if period == "unknown" or not is_open(moment):
        return "unknown"
    return period


def schedule_expired(moment: datetime.datetime | None = None) -> bool:
    """判斷這份寫死的時程是不是已經整個過去了。

    過期代表沒人更新過這個檔案，之後不管等多久都不會再送出加選；呼叫端
    應該明講一聲，而不是讓使用者以為程式在幫他搶。

    Args:
        moment: 要判斷的時刻，預設為現在。

    Returns:
        是否所有時段都已結束。
    """
    today = _as_taipei(moment).date()
    return all(today > last for _, last in _PERIOD_DATES.values())


def get_period_name(period: str) -> str:
    """把時段代碼轉成中文名稱。

    Args:
        period: get_current_period() 的回傳值。

    Returns:
        中文時段名稱，未知時回傳「非選課時段」。
    """
    return _PERIOD_NAMES.get(period, "非選課時段")


def get_period_link(period: str) -> str:
    """把時段代碼轉成選課系統網址。

    Args:
        period: 時段代碼。

    Returns:
        該時段的網址；不知道是哪一段時把兩個都給。
    """
    return _PERIOD_LINKS.get(
        period, f"{DEPT_SELECT_LINK} / {OPEN_SELECT_LINK}")


def describe(moment: datetime.datetime | None = None) -> str:
    """組出一行給使用者看的時段說明。

    「今天在時程內但還沒開門」跟「根本不是選課期間」對使用者的意義完全
    不同，所以分開講。

    Args:
        moment: 要判斷的時刻，預設為現在。

    Returns:
        例如「全校加退選（開放中，每日 09:00–17:00）」。
    """
    hours = f"每日 {DAILY_OPEN:%H:%M}–{DAILY_CLOSE:%H:%M}"
    scheduled = get_scheduled_period(moment)
    if scheduled == "unknown":
        if schedule_expired(moment):
            return "非選課時段（程式內建的選課時程已全部結束）"
        return "非選課時段"
    name = get_period_name(scheduled)
    if is_open(moment):
        return f"{name}（開放中，{hours}）"
    return f"{name}（今日已收攤或尚未開放，{hours}）"

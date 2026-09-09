"""core.periods：選課時段要精確到分鐘，而且判斷失準時寧可不加選。"""

import datetime

from ntust_class_notifier.core import periods

FIRST_DAY, LAST_DAY = periods.PERIOD_OPEN_SELECT
DEPT_FIRST_DAY = periods.PERIOD_DEPT_SELECT[0]


def _at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    """組出一個台北時間。

    Args:
        day: 日期。
        hour: 小時。
        minute: 分鐘。

    Returns:
        帶 Asia/Taipei 時區的時刻。
    """
    return datetime.datetime(
        day.year, day.month, day.day, hour, minute, tzinfo=periods.TAIPEI)


def test_the_first_morning_is_still_closed_before_nine() -> None:
    # 只比日期的話，開放首日的凌晨就會開始送出加選。
    assert periods.get_current_period(_at(FIRST_DAY, 0, 30)) == "unknown"
    assert periods.get_current_period(_at(FIRST_DAY, 8, 59)) == "unknown"
    assert periods.get_current_period(_at(FIRST_DAY, 9, 0)) == "open"


def test_the_last_day_ends_at_five_not_at_midnight() -> None:
    assert periods.get_current_period(_at(LAST_DAY, 16, 59)) == "open"
    assert periods.get_current_period(_at(LAST_DAY, 17, 0)) == "unknown"
    assert periods.get_current_period(_at(LAST_DAY, 23, 30)) == "unknown"


def test_nights_inside_the_period_are_closed_too() -> None:
    middle = FIRST_DAY + datetime.timedelta(days=1)

    assert periods.get_current_period(_at(middle, 3, 0)) == "unknown"
    assert periods.get_current_period(_at(middle, 12, 0)) == "open"


def test_dept_period_uses_the_same_daily_hours() -> None:
    assert periods.get_current_period(_at(DEPT_FIRST_DAY, 8, 0)) == "unknown"
    assert periods.get_current_period(_at(DEPT_FIRST_DAY, 10, 0)) == "dept"


def test_outside_every_period_is_unknown() -> None:
    after = LAST_DAY + datetime.timedelta(days=1)

    assert periods.get_current_period(_at(after, 10, 0)) == "unknown"


def test_other_timezones_are_converted_not_compared_raw() -> None:
    # 09:00 台北 = 01:00 UTC；跑在 UTC 機器上的人也要拿到一樣的答案。
    utc_nine = datetime.datetime(
        FIRST_DAY.year, FIRST_DAY.month, FIRST_DAY.day, 1, 0,
        tzinfo=datetime.timezone.utc)

    assert periods.get_current_period(utc_nine) == "open"


def test_naive_datetimes_are_read_as_taipei_time() -> None:
    # 時程是學校用當地時間公告的，沒帶時區的時刻只能當成台北時間。
    naive = datetime.datetime(
        FIRST_DAY.year, FIRST_DAY.month, FIRST_DAY.day, 10, 0)

    assert periods.get_current_period(naive) == "open"


def test_scheduled_period_ignores_the_daily_hours() -> None:
    night = _at(FIRST_DAY, 2, 0)

    # 半夜看到提醒的人還是需要那個網址，明天九點才點得下去。
    assert periods.get_scheduled_period(night) == "open"
    assert periods.get_current_period(night) == "unknown"


def test_schedule_expired_only_after_the_last_period() -> None:
    assert periods.schedule_expired(_at(LAST_DAY, 23, 0)) is False
    assert periods.schedule_expired(
        _at(LAST_DAY + datetime.timedelta(days=1), 9, 0)) is True


def test_describe_separates_closed_for_today_from_not_a_period() -> None:
    assert "開放中" in periods.describe(_at(FIRST_DAY, 10, 0))
    assert "收攤" in periods.describe(_at(FIRST_DAY, 2, 0))
    assert periods.describe(
        _at(FIRST_DAY - datetime.timedelta(days=1), 10, 0)) == "非選課時段"

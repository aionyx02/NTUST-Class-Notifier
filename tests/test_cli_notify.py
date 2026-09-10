"""cli.notify：什麼時候會登入選課系統，什麼時候該出聲。"""

import asyncio
import logging

import pytest

from ntust_class_notifier import config
from ntust_class_notifier.cli import notify


def test_a_closed_switch_is_not_even_mentioned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = config.Settings(student_id="B11215000", password="pw")

    with caplog.at_level(logging.DEBUG, logger=notify.logger.name):
        assert asyncio.run(notify.login_selector(settings)) is None

    # 這支也拿來展示：開關沒打開時，log 裡不該出現一個沒在運作的功能。
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


def test_a_switch_turned_on_without_credentials_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = config.Settings(auto_enroll=True, student_id="B11215000")

    with caplog.at_level(logging.DEBUG, logger=notify.logger.name):
        assert asyncio.run(notify.login_selector(settings)) is None

    # 自己把開關打開卻漏填密碼是設定錯誤，不講的話整場選課都不會送出，
    # 使用者卻以為它正在幫忙搶。
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "PASSWORD" in warnings[0]

"""共用的 pytest fixture。"""

import os

import pytest

from ntust_class_notifier import config

_MANAGED_VARS = (
    "DISCORD_BOT_TOKEN",
    "DISCORD_TARGET_IDS",
    "DISCORD_TARGET_USER_IDS",
    "STUDENT_ID",
    "PASSWORD",
    "NTUST_DATA_DIR",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """清掉所有本專案的環境變數，並停用 .env 讀取。

    Args:
        monkeypatch: pytest 的環境變數工具。

    Returns:
        同一個 monkeypatch，方便測試再設定自己要的變數。
    """
    for name in _MANAGED_VARS:
        monkeypatch.delenv(name, raising=False)
    for name in list(os.environ):
        if name.startswith(config.RULE_VAR):
            monkeypatch.delenv(name, raising=False)
    # 測試不該去讀開發機上的 .env。
    monkeypatch.setattr(config, "load_env", lambda: None)
    return monkeypatch

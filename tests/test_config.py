"""config 模組的 .env 讀取。"""

import pathlib

import pytest

from ntust_class_notifier import config


def test_look_up_classes_merges_numbered_vars(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("LOOK_UP_CLASSES", "課號:CS")
    clean_env.setenv("LOOK_UP_CLASSES_2", "課名:體育")
    clean_env.setenv("LOOK_UP_CLASSES_10", "老師:姚")
    clean_env.setenv("LOOK_UP_CLASSES_1", "課號:EE")

    # _1 _2 … _10 要照自然順序，不是字串順序。
    assert config.look_up_classes() == "課號:CS;課號:EE;課名:體育;老師:姚"


def test_look_up_classes_ignores_blank_values(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("LOOK_UP_CLASSES", "  ")
    clean_env.setenv("LOOK_UP_CLASSES_1", "課號:CS")

    assert config.look_up_classes() == "課號:CS"


def test_look_up_classes_empty_when_unset(
    clean_env: pytest.MonkeyPatch,
) -> None:
    assert config.look_up_classes() == ""


@pytest.mark.parametrize(
    "raw",
    ["1;2;3", "1,2,3", "1 2 3", " 1; 2 ,3 "],
)
def test_parse_target_ids_accepts_common_separators(raw: str) -> None:
    assert config.parse_target_ids(raw) == (1, 2, 3)


def test_parse_target_ids_empty() -> None:
    assert config.parse_target_ids("") == ()


def test_parse_target_ids_rejects_non_numeric() -> None:
    with pytest.raises(config.ConfigError):
        config.parse_target_ids("123;abc")


def test_settings_from_env(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LOOK_UP_CLASSES", "課號:CS")
    clean_env.setenv("DISCORD_BOT_TOKEN", "token")
    clean_env.setenv("DISCORD_TARGET_IDS", "42")

    settings = config.Settings.from_env()

    assert settings.look_up_classes == "課號:CS"
    assert settings.discord_target_ids == (42,)
    assert settings.discord_enabled is True
    assert settings.selector_enabled is False


def test_settings_accepts_legacy_target_var(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("DISCORD_TARGET_USER_IDS", "7;8")

    assert config.Settings.from_env().discord_target_ids == (7, 8)


def test_settings_needs_both_account_fields(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("STUDENT_ID", "B11000000")

    assert config.Settings.from_env().selector_enabled is False


def test_data_dir_honours_env_var(
    clean_env: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    target = tmp_path / "state"
    clean_env.setenv("NTUST_DATA_DIR", str(target))

    assert config.data_dir() == target
    assert target.is_dir()  # 不存在時要自己建立。


def test_auto_enroll_stays_off_until_it_is_asked_for(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("STUDENT_ID", "B11215000")
    clean_env.setenv("PASSWORD", "pw")

    # .env 留著帳密（多半是為了別的用途）不該讓程式自己去登入搶課，
    # 一定要明確打開開關。
    assert config.auto_enroll_enabled() is False
    assert config.Settings.from_env().selector_enabled is False


def test_auto_enroll_true_plus_credentials_enables_it(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("AUTO_ENROLL", "true")
    clean_env.setenv("STUDENT_ID", "B11215000")
    clean_env.setenv("PASSWORD", "pw")

    assert config.Settings.from_env().selector_enabled is True


def test_auto_enroll_false_keeps_the_credentials_but_disables_enrolling(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("STUDENT_ID", "B11215000")
    clean_env.setenv("PASSWORD", "pw")
    clean_env.setenv("AUTO_ENROLL", "false")

    settings = config.Settings.from_env()

    assert settings.selector_enabled is False
    assert settings.student_id == "B11215000"  # 不必刪掉帳密就能關掉。


def test_auto_enroll_accepts_the_usual_spellings(
    clean_env: pytest.MonkeyPatch,
) -> None:
    for raw in ("true", "TRUE", "1", "yes", "on"):
        clean_env.setenv("AUTO_ENROLL", raw)
        assert config.auto_enroll_enabled() is True
    for raw in ("false", "False", "0", "no", "off"):
        clean_env.setenv("AUTO_ENROLL", raw)
        assert config.auto_enroll_enabled() is False


def test_auto_enroll_rejects_anything_else(
    clean_env: pytest.MonkeyPatch,
) -> None:
    clean_env.setenv("AUTO_ENROLL", "maybe")

    with pytest.raises(config.ConfigError, match="AUTO_ENROLL"):
        config.auto_enroll_enabled()

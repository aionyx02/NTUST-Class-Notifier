"""core.ruleset：規則字串的解析（純邏輯，不查任何東西）。"""

import pytest

from ntust_class_notifier.core import ruleset


def test_named_fields_and_and_semantics() -> None:
    (rule,) = ruleset.parse_rules("課號:CS 學制:大學部")

    assert rule.codes == ("CS",)
    assert rule.levels == ("OnlyUnderGraduate",)
    assert rule.describe() == "課號 CS 開頭、學制 大學部"


def test_semicolon_makes_two_rules() -> None:
    parsed = ruleset.parse_rules("課號:CS; 課名:程式設計")

    assert len(parsed) == 2
    assert parsed[1].names == ("程式設計",)


def test_comma_lists_multiple_values() -> None:
    (rule,) = ruleset.parse_rules("課號:CS,EE21")

    assert rule.codes == ("CS", "EE21")


def test_english_aliases_and_fullwidth_colon() -> None:
    (rule,) = ruleset.parse_rules("code：CS level:大學部 vacant:yes")

    assert rule.codes == ("CS",)
    assert rule.levels == ("OnlyUnderGraduate",)
    assert rule.only_vacant is True


def test_bare_alnum_token_is_course_code() -> None:
    (rule,) = ruleset.parse_rules(["CS10", "EE21"])

    assert rule.codes == ("CS10", "EE21")


def test_legacy_format_is_detected() -> None:
    (rule,) = ruleset.parse_rules("1151&PE139A053&資訊工程系三")

    assert rule.semester == "1151"
    assert rule.codes == ("PE139A053",)
    assert rule.dept == "資訊工程系三"


def test_empty_source_returns_no_rules() -> None:
    assert ruleset.parse_rules("") == []


def test_unknown_field_suggests_the_right_one() -> None:
    with pytest.raises(ruleset.RuleError, match="老師"):
        ruleset.parse_rules("老蘇:姚")


def test_bad_level_value_is_rejected() -> None:
    with pytest.raises(ruleset.RuleError, match="學制"):
        ruleset.parse_rules("學制:大四")


def test_chinese_bare_token_is_rejected() -> None:
    with pytest.raises(ruleset.RuleError):
        ruleset.parse_rules("程式設計")


def test_too_many_expanded_queries_is_rejected() -> None:
    codes = ",".join(f"C{index:02d}" for index in range(20))
    with pytest.raises(ruleset.RuleError):
        ruleset.parse_rules(f"課號:{codes} 課名:甲,乙")


def test_empty_rule_is_broad() -> None:
    assert ruleset.Rule().is_broad is True
    # 系所不能交給伺服器篩，所以還是要抓全校。
    assert ruleset.Rule(dept="資工三").is_broad is True
    assert ruleset.Rule(codes=("CS",)).is_broad is False


def test_field_help_lists_every_field() -> None:
    help_text = ruleset.field_help()

    for spec in ruleset.FIELDS:
        assert spec.name in help_text

"""分層規則：上層可以用下層，反過來不行。

這支測試讓架構自己守住自己——新增的 import 只要違反方向就會失敗。
"""

import pathlib
import re

import ntust_class_notifier

PACKAGE = pathlib.Path(ntust_class_notifier.__file__).parent

# 每一層可以 import 的層（config 是設定，各層都能用）。
ALLOWED = {
    "core": {"core"},
    "ui": {"core", "ui"},
    "clients": {"core", "clients"},
    "app": {"core", "clients", "ui", "app"},
    "cli": {"core", "clients", "ui", "app", "cli"},
}

_IMPORT = re.compile(r"^from ntust_class_notifier\.(\w+) import", re.MULTILINE)


def _imports(path: pathlib.Path) -> set[str]:
    return set(_IMPORT.findall(path.read_text(encoding="utf-8")))


def test_layers_only_depend_downwards() -> None:
    violations = []
    for path in sorted(PACKAGE.rglob("*.py")):
        layer = path.relative_to(PACKAGE).parts[0]
        if layer not in ALLOWED:
            continue  # config.py、__main__.py 這種放在套件根目錄的檔案。
        for imported in _imports(path) - {"config"}:
            if imported not in ALLOWED[layer]:
                violations.append(
                    f"{path.relative_to(PACKAGE)} 匯入了上層的 {imported}")

    assert violations == []


def test_core_and_ui_do_no_network() -> None:
    # 只有 clients 那一層可以碰 httpx / discord。
    offenders = []
    for layer in ("core", "ui"):
        for path in sorted((PACKAGE / layer).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if re.search(r"^import (httpx|discord)", text, re.MULTILINE):
                offenders.append(str(path.relative_to(PACKAGE)))

    assert offenders == []


def test_every_layer_has_a_docstring() -> None:
    missing = [
        str(path.relative_to(PACKAGE))
        for path in sorted(PACKAGE.rglob("__init__.py"))
        if not path.read_text(encoding="utf-8").lstrip().startswith('"""')
    ]

    assert missing == []

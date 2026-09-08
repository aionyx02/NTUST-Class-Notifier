"""讓 `python -m ntust_class_notifier` 等同於 `ntust-notify`。"""

from ntust_class_notifier.cli import notify

if __name__ == "__main__":
    notify.run()

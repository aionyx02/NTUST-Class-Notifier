"""台科大課程空額監控。

分層（上層可以用下層，反過來不行）：

* `core`：純資料與純邏輯——課程模型、學期換算、篩選規則解析。
* `clients`：對外系統——課程查詢 API、Discord、選課系統、提示音。
* `app`：應用邏輯——把規則變成查詢、比對兩輪結果、三種監控迴圈。
* `ui`：輸出——終端機畫面與 Discord 看板的文字組裝。
* `cli`：三個指令的進入點，負責解析參數與組裝物件。

`config` 是唯一讀取 .env 的地方，各層都可以用。
"""

import importlib.metadata

try:
    # 版本號只有 pyproject.toml 一份，發布流程也只改那一份；手寫在這裡的
    # 副本一定會跟它對不起來。
    __version__ = importlib.metadata.version("ntust-class-notifier")
except importlib.metadata.PackageNotFoundError:  # 沒安裝、直接跑原始碼
    __version__ = "0+unknown"

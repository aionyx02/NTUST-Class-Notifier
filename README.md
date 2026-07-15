# NTUST-Class-Notifier

[![Python](https://img.shields.io/badge/Python-3.13%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](https://opensource.org/licenses/MIT)

監控台科大 (NTUST) 課程空額，並可透過 **Discord Bot** 私訊通知

## 功能

- **課程空額監控** — 透過 `querycourse.ntust.edu.tw` API 並發查詢多門課程的目前人數 / 上限，每 15 秒偵測變動。
- **Discord 通知** — 設定 Bot Token 與目標使用者後，偵測到空位會主動私訊通知。

> 功能皆為獨立可選：只設 `LOOK_UP_CLASSES` 就是純監控 + 終端輸出；加上 Discord 變數才通知。

## 專案結構

```
.
├── main.py             # 主入口：解析環境變數、排程監控與加選任務
├── course_lookup.py    # 課程查詢（querycourse API，非同步並發）
└── discord_bot.py      # Discord Bot 啟動與私訊通知
```

## 安裝與使用

### 1. 先決條件

- **Python 3.13+**
- [**uv**](https://docs.astral.sh/uv/)（套件與虛擬環境管理）

### 2. 取得程式並安裝依賴

```bash
git clone https://github.com/xinshoutw/NTUST-Class-Notifier.git
cd NTUST-Class-Notifier
uv sync
```

### 3. 設定環境變數

寫入專案根目錄的 `.env` 檔（或以 shell `export` 設定）：

| 變數 | 必須 | 說明 |
| --- | --- | --- |
| `LOOK_UP_CLASSES` | ✅ | 要監控的課程清單，格式 `學期&課程代碼&系所(可選)`，多門以 `;` 分隔。 |
| `DISCORD_BOT_TOKEN` | ⬜ | Discord Bot Token；未設定則不啟動 Bot、不發通知。 |
| `DISCORD_TARGET_USER_IDS` | ⬜ | 收通知的 Discord User ID，多個以 `;` 分隔。 |

`.env` 範例：

```dotenv
LOOK_UP_CLASSES=1151&PE139A053&資訊工程系三;1151&CS130A001
DISCORD_BOT_TOKEN=r9mfsU...
DISCORD_TARGET_USER_IDS=810822763601461318;1278934756926423052
```

### 4. 執行

```bash
uv run main.py
```

程式會依環境變數啟動對應功能：讀取 `LOOK_UP_CLASSES` 每 15 秒監控空額；有 Discord 變數則私訊通知。以 `Ctrl+C` 中止。

## 常見問題

1. **沒有收到 Discord 通知？** — 確認已設定 `DISCORD_BOT_TOKEN` 與 `DISCORD_TARGET_USER_IDS`，且 Bot 與該使用者共享伺服器、使用者允許陌生私訊。
2. **如何調整查詢頻率？** — 調整 `main.py` 中各任務的 `asyncio.sleep(...)` 秒數。
3**遇到 429 / Too Many Requests** — 請求過於頻繁被限流，拉長查詢間隔即可。

## 免責聲明

本專案僅供個人學習用途，使用者須自行遵守學校選課系統的相關規範並承擔使用風險。

## 貢獻 & 授權

歡迎 fork 與提交 Pull Request，或於 Issue 中討論。本專案採 MIT 授權。

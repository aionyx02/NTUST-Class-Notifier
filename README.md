# NTUST-Class-Notifier

[![Python](https://img.shields.io/badge/Python-3.13%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](https://opensource.org/licenses/MIT)

監控台科大 (NTUST) 課程空額，並可透過 **Discord Bot** 私訊通知

## 功能

- **課程空額監控** — 透過 `querycourse.ntust.edu.tw` API 並發查詢多門課程的目前人數 / 上限，每 15 秒偵測變動。
- **Discord 通知** — 設定 Bot Token 與目標使用者後，偵測到空位會主動私訊通知。
- **終端機即時監看** — `course_watch.py` 每 5 秒輪詢一次，只在人數變動時輸出一行（被選走整行紅色、有人退選整行綠色）。
- **終端機搶課監控** — `course_alert.py` 一偵測到空位就大字提醒＋音效（Windows / macOS），純提醒不自動加選，也不需要 Discord。
- **統一的篩選規則** — 三支程式共用 `欄位:值` 規則（課號、課名、老師、學制、系所、只看空位），寫在 `.env` 或命令列都行。

> 功能皆為獨立可選：只設 `LOOK_UP_CLASSES` 就是純監控 + 終端輸出；加上 Discord 變數才通知。

## 專案結構

```
.
├── main.py             # 主入口：解析環境變數、排程監控與加選任務
├── course_alert.py     # 終端機搶課監控：有空位就提醒＋音效（只提醒，不自動加選）
├── course_watch.py     # 終端機即時人數監看（可獨立執行）
├── course_filter.py    # 篩選規則：解析、中文說明與查詢執行（三支共用）
├── course_lookup.py    # 課程查詢（querycourse API，非同步並發）
├── course_selector.py  # 選課系統 SSO 登入、自動加選與提示音
├── selection_period.py # 選課時段（電選課加選 / 全校加退選）與連結
├── discord_bot.py      # Discord Bot 啟動與私訊通知
└── .env.example        # 可直接複製成 .env 的範例（含規則說明）
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
| `LOOK_UP_CLASSES` | ✅ | 篩選規則，格式 `欄位:值`，多條以 `;` 分隔（見下方「篩選規則」）。舊格式 `學期&課程代碼&系所` 仍可用。 |
| `DISCORD_BOT_TOKEN` | ⬜ | Discord Bot Token；未設定則不啟動 Bot、不發通知。 |
| `DISCORD_TARGET_USER_IDS` | ⬜ | 收通知的 Discord User ID，多個以 `;` 分隔。 |

`.env` 範例：

```dotenv
LOOK_UP_CLASSES=課號:CS 學制:大學部; 課號:PE139A053 系所:資訊工程系三
DISCORD_BOT_TOKEN=r9mfsU...
DISCORD_TARGET_USER_IDS=810822763601461318;1278934756926423052
```

或直接複製專案內的範例檔：`cp .env.example .env`

### 4. 執行

```bash
uv run main.py
```

程式會依環境變數啟動對應功能：讀取 `LOOK_UP_CLASSES` 每 15 秒監控空額；有 Discord 變數則私訊通知。以 `Ctrl+C` 中止。

## 篩選規則

三支程式共用同一套規則，寫在 `.env` 的 `LOOK_UP_CLASSES` 或直接打在命令列。

**語法**：`欄位:值`，同一條規則用空白隔開，多條規則用 `;` 分隔（取聯集）。**沒寫的欄位就是不限制。**

```dotenv
LOOK_UP_CLASSES=課號:CS 學制:大學部; 課號:PE139A053 系所:資訊工程系三
```
> 意思是「（CS 開頭 **且** 大學部）**或**（PE139A053）」。

| 欄位 | 別名 | 說明 | 範例 |
| --- | --- | --- | --- |
| `學期` | `semester` | 學期代碼；不寫就自動用最新學期 | `學期:1151` |
| `課號` | `code` | 課程代碼**前綴**，不分大小寫 | `課號:CS` |
| `課名` | `name` | 課名**子字串** | `課名:程式設計` |
| `老師` | `teacher` | 老師姓名**子字串** | `老師:姚` |
| `學制` | `level` | `大學部` / `研究所` / `通識` | `學制:大學部` |
| `系所` | `dept` | 系所名額身分（命中 50 門以內才查） | `系所:資訊工程系三` |
| `只看空位` | `vacant` | `是` / `否` | `只看空位:是` |

- **同一欄位可用逗號列多值**（任一符合即可）：`課號:CS,EE21`。
- **命令列會取代 `.env`**，兩邊都沒有就是監看全校所有課程。
- **純英數的裸字視為課號**：`course_watch.py CS10` 等同 `課號:CS10`。
- **中文欄位名可換成英文別名**：`code:CS level:大學部 vacant:yes` 等價。
- **全形冒號也吃**：`課號：CS` 可以正常解析。

### 規則怎麼被解讀

啟動時會逐條回顯，確認系統跟你想的一樣：

```
── 監看 50 門課程（每 5 秒；Ctrl+C 結束）──
   規則 1：課號 CS 開頭、學制 大學部          →  49 門
   規則 2：課號 PE139A053 開頭、系所 資訊工程系三  →  1 門
   學期 1151
```

### 常見錯誤

| 寫法 | 結果 |
| --- | --- |
| `老蘇:姚` | 報錯並提示「你是不是要打『老師』？」 |
| `學制:大四` | 報錯：學制只能是 大學部／研究所／通識 |
| `程式設計`（中文裸字） | 報錯：請寫成 `課名:程式設計` 或 `老師:程式設計` |
| 全部規則都命中 0 門 | 停止並列出放寬建議（子字串／前綴／學期） |

## 終端機即時人數監看

不需要 Discord，直接在終端機盯人數變化：

```bash
uv run course_watch.py 課號:CS 學制:大學部   # CS 開頭的大學部課程
uv run course_watch.py CS10 EE21             # 裸字視為課號，多個取聯集
uv run course_watch.py 課名:程式設計 老師:姚  # 課名與老師都是子字串
uv run course_watch.py -i 10 -s 1151         # 自訂週期與學期
uv run course_watch.py                       # 讀 .env；.env 也空就是全校
```

- **只在有變動時輸出** — 人數沒變就不印任何東西，底部僅有一行狀態列。整行文字上色：
  - 紅色 `44 → 45/55 (+1) 被選走`：名額被別人選走
  - 綠色 `45 → 44/55 (-1) 有人退選`：有人釋出名額
- **`只看空位:是`** — 只印跟空位有關的變動，但保留「剛好把最後一個名額填滿」那一筆，不會靜靜消失。
- 其他選項：`-i/--interval` 每輪週期秒數（預設 5，最低 1）、`-s/--semester`、`--list` 列出全部課程（預設超過 50 門省略）、`--no-color`、`-d` 除錯訊息。

## 終端機搶課監控

不啟動 Discord bot，直接在終端機等空位：

```bash
uv run course_alert.py 課號:CS1003301                    # 監控單一課程
uv run course_alert.py CS1003301 EE21                    # 裸字視為課號
uv run course_alert.py 課號:PE139A053 系所:資訊工程系三   # 一併檢查系所名額
uv run course_alert.py 課名:程式設計 學制:大學部          # 課名 + 學制
uv run course_alert.py                                   # 讀 .env；.env 也空就是全校
```

- **只提醒，不自動加選** — 只查詢公開的課程查詢 API，不會登入選課系統、不會替你送出加選請求。
- **只在「額滿 → 有空位」的瞬間提醒** — 大字綠色橫幅（課名、老師、時間、剩餘名額、選課連結）＋音效；空位持續存在不重複洗版，名額被補滿時補一行紅字。
- **音效** — Windows 用系統嗶聲、macOS 用系統音效，其他平台為終端機響鈴；`--no-sound` 可關閉。
- **系所名額檢查** — 寫了 `系所:…` 時會查該課程的系所保留名額，擋掉「總數有空位但你的系已滿」的假空位；命中超過 50 門則跳過並提示（那支 API 一門課要一次請求）。
- 其他選項同上，另有 `--no-sound`。

## 學期與週期

- **學期自動判斷** — 以 6 月為界推算（電選課加選在 6 月下旬，搶的已是下個上學期），啟動時再用一次輕量查詢確認；查不到資料就自動退一學期並告知。
- **舊學期只列不監看** — 指定比目前舊的學期（例如 `-s 1142`）時，列出結果就結束，不會空轉輪詢。
- **週期自動保護** — `-i` 是「每輪週期」且已扣掉查詢耗時；查詢比週期還久時自動改用「查詢耗時 × 2」。全校查詢約 4200 門、2 MB、伺服器要跑 70~80 秒，所以全校模式實際約 2~3 分鐘一輪。

## 常見問題

1. **沒有收到 Discord 通知？** — 確認已設定 `DISCORD_BOT_TOKEN` 與 `DISCORD_TARGET_USER_IDS`，且 Bot 與該使用者共享伺服器、使用者允許陌生私訊。
2. **如何調整查詢頻率？** — 兩支終端機腳本用 `-i` 指定每輪週期；`main.py` 則調整各任務的 `asyncio.sleep(...)` 秒數。
3. **規則寫了卻沒作用？** — 啟動時的逐條回顯會顯示每條規則命中幾門；`→ 0 門` 就代表那條沒抓到東西。
4. **遇到 429 / Too Many Requests** — 請求過於頻繁被限流，拉長 `-i` 即可；規則涵蓋太多課程時週期也會自動放寬。

## 免責聲明

本專案僅供個人學習用途，使用者須自行遵守學校選課系統的相關規範並承擔使用風險。

## 貢獻 & 授權

歡迎 fork 與提交 Pull Request，或於 Issue 中討論。本專案採 MIT 授權。

# NTUST-Class-Notifier

[![Python](https://img.shields.io/badge/Python-3.13%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](https://opensource.org/licenses/MIT)

監控台科大 (NTUST) 課程空額：終端機即時盯人數、課程一有空位就提醒，或透過 Discord Bot 發通知。

---

## 目錄

- [功能](#功能)
- [需求](#需求)
- [安裝](#安裝)
- [快速開始](#快速開始)
- [設定](#設定)
- [使用方式](#使用方式)
- [篩選規則](#篩選規則)
- [專案結構](#專案結構)
- [開發](#開發)
- [疑難排解](#疑難排解)
- [免責聲明](#免責聲明)
- [授權](#授權)

---

## 功能

| 指令 | 用途 | 需要 Discord | 會登入選課系統 |
| --- | --- | :---: | :---: |
| `ntust-watch` | 每 5 秒盯人數，只在有變動時輸出一行（被選走紅色、有人退選綠色） | ✗ | 選填 |
| `ntust-alert` | 課程一出現空位就大字提醒＋音效 | ✗ | 選填 |
| `ntust-notify` | 把狀態發到 Discord，永遠只留一則自動更新的訊息 | ✓ | 選填 |

**自動加選三支完全一樣，而且預設關閉**：`.env` 要明確寫 `AUTO_ENROLL=true`、
而且 `STUDENT_ID` / `PASSWORD` 都填了，才會登入選課系統並自動加選；只填帳密
不會啟用。`--no-enroll` 可以單次停用。送出時間也只限學校公告的選課時段、
而且是當天開放時間（09:00–17:00）內。

三個指令共用同一套 `欄位:值` 篩選規則，寫在 `.env` 或直接打在命令列都行。

## 需求

- [Python 3.13+](https://www.python.org/)
- [uv](https://docs.astral.sh/uv/)（套件與虛擬環境管理）

## 安裝

```bash
git clone https://github.com/aionyx02/NTUST-Class-Notifier.git
cd NTUST-Class-Notifier
uv sync
```

### 或者：下載 `ntust.ps1`（Windows，不用 clone）

到 [Releases](https://github.com/aionyx02/NTUST-Class-Notifier/releases)
下載 `ntust.ps1`，參數直接接在後面：

```powershell
.\ntust.ps1 課號:TCG175302              # 監看人數
.\ntust.ps1 -Mode alert 課號:TCG175302  # 搶課提醒＋音效
.\ntust.ps1 -AutoEnroll 課號:TCG175302  # 一併自動加選
.\ntust.ps1 課號:CS -- -i 10 --list     # 「--」後面原樣傳給程式
```

腳本旁邊找得到 `pyproject.toml` 就跑本機原始碼，找不到就直接跑 GitHub 上
對應版本的程式碼，兩種情況都不需要編譯。**自動加選預設關閉**，要開才加
`-AutoEnroll`；想完全照 `.env` 的 `AUTO_ENROLL` 決定就加 `-UseEnvSwitch`。

#### 帳密

沒有 `.env` 也能用。第一次加 `-SaveCredential` 輸入一次，之後就不用再輸入：

```powershell
.\ntust.ps1 -AutoEnroll -StudentId B11415024 -SaveCredential 課號:TCG175302
.\ntust.ps1 -AutoEnroll 課號:TCG175302   # 之後直接跑
.\ntust.ps1 -ForgetCredential            # 刪掉存起來的帳密
```

取得順序是 `-StudentId`／提示輸入 → 加密檔 → `.env`；`.env` 已經備齊帳密時
不會多問。**沒有 `-Password` 這個參數**：PowerShell 會把每一行指令原文寫進
`ConsoleHost_history.txt` 永久保存，密碼一律用 `Read-Host -AsSecureString`
輸入，並以環境變數交給子行程，不會出現在歷史紀錄或行程命令列裡。
`-SaveCredential` 存的檔在 `%LOCALAPPDATA%\ntust-class-notifier\`，用 Windows
DPAPI 加密——只有你這個 Windows 帳號、在這台機器上解得開。

## 快速開始

```bash
# 1. 直接在終端機盯一門課，不用任何設定
uv run ntust-watch 課號:CS1003301

# 2. 想用 .env 的規則、或想發 Discord 通知，先建立設定檔
cp .env.example .env      # Windows PowerShell：copy .env.example .env

# 3. 編輯 .env 的 LOOK_UP_CLASSES，然後
uv run ntust-notify
```

## 設定

設定寫在專案根目錄的 `.env`（也可以用環境變數）。完整說明與範例見 [`.env.example`](.env.example)。

| 變數 | 必填 | 可以放什麼 |
| --- | :---: | --- |
| `LOOK_UP_CLASSES` | ✓ | 篩選規則，格式 `欄位:值`，多條以 `;` 分隔（見[篩選規則](#篩選規則)） |
| `LOOK_UP_CLASSES_1`, `_2`… | | 想一行寫一條規則時用，會與 `LOOK_UP_CLASSES` 合併 |
| `DISCORD_BOT_TOKEN` | | Discord Bot Token；沒填就不啟動 Bot |
| `DISCORD_TARGET_IDS` | | 收通知的 ID，多個以 `;` 分隔。伺服器 ID → 自動挑一個能發言的文字頻道、頻道 ID → 發到該頻道、使用者 ID → 發私訊。舊名稱 `DISCORD_TARGET_USER_IDS` 仍可用 |
| `AUTO_ENROLL` | | 自動加選總開關，填 `true` / `false`。**沒設定就是 `false`**，必須明確寫 `true` 才會啟用 |
| `STUDENT_ID` / `PASSWORD` | | 選課系統帳密。要與 `AUTO_ENROLL=true` 同時具備才會登入；只填帳密不會加選。**有風險，預設請留空** |
| `NTUST_DATA_DIR` | | cookie 等執行期檔案的存放目錄（預設 `~/.ntust-class-notifier`） |
| `NO_COLOR` | | 設任何值就停用終端機色彩 |

```dotenv
LOOK_UP_CLASSES=課號:CS 學制:大學部; 課號:PE139A053 系所:資訊工程系三
DISCORD_BOT_TOKEN=r9mfsU...
DISCORD_TARGET_IDS=810822763601461318;1278934756926423052

# 自動加選：預設關閉，三個都齊了才會登入選課系統
AUTO_ENROLL=true
STUDENT_ID=B11215000
PASSWORD=...
```

## 使用方式

### 終端機即時人數監看

```bash
uv run ntust-watch 課號:CS 學制:大學部    # CS 開頭的大學部課程
uv run ntust-watch CS10 EE21              # 裸字視為課號，多個取聯集
uv run ntust-watch 課名:程式設計 老師:姚   # 課名與老師都是子字串
uv run ntust-watch -i 10 -s 1151          # 自訂週期與學期
uv run ntust-watch                        # 讀 .env；.env 也空就是全校
```

人數沒變就不輸出任何東西，底部只留一行狀態列。有變動時整行上色：

```
[14:32:07] CS1003301  計算機程式設計   姚智原   W6,W7,W8   44 → 45/55 (+1)  被選走，剩 10
```

### 終端機搶課監控

```bash
uv run ntust-alert 課號:CS1003301                    # 監控單一課程
uv run ntust-alert CS1003301 EE21                    # 裸字視為課號
uv run ntust-alert 課號:PE139A053 系所:資訊工程系三   # 一併檢查系所名額
uv run ntust-alert                                   # 讀 .env；.env 也空就是全校
```

只在課程「額滿 → 有空位」的瞬間提醒（大字綠色橫幅＋音效），空位持續存在不會重複洗版，名額被補滿時補一行紅字。預設**只提醒、不加選**；`.env` 打開 `AUTO_ENROLL=true` 並填了帳密時，會用跟另外兩支完全相同的規則一併送出加選。

### Discord 通知

```bash
uv run ntust-notify
```

- **永遠只有一則訊息** — 每次更新都直接編輯同一則（不是先刪再送，所以更新失敗時頻道也不會空著）；訊息 ID 存在 `NTUST_DATA_DIR`，程式重開也接得回同一則。
- **人數一有變動就更新** — 任何一門課被選走或有人退選都會馬上重送看板，下方列出這一輪的變動（`-` 紅色被選走、`+` 綠色有人退選）；完全沒有變動時每 5 分鐘也會重送一次。
- **收件對象自動判斷** — 伺服器 / 頻道 / 使用者 ID 都能填，判斷結果會寫在啟動 log。

### 共用選項

| 選項 | 說明 |
| --- | --- |
| `-i, --interval` | 每輪週期秒數（`ntust-watch` 預設 5、`ntust-alert` 預設 3，最低 1） |
| `-s, --semester` | 學期代碼，例如 `1151`（預設自動判斷） |
| `--list` | 啟動時列出全部課程（預設超過 50 門省略） |
| `--no-color` | 停用色彩輸出 |
| `--no-sound` | 停用提示音（只有 `ntust-alert` 有） |
| `-d, --debug` | 顯示除錯訊息 |

## 篩選規則

**語法**：`欄位:值`。同一條規則用空白隔開（＝而且），多條規則用 `;` 分隔（＝或者），**沒寫的欄位就是不限制**。

```dotenv
LOOK_UP_CLASSES=課號:CS 學制:大學部; 課號:PE139A053 系所:資訊工程系三
```

> 意思是「（CS 開頭 **且** 大學部）**或**（PE139A053）」。

| 欄位 | 別名 | 比對方式 | 範例 |
| --- | --- | --- | --- |
| `課號` | `code` | 課號**開頭**，不分大小寫 | `課號:CS` |
| `課名` | `name` | 課名**子字串** | `課名:程式設計` |
| `老師` | `teacher` | 老師姓名**子字串** | `老師:姚` |
| `學制` | `level` | `大學部` / `研究所` / `通識` | `學制:大學部` |
| `系所` | `dept` | 系所名額身分（命中 50 門以內才查） | `系所:資訊工程系三` |
| `只看空位` | `vacant` | `是` / `否` | `只看空位:是` |
| `學期` | `semester` | 四碼學期代碼（預設自動判斷） | `學期:1151` |

其他細節：

- **同一欄位可用逗號列多值**（任一符合）：`課號:CS,EE21`。
- **命令列會取代 `.env`**，兩邊都沒有就是監看全校所有課程。
- **`.env` 同名變數不能寫兩行**（只有最後一行有效），要分行請用 `LOOK_UP_CLASSES_1`、`_2`…。
- **純英數的裸字視為課號**：`ntust-watch CS10` 等同 `課號:CS10`。
- **全形冒號也吃**：`課號：CS` 可以正常解析。
- **舊格式仍可用**：`學期&課號&系所`（例如 `1151&PE139A053&資訊工程系三`）會自動辨識。

啟動時會逐條回顯，確認程式跟你想的一樣：

```
── 監看 50 門課程（每 5 秒；Ctrl+C 結束）──
   規則 1：課號 CS 開頭、學制 大學部              →  49 門
   規則 2：課號 PE139A053 開頭、系所 資訊工程系三  →  1 門
   學期 1151
```

常見錯誤：

| 寫法 | 結果 |
| --- | --- |
| `老蘇:姚` | 報錯並提示「你是不是要打『老師』？」 |
| `學制:大四` | 報錯：學制只能是 大學部／研究所／通識 |
| `程式設計`（中文裸字） | 報錯：請寫成 `課名:程式設計` 或 `老師:程式設計` |
| 全部規則都命中 0 門 | 停止並列出放寬建議（子字串／前綴／學期） |

## 專案結構

分層由上而下，**上層可以用下層，反過來不行**：

```
.
├── src/ntust_class_notifier/
│   ├── config.py          # 唯一讀取 .env 的地方
│   ├── core/              # 純資料與純邏輯，不做任何 I/O
│   │   ├── models.py      #   課程資料、學期換算
│   │   ├── ruleset.py     #   欄位:值 規則的解析與說明
│   │   ├── changes.py     #   兩輪之間的比對結果
│   │   └── periods.py     #   選課時段與連結
│   ├── clients/           # 對外系統
│   │   ├── course_api.py  #   querycourse 課程查詢 API
│   │   ├── discord_bot.py #   Discord 連線、收件對象判斷
│   │   ├── enrollment.py  #   選課系統 SSO 登入與加選
│   │   └── sound.py       #   跨平台提示音
│   ├── app/               # 應用邏輯（core + clients）
│   │   ├── search.py      #   規則 → 實際查詢、決定學期
│   │   ├── monitor.py     #   輪詢節奏（查詢太久就自動放寬）
│   │   ├── watch.py       #   人數監看迴圈
│   │   ├── alert.py       #   搶課提醒迴圈
│   │   └── notify.py      #   Discord 監控迴圈
│   ├── ui/                # 輸出（只把資料變成畫面）
│   │   ├── console.py     #   色彩、等寬對齊、狀態列
│   │   ├── report.py      #   終端機的規則回顯、清單與變動
│   │   └── board.py       #   Discord 看板的文字組裝
│   ├── cli/               # 三個指令的進入點
│   │   ├── options.py     #   共用參數解析與規則載入
│   │   └── watch.py / alert.py / notify.py
│   └── __main__.py        # python -m ntust_class_notifier
├── tests/                 # pytest 測試（全部離線，不會打到學校 API）
├── .github/workflows/     # CI：ruff + pytest
├── .env.example           # 設定範例（含完整參數說明）
└── pyproject.toml         # 相依套件、指令進入點與 ruff / pytest 設定
```

幾個刻意的設計：

- **人數變動只算一次** — `core/changes.py` 的 `diff_rounds()` 產生 `Change`，終端機（`ui/report.py`）與 Discord（`ui/board.py`）各自把同一份結果 render 成自己的樣子。
- **進入點很薄** — `cli/` 只負責解析參數與組裝物件，監控邏輯都在 `app/`，所以測試可以直接呼叫 `app` 而不用碰 argparse。
- **只有 `clients/` 會碰網路** — `core/` 與 `ui/` 都是純函式，測試不需要任何 mock server。

也可以用模組方式執行，例如 `uv run python -m ntust_class_notifier.cli.watch 課號:CS`。

## 開發

```bash
uv sync --all-groups   # 安裝含開發用的相依套件
uv run pytest          # 執行測試
uv run ruff check .    # 檢查程式風格
```

- 測試全部離線（用假的查詢客戶端與假的 Discord 物件），不會打到學校 API，也不會連 Discord。
- 風格規範寫在 `pyproject.toml` 的 `[tool.ruff]`：行寬 80、每個 import 各自一行、docstring 採 Google 格式。
- `.github/workflows/ci.yml` 會在 push 與 PR 時跑同樣的兩個指令。
- 新增程式碼時請遵守分層方向：`cli` → `app` → `clients` / `ui` → `core`，不要讓 `core`、`ui` 反過來 import 上層——`tests/test_architecture.py` 會擋下來。

## 疑難排解

1. **沒有收到 Discord 通知？** — 確認已設定 `DISCORD_BOT_TOKEN` 與 `DISCORD_TARGET_IDS`。啟動 log 會印出「通知對象 … 判定為 …」，沒有這一行就代表 ID 不對；填使用者 ID 時 Bot 需與該使用者共享伺服器且對方允許陌生私訊，填伺服器 ID 時 Bot 需要「發送訊息」權限。
2. **規則寫了卻沒作用？** — 啟動時的逐條回顯會顯示每條規則命中幾門，`→ 0 門` 就是那條沒抓到東西。
3. **遇到 429 / Too Many Requests？** — 程式會自己退避重試並延長下一輪，log 會出現「被限流，先等 N 秒」；一直出現就拉長 `-i`。規則涵蓋太多課程時週期也會自動放寬。
4. **找不到 `ntust-watch` 指令？** — 先跑一次 `uv sync`（會把專案裝進虛擬環境），或改用 `uv run python -m ntust_class_notifier.cli.watch`。

## 免責聲明

本專案僅供個人學習用途，使用者須自行遵守學校選課系統的相關規範並承擔使用風險。

## 授權

MIT License，詳見 [LICENSE](LICENSE)。歡迎 fork 與提交 Pull Request，或於 Issue 中討論。

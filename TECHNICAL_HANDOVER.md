# PAD Self-Healing IT 技術交接文件

## 1. 專案概觀

### 1.1 系統用途

`CustomerDeliveryBundle` 是一套提供 Power Automate Desktop（PAD）使用的瀏覽器自動化與錯誤自我修復工具包，主要功能如下：

1. 啟動指定的 Chrome 或 Edge 瀏覽器。
2. 透過 PAD 瀏覽器擴充功能及 CDP 連線，讓 PAD 進行網頁操作。
3. PAD 發生網頁操作錯誤時，擷取錯誤畫面與頁面資訊。
4. 呼叫內部 AI Gateway 分析錯誤並嘗試修復。
5. 將修復結果寫入 `result.json`，並可透過 Teams 發送通知。

### 1.2 目前交付範圍

目前 `PAD_flows\chrome.txt` 與 `PAD_flows\edge.txt` 為瀏覽器啟動流程：

- `chrome.txt`：啟動 Chrome 並開啟指定網站。
- `edge.txt`：啟動 Edge 並開啟指定網站。
- `AI_ExceptionHandler.txt`：PAD 發生錯誤時使用的錯誤處理流程。

### 1.3 主要使用者

- Power Automate Desktop 流程維護人員
- IT 維運與桌面環境管理人員
- 需要處理 PAD 網頁自動化錯誤的業務或支援人員

### 1.4 服務等級

本工具包目前沒有定義正式 SLA。執行時需要使用者電腦保持開機、PAD 可用、瀏覽器可啟動，且能連線到內部 AI Gateway。

---

## 2. 系統架構與環境資訊

### 2.1 系統架構簡圖

```text
[Power Automate Desktop]
          │
          ├── chrome.txt / edge.txt
          │          │
          │          ▼
          │   start_pad_browser.ps1
          │          │
          │          ▼
          │   Chrome / Edge + PAD Extension
          │          │
          │          └── CDP Port 9222
          │
          └── 發生錯誤
                    │
                    ▼
          AI_ExceptionHandler.txt
                    │
                    ▼
          start_webwright_agent.ps1
                    │
                    ▼
          webwright_agent.py
                    │
                    ├── 讀取截圖與頁面資訊
                    ├── 呼叫內部 AI Gateway
                    ├── 嘗試網頁修復
                    └── 寫入 result.json / Teams 通知
```

### 2.2 執行環境

| 項目 | 目前需求 | 備註 |
|---|---|---|
| 作業系統 | Windows | 流程使用 PowerShell、PAD 與 Windows 瀏覽器 |
| Power Automate | Power Automate Desktop | 需安裝 PAD 及對應瀏覽器擴充功能 |
| 瀏覽器 | Chrome 或 Microsoft Edge | 目前 Edge 流程連至內部 WMS 網站 |
| Python | Python 3.11 以上 | 由 `install.ps1` 建立 `.venv` |
| Python 套件 | Playwright、httpx、BeautifulSoup4 | 可由 `wheelhouse` 離線安裝 |
| CDP | 本機 TCP Port 9222 | 用於連接已啟動的瀏覽器 |
| AI Gateway | `http://172.22.8.15:8080` | 內部網路服務，非公開服務 |
| Teams | Microsoft Teams Connector | 錯誤通知使用，需由接手人員重新確認連線權限 |

---

## 3. 本地建置與安裝

### 3.1 前置條件

1. Windows 電腦。
2. 已安裝 Power Automate Desktop。
3. 已安裝 Chrome 或 Edge，以及 PAD 瀏覽器擴充功能。
4. 可連線至內部 AI Gateway。
5. 具備安裝 Python 或使用 `winget` 的權限。

### 3.2 安裝步驟

在 PowerShell 中進入專案資料夾：

```powershell
cd D:\CustomerDeliveryBundle
```

可使用以下任一方式安裝：

```powershell
.\install.bat
```

或：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

安裝程式會執行以下工作：

- 確認或安裝 Python。
- 建立 `.venv` 虛擬環境。
- 從 `wheelhouse` 安裝 Python 套件；若不存在則嘗試從 PyPI 安裝。
- 建立執行期間使用的資料夾。

目前程式使用 CDP 連接本機瀏覽器，通常不需要另外下載 Playwright 瀏覽器；只有設定 `INSTALL_PLAYWRIGHT_BROWSERS=true` 時才會下載 Chromium。

### 3.3 PAD 流程使用方式

`PAD_flows` 內的 `.txt` 是 PAD 流程內容或匯出參考，不是可直接雙擊執行的 Windows 程式。需依公司 PAD 流程管理方式匯入或貼入 PAD。

| 檔案 | 用途 |
|---|---|
| `chrome.txt` | 啟動 Chrome 的 PAD 流程片段 |
| `edge.txt` | 啟動 Edge 的 PAD 流程片段 |
| `AI_ExceptionHandler.txt` | PAD 錯誤處理、自我修復與 Teams 通知流程 |

---

## 4. 程式碼目錄結構

```text
D:\CustomerDeliveryBundle
├── PAD_flows
│   ├── AI_ExceptionHandler.txt   # PAD 錯誤處理與 Teams 通知
│   ├── chrome.txt                # Chrome 啟動流程
│   └── edge.txt                  # Edge 啟動流程
├── home\ubuntu
│   ├── start_pad_browser.ps1     # 啟動瀏覽器、PAD 擴充功能及 CDP
│   ├── start_webwright_agent.ps1 # 啟動 Python 自我修復代理程式
│   └── webwright_agent.py        # AI 分析、頁面擷取與修復執行器
├── CustomerPackage\runtime
│   └── RPA_Error                  # 執行期間錯誤產物與暫存資料
├── wheelhouse                     # Python 離線安裝套件
├── install.ps1                    # PowerShell 安裝腳本
├── install.bat                    # Windows 快速安裝入口
└── requirements.txt               # Python 套件清單
```

---

## 5. 設定與敏感資訊

| 設定項目 | 是否必填 | 說明 | 建議存放位置 |
|---|---:|---|---|
| `OPENAI_API_KEY` / `AI_API_KEY` | 是 | AI Gateway 驗證用金鑰 | 密碼金庫或執行環境變數 |
| `AI_API_BASE_URL` | 是 | 目前固定為內部 AI Gateway | 設定檔或 IT 管理文件 |
| `AI_MODEL` | 否 | 目前預設為 `gpt-5.4-nano` | 設定檔 |
| `AI_SYSTEM_NAME` | 否 | 目前預設為 `PAD_Self_Healing_System` | 設定檔 |
| `AI_CDP_PORT` | 否 | 預設使用 `9222` | 設定檔 |
| `ErrorFolder` | 是 | 預設為 `D:\CustomerDeliveryBundle\CustomerPackage\runtime\RPA_Error` | 目前寫在 PAD 流程中 |

目前 `chrome.txt` 與 `edge.txt` 的 `OpenAiKey` 為空值；若要啟用 AI 錯誤修復，需由安全方式提供 API Key。

### 5.1 程式修改導覽

| 想修改的內容 | 優先查看檔案 | 修改位置／注意事項 |
|---|---|---|
| 修改 Chrome 或 Edge 開啟的網站 | `PAD_flows\chrome.txt`、`PAD_flows\edge.txt` | 修改 `-TargetUrl`；目前兩個檔案只負責啟動瀏覽器 |
| 修改預設瀏覽器、瀏覽器路徑或 CDP Port | `home\ubuntu\start_pad_browser.ps1` | 查看檔案開頭參數、`$browserDefinitions` 及瀏覽器啟動參數 |
| 修改 PAD 擴充功能處理方式 | `home\ubuntu\start_pad_browser.ps1` | 查看 CRX 解包、擴充功能路徑及 Native Messaging 設定 |
| 修改 AI Gateway 位址或 AI 模型 | `home\ubuntu\start_webwright_agent.ps1`、`home\ubuntu\webwright_agent.py` | PowerShell 啟動器會設定內部 Gateway；Python 的 `_resolve_api_settings()` 也有預設值，兩處需同步確認 |
| 修改 AI 判斷規則或提示內容 | `home\ubuntu\webwright_agent.py` | 查看 `_build_prompt_text()`、AI system prompt 及 workflow guidance |
| 修改自癒允許的操作類型 | `home\ubuntu\webwright_agent.py` | 查看 `ALLOWED_ACTIONS`、`SOFT_RECOVERY_ACTIONS`；新增操作前需評估安全性 |
| 修改自癒實際執行方式 | `home\ubuntu\webwright_agent.py` | 查看 `_execute_action_plan()`；包含點擊、輸入、切換分頁、彈窗及導覽 |
| 修改修復成功的判斷方式 | `home\ubuntu\webwright_agent.py` | 查看 `_verify_recovery()` 及 Verification 規則 |
| 修改錯誤截圖、context 或 result.json | `PAD_flows\AI_ExceptionHandler.txt` | 查看錯誤資料夾、`context.json`、`ERROR.jpg`、`result.json` 的讀寫流程 |
| 修改 Teams 錯誤通知 | `PAD_flows\AI_ExceptionHandler.txt` | 查看 Teams Connector 動作及通知內容；更換帳號時需在 PAD 重新連線 |
| 修改 Python 套件 | `requirements.txt`、`wheelhouse`、`install.ps1` | 先更新 `requirements.txt`，離線交付時同步更新 `wheelhouse` |
| 修改安裝流程 | `install.ps1`、`install.bat` | `install.bat` 是入口，主要安裝邏輯在 `install.ps1` |
| 修改錯誤產物保存位置 | `PAD_flows\AI_ExceptionHandler.txt`、`home\ubuntu\start_webwright_agent.ps1` | 目前預設為 `CustomerPackage\runtime\RPA_Error`，修改後需同步確認所有路徑 |

修改後建議至少執行：瀏覽器啟動測試、CDP 連線測試、錯誤處理測試，並確認 `result.json` 能正常產生。

---

## 6. 執行流程與輸出

### 6.1 正常啟動流程

1. PAD 執行 `chrome.txt` 或 `edge.txt`。
2. `start_pad_browser.ps1` 找到瀏覽器與 PAD 擴充功能。
3. 建立專用瀏覽器 Profile 與擴充功能資料夾。
4. 使用 CDP Port 9222 啟動瀏覽器。
5. PAD 連線到指定的網站。

### 6.2 錯誤自我修復流程

1. PAD 錯誤處理流程擷取前景畫面。
2. 建立或讀取 `context.json`。
3. 啟動 `start_webwright_agent.ps1`。
4. Python 代理程式連接現有瀏覽器並取得頁面狀態。
5. 將截圖、頁面資訊與錯誤步驟送至 AI Gateway。
6. AI 回傳修復計畫後，代理程式執行受限制的瀏覽器操作。
7. 執行結果寫入 `result.json`。
8. PAD 讀取結果並可透過 Teams 發送通知。

### 6.3 執行產物

| 檔案 | 用途 |
|---|---|
| `context.json` | PAD 傳給自我修復代理程式的錯誤上下文 |
| `ERROR.jpg` | 錯誤當下的畫面截圖 |
| `result.json` | 修復狀態、訊息、驗證結果與人工處理判斷 |
| `bridge.log` | Python 代理程式執行紀錄 |
| `python_stdout.log` / `python_stderr.log` | Python 標準輸出與錯誤輸出 |
| `pad-browser-state.json` | 瀏覽器啟動狀態 |

`RPA_Error` 可能包含內部網址、頁面內容、畫面截圖或其他敏感資訊，不應直接公開或長期保存。

---

## 7. 維運與故障排除

### 7.1 瀏覽器無法啟動或 CDP 無法連線

檢查順序：

1. 確認 Chrome／Edge 與 PAD 未被其他程式鎖定。
2. 確認本機 Port 9222 沒有被其他瀏覽器程序使用。
3. 確認 PAD 瀏覽器擴充功能已安裝。
4. 確認 `RPA_Error` 資料夾可寫入。
5. 重新執行對應的 PAD 啟動流程。

### 7.2 AI 自我修復失敗

檢查以下項目：

- `OPENAI_API_KEY` 是否已由安全方式提供。
- 電腦是否能連線至 `http://172.22.8.15:8080`。
- `.venv\Scripts\python.exe` 是否存在。
- `context.json` 與 `ERROR.jpg` 是否存在且可讀取。
- `python_stderr.log`、`bridge.log` 及 `result.json` 內容。

### 7.3 Teams 通知失敗

確認 PAD 使用者的 Teams Connector 連線仍有效，且具備目標 Teams 頻道的發送權限。若交接給不同帳號，通常需要在 PAD 中重新建立或重新指定 Teams 連線。

### 7.4 清理執行資料

停止 PAD 與瀏覽器後，可清理 `CustomerPackage\runtime\RPA_Error` 內的舊截圖、日誌、瀏覽器 Profile 與擴充功能暫存資料。交接包只需保留空的 `RPA_Error` 資料夾即可。

---

## 8. 外部服務與權限

| 服務／元件 | 用途 | 需要的權限或條件 |
|---|---|---|
| Power Automate Desktop | 執行 PAD 流程 | 使用者可執行及維護 PAD 流程 |
| Chrome／Microsoft Edge | 網頁自動化 | 安裝 PAD 瀏覽器擴充功能 |
| 內部 AI Gateway | AI 錯誤分析與修復 | 內網連線及 API Key |
| Microsoft Teams | 錯誤通知 | Teams Connector 連線及頻道發送權限 |
| Python / wheelhouse | 執行 AI 代理程式 | 安裝 Python 套件的權限 |

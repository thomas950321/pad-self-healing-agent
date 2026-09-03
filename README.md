<div align="center">

# CustomerDeliveryBundle

**專為 Power Automate Desktop 打造的 AI 瀏覽器自動化與錯誤自我修復工具包**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows%20%2F%20PAD-0078D4.svg)](https://powerautomate.microsoft.com/)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org/)

</div>

---

## 專案簡介

`CustomerDeliveryBundle` 是一套針對企業級 **Power Automate Desktop (PAD)** 網頁自動化流程所設計的「AI 瀏覽器接管與錯誤自我修復」交付工具包。

當 PAD 執行網頁自動化遇到動態彈窗干擾、DOM 元素變更或連線逾時等異常時，此工具包會自動擷取錯誤當下畫面與頁面狀態，調用內部 **AI Gateway** 進行視覺與 DOM 結構分析，即時生成並線上執行修復計畫（如自動關閉關聯彈窗、重新導向或切換分頁），使 RPA 流程無需人工介入即可自動恢復運作。

---

## 解決痛點

在傳統的企業 RPA 流程維護中，網頁自動化經常面臨以下痛點：

1. **高維運成本**：網頁版面微調或非預期彈窗常導致 PAD 流程中斷，需要工程師人工排查與修正。
2. **連線與環境限制**：企業內網環境通常受限於網路隔離，難以即時下載外部 Playwright 瀏覽器或線上套件。
3. **缺乏修復日誌與即時通報**：錯誤發生時缺乏結構化情境資訊（`context.json`）與即時通報機制。

`CustomerDeliveryBundle` 透過 **CDP Port 9222** 直連本地 Chrome/Edge 瀏覽器，結合離線 **Wheelhouse** 套件包與 **Teams 錯誤通報機制**，提供零網際網路依賴、高度穩定且具備 LLM 自癒能力的自動化基礎設施。

---

## 核心優勢

* **AI 自修復代理 (Self-Healing Agent)**：結合畫面擷取與 DOM 分析，自動處理解決網頁彈窗、元素異動與操作異常。
* **CDP 9222 原生接管 (Browser Bridge)**：結合 PAD 擴充功能與 Playwright，穩定維持雙向通訊與瀏覽器控制。
* **企業離線自動建置 (Enterprise Ready)**：提供 PowerShell 自動化部署與 Wheelhouse 離線套件，零網際網路依賴。

---

## 系統架構圖

![系統架構圖](./architecture.svg)

[*開啟線上互動式架構圖 (HTML)*](./architecture.html)

---

## 運作原理

1. **啟動階段**：PAD 執行 `chrome.txt` 或 `edge.txt` 流程片段，喚起 `start_pad_browser.ps1`，開啟指定瀏覽器並監聽本機 CDP Port 9222。
2. **捕捉例外**：當 PAD 發生網頁操作失敗時，`AI_ExceptionHandler.txt` 自動擷取當前螢幕畫面（`ERROR.jpg`）並彙整執行上下文（`context.json`）。
3. **AI 分析與修復**：`start_webwright_agent.ps1` 啟動 Python 自癒代理程式（`webwright_agent.py`），經由 Playwright 直連 CDP 9222 檢視 DOM 結構，並呼叫 AI Gateway 分析錯誤原因與執行修復指令。
4. **結果匯出與通知**：修復結果寫入 `result.json` 與日誌，並可透過 Microsoft Teams 發送結構化錯誤通報。

---

## 快速開始

### 前置需求
* Windows 10/11 或 Windows Server
* 已安裝 **Power Automate Desktop (PAD)** 及其瀏覽器擴充功能
* 已安裝 Chrome 或 Microsoft Edge 瀏覽器
* 可連線至內部 AI Gateway (`http://172.22.8.15:8080`)

### 1. 一鍵安裝（管理員 PowerShell）

```powershell
cd D:\CustomerDeliveryBundle
.\install.bat
```

*或使用 PowerShell 直接執行：*

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

安裝腳本會自動建立 `.venv` 虛擬環境、從 `wheelhouse` 離線安裝所需的 Python 套件（Playwright、httpx、BeautifulSoup4 等），並建置執行期目錄。

### 2. 匯入 PAD 流程

將 `PAD_flows/` 資料夾內的流程片段內容複製並貼入 Power Automate Desktop：

| 檔案 | 用途 |
|---|---|
| [`PAD_flows/chrome.txt`](file:///c:/CustomerDeliveryBundle/PAD_flows/chrome.txt) | 啟動 Chrome 並開啟指定網頁 |
| [`PAD_flows/edge.txt`](file:///c:/CustomerDeliveryBundle/PAD_flows/edge.txt) | 啟動 Edge 並開啟指定網頁 |
| [`PAD_flows/AI_ExceptionHandler.txt`](file:///c:/CustomerDeliveryBundle/PAD_flows/AI_ExceptionHandler.txt) | PAD 錯誤捕捉、AI 自修復與 Teams 通知流程 |

### 3. 設定環境變數

設定 AI Gateway 驗證金鑰及執行參數：

```powershell
$env:OPENAI_API_KEY="your-internal-ai-gateway-key"
```

---

## 專案結構

```text
D:\CustomerDeliveryBundle
├── PAD_flows/
│   ├── AI_ExceptionHandler.txt   # PAD 錯誤處理與 Teams 通知流程
│   ├── chrome.txt                # Chrome 瀏覽器啟動流程片段
│   └── edge.txt                  # Edge 瀏覽器啟動流程片段
├── home/ubuntu/
│   ├── start_pad_browser.ps1     # 瀏覽器啟動、PAD 擴充功能與 CDP 監聽器
│   ├── start_webwright_agent.ps1 # Python 自癒代理程式啟動器
│   └── webwright_agent.py        # AI 頁面分析、DOM 檢視與修復執行器
├── CustomerPackage/runtime/RPA_Error/ # 執行期錯誤截圖與修復產物 (context/result.json)
├── wheelhouse/                   # Python 離線安裝套件庫 (.whl)
├── install.ps1                    # PowerShell 自動化安裝腳本
├── install.bat                    # Windows 一鍵安裝批次檔
├── requirements.txt               # Python 依賴套件清單
└── TECHNICAL_HANDOVER.md          # 詳細 IT 技術規格與維運文檔
```

---

## 授權條款

本專案採用 [MIT License](./LICENSE) 授權。

---

## 維運與交接說明

* **維護團隊**：Enterprise IT & RPA Operations Team
* **技術文件**：完整架構說明與微服務修改指南請參閱 [`TECHNICAL_HANDOVER.md`](file:///c:/CustomerDeliveryBundle/TECHNICAL_HANDOVER.md)。

# 設定指南：準時檢索、Discord 通知、漏跑監察

以下三項都要用你自己嘅帳戶設定，程式碼入面唔會有任何密鑰。第 1 項最重要。

## 1. 準時每日檢索（外部觸發）

**原因**：GitHub 內建排程自 2026 年 8 月下旬起經常遲 3–10 小時。本專案 11 月每日都遲約 4.5–7.7 小時：預定 08:17，實際下晝 2–4 點先開始。外部定時服務經 GitHub API 觸發，會準時開始。`daily.yml` 入面嘅 GitHub 排程保留做後備：當日預定時間（提早兩小時起計）之後已經完成完整檢索，後備觸發就會自動略過，唔會一日讀兩次；前一晚嘅人手或程式更新檢索唔會令當日排程被略過。

### 1a. 建立只可以觸發工作流程嘅 GitHub token

1. GitHub 右上角頭像 → **Settings** → 左下 **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**。
2. 填寫：
   - Token name：`teaching-job-radar-trigger`
   - Expiration：1 年（喺日曆記低到期日）
   - Repository access：**Only select repositories** → `yanyangwogwo-ctrl/teaching-job-radar`
   - Permissions → Repository permissions → **Actions：Read and write**（其他唔使開；Metadata 會自動變成 Read-only）
3. 按 **Generate token**，複製 `github_pat_` 開頭嘅字串。呢個 token 只可以喺呢個 repository 觸發或取消工作流程，唔可以改程式或資料。外洩就喺同一頁刪除。

### 1b. 喺 cron-job.org 設定每日觸發

1. 到 <https://cron-job.org> 註冊（免費），然後 **Create cronjob**。
2. **Common**：
   - Title：`教席雷達每日檢索`
   - URL：`https://api.github.com/repos/yanyangwogwo-ctrl/teaching-job-radar/actions/workflows/daily.yml/dispatches`
   - Execution schedule：**Every day at 08:17**；時區揀 **Asia/Hong_Kong**
3. **Advanced**：
   - Request method：**POST**
   - Headers（逐行加）：
     - `Accept`：`application/vnd.github+json`
     - `Authorization`：`Bearer 你嘅github_pat_token`
     - `X-GitHub-Api-Version`：`2026-03-10`
     - `Content-Type`：`application/json`
   - Request body：`{"ref":"main","inputs":{"reason":"schedule"}}`
4. 儲存後按 **Test run**。回應 **200**（或舊版 API 嘅 **204**）即成功。
5. 到 GitHub → **Actions** → **Daily vacancy monitor**：一分鐘內應該見到一個 `workflow_dispatch` 執行。網站「通知與排程」頁之後會顯示「外部準時觸發」，遲幾多分鐘都睇得到。

想改時間：cron-job.org 同網站「每日更新時間」（即 GitHub 後備）**一齊改成同一個時間**，延遲數字先準。

## 2. Discord 職位通知

1. Discord：開一個伺服器（或者用現有），揀一個頻道 → ⚙️ **編輯頻道** → **整合** → **Webhook** → **新 Webhook** → **複製 Webhook 網址**。
2. GitHub repository → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**：
   - Name：`DISCORD_WEBHOOK_URL`
   - Secret：貼上 webhook 網址
3. 下一輪檢索之後會收到：
   - 符合正式條件嘅新職位（`config/preferences.yaml`：兼職／時薪教學職位 × 哲學、倫理、批判思考、通識、AI 素養／倫理／人文）。城大、科大 Interfolio 呢類只有清單嘅來源，會註明「只按標題／部門判斷」。
   - 來源狀態摘要：每日最多一份，有狀態改變先另外通知。之前積存嘅幾百個舊來源警告會合併成一則，唔會洗版；已截止嘅舊待送職位會略過。

## 3.（可選）漏跑監察

排程完全冇啟動嗰日，系統本身發唔到警告。如果想收到通知：

1. 到 <https://healthchecks.io> 建立一個 check：Period **1 day**，Grace **12 hours**。
2. 複製 ping URL，喺 GitHub 加 secret `HEARTBEAT_URL`。
3. 喺 Healthchecks 嘅 Integrations 加 Discord，就會喺漏跑時收到提示。

## 4. 套用今次修訂後頭幾輪要留意

| 來源 | 預期 | 如果唔係咁 |
|---|---|---|
| 中大 | 「檢索成功」，綠色說明「以相反排序再讀一次，兩次職位編號完全相同」；之後連續 3 輪見唔到嘅舊職位會標「已消失」 | 仍然「部分未完成」：說明會寫第二次讀到幾多，交返嚟睇 |
| 港大 | 每輪約 10–12 分鐘；頭幾日「部分未完成（詳情分批更新）」，詳情補齊後轉「檢索成功」 | 80 秒一次仍出 HTTP 202：可以再放慢或減每輪數量 |
| 香港藝術學院 | 雲端經常「暫時未能讀取（Cloudflare）」，保留舊資料 | 偶然成功係正常；建議每星期手動睇一次官方頁 |
| 能仁、建造學院 | 「需手動查看」，唔再每日探測 | 需要時手動開官方招聘頁 |
| GitHub Actions | 平日綠色 ✓；網站擋或者逾時只會喺來源頁顯示，唔會變紅 | 紅色 ✗ 代表有嘢要修（例如網站改版），睇 Actions 記錄或者來源頁紅色「檢索失敗」 |

網站介面（`dist/` 入面嘅 HTML／CSS／JS）有更新，要喺原本發布網站嘅 ChatGPT Sites 重新發布一次，新嘅狀態標籤（例如「暫時未能讀取」）同核對說明先會出現。未重新發布之前，舊介面照樣讀到新資料，只係部分狀態會顯示「狀態不明」。

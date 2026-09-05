# 實作規格

版本：2026-09-05；對應使用者提供的 13 校需求。

## 1. 架構

Python 3.12 每日讀取公開來源 → `data/store.json` 原子寫入 → 產生 JSON／CSV → 靜態 HTML／CSS／JavaScript dashboard。通知使用持久化 outbox 和 Discord webhook。雲端模式由 GitHub Actions 執行及提交資料，Sites 網頁讀取公開 raw GitHub JSON；不需要常駐伺服器或付費 AI 模型。

這是一個單一使用者／單一寫入程序的資料集。雲端排程設 concurrency，禁止同時寫入；本機須依序執行。若將來有多人編輯、頻密寫入或大量資料，應遷移至 SQL 資料庫。

## 2. 來源

完整官方 URL、host allowlist、讀取方式及說明以 `config/sites.yaml` 為準。11 個已實作模組，2 個因公開讀取受阻而停用。來源成功要求清單、分頁和預期詳情均完整；詳情失效也要顯示 partial。

來源格式包括同頁摺疊區塊、HTML 清單＋詳情、PDF、PageUp、Oracle Recruiting 公開 JSON、Taleo 唯讀搜尋 POST＋資料字串。從不提交申請表。

通用 HTML 模組支援 YAML selectors；不同平台不會只靠 URL 猜測。空白、驗證頁、找不到 selectors、無法確認的零結果均不是成功。

## 3. 資料契約

職位必要欄位：`id, source_id, institution, title, reference, url, source_url, department, employment_type, posted_date, deadline, deadline_type, deadline_raw, description, detail_complete, role, subjects, score, evidence, matches, first_seen, last_seen, changed_at, status, missing_count`。

- 穩定 ID：來源群組＋官方編號的 hash；無編號才用正規化網址。不能用標題或全文 hash 當職位 ID。HKBU 主站與 SCE 的觀測分開保存，避免某來源移除另一來源的記錄。
- `posted_date` 只採用官方明示刊登日期；不從編號、URL、PDF 檔名或「30+ days ago」推算。
- ISO 時間戳先轉香港時間，再取日期；日期欄位本身不加時區。網站沒有日期則顯示 `first_seen`，標明「首見」。
- `deadline_type` 可為 `closing, review, until-filled, screening-or-closing, unknown`。開始審閱、下架日期和持續招聘不等於截止日期。Taleo 的 unposting 額外保存為 `unposting_raw`，不拿來推斷截止。
- `content_hash` 追蹤廣告內容改動；不把每次 last_seen 更新當內容變更。
- 詳情不完整時保留過去已驗證的內文及日期，不用空欄位覆蓋。
- 超過香港當日的明確截止日期 → closed。截止當日仍顯示 open；不猜測未提供的截止時間。即使爬取失敗，已知期限仍會生效。
- 連續三次完整、非異常檢索找不到 → missing；數量突降超過 60% 或分頁不完整不增加消失計數。重新找到會恢復 open（除非已過明確截止日）。
- 舊記錄及歷史不刪除。

## 4. 新職位與相關度

比對「新出現」使用穩定 ID；顯示和篩選新舊日期採用刊登日期，缺少時用首見。首次有資料的來源建立安靜基準；網站失敗而取得零筆，不建立基準。來源首次部分成功所取得的資料也是基準，尚未讀到的其他廣告將來可能以新發現記錄處理。

科目由 `preferences.yaml` 字词比對，回傳命中詞和內文節錄。最高科目權重加上兼職教學角色 20 分，上限 100；必須通過角色、聘用類型、院校及排除規則。分數不是獲聘機率，未推斷使用者是否完全符合學歷要求。

排除泛指學位的 Doctor／Master of Philosophy、teaching philosophy，以及平等機會／倫理操守等通用頁尾，減少誤報。英文用字界線，不把 chair 當 AI。一般 engineering／machine learning 不會單憑 AI 進入通知。規則仍可能誤判，提供原文證據讓使用者核對。

## 5. 介面

- 關鍵字空格／逗號分隔、引號詞組、AND／OR、`-` 排除；語法常駐顯示。
- 院校獨立複選，全部取消代表零院校，不偷偷恢復全選。
- 日期上下界包括當日；未知截止在排序最後，不能通過指定截止區間。
- 搜尋頁提供 `presetEnabled` 開關；新「預設條件」分頁編輯 `presetKeywords`（原科目＋職位名稱合併）、`presetMode`（預設 OR）及 `partTimeOnly`。合併詞組以逗號／換行分隔，空格保留，均搜尋職位名、部門、科目與完整內文，沒有另設名稱或科目門檻。開關只控制預設組，獨立的手動 query／排除／院校／日期／狀態仍適用；關閉不刪除內容。
- 舊常用搜尋的 subjectKeywords／roleKeywords 遷移成合併 OR 清單；現有新格式空字串及關閉狀態優先保留。統計顯示於分頁列右側，來源警告位於搜尋結果上方。搜尋不以預先計算的 `matches` 或相關度分數作隱藏門檻，正式通知評分規則維持獨立。
- 預設 `sort=newest`，採刊登／首見日由新到舊。固定最近兩個曆月（香港當日，包含邊界；月尾夾到目標月份最後一日），早於範圍或未到刊登日的結果不顯示。限制獨立於其他日期篩選及排序，舊刊登日不能因首見新而通過；所有歷史仍保存。
- 官方原文／招聘連結放在職位卡、來源卡及詳情視窗頂端右側。
- 儲存搜尋存在瀏覽器，只是個人篩選偏好。它不改變服務端正式 Discord 設定。
- 每日時間選單讀取 GitHub 工作流程的單一 daily cron，UTC 轉香港時間，或直接使用 Asia/Hong_Kong。選定時間後先重新讀取最新工作流程，只替換 cron 行，複製供擁有者於 GitHub 編輯頁確認。只有重新讀取已提交的工作流程才能顯示已儲存；不以 localStorage 或本地選擇冒充正式排程。網站不持有 GitHub 寫入 token，公開訪客不能直接寫入。
- HTML 字串轉義、只提供 HTTPS 原文連結、新分頁 noopener；不把外部 HTML 原樣插入。
- 手機版可摺疊院校／日期篩選，職位詳細資料用原生對話框，支援鍵盤。
- 雲端資料讀取失敗時使用有標示的網站保存版本，不更改原本檢索時間。最近檢索採用 last_run.finished_at，不能用單純匯出的 generated_at 充當爬取時間。
- CSV 含 BOM，公式前綴會轉義。

## 6. 通知可靠性

每個新職位最多一個 outbox event。職位過期或不再匹配則不送；詳情暫時失效則延後。已確認送出不重送；未確認保留待送。使用 `wait=true` 要求 Discord 回傳訊息 ID；限制訊息长度及禁用 mentions。

先提交待送資料，再送訊息，最後提交逐則本機保存的確認記錄。Discord 與 Git repository 之間沒有跨服務交易；回覆丟失／收據提交前程序中斷可造成重送，不能宣稱 exactly-once。

來源故障按來源／日／錯誤指紋記錄；發送前合併舊故障，只送最新仍未恢復的一則。排程完全停止需要獨立 heartbeat 服務，介面明示尚未啟用與是否過時。

## 7. 輸入與網路

只讀 allowlist 上的公開 HTTPS 主機和指定唯讀搜尋 POST。每主機間隔至少 1.1 秒，採 robots 的更長 Crawl-delay。每來源請求數上限 400、每頁 6 MB、分頁上限、timeout、最多三個来源並行。robots 404／410 可繼續；401／403／429、5xx、驗證內容、無法確認規則則停止。通用 parser 支援 wildcard、終點 `$`、最長路徑及 Allow 同長優先，失敗快取保持拒絕。[robots 標準](https://www.rfc-editor.org/rfc/rfc9309)

所有密鑰只透過環境變數。公開匯出不包含 webhook、HTTP cookies 或 credential；原始私人書籤不在專案內。

## 8. 已完成驗證及限制

離線回歸測試覆蓋 AND／OR／排除、日期時區、資料持久化、首次基準、重跑去重、數量突降、消失門檻、暫時詳情失效、已截止處理、robots fail-closed 及 Discord 確認重試。Discord 測試使用 mock，未曾實際向頻道發送。

各來源有真實公開檢索驗證；首次結果見 SOURCE_AUDIT.md，最新雲端結果見 Actions 及 dashboard。未執行瀏覽器視覺／跨裝置驗收。Discord 端到端驗收要在使用者頻道設定完成後執行。

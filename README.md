# 教席雷達 · 大專職位監察

每日查看香港大專官方招聘頁，集中搜尋兼職教學機會。這個版本已用真實公開資料測試；介面上的「來源狀態」是實際結果，不會把讀取失敗當成沒有新職位。

## 現在可以做甚麼

- 按院校、日期、職位狀態篩選；輸入多個關鍵字，選 AND／OR，或排除字詞。
- 搜尋頁只保留預設條件開關；「預設條件」分頁可編輯科目、職位名稱及兼職限制。科目組任一 OR、職位名稱組任一 OR，兩組以 AND 同時成立，恢復原有精準搜尋方法。科目搜尋職位名、部門及清理無關文字後的內文；職位名稱只搜尋標題。清空一欄可取消該組限制。
- 關閉預設條件會暫停科目、職位名稱及兼職限制，保留院校、日期、手動搜尋及排除條件；再開啟沿用修改後內容。常用搜尋保留開關及兩組條件；合併版本嘅舊搜尋會轉回兩組，並在預設分頁保留原有合併文字供核對，唔會自動覆寫儲存記錄。
- 統計數字維持在分頁列右側，來源警告在搜尋結果上方；小螢幕會換行。
- 預設按刊登／首見日期由新到舊；只顯示香港當日往前兩個曆月內的職位。舊廣告即使今日首次收集，仍按已知刊登日隱藏；未知刊登日才採首見。
- 搜尋職位名、部門、科目和已取得的內文。`philosophy, parttime, lecturer`；`"AI literacy"`；`-nursing`。
- 儲存常用搜尋；匯出目前結果為 Excel 可開啟的 UTF-8 CSV。
- 查看刊登日期、首見日期、截止／開始審閱日期、相關科目和原文證據。
- 職位卡、來源卡及詳情視窗的官方連結均放在頂端右側。
- 保留歷史及更新紀錄。首次有資料的檢索建立安靜基準，其後才將新發現的相關職位列入 Discord 待送清單。

網站會讀取 GitHub 上最近一次檢索的公開資料；若連線失敗，會清楚標示正在顯示網站保存的舊版本。**Discord 尚未設定**；每日排程的最近執行結果以 Actions 及網頁狀態為準。

## 本機開啟

需要 Python 3.12。於此專案資料夾開啟 Cursor Terminal：

```bash
python -m pip install -r requirements.txt
python -m http.server 8000 --directory dist
```

在瀏覽器開啟 `http://localhost:8000`。不要直接雙擊 HTML，因為瀏覽器通常不允許本機檔案讀取旁邊的資料 JSON。

另一個 Terminal 可以更新資料（預設完全不發送通知）：

```bash
python -m monitor.run
```

如只更新一間：`python -m monitor.run --sources hkuspace`。檢索有未完成來源時結束碼是 1；成功來源的新資料仍會保存。不要同時開兩個本機檢索程序。

## 啟用每天自動更新

使用你自己的公開 GitHub repository 和標準 GitHub Actions runner 保存、更新資料。現有 Sites 網頁直接讀取公開資料，繼續使用同一條網址。公開的內容是程式、公開職位、歷史和通知規則。不要把私人書籤、履歷或任何密鑰上傳。

1. 將本專案放到 GitHub repository 的預設分支，保留 `data/store.json` 及 `.github/workflows/`。`data/store.json` 包含已建立的基準；刪掉會失去歷史及去重記錄。
2. 檢查 `dist/feed-config.json` 指向這個 repository 的 `main/dist/data/jobs.json`；預設已指向 `yanyangwogwo-ctrl/teaching-job-radar`。
3. 建立 Discord 頻道的 webhook，將完整網址存進 **Settings → Secrets and variables → Actions → New repository secret**，名稱用 `DISCORD_WEBHOOK_URL`。密鑰不要貼在公開頁面、程式或聊天中。
4. 確認 repository 允許 Actions 的 `GITHUB_TOKEN` 寫入內容。工作流程只會推送資料記錄；若分支保護拒絕 bot，需由擁有者配置允許的寫入方式，不要強制推送。
5. 初次上傳程式會觸發一次檢索。之後可用 **Actions → Daily vacancy monitor → Run workflow** 手動執行；每日排程會獨立運作。完成後重新開啟教席雷達，檢查來源狀態及最近執行時間。

手動執行時，`sources` 留空會檢查全部；亦可輸入 `hsu` 或 `hsu,hkust`，只重試指定院校。其他院校的資料及來源狀態會保留；每日定時執行仍然檢查全部來源。

維護程式時，可在提交訊息另起一行 `Recheck-Sources: hsu`，讓這次程式更新只重試相關來源。沒有這行就照常全校檢索；排程不採用提交訊息。所有來源 ID 仍須通過設定檔核對，測試步驟不會跳過。

檢索期間若只有程式被更新，系統會保留兩邊修改再儲存職位；若兩邊都修改職位資料，則停止合併，避免覆寫。每輪另保留 7 日資料備份，在 Actions 執行頁的 Artifacts 下載；未成功保存待送資料前，不會發出 Discord 通知。[備份功能說明](https://github.com/actions/upload-artifact)

工作流程預設每天香港時間約 **08:17** 執行一次。GitHub 排程可能延遲；長期沒有 repository 活動也可能被停用。請保留必要的失敗通知，並定期確認頁面更新時間。[GitHub 排程說明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

日常使用：[教席雷達](https://teaching-job-radar.fhk357753357.chatgpt.site)。網頁載入時向 GitHub 取得資料，GitHub 可能有數分鐘快取；任何暫時無法讀取最新資料的情況都會提示。資料更新不需要重新發布網站；改動 HTML／CSS／JavaScript 才需要更新 Sites 版本。

### 在網頁調整每天時間

開啟「通知與排程」，選擇香港時間，再按「複製新設定」。到其提供的 GitHub 編輯頁，於編輯框全選並貼上，再用有寫入權限的 GitHub 帳戶按 **Commit changes** 確認。返回網頁按「檢查是否已儲存」核對；快取可能需要數分鐘更新。

網頁會先讀取最新工作流程，只替換每日時間，保留其餘內容。**選擇時間或複製設定不等於已儲存**；只有 GitHub 預設分支的 `.github/workflows/daily.yml` 是正式排程。公開訪客不會取得專案寫入密鑰，瀏覽職位也不需要登入。現有靜態版本未提供網頁內一鍵寫入 GitHub 的管理員登入；確認仍在 GitHub 完成。[GitHub 編輯及儲存說明](https://docs.github.com/en/repositories/working-with-files/managing-files/editing-files)

資料匯出的時間說明也會從工作流程讀取，不再由另一個文字設定維護。系統支援每日一次 UTC 或 Asia/Hong_Kong 排程，其他格式會要求人工檢查，避免錯改。

Actions 費用／免費額度見[官方帳單說明](https://docs.github.com/en/billing/concepts/product-billing/github-actions)。本專案沒有付費 AI API 依賴，也沒有建立任何訂閱。

目前爬取和匹配均使用普通 HTTP、PDF／HTML 解析及關鍵字規則，沒有調用 ChatGPT、OpenAI 或其他模型 API。GitHub 公開專案的標準 runner 運算時間免費；未啟用大型付費 runner。Sites 託管受 ChatGPT 方案及測試版用量限制約束，不能承諾永久無上限免費。[Sites 官方說明](https://learn.chatgpt.com/docs/sites)

網站公開免登入，但官方 Sites 文件沒有逐地區可達性保證；尚未以香港／內地等不同網絡實測。不能直接把 ChatGPT 帳戶的支援地區清單套用到公開網站。程式和資料均保存在 GitHub，可另行部署到其他相容靜態託管。

### 未完整來源的處理方式

- 暫時連線逾時：已允許確認可讀的公開 GET 在連線／等候回應標頭逾時後重試一次，仍遵守限速及請求上限。已收到回應後的內文讀取失敗、驗證頁、拒絕存取及 robots 錯誤不會重試。
- 恒生：已有可讀的官方 HTML 清單及詳情結構；下一步是從雲端執行環境確認讀取規則可用，再加入並驗證專用解析器。目前仍停用，不能因搜尋工具看得到就假定爬蟲可用。
- 嶺南：需要校方允許的公開入口／輸出；目前授權限制未解，不會以瀏覽器自動化繞過。
- 港大／科大／城大：保留已取得清單，尋找可讀的官方詳情或院系公告作補充，補充來源不可冒充完整全校清單。
- 樹仁失效 PDF：重新核對官方現行附件；已刪除原文不能憑空恢復。中大數目差異：用分組查詢及職位 ID 核對，未確認完整前繼續顯示部分完成。

## 通知及漏跑監察

- 正式通知條件在 `config/preferences.yaml`，與網頁上儲存的搜尋分開。預設為兼職／時薪 Lecturer、Tutor、Instructor（包括 Teacher 別名），科目限哲學、倫理、批判思考、通識及 AI 素養／倫理／人文。
- 第一次取得資料只建立基準，不發送大量舊廣告；初次只讀到部分清單時，下一輪才發現的廣告仍可能是較早刊登的職位，通知會顯示真實刊登日期。
- 同一來源的職位以編號或固定網址去重。不同官方來源的重刊廣告目前各自保留，可能重複出現，避免誤合併不同職位。
- 先保存待送資料，再通知 Discord，收到確認後保存收據。一般重跑不會重複發送；如果 Discord 已接收但回覆丟失，或程序在保存收據前中斷，重試仍有重複可能。
- 詳情暫時讀不到會延後職位通知，保留已保存內文、日期及待送記錄；不會靜靜地丟掉通知。
- 每次來源失敗都有狀態及錯誤紀錄。Discord 啟用後也會通知；累積故障訊息按來源合併為最新一則。
- **整個排程沒有啟動，無法靠同一個排程自己發警告。** 網頁開啟時會檢查資料是否超過 36 小時。若需要主動漏跑通知，可在獨立的 Healthchecks 服務建立每 24 小時、寬限 12 小時的檢查，將 ping URL 設為 secret `HEARTBEAT_URL`，並在該服務設定 Discord 通知。本專案只有回報程式，尚未建立外部檢查或帳戶。

手动測試發送前，先確認環境變數中的 webhook 是你指定的頻道。只有以下明確指令會發送：

```bash
python -m monitor.run --notify-only
```

沒有待送職位時，可能只收到來源故障通知；不會為了測試而製造假職位。

## 修改條件及新增來源

常用設定只有兩個檔案：

- `config/preferences.yaml`：科目關鍵字、相關度門檻、通知院校、排除字詞。
- `config/sites.yaml`：13 個官方 URL、讀取模組、啟用狀態及備註。

相同格式的來源可只改 YAML；普通靜態 HTML 來源可使用 `generic_html` 和 CSS selectors，見 `config/site-template.yaml`。**不能保證任何新 URL 都不用修改程式**：新招聘系統、登入限制、改版及不同 PDF 格式仍需調整讀取模組。只貼 URL 不足以可靠辨識職位。

前端沒有登入或寫入資料庫功能；瀏覽者無法更改正式通知規則。儲存搜尋只存在目前瀏覽器，手機和電腦不會自動同步。

## 資料及維護

| 位置 | 用途 |
|---|---|
| `data/store.json` | 唯一正式記錄：職位、歷史、來源狀態、待送及收據 |
| `dist/data/jobs.json` | 網頁讀取的公開資料 |
| `dist/data/jobs.csv` | 完整歷史 CSV；網頁按鈕另外匯出當前篩選結果 |
| `dist/data/history.json` | 公開的變更紀錄 |
| `monitor/` | HTTP 讀取、來源模組、匹配、比對、通知 |
| `tests/` | 不連外、不發送訊息的回歸測試 |

每天的 GitHub 工作流程把正式記錄提交回 repository，下一輪再讀取。Actions cache、暫存 artifact、瀏覽器 localStorage 都不是職位資料庫。不要人手編輯 CSV 來改正式資料；CSV 是可再產生的匯出。

測試：

```bash
python -m unittest discover -s tests -v
node --test tests/*.test.mjs
```

網站結構改變時，先停用受影響來源並查看錯誤；不要刪掉歷史。讀取遵守各主機 robots、慢速連線及請求上限，遇到驗證、401／403／429 即停止，不會繞過或登入應徵者系統。

首次驗證結果及仍受限來源見 `SOURCE_AUDIT.md`；詳細設計見 `SPEC.md`。

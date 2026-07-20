# 台股訊號儀表板 — 上線部署指南

這份專案把「假資料展示頁」升級成「真資料版」，架構是：

```
GitHub Actions（排程）
  ├─ close_of_day.yml  每個交易日 15:40 台北時間跑一次
  │     └─ scripts/fetch_and_score.py
  │           抓證交所 OpenAPI → 算訊號/爆發/風險分數 → 寫入 Supabase
  │
  └─ intraday.yml      盤中每 5 分鐘跑一次（09:00-13:30）
        └─ scripts/fetch_realtime.py
              抓少量個股即時報價 → 寫入 Supabase stock_realtime

Supabase（PostgreSQL，免費層）
  存放所有計算結果，開放給前端「唯讀」存取

dashboard_live.html（靜態頁面）
  部署到 GitHub Pages / Netlify / Vercel 皆可
  前端直接用 fetch() 打 Supabase REST API 讀資料
```

沒有自己的伺服器、沒有月費，全部用各家的免費層組成。

---

## Step 1：建立 Supabase 專案

1. 到 https://supabase.com 免費註冊，建立一個新專案（New Project）
2. 進專案後左側選單找 **SQL Editor**，新增一個 Query
3. 把本專案的 `schema.sql` 整段貼上，按 Run 執行 → 會建立好所有資料表
4. 左側選單 **Settings → API**，記下兩組值：
   - `Project URL`（例如 `https://xxxx.supabase.co`）
   - `anon public` key（前端要用，唯讀）
   - `service_role` key（後端排程要用，有寫入權限，**絕對不要放進前端或公開的程式碼**）

---

## Step 2：建立 GitHub Repo 並設定 Secrets

1. 把這個資料夾整包推上一個新的 GitHub repo（可以是 private）
2. Repo 頁面 → **Settings → Secrets and variables → Actions**
3. 新增兩個 Repository secret：
   - `SUPABASE_URL` = 你的 Project URL
   - `SUPABASE_KEY` = 你的 **service_role** key（給排程用，有寫入權限）

這樣 `.github/workflows/` 底下的兩個 workflow 才能在執行時讀到連線資訊。

---

## Step 3：測試排程腳本

先手動跑一次，確認證交所 API 抓得到資料、Supabase 寫得進去：

1. GitHub repo 頁面 → **Actions** 分頁
2. 左側選 `收盤後訊號計算` → 右側 `Run workflow` → 綠色按鈕觸發
3. 等它跑完，點進去看 log：
   - 如果看到 `[OK] 寫入 signal_scores：N 筆` 代表成功
   - 如果看到 `[FATAL]` 或 `[ERROR]`，通常是證交所端點路徑變了，
     打開 https://openapi.twse.com.tw/v1/swagger.json 核對 `scripts/fetch_and_score.py`
     裡用到的路徑（`STOCK_DAY_ALL`、`T86`、`BWIBBU_ALL`）是否還存在

> 三大法人 `T86` 端點的回傳欄位名稱，官方文件不一定跟程式裡假設的完全一致，
> 第一次跑完建議打開 Supabase 的 `stock_daily` 表檢查 `foreign_net` / `trust_net` /
> `dealer_net` 是不是都是 0——如果全部是 0，代表欄位對應要調整，
> 照著 swagger.json 裡 `/fund/T86` 的實際欄位名微調
> `scripts/fetch_and_score.py` 裡 `fetch_institutional_t86()` 這個函式。

---

## Step 4：填入前端連線資訊

打開 `dashboard_live.html`，找到這段（在 `<script>` 開頭附近）：

```javascript
const SUPABASE_URL = 'https://YOUR-PROJECT.supabase.co';
const SUPABASE_ANON_KEY = 'YOUR-ANON-PUBLIC-KEY';
```

換成你的 Project URL 跟 **anon public key**（注意不是 service_role key，
anon key 是設計給前端公開使用的唯讀金鑰，配合 schema.sql 裡的 RLS 規則，
它只能讀不能寫）。

---

## Step 5：部署前端頁面

三選一，都免費：

**GitHub Pages**
1. Repo → Settings → Pages → Source 選 `main` branch，資料夾選 `/ (root)`
2. 存檔後幾分鐘會給你一個 `https://你的帳號.github.io/repo名稱/dashboard_live.html` 網址

**Netlify / Vercel**
1. 把整個資料夾拖進 Netlify 的部署頁面，或用 Vercel CLI `vercel deploy`
2. 完成後會給一個網址，之後每次 git push 都會自動重新部署

---

## Step 6：驗證整條線路

1. 等到下一個交易日收盤後 15:40（或手動觸發 workflow）
2. 打開部署好的網址，切到「訊號」頁籤
3. 應該會看到真實股票代號、真實收盤價，「操作建議」是照 `fetch_and_score.py`
   裡的簡化規則算出來的

---

## 目前的限制（誠實列出，避免誤解成「已完工」）

| 項目 | 狀態 |
|---|---|
| 訊號中心（signal_scores） | ✅ 已接證交所 STOCK_DAY_ALL + T86，簡化版評分規則 |
| 爆發前兆雷達（explosion_scores） | ⚠️ 已接，但「量比20」「量Z60」「均線收斂」目前用暫代值（沒有累積歷史資料前無法算真正的 20 日均量），需要之後累積 `stock_daily` 至少 20 個交易日後改寫成真計算 |
| 風險示警（risk_scores） | ⚠️ 已接，但「連續賣超天數」「交易性風險/流動性」尚未實作，需要另外設計 |
| 全球資產對照 | ❌ 完全沒接，需要你另外找一個國際指數/匯率資料源（畫面骨架已保留） |
| 盤中即時報價 | ⚠️ 用非正式的 MIS 端點，只查 watchlist 清單裡的少數幾檔，不是全市場，且該端點可能隨時失效 |
| 評分公式本身 | ⚠️ 是簡化版規則，不是原截圖裡那套完整系統的還原，需要你自行回測調整權重 |
| 頂部篩選列（區間/排序/Top N/銀行） | ❌ 目前只是畫面裝飾，還沒接到實際篩選邏輯 |

簡單說：這是一個**能動的骨架**，資料是真的、會自動更新，但精細的分數計算邏輯
（尤其是需要長期歷史資料的均線收斂、量能 Z-score、法人連續天數等）需要
累積資料 + 你自己定義規則後才能做到原截圖的完整度。

---

## 常見問題

**Q: GitHub Actions 免費額度會不會用完？**
公開 repo 完全免費、無限制。私有 repo 每月有 2000 分鐘免費額度，
這兩個 workflow 一次執行約 1-2 分鐘，一個月加起來遠低於額度。

**Q: 為什麼盤中報價有時候抓不到？**
`fetch_realtime.py` 用的是非正式端點，證交所沒有保證這個端點永遠可用，
如果長期抓不到，建議改用付費資料商（TEJ/CMoney）的正式即時報價 API。

**Q: 可以加更多股票到訊號中心嗎？**
`STOCK_DAY_ALL` 本來就是全市場資料，`signal_scores` 表已經是全市場計算結果，
不需要額外設定；即時報價的 `WATCHLIST`（在 `fetch_realtime.py` 裡）才需要手動維護清單。

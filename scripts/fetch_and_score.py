#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股訊號儀表板 — 收盤後完整計算腳本
=====================================
用途：抓取證交所 OpenAPI 資料，計算訊號分數 / 爆發前兆分數 / 風險分數，寫入 Supabase。
執行時機：每個交易日收盤後（建議 15:30 之後，法人買賣超資料約 15:00~16:00 陸續釋出）。

環境變數（由 GitHub Actions secrets 注入）：
  SUPABASE_URL       Supabase 專案 URL，例如 https://xxxx.supabase.co
  SUPABASE_KEY       Supabase service_role key（有寫入權限，勿外流）

⚠️ 重要：
  1. 證交所 OpenAPI 端點路徑可能異動，執行前建議先用瀏覽器打開
     https://openapi.twse.com.tw/v1/swagger.json 確認本檔用到的端點仍存在。
  2. 評分公式（見下方 SCORING RULES）是簡化版規則，僅供參考，不構成投資建議，
     使用前務必自行回測與調整權重。
"""

import os
import sys
import json
import math
import statistics
import time
from datetime import date, datetime, timezone, timedelta
import urllib.request
import urllib.error

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

TWSE_BASE = "https://openapi.twse.com.tw/v1"

# --------------------------------------------------------------------------
# 基礎工具：HTTP GET / Supabase upsert
# --------------------------------------------------------------------------

def http_get_json(url, timeout=30, referer=None, retries=2, retry_wait=3):
    """帶重試機制的 HTTP GET，證交所伺服器偶爾會抖動（回傳空內容或逾時），
    失敗時等幾秒重試，避免單次抖動就讓整個排程失敗。
    """
    headers = {"User-Agent": "Mozilla/5.0 (signal-dashboard/1.0)"}
    if referer:
        headers["Referer"] = referer
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            if attempt < retries:
                print(f"[WARN] 請求失敗（第 {attempt + 1} 次）：{e}，{retry_wait} 秒後重試...")
                time.sleep(retry_wait)
    raise last_err


def supabase_upsert(table, rows, on_conflict):
    """用 Supabase REST API 做 upsert（insert or update）"""
    if not rows:
        return
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(f"[WARN] 未設定 SUPABASE_URL / SUPABASE_KEY，跳過寫入 {table}（僅本地測試模式）")
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        print(f"[OK] 寫入 {table}：{len(rows)} 筆")
    except urllib.error.HTTPError as e:
        print(f"[ERROR] 寫入 {table} 失敗：{e.code} {e.read().decode('utf-8')[:500]}")


def supabase_select(table, query, timeout=60, page_size=1000, max_pages=100):
    """用 Supabase REST API 讀資料（給計算主升段/流動性要抓歷史資料用）。

    ⚠️ 重要：Supabase/PostgREST 預設單次請求最多只回傳 1000 筆，就算網址參數
    寫 limit=50000 也一樣會被伺服器砍到 1000 筆——URL 裡的 limit 只是「上限」，
    不會突破伺服器自己的分頁限制。要真的拿到全部資料，必須用 Range header
    分頁抓取，抓到某一頁回傳筆數小於 page_size，代表已經到底了。
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    all_rows = []
    for page in range(max_pages):
        start = page * page_size
        end = start + page_size - 1
        req = urllib.request.Request(url, headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Range-Unit": "items",
            "Range": f"{start}-{end}",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                chunk = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"[WARN] 讀取 {table} 第 {page+1} 頁失敗：{e}")
            break
        all_rows.extend(chunk)
        if len(chunk) < page_size:
            break  # 這頁沒填滿，代表已經是最後一頁
    return all_rows


def fetch_monthly_revenue():
    """個股月營收（含去年同月增減%）
    端點：/opendata/t187ap05_L
    公司依規定每月10日前公布上月營收，所以這份資料月初幾天可能還是上上個月的
    """
    try:
        data = http_get_json(f"{TWSE_BASE}/opendata/t187ap05_L")
    except Exception as e:
        print(f"[WARN] 月營收資料抓取失敗：{e}")
        return {}
    out = {}
    for row in data:
        code = row.get("公司代號")
        if not code:
            continue
        yoy = safe_float(row.get("營業收入-去年同月增減(%)"))
        out[code] = {"revenue_yoy_pct": yoy}
    return out


def fetch_price_and_volume_history():
    """從 Supabase 撈每檔股票近期收盤價 + 成交量 + 法人買賣超歷史，一次查詢同時取出三者。

    原本這是兩個獨立函式（fetch_price_history / fetch_volume_history），各自對
    stock_daily 整張表分頁查詢一次——但兩者查的是同一批列，只是選的欄位不同，
    等於同一份資料被完整抓了兩遍。隨著資料庫累積的交易日變多，這張表會越來越大，
    重複查詢的成本也跟著變大，所以合併成一次查詢、一次分頁，各自組成獨立的
    {code: [(date, value), ...]} 結構回傳，避免收盤價/成交量/法人買賣超的 tuple 順序搞混。

    ⚠️ institutional_net 這欄跟收盤價/成交量不一樣，特意不用 safe_float()（會把
    None 當成 0.0），而是保留 None：None 代表「那天 T86 抓取失敗，不知道法人買賣超
    狀態」，0 代表「那天真的有抓到資料，法人買賣超剛好淨零」，兩者意義不同，算連續
    賣超天數時混在一起會誤判（把「不知道」當成「沒有賣超」而錯誤中斷連續天數，
    或反過來誤算成有賣超）。
    """
    rows = supabase_select(
        "stock_daily",
        "select=code,trade_date,close,volume,institutional_net&order=trade_date.desc",
    )
    price_history = {}
    volume_history = {}
    institutional_history = {}
    for r in rows:
        code = r.get("code")
        trade_date = r.get("trade_date")
        if code not in price_history:
            price_history[code] = []
            volume_history[code] = []
            institutional_history[code] = []
        price_history[code].append((trade_date, safe_float(r.get("close"))))
        volume_history[code].append((trade_date, safe_float(r.get("volume"))))
        inst_net_raw = r.get("institutional_net")
        inst_net = None if inst_net_raw is None else safe_float(inst_net_raw)
        institutional_history[code].append((trade_date, inst_net))
    # 依日期由舊到新排序
    for code in price_history:
        price_history[code].sort(key=lambda x: x[0])
        volume_history[code].sort(key=lambda x: x[0])
        institutional_history[code].sort(key=lambda x: x[0])
    return price_history, volume_history, institutional_history


def safe_float(v, default=0.0):
    try:
        if v in (None, "", "--", "N/A"):
            return default
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return default


# --------------------------------------------------------------------------
# 1. 抓取證交所資料
# --------------------------------------------------------------------------

def fetch_stock_day_all():
    """全市場個股日成交資訊（收盤價、漲跌、成交量）
    端點：/exchangeReport/STOCK_DAY_ALL
    """
    data = http_get_json(f"{TWSE_BASE}/exchangeReport/STOCK_DAY_ALL")
    out = {}
    for row in data:
        code = row.get("Code")
        if not code:
            continue
        out[code] = {
            "code": code,
            "name": row.get("Name", ""),
            "close": safe_float(row.get("ClosingPrice")),
            "change": safe_float(row.get("Change")),
            "open": safe_float(row.get("OpeningPrice")),
            "high": safe_float(row.get("HighestPrice")),
            "low": safe_float(row.get("LowestPrice")),
            "volume": safe_float(row.get("TradeVolume")),
        }
    return out


def fetch_institutional_t86():
    """三大法人買賣超日報（外資/投信/自營商）
    端點：這份報表不在 openapi.twse.com.tw（新版 REST API）裡，
    是證交所另一套舊版網頁 API（www.twse.com.tw/fund/T86），
    回傳格式是 {"fields": [...欄位名...], "data": [[...每列資料...], ...]}，
    不是一般的物件陣列，所以要用欄位名稱去對應每一欄的位置（index），
    這樣即使 TWSE 之後調整欄位順序，只要欄位名稱關鍵字沒變就還抓得到。

    回傳：(out, status, detail)
      out    : {code: {...}} 抓到的法人買賣超資料，失敗時為 {}
      status : "ok" | "network_error" | "no_data_published" | "format_changed"
               寫進 fetch_status 表，讓 dashboard 分辨「今天為什麼沒有法人資料」，
               而不是每次失敗都長得一樣、事後只能翻 log 猜原因。
      detail : 給人看的補充說明（錯誤訊息 / TWSE 回傳的 stat 值 / 欄位清單），成功時為 None
    """
    date_str = TODAY.replace("-", "")
    url = f"https://www.twse.com.tw/fund/T86?response=json&date={date_str}&selectType=ALLBUT0999"
    try:
        # 已測試確認：這個舊版端點對 GitHub Actions 的請求環境穩定連不上/被降速，
        # 即使拉長到 150 秒仍會逾時。不再重試、逾時設短一點，讓它快速失敗，
        # 避免每天排程都白白浪費好幾分鐘在一個確定會失敗的請求上。
        # 之後若要真的拿到法人買賣超資料，建議改用付費資料商（TEJ/CMoney等）的正式 API。
        # 這個端點時好時壞（伺服器有時候在合理時間內回應，有時候會逾時），
        # 30秒 + 重試1次是在「多一次機會抓到真資料」跟「不要每天多花太多時間等一個不保證成功的請求」之間取平衡
        data = http_get_json(url, timeout=30, retries=1, referer="https://www.twse.com.tw/zh/page/trading/fund/T86.html")
    except Exception as e:
        print(f"[WARN] 三大法人資料抓取失敗（網路/逾時）：{e}，本次僅用價量資料計算")
        return {}, "network_error", str(e)

    if not isinstance(data, dict) or data.get("stat") != "OK":
        stat = data.get("stat") if isinstance(data, dict) else "未知格式"
        print(f"[WARN] 三大法人資料狀態異常（{stat}），可能是非交易日或當日資料尚未公布，本次僅用價量資料計算")
        return {}, "no_data_published", str(stat)

    fields = data.get("fields", [])
    rows = data.get("data", [])

    def find_col(keyword, exclude=None):
        for i, f in enumerate(fields):
            if keyword in f and (not exclude or exclude not in f):
                return i
        return None

    idx_code = find_col("證券代號")
    idx_foreign = find_col("外陸資買賣超股數", exclude="自營商")
    idx_trust = find_col("投信買賣超股數")
    idx_dealer = None
    for i, f in enumerate(fields):
        if f.strip() == "自營商買賣超股數":
            idx_dealer = i
            break

    if idx_code is None:
        print(f"[WARN] 三大法人資料欄位對不上（找不到「證券代號」欄），目前欄位：{fields}")
        return {}, "format_changed", f"欄位：{fields}"

    out = {}
    for row in rows:
        code = str(row[idx_code]).strip()
        if not code:
            continue
        # 股數本來就一定是整數，這裡用 int() 而不是留著 safe_float() 回傳的 float，
        # 是因為 Supabase 的 foreign_net/trust_net/dealer_net/institutional_net 這幾欄
        # 是 bigint，如果寫入的數字帶著小數點（即使小數部分是 .0，例如 15000.0），
        # Postgres 的 bigint 解析器會直接拒絕、回傳 22P02 錯誤，之前 T86 一直抓不到
        # 真實資料所以沒踩到，這次 T86 終於抓到資料才第一次暴露這個問題。
        foreign_net = int(safe_float(row[idx_foreign])) if idx_foreign is not None else 0
        trust_net = int(safe_float(row[idx_trust])) if idx_trust is not None else 0
        dealer_net = int(safe_float(row[idx_dealer])) if idx_dealer is not None else 0
        out[code] = {
            "foreign_net": foreign_net,
            "trust_net": trust_net,
            "dealer_net": dealer_net,
            "institutional_net": foreign_net + trust_net + dealer_net,
        }
    return out, "ok", None


def fetch_taiex_index():
    """發行量加權股價指數（TAIEX）當日收盤
    端點：/exchangeReport/MI_INDEX（大盤統計資訊）
    """
    try:
        data = http_get_json(f"{TWSE_BASE}/exchangeReport/MI_INDEX")
    except Exception as e:
        print(f"[WARN] TAIEX 大盤指數抓取失敗：{e}")
        return None
    for row in data:
        name = row.get("指數") or row.get("Index")
        if name and "發行量加權股價指數" in name:
            return safe_float(row.get("收盤指數") or row.get("ClosingIndex"))
    return None


def fetch_backtest_raw_data(lookback_days):
    """回測需要兩份歷史資料：
      (1) signal_scores 每天的推薦名單（code + recommendation）
      (2) stock_daily 每天的開盤價/收盤價
    抓取範圍用日曆天數做寬鬆篩選（乘 1.6 倍再加緩衝天數），確保涵蓋到
    lookback_days+1 個交易日——多抓一天是因為要驗證「最後一個基準日」的隔天
    開盤價也要在範圍內，才能算出那一天的回測結果。

    回傳：(signal_by_date, price_by_date)
      signal_by_date: {trade_date: [(code, recommendation), ...]}
      price_by_date:  {trade_date: {code: {"close":..., "open":...}}}
      price 的 open 保留 None 語意：None 代表那天還沒有開盤價紀錄
      （這個欄位是這次才開始寫入的，部署之前的舊資料沒有這筆）
    """
    cutoff_date = (date.today() - timedelta(days=int((lookback_days + 1) * 1.6) + 3)).isoformat()

    signal_rows = supabase_select(
        "signal_scores",
        f"select=code,trade_date,recommendation&trade_date=gte.{cutoff_date}&order=trade_date.desc",
    )
    signal_by_date = {}
    for r in signal_rows:
        d = r.get("trade_date")
        signal_by_date.setdefault(d, []).append((r.get("code"), r.get("recommendation")))

    price_rows = supabase_select(
        "stock_daily",
        f"select=code,trade_date,close,open&trade_date=gte.{cutoff_date}&order=trade_date.desc",
    )
    price_by_date = {}
    for r in price_rows:
        d = r.get("trade_date")
        code = r.get("code")
        open_raw = r.get("open")
        price_by_date.setdefault(d, {})[code] = {
            "close": safe_float(r.get("close")),
            "open": None if open_raw is None else safe_float(open_raw),
        }
    return signal_by_date, price_by_date


def fetch_calibration_raw_data():
    """長期校準統計需要的資料，結構跟 fetch_backtest_raw_data 一樣，但用途不同：
      (1) signal_scores 全部歷史推薦名單
      (2) stock_daily 全部歷史收盤價（不需要開盤價，這裡用收盤比對）
    跟隔日開盤回測不同，這裡刻意不限日期範圍——收盤價資料本來就一直都在存，
    可以直接拿全部歷史來算，不用像開盤價那樣等新資料重新累積。
    """
    signal_rows = supabase_select(
        "signal_scores",
        "select=code,trade_date,recommendation&order=trade_date.desc",
    )
    signal_by_date = {}
    for r in signal_rows:
        d = r.get("trade_date")
        signal_by_date.setdefault(d, []).append((r.get("code"), r.get("recommendation")))

    price_rows = supabase_select(
        "stock_daily",
        "select=code,trade_date,close&order=trade_date.desc",
    )
    price_by_date = {}
    for r in price_rows:
        d = r.get("trade_date")
        code = r.get("code")
        price_by_date.setdefault(d, {})[code] = safe_float(r.get("close"))
    return signal_by_date, price_by_date


def fetch_recent_explosion_confirms(lookback_days=10):
    """從 explosion_scores 表撈近期（約 lookback_days 個交易日內）曾經是「爆發確認」
    的股票，及當時記錄的 20 日箱頂價格，用來判斷今天是不是處在「爆發後回測」階段。

    因為週末/國定假日不會有排程紀錄（is_weekend() 防呆擋掉了週末），這裡用日曆天數
    做篩選時刻意留寬鬆一點的緩衝（乘 1.6 倍再加 3 天），確保涵蓋到週末造成的日期
    跳號，不會因為篩選範圍抓太緊而漏掉真正在 lookback_days 個交易日內的資料。
    每檔股票只保留「最近一次」出現 confirm 狀態的那筆（不受篩選範圍內出現幾次影響）。
    """
    cutoff_date = (date.today() - timedelta(days=int(lookback_days * 1.6) + 3)).isoformat()
    rows = supabase_select(
        "explosion_scores",
        f"select=code,trade_date,box_top_20d&status=eq.confirm&trade_date=gte.{cutoff_date}&order=trade_date.desc",
    )
    out = {}
    for r in rows:
        code = r.get("code")
        if code in out:
            continue  # 已經有更新的那筆了（因為 order=trade_date.desc，第一筆就是最新的）
        out[code] = {
            "trade_date": r.get("trade_date"),
            "box_top_20d": safe_float(r.get("box_top_20d")),
        }
    return out


def fetch_taiex_history():
    """從 Supabase 撈 TAIEX 大盤指數的歷史收盤值，用來算日漲跌%與5/20/60日漲跌%。

    global_factors 表每次執行都會 upsert 一筆 factor_code='TAIEX'、trade_date=今天
    的資料，所以查詢這張表過去的紀錄，就能重建出 TAIEX 自己的歷史序列，
    不需要另外開新表——這批資料早就存在，只是先前沒有回頭讀取利用。
    """
    rows = supabase_select(
        "global_factors",
        "select=trade_date,value&factor_code=eq.TAIEX&order=trade_date.desc",
    )
    hist = [(r.get("trade_date"), safe_float(r.get("value"))) for r in rows]
    hist.sort(key=lambda x: x[0])
    return hist


def fetch_yahoo_quote(symbol):
    """透過 Yahoo Finance 非官方 chart API 抓國際指數/匯率報價（例如 ^SOX、TWD=X）
    這是業界廣泛使用的非官方端點，比證交所的舊版報表穩定，但仍非正式合約 API，
    未來如果失效，建議改用付費資料商。
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
    try:
        data = http_get_json(url, timeout=20, retries=1)
    except Exception as e:
        print(f"[WARN] Yahoo Finance 抓取 {symbol} 失敗：{e}")
        return None
    try:
        result = data["chart"]["result"][0]
        meta = result["meta"]
        close = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        if close is None or prev_close in (None, 0):
            return None
        chg_pct = (close - prev_close) / prev_close * 100
        return {"close": close, "chg_pct": round(chg_pct, 2)}
    except (KeyError, IndexError, TypeError) as e:
        print(f"[WARN] Yahoo Finance 回傳格式異常（{symbol}）：{e}")
        return None


# 全球資產對照要用到的台股 ETF 代號對照表（這些 ETF 本身就在 STOCK_DAY_ALL 裡，不用額外接資料源）
GLOBAL_ETF_MAP = {
    "00646": {"category": "美股ETF", "name": "元大S&P500", "note": "美國大型股"},
    "00662": {"category": "美股ETF", "name": "富邦NASDAQ", "note": "美國科技股"},
    "00668": {"category": "美股ETF", "name": "國泰美國道瓊", "note": "美國藍籌"},
    "00635U": {"category": "原物料", "name": "期元大S&P黃金", "note": "避險資產"},
    "00642U": {"category": "原物料", "name": "期元大S&P石油", "note": "通膨指標"},
    "00682U": {"category": "原物料", "name": "期元大美元指數", "note": "外資流向"},
}


def pct_change_n_days_ago(history_for_code, n):
    """算「N個交易日前」到今天的漲跌幅%，資料不足時回傳 None（不瞎猜）"""
    if len(history_for_code) < n + 1:
        return None
    closes = [c for _, c in history_for_code]
    old_close = closes[-(n + 1)]
    new_close = closes[-1]
    if old_close == 0:
        return None
    return round((new_close - old_close) / old_close * 100, 2)


def compute_correlation(returns_a, returns_b):
    """算兩組報酬率序列的 Pearson 相關係數，不依賴 statistics.correlation()
    （這個函式要 Python 3.10+ 才有，手動實作避免受限於執行環境的 Python 版本）。
    兩個序列標準差任一為 0（完全沒有波動）時回傳 None，避免除以 0。
    """
    n = len(returns_a)
    if n == 0 or n != len(returns_b):
        return None
    mean_a = sum(returns_a) / n
    mean_b = sum(returns_b) / n
    cov = sum((a - mean_a) * (b - mean_b) for a, b in zip(returns_a, returns_b))
    var_a = sum((a - mean_a) ** 2 for a in returns_a)
    var_b = sum((b - mean_b) ** 2 for b in returns_b)
    if var_a == 0 or var_b == 0:
        return None
    return cov / (var_a ** 0.5 * var_b ** 0.5)


def compute_vs_taiex_correlation(series_a, series_b, min_days=20, lookback=60):
    """算 series_a（例如某檔ETF）跟 series_b（TAIEX）的近 lookback 個交易日「日報酬率」
    相關係數，用來衡量這檔資產平常跟大盤走勢的連動程度：接近 +1 代表高度同向，
    接近 -1 代表高度反向（避險特性），接近 0 代表關聯不大。
    兩個序列都是 [(date, close), ...]，用日期對齊（避免其中一邊缺某天資料時，
    位置對不上卻硬算出錯誤的相關係數）。可用天數不足 min_days 天時回傳 None。
    """
    dict_b = dict(series_b)
    common_dates = sorted(d for d, _ in series_a if d in dict_b)
    if len(common_dates) < min_days + 1:
        return None
    dict_a = dict(series_a)
    common_dates = common_dates[-(lookback + 1):]  # 只取最近 lookback+1 天（+1是為了能算出lookback筆報酬率）
    closes_a = [dict_a[d] for d in common_dates]
    closes_b = [dict_b[d] for d in common_dates]
    returns_a, returns_b = [], []
    for i in range(1, len(closes_a)):
        if closes_a[i - 1] and closes_b[i - 1]:
            returns_a.append((closes_a[i] - closes_a[i - 1]) / closes_a[i - 1])
            returns_b.append((closes_b[i] - closes_b[i - 1]) / closes_b[i - 1])
    if len(returns_a) < min_days:
        return None
    corr = compute_correlation(returns_a, returns_b)
    return round(corr, 2) if corr is not None else None


def fetch_taifex_futures_night_session(contract="TX"):
    """抓台指期近月合約的夜盤（盤後交易時段）收盤價，當隔夜開盤因子之一。
    端點：期交所 OpenAPI /DailyMarketReportFut（期貨每日交易行情），這個端點路徑
    跟欄位名稱是查證過的（見期交所官方 swagger.json），但沒辦法直接測試連線確認
    TradingSession 這個欄位「夜盤」實際會寫哪個字串，所以用關鍵字比對抓「非日盤」
    的那筆，並且把這次抓到的所有 TradingSession 值印出來，方便部署後對照 log
    確認比對邏輯有沒有抓對——如果沒抓對，之後可以照實際印出來的值調整關鍵字。
    """
    try:
        data = http_get_json("https://openapi.taifex.com.tw/v1/DailyMarketReportFut", timeout=20, retries=1)
    except Exception as e:
        print(f"[WARN] 期交所台指期資料抓取失敗：{e}")
        return None

    if not isinstance(data, list) or not data:
        print("[WARN] 期交所台指期資料格式異常（預期是非空陣列）")
        return None

    tx_rows = [r for r in data if str(r.get("Contract", "")).strip() == contract]
    if not tx_rows:
        print(f"[WARN] 期交所資料裡找不到 {contract} 合約")
        return None

    sessions_seen = sorted(set(str(r.get("TradingSession", "")) for r in tx_rows))
    print(f"[INFO] {contract} 這次抓到的 TradingSession 值：{sessions_seen}（用來核對夜盤比對邏輯有沒有抓對）")

    # 近月合約：ContractMonth(Week) 依字串排序取最早到期的那一個（合約月份格式通常是
    # YYYYMM，字串排序剛好等於時間先後排序）
    tx_rows.sort(key=lambda r: str(r.get("ContractMonth(Week)", "")))
    near_month = tx_rows[0].get("ContractMonth(Week)", "")
    near_month_rows = [r for r in tx_rows if r.get("ContractMonth(Week)", "") == near_month]

    # 挑「夜盤」那筆：常見代稱含這些關鍵字，用關鍵字比對而非完全比對字串，
    # 增加對未知確切字串格式的容錯度
    night_keywords = ["盤後", "夜", "AfterHour", "afterhour", "AFTER", "Night", "night", "AH"]
    night_row = next(
        (r for r in near_month_rows if any(kw in str(r.get("TradingSession", "")) for kw in night_keywords)),
        None,
    )
    if night_row is None:
        print(f"[WARN] {contract} 近月合約找不到符合夜盤關鍵字的資料列，"
              f"TradingSession值：{[r.get('TradingSession') for r in near_month_rows]}")
        return None

    last = safe_float(night_row.get("Last"))
    chg_pct_raw = night_row.get("%")
    chg_pct = None
    if chg_pct_raw not in (None, "", "-"):
        chg_pct = safe_float(chg_pct_raw)
    if chg_pct is None:
        change = safe_float(night_row.get("Change"))
        if last and (last - change):
            chg_pct = round(change / (last - change) * 100, 2)
    if not last:
        return None
    return {"close": last, "chg_pct": chg_pct, "contract_month": near_month}


# 隔夜開盤綜合分數的權重：台指期夜盤最直接反映開盤預期，給最高權重；
# 費半SOX是台股電子/半導體權重股的重要驅動，次之；美元/台幣貶值通常代表外資
# 壓力偏空，給最低權重且方向要反過來算（USD/TWD 上漲=台幣貶值=偏空）。
# 這是簡化版權重，之後可以依實際回測（隔夜分數 vs 隔日開盤表現）調整。
OVERNIGHT_WEIGHTS = {"TXF_NIGHT": 0.50, "^SOX": 0.35, "TWD=X": -0.15}

OVERNIGHT_VERDICT_THRESHOLDS = [
    (1.0, "強偏多開盤"),
    (0.3, "偏多開盤"),
    (-0.3, "中性開盤"),
    (-1.0, "偏空開盤"),
]


def compute_overnight_composite(factor_rows):
    """把已經算好的隔夜因子（台指期夜盤、SOX、美元/台幣）依權重合併成一個綜合分數，
    再轉成一句話判讀，不用每次都自己解讀好幾個獨立因子的方向。

    factor_rows: compute_global_factors() 目前已經組好的 rows 列表（含 TXF_NIGHT、
      ^SOX、TWD=X 這幾筆），從裡面撈出 chg_pct 來加權——沒有另外重抓資料。
    缺任何一個因子時，只用抓得到的那幾個因子、依各自權重佔比重新正規化（例如
    只有兩個因子有資料，就用那兩個因子的權重比例重算），避免因子缺一個就整個
    不給分數；所有因子都缺時回傳 None。
    """
    by_code = {r["factor_code"]: r for r in factor_rows}
    available = []
    for code, weight in OVERNIGHT_WEIGHTS.items():
        row = by_code.get(code)
        if row and row.get("chg_pct") is not None:
            available.append((weight, row["chg_pct"]))

    if not available:
        return None, "資料不足"

    total_abs_weight = sum(abs(w) for w, _ in available)
    composite_score = round(sum(w * chg for w, chg in available) / total_abs_weight, 1)

    verdict = "強偏空開盤"
    for threshold, label in OVERNIGHT_VERDICT_THRESHOLDS:
        if composite_score >= threshold:
            verdict = label
            break

    return composite_score, verdict


def compute_global_factors(stock_day, price_history):
    rows = []

    # 1. TAIEX 大盤指數
    # taiex_hist 一律先抓（不管等一下 taiex_close 有沒有抓到），避免後面 ETF 迴圈要用
    # 它算相關係數時，因為 taiex_close 抓取失敗導致這個變數根本沒被賦值而噴錯
    taiex_close = fetch_taiex_index()
    taiex_hist = fetch_taiex_history()
    if taiex_close is not None:
        # taiex_hist 是「不含今天」的歷史（今天這筆要等這次執行最後 upsert 才會寫進
        # global_factors），跟其他歷史相關函式（compute_vol_ratio等）用同一套邏輯：
        # 今天的值當獨立參數，歷史只回推到昨天為止
        taiex_chg_pct = None
        if taiex_hist:
            prev_close = taiex_hist[-1][1]
            if prev_close:
                taiex_chg_pct = round((taiex_close - prev_close) / prev_close * 100, 2)
        taiex_d5 = pct_change_n_days_ago(taiex_hist + [(TODAY, taiex_close)], 5)
        taiex_d20 = pct_change_n_days_ago(taiex_hist + [(TODAY, taiex_close)], 20)
        taiex_d60 = pct_change_n_days_ago(taiex_hist + [(TODAY, taiex_close)], 60)
        if taiex_chg_pct is None:
            taiex_direction = "中性"
        else:
            taiex_direction = "偏多" if taiex_chg_pct > 0 else ("偏空" if taiex_chg_pct < 0 else "中性")
        rows.append({
            "factor_code": "TAIEX", "trade_date": TODAY, "category": "大盤",
            "factor_name": "台股加權指數", "value": taiex_close, "chg_pct": taiex_chg_pct,
            "direction": taiex_direction, "impact_score": None,
            "note": json.dumps({
                "d5": taiex_d5, "d20": taiex_d20, "d60": taiex_d60,
                "note": "台灣證券交易所發行量加權股價指數",
            }, ensure_ascii=False),
        })
    # 用來算 ETF 相關係數的完整序列（含今天，如果今天有抓到的話）
    taiex_full_series = taiex_hist + ([(TODAY, taiex_close)] if taiex_close is not None else [])

    # 2. 國際指數/匯率（Yahoo Finance）+ 台指期夜盤（期交所）
    yahoo_targets = [
        ("^SOX", "半導體", "費半 SOX", "台股電子/半導體最重要隔夜因子之一"),
        ("TWD=X", "匯率", "美元/台幣", "台幣貶值常代表外資壓力"),
    ]
    for symbol, category, name, note in yahoo_targets:
        q = fetch_yahoo_quote(symbol)
        if q:
            direction = "偏多" if q["chg_pct"] > 0 else ("偏空" if q["chg_pct"] < 0 else "中性")
            rows.append({
                "factor_code": symbol, "trade_date": TODAY, "category": category,
                "factor_name": name, "value": q["close"], "chg_pct": q["chg_pct"],
                "direction": direction, "impact_score": None, "note": note,
            })

    night_fut = fetch_taifex_futures_night_session()
    if night_fut:
        chg_pct = night_fut["chg_pct"]
        direction = "中性" if chg_pct is None else ("偏多" if chg_pct > 0 else ("偏空" if chg_pct < 0 else "中性"))
        rows.append({
            "factor_code": "TXF_NIGHT", "trade_date": TODAY, "category": "台指期夜盤",
            "factor_name": "台指期近月(夜盤)", "value": night_fut["close"], "chg_pct": chg_pct,
            "direction": direction, "impact_score": None,
            "note": f"契約月份 {night_fut['contract_month']}，最直接反映台股隔日開盤預期",
        })

    # 隔夜開盤綜合分數：把上面剛算好的台指期夜盤/SOX/美元台幣依權重合併成一個分數，
    # 要放在這三個因子都已經 append 進 rows 之後才能算，因為它是從 rows 裡撈資料，
    # 不是另外重抓
    composite_score, composite_verdict = compute_overnight_composite(rows)
    rows.append({
        "factor_code": "OVERNIGHT_COMPOSITE", "trade_date": TODAY, "category": "綜合",
        "factor_name": "隔夜開盤綜合", "value": composite_score, "chg_pct": composite_score,
        "direction": composite_verdict, "impact_score": None,
        "note": "由 台指期夜盤(50%) + 費半SOX(35%) + 美元/台幣(15%，反向) 依可取得的因子加權計算，簡化版公式僅供參考",
    })

    # 3. 台股掛牌的美股/原物料 ETF（資料已經在 stock_day 裡，只是重新整理格式）
    for code, meta in GLOBAL_ETF_MAP.items():
        sd = stock_day.get(code)
        if not sd:
            continue
        chg_pct = (sd["change"] / (sd["close"] - sd["change"]) * 100) if (sd["close"] - sd["change"]) else 0
        # price_history 是「不含今天」的歷史，直接拿去算 d5/d20/d60 的話，closes[-1] 會是
        # 昨天的收盤價、不是今天，等於這三個百分比都少算了今天這一天的漲跌（跟先前
        # compute_ma_trend() 誤用歷史最後一筆當「今天」是同一類問題）。修法一樣：把
        # 今天的收盤價當獨立資料點接在歷史後面，再算百分比。
        hist_with_today = price_history.get(code, []) + [(TODAY, sd["close"])]
        d5 = pct_change_n_days_ago(hist_with_today, 5)
        d20 = pct_change_n_days_ago(hist_with_today, 20)
        d60 = pct_change_n_days_ago(hist_with_today, 60)
        corr_60d = compute_vs_taiex_correlation(hist_with_today, taiex_full_series)
        rows.append({
            "factor_code": f"ETF_{code}", "trade_date": TODAY, "category": meta["category"],
            "factor_name": meta["name"], "value": sd["close"], "chg_pct": round(chg_pct, 2),
            "direction": None, "impact_score": None,
            "note": json.dumps({"code": code, "d5": d5, "d20": d20, "d60": d60, "corr_60d": corr_60d, "note": meta["note"]}, ensure_ascii=False),
        })

    return rows


REBOUND_DROP_THRESHOLD_PCT = -7.0   # 短線急殺門檻（今日跌幅要低於這個值）
REBOUND_VOL_RATIO_THRESHOLD = 2.0   # 明顯放量門檻（今量至少是20日均量的這個倍數）


def compute_rebound_candidates(stock_day, volume_history):
    """挑出「超賣反彈候選」：短線急殺到極端、且同時爆量的股票，跟一般「重挫=純風險」
    的思路相反——無量陰跌代表恐慌賣壓可能還沒結束，但爆量重挫比較可能是「恐慌性
    出清 + 有人趁機承接」，短線容易出現技術性反彈。這份名單刻意跟 risk_scores
    分開、獨立存在，兩者不衝突：一檔股票可以同時被標記「風險：跌停預警」跟
    「反彈候選觀察」，這是同一件事的兩個角度，不是互斥的判斷。

    必要條件（兩個都要成立才會列入）：
      1. 今日跌幅 <= REBOUND_DROP_THRESHOLD_PCT（短線急殺）
      2. 量比（今量 ÷ 20日均量）>= REBOUND_VOL_RATIO_THRESHOLD（明顯放量）
    量比資料不足（歷史天數太少，無法確定是否真的放量）時直接排除，不拿不確定
    的資料硬湊進候選名單，避免把「量能正常但剛好資料不足」的股票誤判成反彈候選。
    """
    rows = []
    for code, sd in stock_day.items():
        close, high, low, vol = sd["close"], sd["high"], sd["low"], sd["volume"]
        if close <= 0:
            continue
        chg_pct = (sd["change"] / (close - sd["change"]) * 100) if (close - sd["change"]) else 0
        if chg_pct > REBOUND_DROP_THRESHOLD_PCT:
            continue
        vol_ratio = compute_vol_ratio(vol, volume_history.get(code, []))
        if vol_ratio is None or vol_ratio < REBOUND_VOL_RATIO_THRESHOLD:
            continue

        # 收盤價在當日高低區間的位置：偏高代表盤中重挫、但收盤前有承接力道拉回不少，
        # 反彈訊號比較強；偏低代表一路殺到收盤，反彈訊號比較弱。純參考欄位，
        # 不影響是否入選這份名單，只是給你判斷時多一個角度。
        close_position_pct = round((close - low) / (high - low) * 100, 1) if high > low else 50.0

        rows.append({
            "code": code,
            "name": sd["name"],
            "trade_date": TODAY,
            "close": close,
            "chg_pct": round(chg_pct, 2),
            "vol_ratio_20": vol_ratio,
            "close_position_pct": close_position_pct,
            "note": f"當日跌幅 {chg_pct:.2f}%，量比 {vol_ratio:.2f} 倍，短線急殺放量觀察",
        })

    rows.sort(key=lambda r: r["vol_ratio_20"], reverse=True)
    return rows


WEAKENING_SELL_DAYS_THRESHOLD = 3   # 連續賣超天數門檻
WEAKENING_VOL_RATIO_THRESHOLD = 1.5  # 量比達這個值以上視為有止跌訊號，不算轉弱觀察


def compute_weakening_watchlist(stock_day, volume_history, institutional, institutional_history, price_history):
    """挑出「轉弱觀察」名單：法人連續賣超天數達門檻、且近5日股價淨下跌、且沒有
    明顯放量止跌訊號（量比未達門檻）都同時成立的股票——代表法人持續在賣、股價
    也真的走弱、還沒看到止跌跡象，是比單純「連續賣超」更嚴格的一份子集合。

    這份名單刻意獨立於 risk_scores 現有的計分門檻（score>=60才列入）之外，因為
    「轉弱觀察」看的是連續賣超+價跌+量縮這三個條件本身，不是綜合風險分數——
    一檔股票可能單日分數不到風險示警的門檻，但已經連續好幾天在轉弱，這種情況
    也值得被看到，不應該因為單日分數不夠高就被排除在外。

    必要條件（都要成立）：
      1. 法人連續賣超天數 >= WEAKENING_SELL_DAYS_THRESHOLD
      2. 近5個交易日股價淨下跌（沒有5日歷史資料時直接排除，不用不確定的資料硬湊）
      3. 量比 < WEAKENING_VOL_RATIO_THRESHOLD（沒有明顯放量止跌訊號；量比資料
         不足時，保守起見不排除——因為「不確定有沒有爆量」不等於「確定沒有爆量」，
         但也不能證明有爆量止跌，兩害相權取其輕，讓它通過這一關，不因缺資料而
         漏掉真正值得觀察的轉弱股票）
    """
    rows = []
    for code, sd in stock_day.items():
        close, vol = sd["close"], sd["volume"]
        if close <= 0:
            continue
        inst = institutional.get(code, {})
        inst_net = inst.get("institutional_net", 0)
        consecutive_sell_days = compute_consecutive_sell_days(inst_net, institutional_history.get(code, []))
        if consecutive_sell_days < WEAKENING_SELL_DAYS_THRESHOLD:
            continue

        hist_with_today = price_history.get(code, []) + [(TODAY, close)]
        pct_5d = pct_change_n_days_ago(hist_with_today, 5)
        if pct_5d is None or pct_5d >= 0:
            continue  # 沒有5日歷史資料，或近5日股價其實是漲的，不算轉弱

        vol_ratio = compute_vol_ratio(vol, volume_history.get(code, []))
        if vol_ratio is not None and vol_ratio >= WEAKENING_VOL_RATIO_THRESHOLD:
            continue  # 有明顯放量，可能是止跌訊號，不算「還沒止跌」的轉弱觀察

        rows.append({
            "code": code,
            "name": sd["name"],
            "trade_date": TODAY,
            "close": close,
            "consecutive_sell_days": consecutive_sell_days,
            "pct_5d": pct_5d,
            "vol_ratio_20": vol_ratio,
            "note": f"法人連賣{consecutive_sell_days}天，近5日跌{abs(pct_5d):.2f}%，"
                    f"量比{f'{vol_ratio:.2f}倍' if vol_ratio is not None else '資料不足'}",
        })

    rows.sort(key=lambda r: r["consecutive_sell_days"], reverse=True)
    return rows


def compute_liquidity_scores(stock_day, volume_history, min_days=3, risk_threshold_pct=50.0):
    """交易性風險 / 流動性：算「100張（10萬股）這筆單量，佔該股平均每日成交量的百分比」，
    比例越高代表流動性越差（一筆不大的單就可能吃掉一整天的量，賣的時候容易砸自己的價）。

    只列出比例達 risk_threshold_pct（預設50%）以上的股票，避免整份清單塞滿流動性正常的股票，
    跟原本畫面上「交易性風險」是個篩選過的觀察名單的設計精神一致。
    資料不足 min_days 天時整檔跳過，不用太少天數的平均值誤導判斷。
    """
    rows = []
    for code, sd in stock_day.items():
        close = sd["close"]
        if close <= 0:
            continue
        hist = volume_history.get(code, [])
        if len(hist) < min_days:
            continue
        recent = hist[-20:]  # 有多少天算多少天，最多取20天
        volumes = [v for _, v in recent if v > 0]
        if not volumes:
            continue
        avg_volume = sum(volumes) / len(volumes)
        if avg_volume <= 0:
            continue

        est_trade_value_million = round(avg_volume * close / 1_000_000, 2)
        pct_100lots = round((100_000 / avg_volume) * 100, 1)

        if pct_100lots < risk_threshold_pct:
            continue

        risk_score = min(round(pct_100lots, 1), 100)
        if risk_score >= 80:
            label = "極低"
        elif risk_score >= 60:
            label = "低"
        else:
            label = "中"

        rows.append({
            "code": code,
            "name": sd["name"],
            "trade_date": TODAY,
            "close": close,
            "avg_volume_20d": int(avg_volume),
            "avg_volume_days": len(volumes),
            "est_trade_value_million": est_trade_value_million,
            "pct_100lots_of_avg": pct_100lots,
            "liquidity_risk_score": risk_score,
            "liquidity_label": label,
        })

    rows.sort(key=lambda r: r["liquidity_risk_score"], reverse=True)
    return rows[:300]  # 避免清單過長，只留風險最高的前 300 檔


def fetch_pe_pb():
    """個股本益比、殖利率、股價淨值比（可用於篩選基本面體質）
    端點：/exchangeReport/BWIBBU_ALL
    """
    try:
        data = http_get_json(f"{TWSE_BASE}/exchangeReport/BWIBBU_ALL")
    except Exception as e:
        print(f"[WARN] 本益比資料抓取失敗：{e}")
        return {}
    out = {}
    for row in data:
        code = row.get("Code")
        if not code:
            continue
        out[code] = {
            "pe": safe_float(row.get("PEratio")),
            "yield": safe_float(row.get("DividendYield")),
            "pb": safe_float(row.get("PBratio")),
        }
    return out


# --------------------------------------------------------------------------
# 2. 評分邏輯（SCORING RULES）
# --------------------------------------------------------------------------
# 這是一套「簡化版」規則，目的是先讓系統可以動起來、之後你可以依實際回測結果調整權重。
#
# 【訊號分數 net_signal】（v2：新增財務底色 / 營收動能 / 主升段 三個維度）
#   +1  法人（三大合計）買超股數 > 0
#   +1  法人買超股數 > 該股 20 日均量的 5%（買超強度夠大；資料不足20天時fallback用今日成交量當基準）
#   +1  今日漲跌 > 0 且今日成交量 > 昨日成交量（價量齊揚；沒有昨天資料時fallback只看漲跌）
#   +1  財務底色佳：本益比介於 0~25（有獲利、不過度昂貴）且殖利率 > 1.5% 且股價淨值比 0~4
#   +1  營收動能加速：最新月營收年增率 > 20%
#   +1  主升段確認：站上上揚的20日均線、且20MA在60MA之上（需資料庫累積 ≥20 個交易日才會判斷，不足時不加分也不扣分）
#   -1  法人賣超股數 > 0
#   -1  法人賣超股數 > 該股 20 日均量的 5%（同上，資料不足時fallback用今日成交量）
#   -1  今日跌幅 > 5%（單日重挫）
#   附加參考標籤（純資訊性，目前不影響 net_signal 計分）：
#     法人連續買超/賣超天數 >= 3 天時，會在多頭/空頭訊號欄位標記「法人連續買超N天」
#     或「法人連續賣超N天」，資料缺失（T86抓取失敗）的那天視為連續天數中斷，不瞎猜
#     單日振幅（ATR%）> RISK_AMPLITUDE_PCT（跟風險分數用同一個門檻）時標記「ATR波動風險」
#   最終 recommendation 依 net_signal 對照（門檻值沒變，但因為新增了三個正向維度，
#   理論上 strong-bull 的檔數會比 v1 版本多一些，這是預期中的行為，門檻值可依實際回測再調）：
#     net_signal >= 3      → strong-bull 🚀 強多候選
#     net_signal == 2      → bull ↗ 多頭候選
#     net_signal == 1      → watch 🟢 留意
#     net_signal == 0      → neutral — 中性
#     net_signal == -1     → pullback 🟡 觀望
#     net_signal <= -2     → avoid 🚫 避開
#
# 【爆發前兆分數 explosion score，0-100】
#   以量能與突破為主：
#     量能維度貢獻 0-40 分，由兩個訊號加總（各自代表不同角度，都跟今日成交量有關）：
#       量比（今量/20日均量）貢獻 0-25 分：min(量比, 5) / 5 * 25
#         → 看「今天量是平常的幾倍」，資料不足時 fallback 用中性值 1.0 繼續參與計分
#       量能z-score（今量偏離近60日均量幾個標準差）貢獻 0-15 分：
#         min(max(z-score, 0), 4) / 4 * 15
#         → 看「相對這檔股票平常的量能波動幅度，今天算不算異常」，資料不足時該次不貢獻分數（視為0）
#     單日漲幅貢獻 0-30 分：min(max(漲幅,0), 10) / 10 * 30
#     收盤價站上今日均價（強勢收盤）貢獻 0-30 分：(close - low) / (high - low) * 30（若 high=low 則給 15）
#   附加參考欄位（目前只是算出真實數字寫進輸出，尚未納入 score 計分公式）：
#     ma_convergence_pct：均線收斂度，(可用均線中最高-最低)/現價*100，數字越小代表均線越收斂
#     boll_position_pct：布林通道位置（%B），現價落在布林通道的百分比位置，0=貼下軌、100=貼上軌
#     兩者資料不足時皆回傳 None，不是每天都會有值
#   status：
#     score >= 80          → confirm 爆發確認
#     60 <= score < 80      → pre 爆發前兆
#     score < 60，但近10個交易日內曾經是confirm、且今日收盤價仍守在當初box_top_20d之上
#                           → retest 爆發後回測（量能退溫、股價拉回測試前壓力，
#                             尚未跌破支撐；跌破的話視為突破失敗，不列入雷達）
#     其餘                  → 不列入雷達
#
# 【風險分數 risk_score，0-100】
#   跌幅 >= 9.5%           → +50（視為跌停預警）
#   法人賣超股數 > 20日均量 3% → +25
#   單日振幅（high-low)/close > 6% → +15
#   量比 < 0.5（量縮流動性差） → +10
#   risk_level：
#     score >= 80           → high 高度風險
#     score >= 60            → warn 警戒
#     其餘不列入清單
#
# 這些門檻值都寫在下面常數區，方便你之後調整。
# --------------------------------------------------------------------------

def compute_avg_volume(history_for_code, min_days=3, lookback=20):
    """算近期（最多 lookback 天）平均成交量。
    history_for_code: [(date_str, volume), ...] 由舊到新排序（不含今天）
    資料不足 min_days 天時回傳 None（不瞎猜，讓呼叫端自己決定要 fallback 成什麼）
    """
    recent = history_for_code[-lookback:]
    volumes = [v for _, v in recent if v > 0]
    if len(volumes) < min_days:
        return None
    avg_volume = sum(volumes) / len(volumes)
    if avg_volume <= 0:
        return None
    return avg_volume


def compute_vol_ratio(current_volume, history_for_code, min_days=3, lookback=20):
    """算「量比」：今日成交量 ÷ 近期（最多 lookback 天）平均成交量。
    history_for_code: [(date_str, volume), ...] 由舊到新排序（不含今天）
    資料不足 min_days 天時回傳 None（不瞎猜，讓呼叫端自己決定要 fallback 成中性值還是跳過該規則）
    """
    avg_volume = compute_avg_volume(history_for_code, min_days, lookback)
    if avg_volume is None:
        return None
    return round(current_volume / avg_volume, 2)


def compute_vol_zscore(current_volume, history_for_code, min_days=10, lookback=60):
    """算「60日成交量 z-score」：今日量偏離近期（最多 lookback 天）平均量幾個標準差。
    跟量比（vol_ratio）看的是同一件事的不同角度：量比看「倍數」，z-score 看「相對於
    這檔股票平時波動幅度而言，今天算不算異常」——同樣是量增1倍，對成交量本來就很不穩定
    的股票可能很正常（z-score低），對量能一向平穩的股票可能就是顯著異常（z-score高）。

    history_for_code: [(date_str, volume), ...] 由舊到新排序（不含今天）
    資料不足 min_days 天，或近期量能完全沒有波動（標準差為0，除以0會爆），都回傳 None，
    呼叫端寫入時允許 None（欄位設計上就是可為空，代表「資料不足，本次不判斷」）。
    """
    recent = history_for_code[-lookback:]
    volumes = [v for _, v in recent if v > 0]
    if len(volumes) < min_days:
        return None
    mean_volume = statistics.mean(volumes)
    stdev_volume = statistics.stdev(volumes)  # 需要至少2筆資料，上面 min_days>=10 已保證足夠
    if stdev_volume == 0:
        return None
    return round((current_volume - mean_volume) / stdev_volume, 2)


def compute_ma_convergence_pct(current_close, history_for_code, periods=(5, 10, 20)):
    """算「均線收斂度」：抓資料夠的短中期均線（5/10/20日，資料不足的天期就跳過，只用抓得到的），
    看這些均線彼此貼近的程度——數值越小代表均線收斂（常見的「醞釀突破」訊號之一），
    數值越大代表均線發散（多空排列分明）。

    算法：(可用均線中最高的 - 最低的) / 現價 * 100
    至少要能算出兩條均線才有比較意義，不足時回傳 None（不硬湊）。
    """
    if current_close <= 0:
        return None
    closes = [c for _, c in history_for_code] + [current_close]
    ma_values = [sum(closes[-p:]) / p for p in periods if len(closes) >= p]
    if len(ma_values) < 2:
        return None
    spread = max(ma_values) - min(ma_values)
    return round(spread / current_close * 100, 2)


def compute_boll_position_pct(current_close, history_for_code, period=20, k=2, min_days=10):
    """算「布林通道位置」（%B）：現價落在布林通道的哪個位置。
    0% = 貼著下軌，50% = 貼著中軌（均線），100% = 貼著上軌；可以超過 0~100，代表股價已經突破軌道。

    標準布林通道用 20 日，但資料庫還在累積階段時 20 天常常不夠，所以退而求其次：
    只要有 min_days（預設10天）以上資料就先算，天數不足 period 時就用「目前有的天數」
    當觀察窗，等於是縮短版的布林通道，僅供參考，天數不足 min_days 時直接回傳 None。
    """
    if current_close <= 0:
        return None
    closes = [c for _, c in history_for_code] + [current_close]
    if len(closes) < min_days:
        return None
    window = closes[-period:] if len(closes) >= period else closes
    if len(window) < 2:
        return None
    mean_close = statistics.mean(window)
    stdev_close = statistics.stdev(window)
    if stdev_close == 0:
        return None
    upper = mean_close + k * stdev_close
    lower = mean_close - k * stdev_close
    if upper == lower:
        return None
    return round((current_close - lower) / (upper - lower) * 100, 2)


def compute_consecutive_sell_days(current_inst_net, history_for_code):
    """算「法人連續賣超天數」：從今天開始往回數，遇到賣超（淨額 < 0）就繼續累加，
    遇到買超/持平（>= 0）或資料缺失（None，代表那天 T86 抓取失敗、不知道狀態）
    就停止往回數。

    資料缺失的那天無法確認是否真的賣超，保守起見直接視為連續天數中斷，不繼續
    往回數，避免把「不知道」誤算成「有賣超」而高估連續天數。

    current_inst_net: 今天的法人合計買賣超（來自 institutional 字典，T86 抓取
      失敗時上層會 fallback 成 0，等於今天視為「無賣超」，這是跟既有程式碼其他地方
      處理法人資料缺失時一致的作法，不另外特殊處理）
    history_for_code: [(date_str, institutional_net_or_None), ...] 由舊到新排序（不含今天）
    """
    values = [v for _, v in history_for_code] + [current_inst_net]
    count = 0
    for v in reversed(values):
        if v is not None and v < 0:
            count += 1
        else:
            break
    return count


def compute_consecutive_buy_days(current_inst_net, history_for_code):
    """算「法人連續買超天數」，跟 compute_consecutive_sell_days 對稱、方向相反：
    從今天開始往回數，遇到買超（淨額 > 0）就繼續累加，遇到賣超/持平（<= 0）或
    資料缺失（None，代表那天 T86 抓取失敗、不知道狀態）就停止往回數。

    資料缺失的那天無法確認是否真的買超，一樣保守視為連續天數中斷，理由跟
    compute_consecutive_sell_days 相同，不重複贅述。

    current_inst_net: 今天的法人合計買賣超
    history_for_code: [(date_str, institutional_net_or_None), ...] 由舊到新排序（不含今天）
    """
    values = [v for _, v in history_for_code] + [current_inst_net]
    count = 0
    for v in reversed(values):
        if v is not None and v > 0:
            count += 1
        else:
            break
    return count


def compute_low_divergence(current_close, current_volume, price_hist, volume_hist, lookback=20, min_days=10):
    """算「低吸背離」：股價創新低，但當天成交量沒有跟著放大（甚至比上一次探低那天
    的量還小），是常見的底部背離型態——代表雖然價格破底，賣壓其實在減弱（不是
    恐慌性追殺），可能是止跌訊號。

    跟「超賣反彈候選」（compute_rebound_candidates）不同：那個看的是「急殺+爆量」
    （恐慌性出清後有人承接），這個看的是「破底+量縮」（賣壓衰竭型態），是兩種不同
    角度的底部訊號，可能同時出現，也可能只出現一種，不衝突。

    資料不足 min_days 天，或找不到有效的「上一次探低」比較基準時，回傳 False（不判斷）。
    """
    price_with_today = price_hist + [(TODAY, current_close)]
    if len(price_with_today) < min_days:
        return False

    recent = price_with_today[-lookback:]
    closes = [c for _, c in recent]
    if current_close > min(closes):
        return False  # 今天不是這段期間內的新低，不算破底

    prior_prices = recent[:-1]  # 排除今天，找歷史上的低點當比較基準
    if not prior_prices:
        return False
    prior_low_date, _ = min(prior_prices, key=lambda x: x[1])
    vol_dict = dict(volume_hist)
    prior_low_volume = vol_dict.get(prior_low_date)
    if not prior_low_volume or prior_low_volume <= 0:
        return False

    return current_volume < prior_low_volume


def compute_market_consecutive_buy_days(institutional, institutional_history):
    """算「大盤整體」法人買超連續天數，當作「籌碼領先」的比對基準。

    判斷方式：某一天如果法人淨買超的股票家數 > 淨賣超家數，就算大盤的「買超日」
    （用家數而不是金額加總，避免被少數幾檔超大權值股的買賣金額主導整體判斷）。
    從今天往回數，遇到不是買超日就停止，回傳連續買超日數。
    """
    buy_count, sell_count = {}, {}

    def tally(date_str, net):
        if net is None:
            return
        if net > 0:
            buy_count[date_str] = buy_count.get(date_str, 0) + 1
        elif net < 0:
            sell_count[date_str] = sell_count.get(date_str, 0) + 1

    trading_dates = set()
    for hist in institutional_history.values():
        for d, v in hist:
            trading_dates.add(d)
            tally(d, v)
    trading_dates.add(TODAY)
    for inst in institutional.values():
        tally(TODAY, inst.get("institutional_net"))

    count = 0
    for d in sorted(trading_dates, reverse=True):
        if buy_count.get(d, 0) > sell_count.get(d, 0):
            count += 1
        else:
            break
    return count


def compute_ma_trend(current_close, history_for_code, min_days=20):
    """判斷是否符合「主升段確認」：站上20日均線、20日均線向上、且20MA在60MA之上

    current_close: 今天的收盤價（來自 stock_day，不是資料庫歷史）
    history_for_code: [(date_str, close), ...] 由舊到新排序，「不含今天」

    ⚠️ 之前的版本直接把 history_for_code 的最後一筆當成「今天」，但這批歷史資料是
    從 Supabase 撈的，這次執行還沒把今天收盤價寫回資料庫，所以最後一筆其實是
    上一個交易日，等於整個判斷都慢了一天、完全沒用到今天真正發生的價格變化。
    現在改成把「今天」當獨立參數傳進來，跟其他量能相關的函式（compute_vol_ratio /
    compute_vol_zscore）用同一套設計方式，避免同樣的錯誤。

    資料不足 min_days 天（含今天）時回傳 False（不判斷，避免用不足的資料誤判）
    """
    closes = [c for _, c in history_for_code] + [current_close]
    if len(closes) < min_days:
        return False
    ma20_today = sum(closes[-20:]) / 20
    price_today = closes[-1]
    above_ma20 = price_today > ma20_today
    ma20_rising = True
    if len(closes) >= 21:
        ma20_yesterday = sum(closes[-21:-1]) / 20
        ma20_rising = ma20_today > ma20_yesterday
    above_ma60 = True
    if len(closes) >= 60:
        ma60_today = sum(closes[-60:]) / 60
        above_ma60 = ma20_today > ma60_today
    return above_ma20 and ma20_rising and above_ma60


def financial_color_ok(pe_pb_row):
    """判斷「財務底色佳」：本益比合理（有獲利且不過度昂貴）、有配息、股價淨值比不過高"""
    if not pe_pb_row:
        return False
    pe = pe_pb_row.get("pe", 0)
    yield_pct = pe_pb_row.get("yield", 0)
    pb = pe_pb_row.get("pb", 0)
    return (0 < pe <= 25) and (yield_pct > 1.5) and (0 < pb <= 4)


SIGNAL_NET_TO_REC = [
    (3, "strong-bull", "🚀 強多候選"),
    (2, "bull", "↗ 多頭候選"),
    (1, "watch", "🟢 留意"),
    (0, "neutral", "— 中性"),
    (-1, "pullback", "🟡 觀望"),
    (-999, "avoid", "🚫 避開"),
]

RISK_DROP_LIMIT_PCT = -9.5      # 跌幅達此比例視為跌停預警
RISK_INST_SELL_RATIO = 0.03     # 法人賣超佔 20 日均量比例門檻
RISK_AMPLITUDE_PCT = 6.0        # 單日振幅門檻
EXPLOSION_CONFIRM_SCORE = 80
EXPLOSION_PRE_SCORE = 60
EXPLOSION_VOL_RATIO_MAX_POINTS = 25   # 量能維度（滿分40）裡，「量比」佔的上限
EXPLOSION_VOL_ZSCORE_MAX_POINTS = 15  # 量能維度（滿分40）裡，「z-score」佔的上限
EXPLOSION_VOL_ZSCORE_CAP = 4.0        # z-score 超過這個值，額外分數不再往上加


BACKTEST_LOOKBACK_DAYS = 4  # 隔日開盤回測的滾動樣本天數

# 回測分類：key -> (符合這個分類的判斷函式, 「命中」的方向)
# bull_all/bear_all 是額外聚合的大類（strong-bull+bull 合併看、pullback+avoid 合併看），
# 方便一次看整體多頭/空頭候選的勝率，不用逐一分類別看
BACKTEST_CATEGORIES = {
    "strong_bull": (lambda rec: rec == "strong-bull", "up"),
    "bull": (lambda rec: rec == "bull", "up"),
    "bull_all": (lambda rec: rec in ("strong-bull", "bull"), "up"),
    "pullback": (lambda rec: rec == "pullback", "down"),
    "avoid": (lambda rec: rec == "avoid", "down"),
    "bear_all": (lambda rec: rec in ("pullback", "avoid"), "down"),
}


def compute_backtest_stats(lookback_days=BACKTEST_LOOKBACK_DAYS):
    """算「隔日開盤回測」勝率：抓最近幾個交易日的訊號推薦名單，對照隔一個交易日的
    開盤價，驗證推薦方向對不對——多頭類（strong-bull/bull）預期隔日開盤價格會比
    前一天收盤高，空頭類（pullback/avoid）預期會比較低，算出符合預期的比例。

    ⚠️ 這個功能依賴 stock_daily 的 open 欄位，這是這次才開始寫入的，部署當天
    之前的舊資料沒有開盤價紀錄，回測會從部署那天開始逐漸累積到 lookback_days
    天的完整樣本，不會一部署就馬上有完整的樣本。
    """
    signal_by_date, price_by_date = fetch_backtest_raw_data(lookback_days)
    trade_dates = sorted(price_by_date.keys())

    # 找出「有隔天開盤價可以驗證」的基準日：D 本身要有 signal_scores 推薦名單，
    # D 的下一個交易日（trade_dates 裡緊接在後面那個）要存在，回測才有東西可以比對
    evaluable_dates = []
    for i, d in enumerate(trade_dates[:-1]):
        next_d = trade_dates[i + 1]
        if d in signal_by_date:
            evaluable_dates.append((d, next_d))
    evaluable_dates = evaluable_dates[-lookback_days:]  # 只取最近 lookback_days 個可驗證的基準日

    stats = {key: {"total": 0, "success": 0} for key in BACKTEST_CATEGORIES}
    # days_with_data：真正「有比對到至少一筆資料」的基準日，跟 evaluable_dates 不同——
    # evaluable_dates 只代表「那天有推薦名單、且下一個交易日的日期存在」，但下一個
    # 交易日的 open 欄位可能是部署這個功能之前寫入的舊資料，整批是 None，比對時會
    # 全部被跳過、貢獻不到任何 total_count。用 evaluable_dates 的天數當「樣本天數」
    # 對外顯示，會在部署初期虛報樣本量（顯示有N天，但其實大多數天都比對不到東西），
    # 所以另外用這個集合追蹤「真正貢獻過資料」的天數，才是對外該講的數字。
    days_with_data = set()

    for d, next_d in evaluable_dates:
        recs = signal_by_date.get(d, [])
        d_prices = price_by_date.get(d, {})
        next_prices = price_by_date.get(next_d, {})
        for code, recommendation in recs:
            close_d = d_prices.get(code, {}).get("close")
            open_next = next_prices.get(code, {}).get("open")
            if not close_d or open_next is None:
                continue  # 沒有隔天開盤價可比對（停牌、或還沒累積到有 open 的資料）
            days_with_data.add(d)
            pct_change = (open_next - close_d) / close_d * 100
            for key, (match_fn, direction) in BACKTEST_CATEGORIES.items():
                if not match_fn(recommendation):
                    continue
                stats[key]["total"] += 1
                success = (pct_change > 0) if direction == "up" else (pct_change < 0)
                if success:
                    stats[key]["success"] += 1

    rows = []
    for key, s in stats.items():
        win_rate = round(s["success"] / s["total"] * 100, 1) if s["total"] > 0 else None
        rows.append({
            "trade_date": TODAY,
            "category": key,
            "lookback_days": lookback_days,
            "sample_dates": len(days_with_data),
            "total_count": s["total"],
            "success_count": s["success"],
            "win_rate": win_rate,
        })
    return rows


CALIBRATION_MIN_SAMPLE = 20        # 樣本數低於這個值，判讀為「樣本不足」不下結論
CALIBRATION_STRONG_THRESHOLD = 60.0  # 命中率達這個值以上，判讀為「順向有效」
CALIBRATION_WEAK_THRESHOLD = 45.0    # 命中率低於這個值，判讀為「反向，值得留意」


def calibration_verdict(total, win_rate):
    """把命中率轉成一句話的質化判讀，不用自己解讀百分比數字。
    45~60% 之間視為接近丟硬幣、沒有統計優勢，跟命中率過低（反向）分開處理，
    因為「常常錯」跟「沒有比亂猜準」是兩件不同的事，值得分開標示。
    """
    if total < CALIBRATION_MIN_SAMPLE or win_rate is None:
        return "樣本不足，尚待觀察"
    if win_rate >= CALIBRATION_STRONG_THRESHOLD:
        return "順向有效"
    if win_rate <= CALIBRATION_WEAK_THRESHOLD:
        return "反向，值得留意"
    return "無優勢，降權觀察"


def compute_calibration_stats():
    """算「長期校準統計」：用「隔日收盤 vs 前一天收盤」驗證推薦方向對不對，樣本
    抓全部歷史（不像隔日開盤回測限定滾動最近幾天），並附上質化判讀標籤，方便一眼
    看出這個分類的訊號整體「有沒有用」，不用自己去解讀百分比數字。

    跟 compute_backtest_stats() 的差異：
      - 比對基準：這裡用「收盤」，回測用「開盤」
      - 樣本範圍：這裡用「全部歷史」，回測用「滾動最近 N 天」
      - 能不能回溯：這裡可以（收盤價本來就一直都有存），回測不行（開盤價這次才開始存）
    """
    signal_by_date, price_by_date = fetch_calibration_raw_data()
    trade_dates = sorted(price_by_date.keys())

    evaluable_dates = []
    for i, d in enumerate(trade_dates[:-1]):
        next_d = trade_dates[i + 1]
        if d in signal_by_date:
            evaluable_dates.append((d, next_d))
    # 不像回測那樣做 [-lookback_days:] 截斷，這裡刻意保留全部可驗證的基準日

    stats = {key: {"total": 0, "success": 0} for key in BACKTEST_CATEGORIES}
    days_with_data = set()

    for d, next_d in evaluable_dates:
        recs = signal_by_date.get(d, [])
        d_prices = price_by_date.get(d, {})
        next_prices = price_by_date.get(next_d, {})
        for code, recommendation in recs:
            close_d = d_prices.get(code)
            close_next = next_prices.get(code)
            if not close_d or not close_next:
                continue
            days_with_data.add(d)
            pct_change = (close_next - close_d) / close_d * 100
            for key, (match_fn, direction) in BACKTEST_CATEGORIES.items():
                if not match_fn(recommendation):
                    continue
                stats[key]["total"] += 1
                success = (pct_change > 0) if direction == "up" else (pct_change < 0)
                if success:
                    stats[key]["success"] += 1

    rows = []
    for key, s in stats.items():
        win_rate = round(s["success"] / s["total"] * 100, 1) if s["total"] > 0 else None
        rows.append({
            "trade_date": TODAY,
            "category": key,
            "sample_dates": len(days_with_data),
            "total_count": s["total"],
            "success_count": s["success"],
            "win_rate": win_rate,
            "verdict": calibration_verdict(s["total"], win_rate),
        })
    return rows


def recommendation_for(net_signal):
    for threshold, rec, label in SIGNAL_NET_TO_REC:
        if net_signal >= threshold:
            return rec, label
    return "avoid", "🚫 避開"


def compute_signal_scores(stock_day, institutional, pe_pb, revenue, price_history, volume_history, institutional_history):
    rows = []
    # 大盤整體買超連續天數只需要算一次，不用每檔股票重算，拿來當「籌碼領先」的比對基準
    market_buy_streak = compute_market_consecutive_buy_days(institutional, institutional_history)

    for code, sd in stock_day.items():
        inst = institutional.get(code, {})
        inst_net = inst.get("institutional_net", 0)
        close = sd["close"]
        chg_pct = (sd["change"] / (close - sd["change"]) * 100) if (close - sd["change"]) else 0

        net_signal = 0
        bull_tags, bear_tags = [], []

        if inst_net > 0:
            net_signal += 1
            bull_tags.append("法人買超")
        elif inst_net < 0:
            net_signal -= 1
            bear_tags.append("法人賣超")

        # 法人連續買超/賣超天數：純資訊性標籤，不影響 net_signal 計分（跟
        # consecutive_sell_days 剛補實作時的做法一致，先讓標籤準確，之後有需要
        # 再考慮要不要納入計分）。門檻設 3 天以上才標記，避免每天都在切換方向
        # 的正常波動也被貼標籤，稀釋掉真正持續買超/賣超的訊號。
        hist_inst = institutional_history.get(code, [])
        consecutive_buy_days = compute_consecutive_buy_days(inst_net, hist_inst)
        consecutive_sell_days_signal = compute_consecutive_sell_days(inst_net, hist_inst)
        if consecutive_buy_days >= 3:
            bull_tags.append(f"法人連續買超{consecutive_buy_days}天")
        if consecutive_sell_days_signal >= 3:
            bear_tags.append(f"法人連續賣超{consecutive_sell_days_signal}天")

        # 新進買進：法人今天剛從「沒買/賣超」狀態轉為買超的第一天（連續買超天數剛好=1），
        # 代表可能是新資金剛進場，跟上面「連續買超N天」（門檻3天以上，代表已經持續一陣子）
        # 是同一件事的不同階段，不會同時觸發，一個是「剛開始」一個是「已經持續」
        if consecutive_buy_days == 1:
            bull_tags.append("新進買進")

        # 籌碼領先：這檔股票的法人連續買超天數，比大盤整體的連續買超天數還多，
        # 代表這檔股票的法人買盤比大盤趨勢更早開始（不是現在大家都在買、它才跟著買）。
        # 門檻設至少連續買超2天以上才判斷，避免大盤才剛開始買、個股也只買1天，
        # 兩邊天數都是1沒有比較意義的情況被誤判成領先
        if consecutive_buy_days >= 2 and consecutive_buy_days > market_buy_streak:
            bull_tags.append("籌碼領先")

        # 低吸背離：股價破底但成交量沒有跟著放大（賣壓衰竭型態），純資訊性標籤，
        # 跟「超賣反彈候選」是不同角度的底部訊號（那個看急殺+爆量，這個看破底+量縮）
        if compute_low_divergence(close, sd["volume"], price_history.get(code, []), volume_history.get(code, [])):
            bull_tags.append("低吸背離")

        # ATR波動風險：跟 compute_risk_scores 用同一套振幅門檻（RISK_AMPLITUDE_PCT），
        # 純資訊性標籤，不影響 net_signal 計分。風險頁本來就有算這個振幅（atr_pct），
        # 但訊號中心之前完全沒有把它轉成看得到的標籤，只有去風險頁才看得到，
        # 現在讓訊號中心也能一眼看出「這檔股票雖然訊號不錯，但當天波動很大」。
        amplitude_pct = ((sd["high"] - sd["low"]) / close * 100) if close else 0
        if amplitude_pct > RISK_AMPLITUDE_PCT:
            bear_tags.append("ATR波動風險")

        # 買賣超強度判斷：跟 SCORING RULES 文件寫的一致，用「20日均量」當基準，
        # 不是今天的成交量——今天的量本身可能就是因為法人大買/大賣才變大的，
        # 拿當天的量當分母等於用「果」去衡量「因」，每天基準忽大忽小，會讓真正
        # 顯著的買超強度訊號在爆量日被錯誤稀釋、漏掉。
        # 資料不足（歷史天數太少）時 fallback 回今日成交量，至少比完全跳過這條規則合理。
        avg_vol_20 = compute_avg_volume(volume_history.get(code, []))
        vol_base = avg_vol_20 if avg_vol_20 is not None else max(sd["volume"], 1)
        if inst_net > 0 and abs(inst_net) > vol_base * 0.05:
            net_signal += 1
            bull_tags.append("買超強度大")
        if inst_net < 0 and abs(inst_net) > vol_base * 0.05:
            net_signal -= 1
            bear_tags.append("賣超強度大")

        # 價量齊揚：今日漲跌>0 且今日成交量>昨日成交量。原本這條規則因為函式沒拿到
        # 歷史成交量資料，只能簡化成「今日漲跌>0」，忽略「成交量>昨日」那半段；
        # 現在 volume_history 已經傳進來了，補上真正的量增判斷。沒有昨天資料時
        # （新股或資料庫剛開始累積）fallback 回原本「只看漲跌」的簡化版，不會因為
        # 缺資料就整條規則失效。
        hist_vol = volume_history.get(code, [])
        yesterday_volume = hist_vol[-1][1] if hist_vol else None
        price_up = chg_pct > 0
        volume_up = yesterday_volume is None or sd["volume"] > yesterday_volume
        if price_up and volume_up:
            net_signal += 1
            bull_tags.append("價量齊揚" if yesterday_volume is not None else "價漲")
        if chg_pct <= RISK_DROP_LIMIT_PCT:
            net_signal -= 1
            bear_tags.append("單日重挫")

        # 財務底色佳：本益比合理、有配息、股價淨值比不過高
        if financial_color_ok(pe_pb.get(code)):
            net_signal += 1
            bull_tags.append("財務底色佳")

        # 營收動能加速：最新月營收年增率 > 20%
        rev = revenue.get(code)
        if rev and rev.get("revenue_yoy_pct", 0) > 20:
            net_signal += 1
            bull_tags.append(f"營收動能加速（年增{rev['revenue_yoy_pct']:.1f}%）")

        # 主升段確認：站上上揚的20日均線，且20MA在60MA之上（需要至少20天歷史資料，含今天）
        hist = price_history.get(code, [])
        if compute_ma_trend(close, hist):
            net_signal += 1
            bull_tags.append("主升段確認")

        rec, label = recommendation_for(net_signal)
        composite_score = round(net_signal * 2.5 + (chg_pct * 0.3), 2)

        rows.append({
            "code": code,
            "name": sd["name"],
            "trade_date": TODAY,
            "close": close,
            "chg_pct": round(chg_pct, 2),
            "net_signal": net_signal,
            "recommendation": rec,
            "recommendation_label": label,
            "stars": min(max(net_signal, 0), 3),
            "bull_tags": bull_tags,
            "bear_tags": bear_tags,
            "composite_score": composite_score,
        })

    # 多頭候選排名編號：在所有「強多候選」等級的股票裡，依綜合分數高到低排名，
    # #1 是分數最高的那檔，排名編號插在多頭訊號標籤的最前面（對照原截圖的「多頭候選 #5」樣式）
    strong_bull_rows = [r for r in rows if r["recommendation"] == "strong-bull"]
    strong_bull_rows.sort(key=lambda r: r["composite_score"], reverse=True)
    for rank, r in enumerate(strong_bull_rows, start=1):
        r["bull_tags"].insert(0, f"多頭候選 #{rank}")

    return rows


def compute_explosion_scores(stock_day, volume_history, price_history, recent_confirms):
    rows = []
    for code, sd in stock_day.items():
        close, high, low, vol = sd["close"], sd["high"], sd["low"], sd["volume"]
        if close <= 0 or vol <= 0:
            continue
        chg_pct = (sd["change"] / (close - sd["change"]) * 100) if (close - sd["change"]) else 0

        # 量比：今日量 ÷ 近20日均量。資料不足（如新股或資料庫剛開始累積）時，
        # 用 1.0（今日量=均量，中性值）當 fallback，避免資料不足時被誤判成爆量或量縮
        vol_ratio_20 = compute_vol_ratio(vol, volume_history.get(code, []))
        if vol_ratio_20 is None:
            vol_ratio_20 = 1.0
        vol_ratio_score = min(vol_ratio_20, 5) / 5 * EXPLOSION_VOL_RATIO_MAX_POINTS

        # z-score：今日量偏離該股「平常」量能幾個標準差，抓的是量比抓不到的東西——
        # 同樣放量1倍，對量能一向穩定的股票是顯著訊號，對本來就大起大落的股票可能只是常態。
        # 資料不足時無法判斷是否異常，fallback 用 0（不加分也不扣分），跟 vol_ratio 的
        # fallback邏輯不同：vol_ratio資料不足時用中性值1.0繼續參與計分，z-score資料不足時
        # 則是「這個維度本次不貢獻分數」，因為兩者資料不足的意義不同（vol_ratio只要有幾天
        # 資料就能算平均，z-score要更多天數才能合理估計標準差，資料不足時硬算出來的z-score
        # 反而更不可靠，不如不给分）
        vol_z60 = compute_vol_zscore(vol, volume_history.get(code, []))
        vol_z60_for_score = vol_z60 if vol_z60 is not None else 0.0
        vol_zscore_score = min(max(vol_z60_for_score, 0), EXPLOSION_VOL_ZSCORE_CAP) / EXPLOSION_VOL_ZSCORE_CAP * EXPLOSION_VOL_ZSCORE_MAX_POINTS

        vol_score = vol_ratio_score + vol_zscore_score
        chg_score = min(max(chg_pct, 0), 10) / 10 * 30
        if high > low:
            close_strength = (close - low) / (high - low) * 30
        else:
            close_strength = 15
        score = round(vol_score + chg_score + close_strength, 1)

        if score >= EXPLOSION_CONFIRM_SCORE:
            status, status_label, stage = "confirm", "爆發確認", "放量突破"
        elif score >= EXPLOSION_PRE_SCORE:
            status, status_label, stage = "pre", "爆發前兆", "收斂醞釀"
        else:
            # 今天分數沒到門檻，但如果這檔股票在最近（約10個交易日內）曾經放量突破過，
            # 而且今天收盤價還守在當初那次突破的20日箱頂之上（沒有真的跌破支撐），
            # 就標記成「爆發後回測」，而不是直接不列入雷達——這代表突破後量能退溫、
            # 股價拉回測試前壓力（現在變支撐），如果撐得住通常是續漲的觀察名單，
            # 跟「假突破後直接跌破、完全不值得追蹤」的情況要分開看。
            prior_confirm = recent_confirms.get(code)
            if prior_confirm and prior_confirm.get("box_top_20d") and close > prior_confirm["box_top_20d"]:
                status, status_label, stage = "retest", "爆發後回測", "突破後回測"
            else:
                continue

        # 均線收斂度 / 布林通道位置：目前只是「算出真實數字寫進欄位」，還沒納入
        # score 的計分公式（跟 vol_z60 剛補上真數字時的做法一致），資料不足時回傳 None，
        # 不是每天都會有值，需等資料庫累積夠天數才會穩定出現
        ma_convergence_pct = compute_ma_convergence_pct(close, price_history.get(code, []))
        boll_position_pct = compute_boll_position_pct(close, price_history.get(code, []))

        rows.append({
            "code": code,
            "name": sd["name"],
            "industry": "",
            "trade_date": TODAY,
            "status": status,
            "status_label": status_label,
            "stage": stage,
            "score": score,
            "close": close,
            "chg_pct": round(chg_pct, 2),
            "breakout_pct": round(chg_pct, 2),
            "box_top_20d": high,
            "vol_ratio_20": vol_ratio_20,
            "vol_z60": vol_z60,
            "ma_convergence_pct": ma_convergence_pct,
            "boll_position_pct": boll_position_pct,
        })
    return rows


RISK_LOW_VOL_RATIO = 0.5        # 量比低於此值視為量縮流動性差


def compute_risk_scores(stock_day, institutional, volume_history, institutional_history):
    rows = []
    for code, sd in stock_day.items():
        close, high, low, vol = sd["close"], sd["high"], sd["low"], sd["volume"]
        if close <= 0:
            continue
        chg_pct = (sd["change"] / (close - sd["change"]) * 100) if (close - sd["change"]) else 0
        inst = institutional.get(code, {})
        inst_net = inst.get("institutional_net", 0)
        # 資料不足（歷史天數太少）時回傳 None，代表這條規則本次不判斷，不當作「量縮」處理
        vol_ratio = compute_vol_ratio(vol, volume_history.get(code, []))
        consecutive_sell_days = compute_consecutive_sell_days(inst_net, institutional_history.get(code, []))

        score = 0.0
        main_risks = []
        if chg_pct <= RISK_DROP_LIMIT_PCT:
            score += 50
            main_risks.append("跌停預警")
        if inst_net < 0 and vol > 0 and abs(inst_net) > vol * RISK_INST_SELL_RATIO:
            score += 25
            main_risks.append("法人賣超")
        amplitude_pct = ((high - low) / close * 100) if close else 0
        if amplitude_pct > RISK_AMPLITUDE_PCT:
            score += 15
            main_risks.append("量價背離/高波動")
        if vol_ratio is not None and vol_ratio < RISK_LOW_VOL_RATIO:
            score += 10
            main_risks.append("量縮流動性差")

        if score >= 80:
            level = "high"
        elif score >= 60:
            level = "warn"
        else:
            continue

        rows.append({
            "code": code,
            "name": sd["name"],
            "trade_date": TODAY,
            "risk_level": level,
            "risk_score": round(score, 1),
            "main_risk": "、".join(main_risks),
            "suggested_action": "暫不追價，先觀察" if level == "high" else "避開新買，留意反彈",
            "close": close,
            "chg_pct": round(chg_pct, 2),
            "vol_ratio": vol_ratio,
            "atr_pct": round(amplitude_pct, 2),
            "consecutive_sell_days": consecutive_sell_days,
            "liquidity_score": None,
            "note": f"當日跌幅 {chg_pct:.2f}%；{'、'.join(main_risks)}",
        })
    return rows


# --------------------------------------------------------------------------
# 3. 主流程
# --------------------------------------------------------------------------

TODAY = date.today().isoformat()


def is_weekend(date_str):
    """判斷傳入的日期字串（YYYY-MM-DD）是不是週六/週日"""
    return datetime.strptime(date_str, "%Y-%m-%d").weekday() >= 5


def main():
    print(f"=== 開始執行 {TODAY} 收盤後計算 ===")

    # 台股週末不開盤，但證交所的各項端點（STOCK_DAY_ALL、MI_INDEX 等）在週末被
    # 觸發時，常常不會回錯誤或空值，而是直接回傳「上一個交易日」的舊資料。如果照
    # 常寫入，會產生一筆「trade_date 是週末、但數字其實是別天」的髒資料，汙染之後
    # 算5/20/60日漲跌幅等時間序列指標的正確性（這種髒資料不會被後面「stock_day
    # 是否為空」的檢查擋下來，因為證交所確實回了一批看似正常的資料，只是日期不對）。
    # 這裡只擋週六日，國定假日（例如農曆年、國慶日）目前還沒有對照日曆可以判斷，
    # 如果在國定假日手動觸發，一樣會有這個問題，之後可以考慮接台灣證交所公告的
    # 「休市日曆」再補強。
    if is_weekend(TODAY):
        print(f"[SKIP] {TODAY} 是週六/週日，台股不開盤。證交所端點在非交易日常常還是會")
        print("回傳上一個交易日的舊資料，直接寫入會產生日期跟數字對不上的髒資料，")
        print("這次執行直接跳過、不寫入任何資料表。如果需要在假日測試程式碼本身的邏輯，")
        print("建議另外寫測試腳本、不要透過這個正式排程腳本寫入正式的 Supabase 資料表。")
        return

    print("抓取個股日成交資訊 (STOCK_DAY_ALL)...")
    try:
        stock_day = fetch_stock_day_all()
    except Exception as e:
        print(f"[FATAL] STOCK_DAY_ALL 抓取失敗（已重試但仍失敗）：{e}")
        print("這通常是證交所伺服器暫時性問題，晚點手動重跑一次通常就會恢復正常")
        sys.exit(1)
    print(f"  取得 {len(stock_day)} 檔")

    print("抓取三大法人買賣超 (T86)...")
    institutional, institutional_status, institutional_detail = fetch_institutional_t86()
    print(f"  取得 {len(institutional)} 檔（狀態：{institutional_status}）")

    print("抓取本益比/殖利率/淨值比 (BWIBBU_ALL)...")
    pe_pb = fetch_pe_pb()
    print(f"  取得 {len(pe_pb)} 檔")

    print("抓取月營收 (t187ap05_L)...")
    revenue = fetch_monthly_revenue()
    print(f"  取得 {len(revenue)} 檔")

    print("讀取歷史股價/成交量/法人買賣超（算主升段/交易性風險/流動性/爆發前兆/連續賣超天數用）...")
    price_history, volume_history, institutional_history = fetch_price_and_volume_history()
    days_available = max((len(v) for v in price_history.values()), default=0)
    print(f"  目前資料庫累積約 {days_available} 個交易日歷史（需要 ≥20 天才會開始判斷主升段）")

    if not stock_day:
        print("[FATAL] 未取得任何個股資料，中止本次執行（可能是非交易日或端點異動）")
        sys.exit(1)

    print("計算訊號分數...")
    signal_rows = compute_signal_scores(stock_day, institutional, pe_pb, revenue, price_history, volume_history, institutional_history)
    print(f"  產出 {len(signal_rows)} 筆")

    print("計算爆發前兆分數...")
    recent_confirms = fetch_recent_explosion_confirms()
    explosion_rows = compute_explosion_scores(stock_day, volume_history, price_history, recent_confirms)
    print(f"  產出 {len(explosion_rows)} 筆（分數達門檻者，含爆發後回測）")

    print("計算風險分數...")
    risk_rows = compute_risk_scores(stock_day, institutional, volume_history, institutional_history)
    print(f"  產出 {len(risk_rows)} 筆（分數達門檻者）")

    print("計算交易性風險/流動性...")
    liquidity_rows = compute_liquidity_scores(stock_day, volume_history)
    print(f"  產出 {len(liquidity_rows)} 筆（比例達門檻者）")

    print("計算超賣反彈候選...")
    rebound_rows = compute_rebound_candidates(stock_day, volume_history)
    print(f"  產出 {len(rebound_rows)} 筆（短線急殺+爆量者）")

    print("計算轉弱觀察名單...")
    weakening_rows = compute_weakening_watchlist(stock_day, volume_history, institutional, institutional_history, price_history)
    print(f"  產出 {len(weakening_rows)} 筆（連續賣超+價跌+量縮者）")

    # 個股每日快照（給歷史查詢用，可選）
    daily_rows = []
    for code, sd in stock_day.items():
        inst = institutional.get(code, {})
        chg_pct = (sd["change"] / (sd["close"] - sd["change"]) * 100) if (sd["close"] - sd["change"]) else 0
        daily_rows.append({
            "code": code, "name": sd["name"], "trade_date": TODAY,
            "close": sd["close"], "open": sd["open"], "chg_pct": round(chg_pct, 2), "volume": int(sd["volume"]),
            "foreign_net": inst.get("foreign_net"), "trust_net": inst.get("trust_net"),
            "dealer_net": inst.get("dealer_net"), "institutional_net": inst.get("institutional_net"),
        })

    print("計算全球資產對照（TAIEX + 國際指數/匯率 + 台股ETF）...")
    global_rows = compute_global_factors(stock_day, price_history)
    print(f"  產出 {len(global_rows)} 筆")

    print("寫入 Supabase...")
    supabase_upsert("stock_daily", daily_rows, "code,trade_date")
    supabase_upsert("signal_scores", signal_rows, "code,trade_date")
    supabase_upsert("explosion_scores", explosion_rows, "code,trade_date")
    supabase_upsert("risk_scores", risk_rows, "code,trade_date")
    supabase_upsert("global_factors", global_rows, "factor_code,trade_date")
    supabase_upsert("liquidity_scores", liquidity_rows, "code,trade_date")
    supabase_upsert("rebound_candidates", rebound_rows, "code,trade_date")
    supabase_upsert("weakening_watchlist", weakening_rows, "code,trade_date")

    # 回測必須排在 stock_daily 寫入「之後」才算——回測驗證「昨天的推薦」用的是
    # 「今天的開盤價」，而今天的開盤價要等上面 stock_daily 那行 upsert 完成後，
    # 才會存在資料庫裡、可以被回測查回來用。如果順序寫反，回測會永遠少驗證到
    # 最新的一天，變成每次都慢一拍。
    print("計算隔日開盤回測統計...")
    backtest_rows = compute_backtest_stats()
    sample_dates = backtest_rows[0]["sample_dates"] if backtest_rows else 0
    bull_all_row = next((r for r in backtest_rows if r["category"] == "bull_all"), None)
    bull_all_total = bull_all_row["total_count"] if bull_all_row else 0
    print(f"  真正比對到資料的天數 {sample_dates}（目標 {BACKTEST_LOOKBACK_DAYS} 天，累積中屬正常；"
          f"部署初期天數少是因為舊資料沒有開盤價可比對，不是bug）")
    print(f"  多頭候選整體(bull_all)這次比對了 {bull_all_total} 檔次，可用來判斷樣本量是否足夠參考")
    supabase_upsert("backtest_stats", backtest_rows, "trade_date,category")

    # 長期校準統計也要排在 stock_daily 寫入之後，理由跟隔日開盤回測一樣：需要
    # 今天剛寫入的收盤價，才能驗證昨天的推薦。這裡用收盤價、樣本抓全部歷史，
    # 不像回測那樣受限於「開盤價這次才開始存」，理論上樣本量會比回測大很多。
    print("計算長期校準統計...")
    calibration_rows = compute_calibration_stats()
    calib_sample_dates = calibration_rows[0]["sample_dates"] if calibration_rows else 0
    calib_bull_all = next((r for r in calibration_rows if r["category"] == "bull_all"), None)
    print(f"  樣本涵蓋 {calib_sample_dates} 個交易日")
    if calib_bull_all:
        print(f"  多頭候選整體(bull_all)：{calib_bull_all['total_count']} 檔次，"
              f"命中率 {calib_bull_all['win_rate']}，判讀：{calib_bull_all['verdict']}")
    supabase_upsert("calibration_stats", calibration_rows, "trade_date,category")

    status_row = [{
        "id": 1,
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
        "confidence_score": 90 if institutional_status == "ok" else 70,
        "status_note": "ok" if institutional_status == "ok" else f"institutional_{institutional_status}",
        "total_records": len(stock_day),
        "institutional_status": institutional_status,
        "institutional_count": len(institutional),
        "institutional_detail": institutional_detail,
    }]
    supabase_upsert("fetch_status", status_row, "id")

    print("=== 完成 ===")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盤中即時報價更新（每 5 分鐘跑一次，更新 stock_realtime 表）
====================================================================
用途：更新首頁上「即時報價」用的收盤/漲跌%，不做評分計算（評分留給收盤後的
fetch_and_score.py，因為法人資料是日頻，盤中重算沒有意義）。

追蹤清單：動態從 Supabase 的 signal_scores 表撈出「最新交易日」的全部代號
（即收盤後訊號計算涵蓋的全市場清單），不再寫死少數幾檔。

⚠️ 重要提醒：
  證交所沒有正式的「輕量級即時報價 OpenAPI」，本腳本用的是 MIS 即時報價端點
  （mis.twse.com.tw），這是網頁版看盤系統背後在用的端點，非正式 API 合約，
  可能隨時改版或對過於頻繁的請求加以限制。使用原則：
    - 改用「批次查詢」（多檔代號用 | 串接），大幅減少請求次數
    - 每批之間至少間隔數秒，不要併發狂打
    - 若長時間被擋（回傳空值或錯誤），代表可能被限流，應降低頻率或暫停
  如果之後你有付費資料商（TEJ/CMoney）帳號，強烈建議改用付費資料商的正式
  即時報價 API 取代這支腳本，穩定性與合法性都更好。
"""

import os
import json
import time
from datetime import datetime, timezone
import urllib.request
import urllib.error

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# 若 Supabase 查詢失敗時的備援清單
FALLBACK_WATCHLIST = [
    "2330", "2317", "2454", "2308", "2412",
]

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0"
BATCH_SIZE = 50           # 每批查詢檔數
BATCH_SLEEP_SEC = 2.0     # 每批之間的間隔秒數


def supabase_get(path):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[WARN] Supabase 查詢失敗（{path}）：{e}")
        return None


def get_watchlist():
    """從 signal_scores 撈出最新交易日的全部股票代號"""
    latest = supabase_get("signal_scores?select=trade_date&order=trade_date.desc&limit=1")
    if not latest:
        print("[WARN] 無法取得最新交易日，改用備援清單")
        return FALLBACK_WATCHLIST
    trade_date = latest[0]["trade_date"]

    rows = supabase_get(f"signal_scores?select=code&trade_date=eq.{trade_date}")
    if not rows:
        print("[WARN] 無法取得當日股票清單，改用備援清單")
        return FALLBACK_WATCHLIST

    codes = sorted({r["code"] for r in rows if r.get("code")})
    print(f"從 signal_scores（{trade_date}）取得 {len(codes)} 檔追蹤清單")
    return codes


def fetch_batch(codes):
    """一次查詢多檔（MIS API 支援用 | 串接多個 ex_ch）"""
    ex_ch = "|".join(f"tse_{c}.tw" for c in codes)
    url = MIS_URL.format(ex_ch=ex_ch)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://mis.twse.com.tw/stock/index.jsp",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        arr = data.get("msgArray", [])
        results = []
        for info in arr:
            code = info.get("c", "")
            name = info.get("n", "")
            close = info.get("z")
            if close in (None, "-", ""):
                close = info.get("y")
            close = float(close) if close not in (None, "-", "") else None
            y_close = float(info.get("y")) if info.get("y") not in (None, "-", "") else None
            chg_pct = round((close - y_close) / y_close * 100, 2) if (close and y_close) else None
            results.append({"code": code, "name": name, "close": close, "chg_pct": chg_pct})
        return results
    except Exception as e:
        print(f"[WARN] 批次查詢失敗（{len(codes)} 檔）：{e}")
        return []


def supabase_upsert(table, rows, on_conflict):
    if not rows:
        return
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(f"[WARN] 未設定 Supabase 連線資訊，跳過寫入 {table}")
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print(f"[OK] 寫入 {table}：{len(rows)} 筆")
    except urllib.error.HTTPError as e:
        print(f"[ERROR] 寫入 {table} 失敗：{e.code} {e.read().decode('utf-8')[:300]}")


def main():
    watchlist = get_watchlist()
    all_rows = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for i in range(0, len(watchlist), BATCH_SIZE):
        batch = watchlist[i:i + BATCH_SIZE]
        rows = fetch_batch(batch)
        for r in rows:
            r["updated_at"] = now_iso
        all_rows.extend(rows)
        print(f"批次 {i // BATCH_SIZE + 1}：取得 {len(rows)}/{len(batch)} 檔")
        time.sleep(BATCH_SLEEP_SEC)

    print(f"成功取得 {len(all_rows)}/{len(watchlist)} 檔即時報價")
    supabase_upsert("stock_realtime", all_rows, "code")


if __name__ == "__main__":
    main()

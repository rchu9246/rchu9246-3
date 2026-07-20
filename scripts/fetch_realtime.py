#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盤中即時報價輕量更新（每 5 分鐘跑一次，只更新 stock_realtime 表）
====================================================================
用途：更新首頁上「即時報價」用的收盤/漲跌%，不做評分計算（評分留給收盤後的
fetch_and_score.py，因為法人資料是日頻，盤中重算沒有意義）。

⚠️ 重要提醒：
  證交所沒有正式的「輕量級即時報價 OpenAPI」，本腳本用的是 MIS 即時報價端點
  （mis.twse.com.tw），這是網頁版看盤系統背後在用的端點，非正式 API 合約，
  可能隨時改版或對過於頻繁的請求加以限制。使用原則：
    - 只查你關心的股票清單（watchlist），不要對全市場 5000+ 檔做輪詢
    - 每次請求之間至少間隔數秒，不要併發狂打
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

# 在這裡填入你想要盤中追蹤的股票代號（建議先從你關注的訊號候選清單挑，
# 不要放全市場，避免對非正式端點造成壓力）
WATCHLIST = [
    "2330", "2317", "2454", "2308", "2412",
    # 可依照收盤後 signal_scores 中 net_signal 高分的股票，動態更新這份清單
]

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0"


def fetch_one(code):
    """單檔即時報價；上市股票用 tse_ 前綴，上櫃用 otc_（此處預設上市）"""
    ex_ch = f"tse_{code}.tw"
    url = MIS_URL.format(ex_ch=ex_ch)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://mis.twse.com.tw/stock/index.jsp",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        arr = data.get("msgArray", [])
        if not arr:
            return None
        info = arr[0]
        name = info.get("n", "")
        close = info.get("z")  # 當前成交價，收盤前可能是 "-"，改用參考價
        if close in (None, "-", ""):
            close = info.get("y")  # 昨收退回
        close = float(close) if close not in (None, "-", "") else None
        y_close = float(info.get("y")) if info.get("y") not in (None, "-", "") else None
        chg_pct = round((close - y_close) / y_close * 100, 2) if (close and y_close) else None
        return {"code": code, "name": name, "close": close, "chg_pct": chg_pct}
    except Exception as e:
        print(f"[WARN] {code} 查詢失敗：{e}")
        return None


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
    rows = []
    for code in WATCHLIST:
        r = fetch_one(code)
        if r:
            r["updated_at"] = datetime.now(timezone.utc).isoformat()
            rows.append(r)
        time.sleep(1.5)  # 避免過快連續請求
    print(f"成功取得 {len(rows)}/{len(WATCHLIST)} 檔即時報價")
    supabase_upsert("stock_realtime", rows, "code")


if __name__ == "__main__":
    main()

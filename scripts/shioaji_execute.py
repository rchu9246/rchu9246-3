#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shioaji 真實下單執行腳本（shioaji_execute.py）
=====================================
用途：讀取 scripts/decide_trades.py 昨天決定的待買/待出場清單，透過永豐證券
      Shioaji API，在市場真的開盤的時候送出真實委託單（目前設定為模擬環境
      simulation=True，還沒開放正式環境）。

執行時機：必須排在台股開盤之後（建議 09:05 左右，避開開盤瞬間的異常價格波動），
  跟收盤後的 fetch_and_score.py / decide_trades.py 是不同的排程時段。

下單規則（已跟使用者確認）：
  零股機制：盤中零股（IntradayOdd）
  價格類型：限價單（LMT），價格取當下的參考價（contract.reference）
  委託效期：ROD（當日有效單）

⚠️ 重要限制：
  1. 這支腳本一次執行只會「檢查一次」訂單有沒有成交（送出後等幾秒、呼叫
     update_status() 確認一次），不是持續盯著市場、直到成交為止。如果訂單
     送出後沒有立刻成交，之後有沒有成交，這次執行不會知道，要等下次執行
     （隔天）才會重新處理——這是排程批次架構的先天限制，不是即時看盤系統。
  2. ROD 委託單效期只到當天收盤，就算這次執行沒抓到後續成交，那筆委託單
     也不會留到隔天造成重複下單的風險。
  3. 目前是模擬環境（simulation=True），不會動用真實資金；要接正式環境
     （simulation=False）需要另外評估風險並取得使用者明確同意。
  4. 送出委託（api.place_order）如果逾時或連線異常，會自動重試最多
     PLACE_ORDER_MAX_RETRIES 次；單一檔股票重試全部失敗後，會跳過該檔、
     留在待處理清單等下次執行再試，不會讓整支腳本中止、影響其他檔股票。
"""

import os
import sys
import json
import time
import base64
import tempfile
import urllib.request
import urllib.error
from datetime import date, datetime, timezone

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

SHIOAJI_API_KEY = os.environ.get("SHIOAJI_API_KEY", "")
SHIOAJI_SECRET_KEY = os.environ.get("SHIOAJI_SECRET_KEY", "")
SHIOAJI_CA_BASE64 = os.environ.get("SHIOAJI_CA_BASE64", "")
SHIOAJI_CA_PASSWORD = os.environ.get("SHIOAJI_CA_PASSWORD", "")

TODAY = date.today().isoformat()

FILL_CHECK_WAIT_SECONDS = 8  # 送出委託後，等幾秒再查一次成交狀態

PLACE_ORDER_MAX_RETRIES = 3       # 下單逾時/失敗時最多重試次數
PLACE_ORDER_RETRY_BASE_SLEEP = 5  # 重試間隔秒數（每次重試遞增：5s, 10s, 15s...）


# --------------------------------------------------------------------------
# Supabase 輔助函式（跟 decide_trades.py 同一套）
# --------------------------------------------------------------------------

def safe_float(v, default=0.0):
    try:
        if v in (None, "", "--", "N/A"):
            return default
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return default


def supabase_select(table, query, timeout=60, page_size=1000, max_pages=100):
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
            print(f"[WARN] 讀取 {table} 失敗：{e}")
            break
        all_rows.extend(chunk)
        if len(chunk) < page_size:
            break
    return all_rows


def supabase_upsert(table, rows, on_conflict):
    if not rows:
        return
    if not SUPABASE_URL or not SUPABASE_KEY:
        print(f"[WARN] 未設定 SUPABASE_URL/SUPABASE_KEY，跳過寫入 {table}")
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


def supabase_insert(table, rows):
    if not rows:
        return
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
        print(f"[OK] 寫入 {table}：{len(rows)} 筆")
    except urllib.error.HTTPError as e:
        print(f"[ERROR] 寫入 {table} 失敗：{e.code} {e.read().decode('utf-8')[:500]}")


def supabase_delete(table, query):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    req = urllib.request.Request(url, method="DELETE", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        print(f"[WARN] 刪除 {table} 失敗：{e.code} {e.read().decode('utf-8')[:300]}")


def get_account():
    rows = supabase_select("paper_account", "id=eq.1&limit=1")
    return rows[0] if rows else None


def get_positions():
    return supabase_select("paper_positions", "status=in.(open,pending_exit)&order=entry_date.asc")


def get_pending_buys():
    return supabase_select("paper_pending_buys", "order=trade_date.asc")


# --------------------------------------------------------------------------
# Shioaji 下單相關
# --------------------------------------------------------------------------

def setup_shioaji():
    """登入 Shioaji 模擬環境並啟用電子憑證，回傳 api 物件。
    憑證是用 base64 存在環境變數裡的，這裡先解碼寫成暫存檔案再傳給 activate_ca，
    暫存檔案只存在這次執行的容器裡，執行結束容器就會銷毀，不會留下痕跡。
    """
    import shioaji as sj

    if not SHIOAJI_API_KEY or not SHIOAJI_SECRET_KEY:
        print("[FATAL] 未設定 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY，無法登入")
        sys.exit(1)

    api = sj.Shioaji(simulation=True)
    print("正在登入 Shioaji 模擬環境...")
    accounts = api.login(api_key=SHIOAJI_API_KEY, secret_key=SHIOAJI_SECRET_KEY)
    print(f"登入成功，帳戶：{accounts}")

    if SHIOAJI_CA_BASE64 and SHIOAJI_CA_PASSWORD:
        # 清除可能夾帶的空白/換行字元，並自動補齊結尾的 padding（=號）——
        # 複製貼上很長的 base64 字串時，常常會不小心在結尾漏掉一兩個 = 號，
        # 這是可以安全補回去的（padding 只影響字串結尾的解讀方式，不影響
        # 前面實際的內容），能解決最常見的「Incorrect padding」錯誤。
        ca_base64_clean = SHIOAJI_CA_BASE64.strip().replace("\n", "").replace("\r", "").replace(" ", "")
        missing_padding = len(ca_base64_clean) % 4
        if missing_padding:
            ca_base64_clean += "=" * (4 - missing_padding)
        try:
            ca_bytes = base64.b64decode(ca_base64_clean)
        except Exception as e:
            print(f"[FATAL] SHIOAJI_CA_BASE64 解碼失敗：{e}")
            print(f"  目前字串長度：{len(ca_base64_clean)}（正常的 .pfx 憑證轉 base64 後，"
                  f"長度通常有幾千字元以上，如果這個數字看起來明顯太短，代表複製貼上時可能被截斷了）")
            sys.exit(1)
        with tempfile.NamedTemporaryFile(suffix=".pfx", delete=False) as f:
            f.write(ca_bytes)
            ca_path = f.name
        print("正在啟用電子憑證...")
        result = api.activate_ca(ca_path=ca_path, ca_passwd=SHIOAJI_CA_PASSWORD)
        print(f"憑證啟用結果：{result}")
        if not result:
            print("[FATAL] 電子憑證啟用失敗，無法下單（可能是密碼錯誤，或憑證檔案損毀）")
            sys.exit(1)
    else:
        print("[FATAL] 未設定 SHIOAJI_CA_BASE64 / SHIOAJI_CA_PASSWORD，無法啟用憑證、無法下單")
        sys.exit(1)

    return api, sj


def place_and_check(api, sj, contract, action, quantity, reference_price):
    """送出一筆盤中零股限價單，等幾秒後查一次成交狀態，回傳 (filled_qty, filled_price, order_id, status_str)。
    filled_qty 可能小於 quantity（部分成交）或等於 0（完全沒成交，這次執行沒查到成交，
    不代表訂單一定不會成交——訂單在收盤前都還有效，只是我們這次沒等到）。

    送出委託（api.place_order）如果逾時或連線異常，會自動重試最多
    PLACE_ORDER_MAX_RETRIES 次；重試間隔隨次數遞增，避免對方伺服器忙碌時
    連續高頻重試造成反效果。全部重試都失敗的話，會往外拋出例外，由呼叫端
    （main 裡的迴圈）決定要跳過這一檔、留給下次執行再處理。
    """
    order = api.Order(
        price=reference_price,
        quantity=quantity,
        action=action,
        price_type=sj.constant.StockPriceType.LMT,
        order_type=sj.constant.OrderType.ROD,
        order_lot=sj.constant.StockOrderLot.IntradayOdd,
        account=api.stock_account,
    )

    trade = None
    last_error = None
    for attempt in range(1, PLACE_ORDER_MAX_RETRIES + 1):
        try:
            trade = api.place_order(contract, order)
            last_error = None
            break
        except Exception as e:
            last_error = e
            print(f"  [WARN] 送出委託失敗（第 {attempt}/{PLACE_ORDER_MAX_RETRIES} 次嘗試）："
                  f"{type(e).__name__}: {e}")
            if attempt < PLACE_ORDER_MAX_RETRIES:

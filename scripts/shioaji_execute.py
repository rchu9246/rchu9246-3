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
    trade = api.place_order(contract, order)
    order_id = trade.status.id if trade and trade.status else None
    print(f"  委託送出：{action} {quantity}股 @ {reference_price}，order_id={order_id}")

    time.sleep(FILL_CHECK_WAIT_SECONDS)
    api.update_status()

    filled_qty, filled_amount = 0, 0.0
    deals = getattr(trade.status, "deals", None) or []
    for d in deals:
        deal_qty = getattr(d, "quantity", None) or d.get("quantity", 0) if isinstance(d, dict) else 0
        deal_price = getattr(d, "price", None) or d.get("price", 0) if isinstance(d, dict) else 0
        filled_qty += deal_qty
        filled_amount += deal_qty * deal_price

    filled_price = round(filled_amount / filled_qty, 2) if filled_qty > 0 else None
    status_str = str(trade.status.status) if trade and trade.status else "unknown"
    return filled_qty, filled_price, order_id, status_str


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main():
    print(f"=== 開始執行 {TODAY} Shioaji 真實下單（模擬環境） ===")

    api, sj = setup_shioaji()

    account = get_account()
    cash = safe_float(account.get("cash"), 100000.0) if account else 100000.0
    initial_capital = safe_float(account.get("initial_capital"), 100000.0) if account else 100000.0
    print(f"目前現金（我方帳本紀錄）：{cash:,.0f} 元")

    positions = get_positions()
    pending_buys = get_pending_buys()
    print(f"待出場：{len([p for p in positions if p['status']=='pending_exit'])} 檔"
          f"；待買清單：{len(pending_buys)} 檔")

    trade_log = []

    # ------------------------------------------------------------------
    # 賣出被標記 pending_exit 的部位
    # ------------------------------------------------------------------
    to_close = [p for p in positions if p["status"] == "pending_exit"]
    for p in to_close:
        code = p["code"]
        try:
            contract = api.Contracts.Stocks[code]
        except Exception as e:
            print(f"[WARN] {code} 找不到合約資料：{e}，跳過（留在 pending_exit，下次再試）")
            continue
        quantity = int(safe_float(p["quantity"]))
        if quantity <= 0:
            continue
        reference_price = contract.reference
        filled_qty, filled_price, order_id, status_str = place_and_check(
            api, sj, contract, sj.constant.Action.Sell, quantity, reference_price
        )
        if filled_qty <= 0:
            print(f"[WARN] {code} 賣單目前沒有成交紀錄（狀態：{status_str}），"
                  f"留在 pending_exit，下次執行再檢查")
            continue

        entry_price = safe_float(p["entry_price"])
        amount = filled_qty * filled_price
        cash += amount
        pnl_pct = round((filled_price - entry_price) / entry_price * 100, 2) if entry_price else 0
        trade_log.append({
            "trade_date": TODAY, "code": code, "name": p.get("name"),
            "action": "sell", "price": filled_price, "quantity": filled_qty, "amount": round(amount, 0),
            "reason": f"停利/停損出場，損益 {pnl_pct:+.2f}%（Shioaji模擬成交）",
            "order_id": order_id, "order_status": status_str,
        })
        if filled_qty >= quantity:
            supabase_upsert("paper_positions", [{
                "code": code, "entry_date": p["entry_date"], "name": p.get("name"),
                "entry_price": entry_price, "quantity": p["quantity"], "status": "closed",
                "exit_date": TODAY, "exit_price": filled_price, "pnl_pct": pnl_pct,
            }], "code,entry_date")
            print(f"[SELL] {code} {p.get('name')}：{filled_qty}股 @ {filled_price}（全部成交），損益 {pnl_pct:+.2f}%")
        else:
            # 部分成交：剩餘股數繼續留在 pending_exit，下次執行會再對剩餘股數送出新的賣單
            remaining_qty = quantity - filled_qty
            supabase_upsert("paper_positions", [{
                "code": code, "entry_date": p["entry_date"], "name": p.get("name"),
                "entry_price": entry_price, "quantity": remaining_qty, "status": "pending_exit",
                "exit_date": None, "exit_price": None, "pnl_pct": None,
            }], "code,entry_date")
            print(f"[SELL-PARTIAL] {code}：{filled_qty}/{quantity}股成交，剩 {remaining_qty} 股留待下次")

    # ------------------------------------------------------------------
    # 買進待買清單，資金平均分配（允許零股）
    # ------------------------------------------------------------------
    executed_buys = []
    if pending_buys:
        capital_per_stock = cash / len(pending_buys)
        for b in pending_buys:
            code = b["code"]
            try:
                contract = api.Contracts.Stocks[code]
            except Exception as e:
                print(f"[WARN] {code} 找不到合約資料：{e}，跳過（留在待買清單，下次再試）")
                continue
            reference_price = contract.reference
            if not reference_price:
                print(f"[WARN] {code} 沒有參考價，跳過")
                continue
            quantity = int(capital_per_stock // reference_price)
            if quantity <= 0:
                print(f"[WARN] {code} 分配資金 {capital_per_stock:.0f} 元不夠買 1 股"
                      f"（參考價 {reference_price} 元），跳過（留在待買清單，下次再試）")
                continue

            filled_qty, filled_price, order_id, status_str = place_and_check(
                api, sj, contract, sj.constant.Action.Buy, quantity, reference_price
            )
            if filled_qty <= 0:
                print(f"[WARN] {code} 買單目前沒有成交紀錄（狀態：{status_str}），"
                      f"留在待買清單，下次執行再檢查")
                continue

            amount = filled_qty * filled_price
            cash -= amount
            trade_log.append({
                "trade_date": TODAY, "code": code, "name": b.get("name"),
                "action": "buy", "price": filled_price, "quantity": filled_qty, "amount": round(amount, 0),
                "reason": f"強多候選訊號（{b['trade_date']}）進場（Shioaji模擬成交）",
                "order_id": order_id, "order_status": status_str,
            })
            supabase_upsert("paper_positions", [{
                "code": code, "entry_date": TODAY, "name": b.get("name"),
                "entry_price": filled_price, "quantity": filled_qty, "status": "open",
                "exit_date": None, "exit_price": None, "pnl_pct": None,
            }], "code,entry_date")
            print(f"[BUY] {code} {b.get('name')}：{filled_qty}股 @ {filled_price}"
                  f"{'（全部成交）' if filled_qty >= quantity else f'（部分成交，原欲買{quantity}股）'}")
            # 不管全部或部分成交，都視為這筆候選「已處理」，從待買清單移除——
            # 跟賣出不同，因為買進的量是「用可分配資金試算出來的」，部分成交後
            # 剩餘資金會在下次執行時重新按當時的候選清單分配，不強制湊滿原本數量
            executed_buys.append((code, b["trade_date"]))

        for code, trade_date_str in executed_buys:
            supabase_delete("paper_pending_buys", f"code=eq.{code}&trade_date=eq.{trade_date_str}")

    if trade_log:
        supabase_insert("paper_trade_log", trade_log)
    else:
        print("這次執行沒有任何成交")

    # ------------------------------------------------------------------
    # 更新帳戶現金與權益快照
    # ------------------------------------------------------------------
    final_positions = get_positions()
    holdings_value = 0.0
    for p in final_positions:
        entry_price = safe_float(p["entry_price"])
        holdings_value += entry_price * safe_float(p["quantity"])  # 早上剛執行完，用成交價估算即可

    total_equity = cash + holdings_value
    cumulative_return_pct = round((total_equity - initial_capital) / initial_capital * 100, 2) if initial_capital else 0

    supabase_upsert("paper_account", [{
        "id": 1, "cash": round(cash, 0), "initial_capital": initial_capital,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }], "id")

    supabase_upsert("paper_daily_equity", [{
        "trade_date": TODAY, "cash": round(cash, 0), "holdings_value": round(holdings_value, 0),
        "total_equity": round(total_equity, 0), "num_positions": len(final_positions),
        "cumulative_return_pct": cumulative_return_pct,
    }], "trade_date")

    print(f"執行後現金：{cash:,.0f} 元，持股市值：{holdings_value:,.0f} 元，總權益：{total_equity:,.0f} 元")
    print("=== 完成 ===")


if __name__ == "__main__":
    main()
